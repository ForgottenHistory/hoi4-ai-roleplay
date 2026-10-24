"""Diplomatic letters: the player writes to a country in game, the LLM writes that country's reply.

    python -m hoi4_rp letters      # keep running while you play

Loop: poll the player's country for airp_outbox_to (set by the Send button, see letter_mod.py),
read the letter from the writing area's text box through the bridge (`bridge.editbox`), clear both,
ask the LLM for a reply in character, then put the reply text in the airp_reply_body key and open
the reply window.
Conversation history per country is kept while this runs.
"""

from __future__ import annotations

import re
import time
import unicodedata
from typing import Dict, List, Optional

from . import bridge, gamestate, llm
from .letter_mod import EDITBOX_NAME

POLL_SECONDS = 0.5
MAX_HISTORY = 12   # messages kept per country (letters + replies)

_history: Dict[str, List[Dict[str, str]]] = {}


def decode_country(value: float) -> Optional[str]:
    """A country stored in a variable shows up as a packed number: 0x40 marker + the tag's 3 bytes."""
    raw = round(value * 100000) & 0xFFFFFFFF
    if raw >> 24 != 0x40:
        return None
    return bytes([raw & 0xFF, (raw >> 8) & 0xFF, (raw >> 16) & 0xFF]).decode("ascii", "replace")


def read_outbox(block: str) -> Optional[str]:
    """Recipient tag if a letter was just sent, else None."""
    m = re.search(r"\bairp_outbox_to=(-?[\d.]+)", block)
    return decode_country(float(m.group(1))) if m else None


def _unescape(line: str) -> str:
    """Bridge replies are one line: backslash-n is a line break, a doubled backslash is one backslash."""
    return re.sub(r"\\(.)", lambda m: "\n" if m.group(1) == "n" else m.group(1), line)


def read_letter_box() -> str:
    reply = bridge.send(f"bridge.editbox {EDITBOX_NAME}")[0]
    if not reply.startswith("OK"):
        raise RuntimeError(f"could not read the letter: {reply}")
    return _unescape(reply[3:]).strip()


def clear_letter_box() -> None:
    bridge.send(f"bridge.editbox.set {EDITBOX_NAME}")


def _clean_for_game(text: str) -> str:
    """Make LLM text safe for HOI4 localisation: no markup characters, only Latin-1 letters."""
    for a, b in {"‘": "'", "’": "'", "“": '"', "”": '"', "—": "-",
                 "–": "-", "…": "..."}.items():
        text = text.replace(a, b)
    text = re.sub(r"[*_#`]+", "", text)              # markdown
    text = re.sub(r"[§$£\[\]@]", "", text)            # HOI4 colour codes, key refs, icons, scripted loc
    text = re.sub(r"[ 	]*-[ 	]*(?=\S)", lambda m: " - " if m.group(0).strip() != m.group(0) else "-", text)
    text = re.sub(r"[ 	]{2,}", " ", text)
    out = []
    for ch in text:
        if ord(ch) < 256:
            out.append(ch)
        else:
            out.append(unicodedata.normalize("NFKD", ch).encode("latin-1", "ignore").decode("latin-1"))
    return re.sub(r"\n{3,}", "\n\n", "".join(out)).strip()


def _relation(them: gamestate.CountryState, me: gamestate.CountryState) -> str:
    facts = []
    if me.tag in them.enemies:
        facts.append(f"{them.name} and {me.name} are AT WAR with each other.")
    if them.faction and them.faction == me.faction:
        facts.append(f"Both are members of the {them.faction}.")
    if me.tag in them.neighbors:
        facts.append("They share a border.")
    if them.overlord == me.tag:
        facts.append(f"{them.name} is a subject of {me.name}.")
    if me.overlord == them.tag:
        facts.append(f"{me.name} is a subject of {them.name}.")
    if any(w.other == me.tag for w in them.wargoals):
        facts.append(f"{them.name} holds a war goal against {me.name}.")
    if any(w.other == them.tag for w in me.wargoals):
        facts.append(f"{me.name} holds a war goal against {them.name}.")
    if me.tag in them.opinion:
        facts.append(f"{them.name}'s opinion of {me.name}: {them.opinion[me.tag]:+g} (range -100 to +100).")
    if them.tag in me.opinion:
        facts.append(f"{me.name}'s opinion of {them.name}: {me.opinion[them.tag]:+g}.")
    return " ".join(facts) or "No war, alliance, border or subject relation between them."


def _system_prompt(them: gamestate.CountryState, me: gamestate.CountryState) -> str:
    return (
        f"You are roleplaying the government of {them.name} in the grand strategy game Hearts of Iron IV. "
        f"The date is {them.date.strip()}. The situation below is live from the game, which may have "
        f"diverged from real history: trust it over your own historical knowledge.\n\n"
        f"YOUR COUNTRY:\n{gamestate.describe(them)}\n\n"
        f"THE COUNTRY WRITING TO YOU:\n{gamestate.describe(me)}\n\n"
        f"BETWEEN YOU: {_relation(them, me)}\n\n"
        f"Write {them.name}'s reply to their latest letter, in the voice of {them.leader} or their "
        f"foreign office, true to {them.name}'s interests, ideology and situation. Be in character: you "
        f"may be warm, cold, evasive or hostile as fits.\n\n"
        f"Rules: plain text only, no markdown. At most 120 words. You write from {them.name}, so any "
        f"place line is your own capital, not {me.name}'s. Open with a short salutation and end "
        "with a signature. These letters are flavour only: do not claim that anything in the game "
        "changes (no treaties take effect, no units move)."
    )


def _show_reply(player: str, sender: str, text: str) -> None:
    body = text.replace("\r", "").replace("\n", "\\n")
    replies = bridge.send(
        f"bridge.setloc airp_reply_body {body}",
        f"eval_effect {player} = {{ clear_variable = airp_awaiting_from "
        f"set_variable = {{ airp_reply_from = {sender}.id }} }}",
    )
    bad = [r for r in replies if not r.startswith("OK")]
    if bad:
        print("  could not show the reply:", " | ".join(bad))


def handle_letter(player: str, to: str, text: str) -> None:
    print(f"\n{player} -> {to}: {text}")
    try:
        me, them = gamestate.country(player), gamestate.country(to)
    except gamestate.StateError as e:
        print("  could not read the game state:", e)
        _show_reply(player, to, "(The courier could not find his way: the game state is unavailable.)")
        return
    history = _history.setdefault(to, [])
    history.append({"role": "user", "content": f"Letter from {me.name}:\n\n{text}"})
    del history[:-MAX_HISTORY]
    try:
        reply = _clean_for_game(llm.chat([{"role": "system", "content": _system_prompt(them, me)}] + history))
    except llm.LLMError as e:
        print("  LLM error:", e)
        history.pop()
        reply = f"(The courier returned without an answer: {e})"[:300]
        _show_reply(player, to, _clean_for_game(reply))
        return
    history.append({"role": "assistant", "content": reply})
    print(f"{to} replies:\n{reply}")
    _show_reply(player, to, reply)


def run() -> int:
    print(f"letters: watching for letters (model {llm.model()}). Ctrl+C to stop.")
    while True:
        try:
            player = bridge.player_tag()
            to = read_outbox(bridge.country_block(player))
            if to:
                # Clear first so the same letter is never handled twice.
                bridge.send(f"eval_effect {player} = {{ clear_variable = airp_outbox_to }}")
                try:
                    text = read_letter_box()
                    clear_letter_box()
                except RuntimeError as e:
                    print(e)
                    text = ""
                if to == player:
                    print("letter recipient decoded as the player's own country; check on_sent_effect scopes")
                    bridge.send(f"eval_effect {player} = {{ clear_variable = airp_awaiting_from }}")
                elif to and text.strip():
                    handle_letter(player, to, text)
                else:
                    print("empty letter; nothing to answer")
                    bridge.send(f"eval_effect {player} = {{ clear_variable = airp_awaiting_from }}")
        except bridge.BridgeUnavailable:
            print("waiting for HOI4 / the bridge...")
            time.sleep(5)
            continue
        except RuntimeError as e:   # e.g. not in a game yet
            print("bridge:", e)
            time.sleep(3)
            continue
        except KeyboardInterrupt:
            return 0
        time.sleep(POLL_SECONDS)
