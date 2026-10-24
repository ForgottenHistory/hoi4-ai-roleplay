"""Diplomatic letters: the player writes to a country in game, the LLM writes that country's reply.

    python -m hoi4_rp run          # keep running while you play (also: letters)

Loop, every half second: poll the player's country for airp_outbox_to (set by the Send button, see
letter_mod.py), read the letter from the writing area's text box through the bridge
(`bridge.editbox`), clear both, ask the LLM for a reply in character, then put the reply text in the
airp_reply_body key and open the reply window.
Every few seconds of play it also snapshots the world and records what changed (chronicle.py).

Countries also write first (config [letters]): when a recorded event involves the player, the other
country involved writes about it; when no country has written for quiet_days, a neighbour, faction
member or major power writes unprompted. A country waits cooldown_days before writing on its own
again, and a letter only appears when no reply window is open and no reply is awaited.

Memory is per campaign and survives restarts (campaign.py): each country remembers every letter
exchanged with the player and the events it took part in, plus world events involving major powers.
"""

from __future__ import annotations

import random
import re
import time
import unicodedata
from typing import Dict, List, Optional

from . import actions, bridge, campaign, chronicle, gamestate, llm
from .config import settings
from .letter_mod import EDITBOX_NAME, PAGE_NAME, reply_size_for

POLL_SECONDS = 0.5
CHRONICLE_SECONDS = 5.0   # real-time gap between world snapshots (only recorded when the date moved)


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


def _memory(camp: campaign.Campaign, them: gamestate.CountryState, me: gamestate.CountryState) -> str:
    events = [e for e in camp.data["events"]
              if them.tag in e["tags"] or me.tag in e["tags"] or e.get("major")]
    if not events:
        return "Nothing notable recorded yet."
    return "\n".join(f"- {e['when']}: {e['text']}" for e in events)


def _reply_task(them: gamestate.CountryState, me: gamestate.CountryState) -> str:
    return (f"Write {them.name}'s reply to their latest letter, in the voice of {them.leader} or their "
            f"foreign office, true to {them.name}'s interests, ideology and situation. Be in character: you "
            f"may be warm, cold, evasive or hostile as fits.")


def _initiative_task(them: gamestate.CountryState, me: gamestate.CountryState, reason: str) -> str:
    why = (f"Something just happened that concerns you both: {reason} Write about it."
           if reason else
           f"There is no specific occasion: write about whatever matters most to {them.name} regarding "
           f"{me.name} right now, given the situation and what you remember.")
    return (f"{them.name} now writes a new letter to {me.name} on its own initiative, in the voice of "
            f"{them.leader} or their foreign office, true to {them.name}'s interests, ideology and "
            f"situation. {why} Be in character: warm, cold, evasive or threatening as fits.")


def _system_prompt(them: gamestate.CountryState, me: gamestate.CountryState, camp: campaign.Campaign,
                   task: str = "", actions_help: str = "") -> str:
    consequences = (
        f"{actions_help}\n\nOnly those commands change anything in the game: don't claim other changes "
        "(no other treaties take effect, no units move)."
        if actions_help else
        "These letters are flavour only: do not claim that anything in the game changes "
        "(no treaties take effect, no units move)."
    )
    return (
        f"You are roleplaying the government of {them.name} in the grand strategy game Hearts of Iron IV. "
        f"The date is {them.date.strip()}. The situation below is live from the game, which may have "
        f"diverged from real history: trust it over your own historical knowledge.\n\n"
        f"YOUR COUNTRY:\n{gamestate.describe(them)}\n\n"
        f"THE COUNTRY YOU CORRESPOND WITH:\n{gamestate.describe(me)}\n\n"
        f"BETWEEN YOU: {_relation(them, me)}\n\n"
        f"WHAT YOU REMEMBER (events since the chronicle began, oldest first; involving you, "
        f"{me.name}, or the major powers):\n{_memory(camp, them, me)}\n\n"
        f"Your earlier correspondence with {me.name} follows as the conversation, each letter dated. "
        f"Stay consistent with what you wrote before, and let what happened since colour your reply.\n\n"
        f"{task or _reply_task(them, me)}\n\n"
        f"Rules: plain text only, no markdown. At most 120 words. You write from {them.name}, so any "
        f"place line is your own capital, not {me.name}'s. Open with a short salutation and end "
        f"with a signature.\n\n{consequences}"
    )


def _show_reply(player: str, sender: str, text: str) -> None:
    body = text.replace("\r", "").replace("\n", "\\n")
    size = reply_size_for(text)       # which of the reply window's heights fits the letter
    replies = bridge.send(
        f"bridge.setloc airp_reply_body {body}",
        f"eval_effect {player} = {{ clear_variable = airp_awaiting_from "
        f"set_variable = {{ airp_reply_size = {size} }} set_variable = {{ airp_reply_from = {sender}.id }} }}",
    )
    bad = [r for r in replies if not r.startswith("OK")]
    if bad:
        print("  could not show the reply:", " | ".join(bad))


def _conversation(camp: campaign.Campaign, them: gamestate.CountryState,
                  me: gamestate.CountryState) -> List[Dict[str, str]]:
    msgs = []
    for x in camp.letters_with(them.tag):
        if x["from"] == them.tag:
            msgs.append({"role": "assistant", "content": x["text"]})
        else:
            msgs.append({"role": "user", "content": f"Letter from {me.name}, {x['when']}:\n\n{x['text']}"})
    return msgs


def handle_letter(camp: campaign.Campaign, player: str, to: str, text: str) -> None:
    print(f"\n{player} -> {to}: {text}")
    try:
        me, them = gamestate.country(player), gamestate.country(to)
    except gamestate.StateError as e:
        print("  could not read the game state:", e)
        _show_reply(player, to, "(The courier could not find his way: the game state is unavailable.)")
        return
    key = campaign.date_key(them.date)
    camp.at(key)
    camp.add_letter(to, player, text, key)
    camp.save()
    help_text = actions.prompt_help(them.name, me.name, actions.relation(to, player))
    try:
        prompt = ([{"role": "system", "content": _system_prompt(them, me, camp, actions_help=help_text)}]
                  + _conversation(camp, them, me))
        raw = llm.chat(prompt)
    except llm.LLMError as e:
        print("  LLM error:", e)
        _show_reply(player, to, _clean_for_game(f"(The courier returned without an answer: {e})"[:300]))
        return
    letter, commands = actions.parse(raw)
    reply = _clean_for_game(letter)
    camp.add_letter(to, to, reply, key)
    outcomes, events = actions.apply(to, player, them.name, me.name, commands)
    for e in events:
        camp.add_events([{**e, "date": key, "when": campaign.date_words(key), "major": False}])
    camp.save()
    print(f"{to} replies:\n{reply}")
    if commands:
        print(f"  commands: {commands} -> {outcomes or 'nothing applied'}")
    shown = reply + ("\n\n\u00a7G" + "\n".join(outcomes) + "\u00a7!" if outcomes else "")
    _show_reply(player, to, shown)


def write_first(camp: campaign.Campaign, player: str, sender: str, reason: str) -> None:
    """`sender` writes to the player on its own initiative (reason: an event text, or "" if unprompted)."""
    try:
        me, them = gamestate.country(player), gamestate.country(sender)
    except gamestate.StateError as e:
        print(f"  {sender} could not write: {e}")
        return
    key = campaign.date_key(them.date)
    task = _initiative_task(them, me, reason)
    prompt = ([{"role": "system", "content": _system_prompt(them, me, camp, task)}]
              + _conversation(camp, them, me)
              + [{"role": "user", "content": f"(No new letter from {me.name}. It is {campaign.date_words(key)}. "
                                             f"Write your letter to them now.)"}])
    try:
        text = _clean_for_game(actions.parse(llm.chat(prompt))[0])   # no actions in letters they start
    except llm.LLMError as e:
        print(f"  {sender} could not write (LLM error): {e}")
        return
    camp.add_letter(sender, sender, text, key)
    camp.letters_with(sender)[-1]["initiated"] = True
    camp.save()
    print(f"\n{sender} writes to {player}{' about: ' + reason if reason else ''}\n{text}")
    _show_reply(player, sender, text)


def _days(key: str) -> int:
    import datetime
    try:
        y, m, d = (int(x) for x in key.split(".")[:3])
        return datetime.date(y, m, d).toordinal()
    except ValueError:
        return 0


class _Session:
    """The current campaign and its chronicle, kept in step with the running game."""

    def __init__(self) -> None:
        self.camp: Optional[campaign.Campaign] = None
        self.last_snapshot = 0.0
        self.pending: Dict[str, str] = {}      # country -> why it wants to write ("" = unprompted)

    def campaign_for(self, player: str, cid: int) -> campaign.Campaign:
        if not cid:
            cid = campaign.new_id(player)
            print(f"new campaign {cid}")
        if not self.camp or self.camp.id != cid or self.camp.data["player"] != player:
            self.camp = campaign.Campaign(cid, player)
            print(f"campaign {cid} ({player}): {len(self.camp.data['events'])} events, "
                  f"{sum(len(v) for v in self.camp.data['letters'].values())} letters remembered")
        return self.camp

    def chronicle(self, player: str, force: bool = False) -> campaign.Campaign:
        now = time.monotonic()
        if self.camp and not force and now - self.last_snapshot < CHRONICLE_SECONDS:
            return self.camp
        self.last_snapshot = now
        # The .gui can't do either: Enter makes a new line, and the page shows the whole letter.
        # Cheap, and repeating it covers a restarted game.
        bridge.send(f"bridge.editbox.multiline {EDITBOX_NAME}", f"bridge.editbox.mirror {EDITBOX_NAME} {PAGE_NAME}")
        snap = chronicle.snapshot(player)
        camp = self.campaign_for(player, snap["campaign"])
        if camp.at(snap["date"]):
            print(f"{campaign.date_words(snap['date'])}: an earlier save was loaded; later memories dropped")
        old = camp.snapshot
        if old is None or snap["date"] > old["date"]:
            if old is not None:
                events = chronicle.diff(old, snap)
                for e in events:
                    print(f"  [{e['when']}] {e['text']}")
                camp.add_events(events)
            camp.snapshot = snap
            camp.save()
            if old is not None:
                self.plan_letters(camp, player, snap, events)
        return camp

    # ---- who writes to the player on their own
    def _cooling(self, camp: campaign.Campaign, tag: str, today: int) -> bool:
        own = [x for x in camp.letters_with(tag) if x.get("initiated")]
        return bool(own) and today - _days(own[-1]["date"]) < settings().letters.cooldown_days

    def plan_letters(self, camp: campaign.Campaign, player: str, snap: Dict, events: List[Dict]) -> None:
        cfg = settings().letters
        if not cfg.incoming:
            return
        today = _days(snap["date"])
        for e in events:
            if player not in e["tags"]:
                continue
            for other in e["tags"]:
                if other != player and other in snap["countries"] and not self._cooling(camp, other, today):
                    self.pending[other] = (self.pending.get(other, "") + " " + e["text"]).strip()
        if self.pending:
            return
        letters = [x for ls in camp.data["letters"].values() for x in ls]
        camp.data.setdefault("started", snap["date"])
        last = max([x["date"] for x in letters if x["from"] != player] or [camp.data["started"]])
        if today - _days(last) < cfg.quiet_days:
            return
        try:
            me = gamestate.country(player)
        except gamestate.StateError:
            return
        majors = [t for t, c in snap["countries"].items() if c["major"]]
        candidates = [t for t in dict.fromkeys(me.neighbors + me.faction_members + majors)
                      if t != player and t in snap["countries"] and not self._cooling(camp, t, today)]
        if candidates:
            self.pending[random.choice(candidates)] = ""

    def deliver(self, player: str, block: str) -> None:
        """Let one waiting country write, if the player isn't reading or awaiting a letter."""
        if not self.pending or not self.camp:
            return
        if re.search(r"\bairp_(reply_from|awaiting_from|outbox_to)=", block):
            return
        tag = next(iter(self.pending))
        reason = self.pending.pop(tag)
        write_first(self.camp, player, tag, reason)


def run() -> int:
    print(f"watching for letters and world events (model {llm.model()}). Ctrl+C to stop.")
    session = _Session()
    while True:
        try:
            player = bridge.player_tag()
            session.chronicle(player)
            block = bridge.country_block(player)
            to = read_outbox(block)
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
                    handle_letter(session.chronicle(player, force=True), player, to, text)
                else:
                    print("empty letter; nothing to answer")
                    bridge.send(f"eval_effect {player} = {{ clear_variable = airp_awaiting_from }}")
            else:
                session.deliver(player, block)
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
