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

Neighbours keep a policy toward the player (policy.py), refreshed in a background thread so the loop never
waits on the LLM for it. A policy shapes every letter that country writes, its urgency decides who writes
first (urgency 3 writes soon even if it hasn't been quiet), and its offers are what it may propose: a
letter it starts can carry one offer, shown with Accept/Decline (airp_offer / airp_offer_answer).
"""

from __future__ import annotations

import os
import random
import sys
import threading
import re
import time
import unicodedata
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Dict, List, Optional, Tuple

from . import (actions, bridge, campaign, agendas, chronicle, crises, plots, diplomacy, game, gamestate, leaders, llm, news, origins, peace, policy, reputation,
               guarantees, steering, story, storyteller, style, subjects, treaties, ultimatums, war, world)
from .config import settings
from .letter_mod import EDITBOX_NAME, PAGE_NAME, reply_size_for

POLL_SECONDS = 0.5
SENT_KEPT = 20            # copies of the player's latest letters, for a reply a loaded save still waits on
RESUME_SECONDS = 120      # a resumed letter whose reply didn't arrive is tried again after this long
CHRONICLE_SECONDS = 5.0   # real-time gap between world snapshots (only recorded when the date moved)
RUN_DAYS = 3              # game days the game must run, unpaused, before background LLM work, popups and letters start
EVENT_LETTERS = 2          # countries that may start a letter about events within EVENT_DAYS
EVENT_DAYS = 10            # game days
RETRY_SECONDS = 60        # after a failed background LLM call, wait this long before the next one
AI_PARALLEL = 3           # AI-to-AI exchanges running at once (NanoGPT takes several calls at a time)
POLICY_PARALLEL = 3       # policy refreshes running at once (never two for the same country)


def read_outbox(block: str) -> Optional[str]:
    """Recipient tag if a letter was just sent, else None."""
    m = re.search(r"\bairp_outbox_to=(-?[\d.]+)", block)
    return bridge.decode_country(float(m.group(1))) if m else None


def read_awaiting(block: str) -> Optional[str]:
    """The country the player's letter waits on a reply from (the reply window shows it as on its way), else None."""
    m = re.search(r"\bairp_awaiting_from=(-?[\d.]+)", block)
    return bridge.decode_country(float(m.group(1))) if m else None


REP_RESEND_SECONDS = 60   # the box's text is set again this often even unchanged (a restarted game forgets it)


def show_reputation(camp: campaign.Campaign, player: str, last: Tuple[str, float]) -> Tuple[str, float]:
    """Set the top-bar reputation box's text and tooltip when they changed. `last` is (text set, when); returns the
    new one."""
    names = (camp.snapshot or {}).get("countries", {})
    name = lambda t: names.get(t, {}).get("name", t)
    value, tip = reputation.widget(camp, player, name, _clean_for_game)
    text = value + "|" + tip
    if text == last[0] and time.monotonic() - last[1] < REP_RESEND_SECONDS:
        return last
    replies = bridge.send(
        f"bridge.setloc airp_rep_value {value}",
        f"bridge.setloc airp_rep_tip {tip.replace(chr(13), '').replace(chr(10), chr(92) + 'n')}",
        f"eval_effect {player} = {{ add_to_variable = {{ airp_rep_refresh = 1 }} }}")   # shows now, even while paused
    bad = [r for r in replies if not r.startswith("OK")]
    if bad:
        print("  could not set the reputation box:", " | ".join(bad))
    return text, time.monotonic()


def _loc_line(text: str) -> str:
    """Text for bridge.setloc: one line, newlines as \\n (it refuses empty text: a non-breaking space shows nothing)."""
    return text.replace(chr(13), "").replace(chr(10), chr(92) + "n") or chr(160)


def show_treaties(camp: campaign.Campaign, player: str, last: Tuple[str, float]) -> Tuple[str, float]:
    """The treaty row in the diplomacy view's relations list (the bridge adds it): the pairs of countries it shows a
    flag for (bridge.treaties) and each flag's tooltip, loc key airp_rel_<A>_<B> (tags in alphabetical order).
    `last` as in show_reputation."""
    rows = sorted(treaties.panel(camp, player).items())
    text = repr(rows)
    if text == last[0] and time.monotonic() - last[1] < REP_RESEND_SECONDS:
        return last
    names = (camp.snapshot or {}).get("countries", {})
    cmds = [("bridge.treaties " + " ".join(f"{player}:{tag}" for tag, _ in rows)).strip()]
    for tag, (summary, tip) in rows:
        a, b = sorted((player, tag))
        them = names.get(tag, {}).get("name", tag)
        cmds.append(f"bridge.setloc airp_rel_{a}_{b} {_loc_line(f'§YTreaties by letter with {them}§!' + chr(10) + summary + chr(10) + chr(10) + tip)}")
    bad = [r for r in bridge.send(*cmds) if not r.startswith("OK")]
    if bad:
        print("  could not set the treaty box:", " | ".join(bad))
    return text, time.monotonic()


def resume_letter(camp: campaign.Campaign, player: str, to: str) -> None:
    """The game waits on a reply no letter is being answered for: a save made while the reply was being written,
    then loaded. Answer the letter sent to `to` by then (kept in camp.data["sent"]); with no copy, close the wait."""
    now = camp.data.get("last_date") or ""
    sent = [x for x in camp.data.get("sent", []) if x["to"] == to and (not now or x["date"] <= now)]
    thread = camp.letters_with(to)
    text = sent[-1]["text"] if sent else ""
    if text and len(thread) >= 2 and thread[-1]["from"] == to and thread[-2]["from"] == player \
            and thread[-2]["text"] == text:
        # The save is from after the reply was written (it's still remembered): show it again, not a second one
        print(f"\nthe loaded save waits on a reply from {to}: showing it again")
        offer = camp.data.get("pending_offer") or {}
        _show_reply(player, to, thread[-1]["text"], offer=offer.get("from") == to)
        return
    if thread and thread[-1]["from"] == player and (not text or thread[-1]["text"] == text):
        text = thread.pop()["text"]                              # unanswered in memory: answered anew below
    if text:
        print(f"\nthe loaded save waits on a reply from {to}: answering the letter sent "
              f"{campaign.date_words(sent[-1]['date']) if sent else 'before'}")
        handle_letter(camp, player, to, text)
    else:
        print(f"\nthe loaded save waits on a reply from {to}, but no copy of the letter is kept")
        _show_reply(player, to, "(Your letter was lost on the way, and no copy survives. Write again.)")


def read_letter_box() -> str:
    try:
        return bridge.editbox(EDITBOX_NAME)
    except bridge.BridgeError as e:
        raise RuntimeError(f"could not read the letter: {e}") from None


def clear_letter_box() -> None:
    bridge.editbox_set(EDITBOX_NAME)


def _clean_for_game(text: str) -> str:
    """Make LLM text safe for HOI4 localisation: no markup characters, only Latin-1 letters. If the model wrote
    several drafts separated by a rule line (---), only the first is kept."""
    text = re.split(r"\n\s*(?:-{3,}|\*{3,}|_{3,})\s*\n", text.strip())[0]
    for a, b in {"‘": "'", "’": "'", "“": '"', "”": '"', "—": "-",
                 "–": "-", "…": "..."}.items():
        text = text.replace(a, b)
    text = re.sub(r"(?im)^[ \t]*(?:grant|give)[ \t]+(?:rights|dmz|state)[ \t]+\d+[ \t]*$\n?", "", text)   # a bare command
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


def _ties(them: gamestate.CountryState, me: gamestate.CountryState) -> str:
    """The agreements between the two as the game has them, however they came about: at the start (France's
    guarantee of Belgium), by a focus or event, by the game's own diplomacy, or by letter."""
    t, m = them.name, me.name
    words = {"pact": "a non-aggression pact", "market": "market access both ways",
             "they_guarantee": f"{t} guarantees {m}'s independence", "my_guarantee": f"{m} guarantees {t}'s independence",
             "they_access": f"{t} gives {m} military access", "my_access": f"{m} gives {t} military access",
             "they_docking": f"{t} gives {m} docking rights", "my_docking": f"{m} gives {t} docking rights",
             "embargo": f"{t} embargoes {m}"}
    try:
        return "; ".join(w for k, w in words.items() if actions._CHECKS[k](them.tag, me.tag))
    except Exception:
        return ""


def _relation(them: gamestate.CountryState, me: gamestate.CountryState) -> str:
    facts = []
    if me.tag in them.enemies:
        facts.append(f"{them.name} and {me.name} are AT WAR with each other.")
    if them.faction and them.faction == me.faction:
        facts.append(f"Both are members of the {them.faction}.")
    if me.tag in them.neighbors:
        facts.append("They share a border.")
    if (ties := _ties(them, me)):        # from the game: those of the start and of focuses too, not only letter deals
        facts.append(f"In force between them now: {ties}. Don't offer or ask for what already stands.")
    def subject(sub: gamestate.CountryState, lord: gamestate.CountryState) -> str:
        level = sub.autonomy.get(lord.tag) or lord.autonomy.get(sub.tag) or ""
        detail = subjects.relation_text(sub.tag, sub.name, lord.tag, lord.name, level) if level else ""
        return f"{sub.name} is a SUBJECT of {lord.name}, not an independent country. {detail}".strip()
    if them.overlord == me.tag:
        facts.append(subject(them, me))
    if me.overlord == them.tag:
        facts.append(subject(me, them))
    if them.capitulated and me.tag in them.enemies:
        facts.append(f"{them.name} has CAPITULATED to {me.name} and its allies: it has lost the war and is "
                     f"at their mercy.")
    if me.capitulated and them.tag in me.enemies:
        facts.append(f"{me.name} has CAPITULATED: it has lost the war against {them.name} and is at its mercy.")
    if any(w.other == me.tag for w in them.wargoals):
        facts.append(f"{them.name} holds a war goal against {me.name}.")
    if any(w.other == them.tag for w in me.wargoals):
        facts.append(f"{me.name} holds a war goal against {them.name}.")
    for j in me.justifying:
        if j["target"] == them.tag:
            facts.append(f"{me.name} is justifying {gamestate.justification_text(j, me.name_of)}: preparing a "
                         f"pretext for war, which {them.name} knows.")
    for j in them.justifying:
        if j["target"] == me.tag:
            facts.append(f"{them.name} is justifying {gamestate.justification_text(j, them.name_of)}.")
    if me.tag in them.opinion:
        facts.append(f"{them.name}'s opinion of {me.name}: {them.opinion[me.tag]:+g} (range -100 to +100).")
    if them.tag in me.opinion:
        facts.append(f"{me.name}'s opinion of {them.name}: {me.opinion[them.tag]:+g}.")
    comparison = gamestate.strength_comparison(them, me)
    return " ".join(facts or ["No war, alliance, border or subject relation between them."]) + \
        (f" {comparison}" if comparison else "")


def _ago(days: int) -> str:
    """12 -> '12 days ago', 45 -> '6 weeks ago', 400 -> '13 months ago'."""
    if days < 14:
        return "today" if days <= 0 else f"{days} day{'s' if days > 1 else ''} ago"
    if days < 60:
        return f"{days // 7} weeks ago"
    return f"{days // 30} months ago"


def _war(camp: campaign.Campaign, them: gamestate.CountryState, me: gamestate.CountryState) -> str:
    """The war between the two, if any: who is winning, the facts, and how it has moved."""
    if me.tag not in them.enemies:
        return ""
    lines = [gamestate.war_balance(them, me)]
    today = campaign.date_key(them.date)
    war = next((w for w in them.wars if w.enemy == me.tag), None)
    start = campaign.save_key(war.start_date) if war and war.start_date else ""
    if start and today:
        lines.append(f"The war began {_ago(_days(today) - _days(start))}, on {campaign.date_words(start)}.")
    for x in (them, me):
        past = []
        for label, days in (("a month ago", 30), ("three months ago", 90)):
            then = campaign.shift(today, -days)
            v = camp.surrender_at(x.tag, then) if start and then >= start else None
            if v is not None:
                past.append(f"{label} {round(v * 100)}%")
        if past:
            lines.append(f"{x.name}'s surrender progress {', '.join(past)}; now {round((x.surrender or 0) * 100)}%.")
    whole = gamestate.whole_war(them, me)
    if whole:
        lines.append(whole["text"])
        allies = _allies_stances(camp, them, me)
        if allies:
            lines.append(allies)
    lines.append("Your government knows all this. Whatever your tone, don't pretend the war stands otherwise.")
    return "THE WAR BETWEEN YOU:\n" + "\n".join(lines)


def _allies_stances(camp: campaign.Campaign, them: gamestate.CountryState, me: gamestate.CountryState) -> str:
    """For a faction leader (the only one who can make peace for its side): where its allies in this war stand,
    from their own war stances. Empty for anyone else."""
    if them.faction_leader != them.tag and them.faction:
        return ""
    side = [t for t in gamestate.war_side(them, me) if t != them.tag]
    if not side:
        return ""
    groups: Dict[str, List[str]] = {}
    for t in side:
        pol = camp.policy_of(t) or {}
        stance = "capitulated" if t in them.fallen else pol.get("war") or "unknown"
        groups.setdefault(stance, []).append(them.name_of(t))
    order = ["capitulated"] + list(reversed(war.STANCES)) + ["unknown"]
    parts = [f"{s}: {', '.join(groups[s][:8])}" + (f" and {len(groups[s]) - 8} more" if len(groups[s]) > 8 else "")
             for s in order if s in groups]
    return (f"Your allies in this war, by their own war stance (only you, as {'faction leader' if them.faction else 'overlord'}, "
            f"can make peace for your side; they look to you): " + "; ".join(parts) + ".")


def _memory(camp: campaign.Campaign, them: gamestate.CountryState, me: gamestate.CountryState) -> str:
    # How the player answered someone else's proposal is between those two (the deal itself, if any, has its
    # own lines, which others do see).
    events = [e for e in camp.data["events"]
              if (them.tag in e["tags"] or me.tag in e["tags"] or e.get("major"))
              and (e.get("type") != "offer_answer" or them.tag in e["tags"])]
    if not events:
        return "Nothing notable recorded yet."
    return "\n".join(f"- {when}: {text}" for when, text in chronicle.compact(events, camp.snapshot))


def _reply_task(them: gamestate.CountryState, me: gamestate.CountryState) -> str:
    st = style.current()
    voice = st.voice.format(name=them.name, leader=them.leader, other=me.name)
    return (f"Write {them.name}'s reply to their latest letter, {voice}, true to {them.name}'s interests, "
            f"ideology and situation. {st.manner}")


def _initiative_task(them: gamestate.CountryState, me: gamestate.CountryState, reason: str) -> str:
    why = (f"Something just happened that concerns you both: {reason} Write about it."
           if reason else
           f"There is no specific occasion: write about whatever matters most to {them.name} regarding "
           f"{me.name} right now, given the situation and what you remember.")
    st = style.current()
    voice = st.voice.format(name=them.name, leader=them.leader, other=me.name)
    return (f"{them.name} now writes a new letter to {me.name} on its own initiative, {voice}, true to "
            f"{them.name}'s interests, ideology and situation. {why} Your earlier letters are above: say something "
            f"new (a development, a question, a concern, a demand), not what you already said in other words. Don't "
            f"keep announcing an offer for later: propose it when you can, or let it rest. {st.manner}")


OWN_FOCUSES = 8       # completed focuses told in full in a country's own description (the rest by name)
OTHER_FOCUSES = 3     # the same for the country it corresponds with, whose spirits are named only


def _context(them: gamestate.CountryState, me: gamestate.CountryState, camp: campaign.Campaign) -> str:
    """Everything `them` knows when dealing with `me`."""
    pol = policy.describe(policy.current(camp, them.tag), them.name, me.name)
    rep = reputation.describe(camp, me.tag, me.name, them.tag)
    return (
        f"You are the government of {them.name} in the grand strategy game Hearts of Iron IV. "
        f"The date is {them.date.strip()}.\n\n"
        f"{world.text([p.ideology for p in me.parties + them.parties])}\n\n"
        + (f"{wt}\n\n" if (wt := world.tension_text({them.tag: them.name, me.tag: me.name})) else "") +
        f"YOUR COUNTRY:\n{gamestate.describe(them, focus_details=OWN_FOCUSES)}\n\n"
        f"THE COUNTRY YOU CORRESPOND WITH:\n"
        f"{gamestate.describe(me, focus_details=OTHER_FOCUSES, spirit_details=False)}\n\n"
        f"BETWEEN YOU: {_relation(them, me)}\n\n"
        + (f"{sec}\n\n" if (sec := _security(them, me)) else "")
        + (f"{_war(camp, them, me)}\n\n" if me.tag in them.enemies else "")
        + (f"{began}\n\n" if (began := origins.text(camp, them, me)) else "")
        + (f"{pol}\n\n" if pol else "")
        + (f"YOUR GOVERNMENT'S STANDING AIMS (what it means to work toward):\n{aims}\n\n"
           if (aims := agendas.describe(camp, them.tag, them.name_of)) else "")
        + (f"{rep}\n\n" if rep else "")
        + (f"{deals}\n\n" if (deals := treaties.describe(camp, them.tag, them.name, me.name)) else "")
        + (f"{lines}\n\n" if (lines := treaties.commitments_text(camp, them.tag)) else "")
        + f"{guarantees.describe(camp, them.tag, them.name, me.name)}\n\n"
        + (f"{ults}\n\n" if (ults := ultimatums.describe(camp, them.tag, them.name, me.name)) else "")
        + f"WHAT YOU REMEMBER (events since the chronicle began, oldest first; involving you, "
        f"{me.name}, or the major powers):\n{_memory(camp, them, me)}"
    )


_SILENCE = ("{other}'s government is busy running a country and often doesn't answer letters; that is normal, "
            "not a slight. Don't comment on silence, unanswered letters or slow replies. Only if something urgent you "
            "asked for has gone unanswered for a long time may you make at most one brief, reserved remark about it.")


def _history_note(camp: campaign.Campaign, them: gamestate.CountryState, me: gamestate.CountryState) -> str:
    """What the model may assume about earlier letters: only what's in the correspondence, and the player's
    silence is normal (they're playing the game and won't answer most letters)."""
    silence = _SILENCE.format(other=me.name)
    if not camp.letters_with(them.tag):
        return (f"You and {me.name} have never exchanged letters before: this is the first correspondence "
                f"between you. Don't refer to earlier letters, offers or answers.")
    last = camp.letters_with(them.tag)[-1]["date"]
    changed = [e["text"] for e in camp.data["events"]
               if e["date"] > last and them.tag in e["tags"] and e.get("type") in policy.RELATION_TYPES
               and (me.tag in e["tags"] or e["type"] != "war")]
    since = (f" Since the last letter between you, your situation has changed: {' '.join(changed)} The earlier "
             f"letters were written before this; write from your situation as it is now."
             if changed else "")
    return (f"Your earlier correspondence with {me.name} follows as the conversation, each letter dated. Stay "
            f"consistent with what you wrote before, and let what happened since colour your reply. Refer only to "
            f"letters, offers and answers that actually appear there or in what you remember.{since} {silence}")


def _transcript(camp: campaign.Campaign, them: gamestate.CountryState, me: gamestate.CountryState) -> str:
    """The correspondence as plain text (for prompts that aren't the letter conversation itself)."""
    lines = []
    for x in camp.letters_with(them.tag):
        who = them.name if x["from"] == them.tag else me.name
        lines.append(f"--- {x['when']}, {who} wrote:\n{x['text']}")
    return "\n\n".join(lines) or "No letters exchanged yet."


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
        f"{_context(them, me, camp)}\n\n"
        f"{_history_note(camp, them, me)}\n\n"
        f"{task or _reply_task(them, me)}\n\n"
        f"{style.current().rules.format(name=them.name, other=me.name)}\n\n{style.diplomacy()}\n\n{consequences}"
    )


def _show_reply(player: str, sender: str, text: str, offer: bool = False) -> None:
    body = text.replace("\r", "").replace("\n", "\\n")
    size = reply_size_for(text)       # which of the reply window's heights fits the letter
    offer_var = "set_variable = { airp_offer = 1 }" if offer else "clear_variable = airp_offer"
    replies = bridge.send(
        f"bridge.setloc airp_reply_body {body}",
        f"eval_effect {player} = {{ clear_variable = airp_awaiting_from {offer_var} "
        f"set_variable = {{ airp_reply_size = {size} }} set_variable = {{ airp_reply_from = {sender}.id }} "
        f"add_to_variable = {{ airp_reply_refresh = 1 }} add_to_variable = {{ airp_awaiting_refresh = 1 }} }}",   # update the window now, even while paused
    )
    bad = [r for r in replies if not r.startswith("OK")]
    if bad:
        print("  could not show the reply:", " | ".join(bad))


def _conversation(camp: campaign.Campaign, them: gamestate.CountryState,
                  me: gamestate.CountryState, last_note: str = "") -> List[Dict[str, str]]:
    """The correspondence as chat messages; `last_note` is added under the last letter (e.g. a length reminder)."""
    msgs = []
    for x in camp.letters_with(them.tag):
        if x["from"] == them.tag:
            msgs.append({"role": "assistant", "content": x["text"]})
        else:
            msgs.append({"role": "user", "content": f"Letter from {me.name}, {x['when']}:\n\n{x['text']}"})
    if last_note and msgs and msgs[-1]["role"] == "user":
        msgs[-1] = {**msgs[-1], "content": msgs[-1]["content"] + f"\n\n{last_note}"}
    return msgs


def handle_letter(camp: campaign.Campaign, player: str, to: str, text: str) -> None:
    print(f"\n{player} -> {to}: {text}")
    try:
        me, them = gamestate.country(player), gamestate.country(to)
    except gamestate.StateError as e:
        print("  could not read the game state:", e)
        _show_reply(player, to, "(The courier could not find his way: the game state is unavailable.)")
        return
    if them.capitulated:                               # no government left to answer: no LLM call
        print(f"  {them.name} has capitulated; no reply")
        _show_reply(player, to, f"(No answer comes. {them.name} has capitulated, and no government remains "
                                f"to receive your letter.)")
        return
    key = campaign.date_key(them.date)
    camp.at(key)
    camp.add_letter(to, player, text, key)
    # A copy that loading an earlier save doesn't drop: a save made while the reply was being written still waits
    # for it, and the letter box isn't saved (resume_letter)
    camp.data["sent"] = (camp.data.get("sent", []) + [{"to": to, "text": text, "date": key}])[-SENT_KEPT:]
    camp.save()
    rel = actions.relation(to, player)
    rel.pledged = frozenset(treaties.standing(camp, to))   # a commitment standing already isn't granted again
    actions.set_parties(rel, them, me)
    offers, asks = _proposals(camp, to, rel, key, reply=True)
    actions.economy(them.name, me.name, rel)
    actions.union(me.name, agendas.wants(camp, them.tag, me.tag))
    help_text = (actions.reply_help(them.name, me.name, rel) + "\n\n"
                 + actions.proposal_help(them.name, me.name, offers, asks, expected=False,
                                         grants=actions.reply_grants(rel))
                 + (f"\n{_COUNTER}" if offers or asks else "")
                 + ("\n\n" + _peace_note(them, me, camp, offers, reply=True) if rel.at_war else "")
                 + ("\n\n" + actions.cede_help(them.name, me.name, actions.own_states(to))
                    if not rel.at_war and not rel.them_subject else "")
                 + ("\n\n" + actions.subject_help(them.name, me.name, actions.subjects_of(to)) if not rel.at_war else "")
                 + ("\n\n" + actions.subject_note(them.name, them.name_of(them.overlord))
                    if them.overlord and them.overlord != player else "")
                 + ("\n\n" + treaties.help_text() if not rel.at_war else "")
                 + ("\n\n" + ultimatums.help_text(them.name, me.name) if _may_threaten(camp, to, rel) else ""))
    try:
        prompt = ([{"role": "system", "content": _system_prompt(them, me, camp, actions_help=help_text)}]
                  + _conversation(camp, them, me, f"(Reply in {style.current().length}.)"))
        raw = llm.chat(prompt, kind="reply", about=to)
    except llm.LLMError as e:
        print("  LLM error:", e)
        _show_reply(player, to, _clean_for_game(f"(The courier returned without an answer: {e})"[:300]))
        return
    raw, ultimatum = ultimatums.parse(raw)
    if extra := _missing_commands(prompt, raw, to, grants=True):
        raw += "\n\n" + extra
    raw, terms = treaties.parse_terms(raw)
    letter, commands, proposals = actions.parse(raw)
    if fixed := _unmatched(prompt, raw, to, proposals + [k for k, _ in commands],
                           offers + asks + actions.reply_grants(rel), grants=True):
        raw, terms = treaties.parse_terms(fixed)
        letter, commands, proposals = actions.parse(raw)
    commands, renounced = treaties.take_renounce(commands)
    # Payments agreed for later ([[civ 1 in 12 months]]) are kept by the treaty, not made now; only what they could
    # grant now (asked for, and possible) counts.
    later = [k for k, _ in commands if treaties.deferred(terms, k) and k.startswith("give:")
             and actions.possible(rel, k)]
    commands = [c for c in commands if not treaties.deferred(terms, c[0])]
    reply = _clean_for_game(letter)
    camp.add_letter(to, to, reply, key)
    outcomes, events = actions.apply(to, player, them.name, me.name, commands,
                                     allowed=["opinion"] + actions.GIVES + actions.UNILATERAL + actions.state_gives(rel)
                                     + ["give:civ", "give:mil", "give:dismantle"] + actions.govt_gives(rel)
                                     + actions.rights_gives(rel) + actions.dmz_gives(rel)
                                     + actions.EQUIPMENT_GIVES + actions.war_gives(rel) + actions.third_gives(rel))
    if not rel.at_war:                                  # land for a third country, at the player's request
        more_shown, more_events = actions.apply_cedes(to, player, them.name, me.name, commands,
                                                      (camp.snapshot or {}).get("countries", {}))
        sub_shown, sub_events = actions.apply_subjects(to, player, them.name, me.name, commands)
        outcomes, events = outcomes + more_shown + sub_shown, events + more_events + sub_events
    for e in events:
        camp.add_events([{**e, "date": key, "when": campaign.date_words(key), "major": False}])
    treaties.record(camp, to, [e["type"] for e in events], terms, key, them.name, later=later)
    outcomes += [(line, False) for line in guarantees.record(camp, to, [e["type"] for e in events], terms)]
    outcomes += [(f"Agreed: {them.name} pays us {actions.get_deal(k).label} in {terms['payments'][k][2]} months.",
                  False) for k in later]
    if renounced:
        outcomes += treaties.renounce(camp, to, player)
    for line in steering.sync(camp, to):                # commitments made or renounced steer its AI
        print(line)
    # Proposal items are only kept if still possible after what was just applied.
    after = actions.relation(to, player) if events else rel
    items = _attach_proposal(camp, to, actions.package([p for p in proposals if actions.possible(after, p)],
                                                       offers + asks), key, terms)
    _dropped(to, proposals, items)
    outcomes += _set_ultimatum(camp, to, player, rel, ultimatum, items, key, them.name, me.name)
    outcomes += [n] if (n := steering.take_notice(camp, to, them.name)) else []
    camp.save()
    print(f"{to} replies{' (proposes ' + ', '.join(items) + ')' if items else ''}:\n{reply}")
    if commands:
        print(f"  commands: {commands} -> {[o for o, _ in outcomes] or 'nothing applied'}")
    shown = reply + ("\n\n" + actions.outcome_text(outcomes) if outcomes else "")
    if items:
        shown += ("\n\n" + actions.proposal_line(items, them.name) + treaties.terms_line(terms, items, them.name)
                  + guarantees.proposal_line(camp, items, to, them.name, terms) + _peace_line(items, me, them))
    _show_reply(player, to, shown, offer=bool(items))


def _may_threaten(camp: campaign.Campaign, tag: str, rel: "actions.Relation") -> bool:
    """May `tag` set an ultimatum in the letter it writes now (ultimatums.allowed)?"""
    pol = policy.current(camp, tag) or {}
    return ultimatums.allowed(camp, tag, rel, pol.get("stance", "indifferent"))


def _set_ultimatum(camp: campaign.Campaign, tag: str, player: str, rel: "actions.Relation", ultimatum, items,
                   key: str, them_name: str, me_name: str) -> List[tuple]:
    """Set the ultimatum the letter carries, if it may: its proposal's asks are its demands. Returns the line to
    show under the letter."""
    if not ultimatum:
        return []
    if not _may_threaten(camp, tag, rel):
        print(f"  ultimatum from {tag} not allowed now, ignored")
        return []
    months, demand = ultimatum
    u, line = ultimatums.issue(camp, tag, player, months, demand, items, key, them_name, me_name)
    if items and camp.data.get("pending_offer"):
        camp.data["pending_offer"]["ultimatum"] = u["id"]     # accepting the proposal meets the ultimatum
    return [(line, True)]


def _peace_line(items: List[str], me: gamestate.CountryState, them: gamestate.CountryState) -> str:
    """Under a peace offer: what the peace conference would bring."""
    return "\n§Y" + peace.estimate(me, them)[1] + "§!" if "give:peace" in items else ""


def _attach_proposal(camp: campaign.Campaign, tag: str, items: List[str], key: str,
                     terms: Optional[Dict] = None) -> List[str]:
    """Remember the proposal package on the letter just added and as the pending Accept/Decline, with the terms
    that concern it (treaties.py)."""
    if items:
        camp.letters_with(tag)[-1]["proposal"] = items
        camp.data["pending_offer"] = {"from": tag, "keys": items, "date": key,
                                      "terms": treaties.for_items(terms, items)}
    return items


# A letter that speaks of a deal but carries no command for it leaves nothing to accept. The LLM sometimes writes the
# offer into the text and forgets the commands; one short follow-up asks for them.
_DEAL_WORDS = re.compile(r"\b(offer|propos|in return|in exchange|pact|guarantee|access|passage|market|trade|"
                         r"factor|pledge|alliance|faction|docking|rifles|artillery|trucks|equipment|cede|"
                         r"oppos|befriend|sanction|stand behind)", re.I)
_REPAIR_KINDS = re.compile(r"\[\[\s*(offer|ask|for)\b", re.I)


def _missing_commands(prompt: List[Dict], raw: str, about: str, grants: bool) -> str:
    """`raw` has no commands but its text speaks of a deal: the commands its text makes ('' if none). Proposal
    commands only (offer, ask, terms), and with `grants` (a reply) also what it grants outright; never a step of
    its own (ending an agreement, an embargo, an ultimatum), which the letter would have to mean."""
    if "[[" in raw or not _DEAL_WORDS.search(raw):
        return ""
    ask = (f"Your letter above has no commands, so nothing in it takes effect. If its text proposes a deal"
           f"{' or grants something' if grants else ''}, answer with exactly the commands for it, as the instructions "
           f"describe, one per item. If it proposes nothing concrete, answer NONE. Commands only, no other text.")
    try:
        answer = llm.chat(prompt + [{"role": "assistant", "content": raw}, {"role": "user", "content": ask}],
                          kind="commands", about=about)
    except llm.LLMError as e:
        print(f"  commands for {about}'s letter failed: {e}")
        return ""
    found = [c for c in re.findall(r"\[\[[^\[\]]+\]\]", answer)
             if _REPAIR_KINDS.match(c) or (grants and not re.match(r"\[\[\s*(end|embargo|ultimatum|opinion)\b",
                                                                       c, re.I))]
    if found:
        print(f"  {about}'s letter spoke of a deal without commands; added {' '.join(found)}")
    return " ".join(found)


_PROPOSAL_COMMAND = re.compile(r"\[\[\s*(offer|ask|for)\b[^\[\]]*\]\]", re.I)


def _unmatched(prompt: List[Dict], raw: str, about: str, covered: List[str], allowed: List[str],
               grants: bool = False) -> str:
    """The letter's text speaks of a kind of deal its commands leave out (it says it can spare a factory, and no
    factory is in the proposal): ask once whether the letter offers it. Returns `raw` with its proposal commands
    replaced by the corrected ones, or '' if they stand."""
    missing = actions.unmatched_topics(_PROPOSAL_COMMAND.sub("", raw), covered, allowed)
    if not missing:
        return ""
    what = ", ".join(missing)
    ask = (f"Your letter's text speaks of {what}, but its commands don't include that. If the letter offers or asks "
           f"for it as part of the deal, answer with all the proposal commands the letter should carry: the whole "
           f"proposal, terms included, as the instructions describe. If the letter declines it or only mentions it in "
           f"passing, answer SAME. Commands or SAME only, no other text.")
    try:
        answer = llm.chat(prompt + [{"role": "assistant", "content": raw}, {"role": "user", "content": ask}],
                          kind="commands", about=about)
    except llm.LLMError as e:
        print(f"  checking {about}'s proposal failed: {e}")
        return ""
    found = [c for c in re.findall(r"\[\[[^\[\]]+\]\]", answer)
             if _REPAIR_KINDS.match(c) or (grants and not re.match(r"\[\[\s*(end|embargo|ultimatum|opinion)\b", c, re.I))]
    if not found:
        return ""
    print(f"  {about}'s letter spoke of {what} outside its proposal; proposal now {' '.join(found)}")
    return _PROPOSAL_COMMAND.sub("", raw).rstrip() + "\n\n" + " ".join(found)


def _dropped(about: str, proposals: List[str], items: List[str]) -> None:
    """Log proposal items the letter made that weren't allowed or possible, so a lost deal shows in the log."""
    lost = [p for p in proposals if p not in items]
    if lost:
        print(f"  {about}'s proposal items dropped (not possible now, or carried by a state in the deal): "
              f"{', '.join(lost)}")


_COUNTER = ("You are answering their letter, so you may make a counter-offer: a package that answers what they "
            "proposed, asked or threatened (for example something you give in return for something you ask). Your "
            "policy above is your intent: depart from it only when their letter gives you good reason, and make "
            "no proposal if you simply refuse.")


def _security(them: gamestate.CountryState, me: gamestate.CountryState) -> str:
    """In peacetime: how strong `them` is against `me`, who would defend it, and what that means for its tone."""
    if me.tag in them.enemies or not them.strength or not me.strength:
        return ""
    fac = lambda s: s.strength.get("civ", 0) + s.strength.get("mil", 0) + s.strength.get("dock", 0)
    ratio = max(fac(me) / max(fac(them), 1), me.strength.get("army_k", 0) / max(them.strength.get("army_k", 0), 1))
    lines = gamestate.query(f'{them.tag} = {{ every_other_country = {{ limit = {{ has_guaranteed = {them.tag} '
                            f'NOT = {{ tag = {me.tag} }} }} log = "AIRPG|[THIS.GetName]" }} }}')
    guards = [x[6:].strip() for x in lines if x.startswith("AIRPG|")]
    if them.faction and them.faction != me.faction:
        guards.append(f"its faction, the {them.faction}" + (f" led by {them.name_of(them.faction_leader)}"
                                                            if them.faction_leader != them.tag else ""))
    shield = f"pledged to defend it: {', '.join(guards)}" if guards else "no one has pledged to defend it"
    if bridge.trigger(f"{me.tag} = {{ has_guaranteed = {them.tag} }}"):
        shield += f"; {me.name} itself has guaranteed its independence, and can be held to its word"
    if ratio >= 3:
        return (f"YOUR SECURITY: {them.name} is far weaker than {me.name} (about {ratio:.0f} to 1 in industry or "
                f"soldiers), and {shield}. A government this exposed is careful: it usually yields a modest demand "
                f"of so strong a neighbour, getting what it can in return (a guarantee, factories, a pact), rather "
                f"than invite war; it refuses only what would end it as a nation, and then courteously, without "
                f"taunts, lectures or moralising; and it takes no hostile step (an embargo, ending agreements) it "
                f"cannot back up. "
                + ("Its protectors may stiffen its back, but their help is never certain. " if guards else "")
                + "Only a leader reckless by nature would act otherwise.")
    if ratio >= 1.5 and guards:
        return (f"YOUR SECURITY: {them.name} is weaker than {me.name} on its own (about {ratio:.1f} to 1), but not "
                f"alone: {shield}. It can stand firm, while avoiding needless provocation.")
    if ratio >= 1.5:
        return (f"YOUR SECURITY: {them.name} is weaker than {me.name} (about {ratio:.1f} to 1), and {shield}. It "
                f"speaks with care and avoids needless provocation.")
    if ratio <= 1 / 1.5:
        return f"YOUR SECURITY: {them.name} is stronger than {me.name}, and can speak from strength."
    return f"YOUR SECURITY: {them.name} and {me.name} are roughly matched in strength; {shield}."


def _peace_offer(camp: campaign.Campaign, tag: str, rel: "actions.Relation", today: int, reply: bool) -> List[str]:
    """["give:peace"] if `tag` may offer the player peace now: peace can be made (peace.possible), and its war
    stance is 'seek peace' or softer (in a reply, where the player may have asked for peace: 'open to terms' or
    softer). A refused peace isn't offered again until the war turns (the policy basis changes) or refusal_days
    pass."""
    if not actions.possible(rel, "give:peace"):
        return []
    pol = policy.current(camp, tag)
    if not pol or pol.get("war") not in war.STANCES or war.STANCES.index(pol["war"]) < (1 if reply else 2):
        return []
    basis = policy.basis(camp.snapshot, tag, camp.data["player"])
    for r in camp.data.get("refusals", []):
        if (r["from"] == tag and "give:peace" in r["keys"] and r.get("basis") == basis
                and today - _days(r["date"]) < settings().letters.refusal_days):
            return []
    return ["give:peace"]


def _peace_help(them: gamestate.CountryState, me: gamestate.CountryState, camp: campaign.Campaign,
                reply: bool) -> str:
    """What the LLM needs to decide on peace: its stance and how a peace conference would turn out."""
    margin, _ = peace.estimate(me, them)
    pct = round(abs(margin) * 100)
    outcome = ("a white peace: neither side gains anything" if pct == 0 else
               f"{me.name}'s side would receive about {pct}% of the war score, to take from your side" if margin > 0
               else f"your side would receive about {pct}% of the war score, to take from {me.name}'s side")
    pol = policy.current(camp, them.tag) or {}
    when = (f"if {me.name} asked for peace or terms and your war stance accepts it, or if your stance is to seek "
            f"peace" if reply else "your war stance is to seek peace, so this is the moment to offer it")
    sides = peace.table(me, them)
    return (f"PEACE. You may offer {me.name} peace with [[offer peace]], on its own (no other items): "
            f"{when}. If {me.name} accepts, a peace conference settles the terms by how the war stands; right now "
            f"that would be {outcome}. {sides + ' ' if sides else ''}{_TERMS_NOTE} Your war stance: "
            f"{pol.get('war', 'unknown')}. Never offer "
            f"peace while your stance is to fight on, and don't offer it if you refuse their request; say in the "
            f"letter what you offer or refuse.")


_TERMS_NOTE = ("A letter can offer or accept peace, but cannot fix its terms: the peace conference decides who gets "
               "what. You may say what you hope for, but don't bargain over or promise specific provinces, payments "
               "or guarantees as if the letter could settle them.")


def _peace_note(them: gamestate.CountryState, me: gamestate.CountryState, camp: campaign.Campaign,
                offers: List[str], reply: bool) -> str:
    """Peace instructions for a letter between enemies: how to offer it, or why it can't be offered now."""
    if me.tag not in them.enemies:
        return ""
    if "give:peace" in offers:
        return _peace_help(them, me, camp, reply)
    pol = policy.current(camp, them.tag) or {}
    stance = pol.get("war", "")
    why_not = peace.blocker(me, them)
    if why_not:
        return (f"PEACE. No peace can be concluded now: {why_not}. Don't offer or promise peace or terms in this "
                f"letter; if {me.name} asks for peace, answer in character and say why it cannot be yet. {_TERMS_NOTE}")
    if stance == "fight on":
        return (f"PEACE. Your war stance is to fight on: you don't offer peace now, and if {me.name} asks for it, "
                f"you refuse. {_TERMS_NOTE}")
    return f"PEACE. Make no peace offer in this letter. {_TERMS_NOTE}"


def _proposals(camp: campaign.Campaign, tag: str, rel: "actions.Relation", key: str, reply: bool = False):
    """What `tag` may propose now: its policy's offers and asks, or, for a country without a policy (not a
    neighbour), anything currently possible, left to the LLM's judgement. Nothing if anyone proposed
    within proposal_gap_days; never an item the player refused within refusal_days, unless something
    happened in the world between the two since. Peace (at war) follows its own rules (_peace_offer) and
    isn't held back by the gap."""
    cfg = settings().letters
    today = _days(key)
    peace_offer = _peace_offer(camp, tag, rel, today, reply)
    if reply:
        # Answering the player: any deal possible now, as a counter-offer or package, with no gap or refusal
        # memory (the player reopened the matter). Its policy, in the prompt, is its intent, not its limit.
        pol = policy.current(camp, tag)
        if not pol and policy.stale(camp, tag):
            return peace_offer, []
        offers = (actions.GIVES + actions.state_gives(rel) + actions.subject_gives(rel)
                  + ["give:civ", "give:mil", "give:dismantle"] + actions.govt_gives(rel) + actions.rights_gives(rel)
                  + actions.dmz_gives(rel) + actions.EQUIPMENT_GIVES + actions.war_gives(rel)
                  + actions.third_gives(rel))
        asks = (actions.ASKS + actions.state_asks(rel) + actions.subject_asks(rel)
                + ["ask:civ", "ask:mil", "ask:dismantle"] + actions.govt_asks(rel) + actions.rights_asks(rel)
                + actions.dmz_asks(rel) + actions.EQUIPMENT_ASKS + actions.war_asks(rel) + actions.third_asks(rel))
        standing = treaties.standing(camp, tag)
        ok = lambda k: k != "give:peace" and k not in standing and actions.possible(rel, k)
        return peace_offer + [k for k in offers + actions.rights_after(rel) if ok(k)], [k for k in asks if ok(k)]
    proposed = [x["date"] for ls in camp.threads().values() for x in ls if x.get("proposal")]
    if proposed and today - _days(max(proposed)) < cfg.proposal_gap_days:
        return peace_offer, []
    player = camp.data["player"]
    refused = set()
    for r in camp.data.get("refusals", []):
        if r["from"] != tag or r.get("to", player) != player or today - _days(r["date"]) >= cfg.refusal_days:
            continue
        if any(e["date"] > r["date"] and tag in e["tags"] and player in e["tags"]
               and e.get("type") in chronicle.WORLD_TYPES for e in camp.data["events"]):
            continue                                       # things changed since: they may ask again
        refused.update(r["keys"])
    pol = policy.current(camp, tag)
    if not pol and policy.stale(camp, tag):
        return [], []                                      # its old aims no longer apply; wait for the new ones
    offers = pol["offers"] if pol else (actions.GIVES + actions.state_gives(rel) + actions.rights_gives(rel)
                                        + actions.dmz_gives(rel) + actions.war_gives(rel))
    asks = pol["asks"] if pol else (actions.ASKS + actions.state_asks(rel) + actions.rights_asks(rel)
                                    + actions.dmz_asks(rel) + actions.war_asks(rel))
    refused |= treaties.standing(camp, tag)                # a pledge already given isn't asked for again
    ok = lambda k: k not in refused and k != "give:peace" and actions.possible(rel, k)
    return peace_offer + [k for k in offers + actions.rights_after(rel) if ok(k)], [k for k in asks if ok(k)]


def write_first(camp: campaign.Campaign, player: str, sender: str, reason: str) -> None:
    """`sender` writes to the player on its own initiative (reason: an event text, or "" if unprompted)."""
    try:
        me, them = gamestate.country(player), gamestate.country(sender)
    except gamestate.StateError as e:
        print(f"  {sender} could not write: {e}")
        return
    key = campaign.date_key(them.date)
    pol = policy.current(camp, sender)
    rel = actions.relation(sender, player)
    offers, asks = _proposals(camp, sender, rel, key)
    actions.economy(them.name, me.name, rel)
    actions.union(me.name, agendas.wants(camp, them.tag, me.tag))
    steps = [k for k in (pol["breaks"] if pol else []) if actions.possible(rel, k)]
    if actions.possible(rel, "end:embargo") and "end:embargo" not in steps:
        steps.append("end:embargo")                       # lifting an embargo is always allowed
    # Only an urgent policy (3) makes a proposal or step the point of the letter; otherwise the country
    # mostly just writes, and proposes only if it fits.
    expected = (bool(pol) and pol["urgency"] >= 3 and not reason
                and bool(offers or asks or [s for s in steps if s != "end:embargo"]))
    task = (_initiative_task(them, me, reason) + "\n\n"
            + actions.letter_help(them.name, me.name, offers, asks, steps, expected)
            + ("\n\n" + actions.bound_note(them.name, me.name, rel.bound) if rel.bound else "")
            + ("\n\n" + _peace_note(them, me, camp, offers, reply=False) if rel.at_war else "")
            + ("\n\n" + treaties.help_text() if (offers or asks) and not rel.at_war else "")
            + ("\n\n" + ultimatums.help_text(them.name, me.name) if _may_threaten(camp, sender, rel) else ""))
    prompt = ([{"role": "system", "content": _system_prompt(them, me, camp, task)}]
              + _conversation(camp, them, me)
              + [{"role": "user", "content": f"(No new letter from {me.name}. It is {campaign.date_words(key)}. "
                                             f"Write your letter to them now, in {style.current().length}.)"}])
    try:
        raw = llm.chat(prompt, kind="letter", about=sender)
    except llm.LLMError as e:
        print(f"  {sender} could not write (LLM error): {e}")
        return
    raw, ultimatum = ultimatums.parse(raw)
    if (offers or asks) and (extra := _missing_commands(prompt, raw, sender, grants=False)):
        raw += "\n\n" + extra
    raw, terms = treaties.parse_terms(raw)
    letter, commands, proposals = actions.parse(raw)
    if (offers or asks) and (fixed := _unmatched(prompt, raw, sender, proposals, offers + asks)):
        raw, terms = treaties.parse_terms(fixed)
        letter, commands, proposals = actions.parse(raw)
    commands, renounced = treaties.take_renounce(commands)
    text = _clean_for_game(letter)
    camp.add_letter(sender, sender, text, key)
    camp.letters_with(sender)[-1]["initiated"] = True
    outcomes, events = actions.apply(sender, player, them.name, me.name, commands, allowed=steps)
    camp.add_events([{**e, "date": key, "when": campaign.date_words(key), "major": False} for e in events])
    if renounced:
        outcomes += treaties.renounce(camp, sender, player)
        for line in steering.sync(camp, sender):
            print(line)
    after = actions.relation(sender, player) if events else rel
    items = _attach_proposal(camp, sender, actions.package([p for p in proposals if actions.possible(after, p)],
                                                           offers + asks), key, terms)
    _dropped(sender, proposals, items)
    outcomes += _set_ultimatum(camp, sender, player, rel, ultimatum, items, key, them.name, me.name)
    outcomes += [n] if (n := steering.take_notice(camp, sender, them.name)) else []
    camp.save()
    print(f"\n{sender} writes to {player}{' about: ' + reason if reason else ''}"
          f"{' (proposes ' + ', '.join(items) + ')' if items else ''}\n{text}")
    if outcomes:
        print(f"  steps: {[o for o, _ in outcomes]}")
    shown = text + ("\n\n" + actions.outcome_text(outcomes) if outcomes else "")
    if items:
        shown += ("\n\n" + actions.proposal_line(items, them.name) + treaties.terms_line(terms, items, them.name)
                  + guarantees.proposal_line(camp, items, sender, them.name, terms) + _peace_line(items, me, them))
    _show_reply(player, sender, shown, offer=bool(items))


def resolve_offer(camp: campaign.Campaign, player: str, answer: int) -> str:
    """The player clicked Accept (1) or Decline (2) on a proposal package (offers and requests), or on an
    obligation under a treaty (an instalment, a war pledge). Returns why the other country should write (a
    broken treaty), else ''."""
    bridge.send(f"eval_effect {player} = {{ clear_variable = airp_offer_answer clear_variable = airp_offer }}")
    offer = camp.data.get("pending_offer")
    camp.data["pending_offer"] = None
    if not offer:
        return ""
    if offer.get("treaty_due"):
        return treaties.answer(camp, player, offer["treaty_due"], answer == 1)
    tag = offer["from"]
    items = (offer.get("keys") or ([offer["key"]] if offer.get("key") else None)   # older saves: one item,
             or [f"give:{offer['command']}"])                                     # or only the word
    names = (camp.snapshot or {}).get("countries", {})
    them_name = names.get(tag, {}).get("name", tag)
    me_name = names.get(player, {}).get("name", player)
    key = camp.data.get("last_date") or offer["date"]
    rel = actions.relation(tag, player)                     # also loads state names for territory items
    actions.state_gives(rel), actions.state_asks(rel), actions.rights_gives(rel), actions.rights_asks(rel)
    actions.dmz_gives(rel), actions.dmz_asks(rel)
    what = actions.package_text(items, them_name)          # "France offers ...; France asks for ..."
    if answer == 1:
        later = [k for k in items if treaties.deferred(offer.get("terms"), k)]   # paid later, not now
        now = [k for k in items if k not in later]
        outcomes, events = actions.apply(tag, player, them_name, me_name, [(k, 0) for k in now], allowed=now)
        camp.add_events([{**e, "date": key, "when": campaign.date_words(key), "major": False} for e in events])
        treaties.record(camp, tag, [e["type"] for e in events], offer.get("terms"), key, them_name, later=later)
        guarantees.record(camp, tag, [e["type"] for e in events], offer.get("terms"))
        for line in steering.sync(camp, tag):              # a commitment it made steers its AI
            print(line)
        missed = len(now) - len(events)
        text = (f"{me_name} accepted the proposal ({what})."
                + (f" {missed} part(s) could no longer be put in place." if missed else ""))
        print(f"accepted {tag}'s proposal: {[o for o, _ in outcomes] or 'nothing applied'}")
        if offer.get("ultimatum"):
            ultimatums.met_by_deal(camp, tag, me_name, them_name)
    else:
        text = f"{me_name} declined the proposal ({what})."
        camp.data.setdefault("refusals", []).append({"from": tag, "to": player, "keys": items, "date": key,
                                                     "basis": policy.basis(camp.snapshot, tag, player)})
        print(f"declined {tag}'s proposal: {items}")
        if offer.get("ultimatum"):
            ultimatums.rejected(camp, tag, me_name, them_name)
    camp.add_events([{"date": key, "when": campaign.date_words(key), "type": "offer_answer",
                      "tags": [tag, player], "text": text, "major": False}])
    camp.save()
    return ""


def _days(key: str) -> int:
    import datetime
    try:
        y, m, d = (int(x) for x in key.split(".")[:3])
        return datetime.date(y, m, d).toordinal()
    except ValueError:
        return 0


def active(snap: Optional[Dict], tag: str) -> bool:
    """Exists and hasn't capitulated. A capitulated country has no government left: it gets no policy and writes
    no letters (it comes back, if at all, after the peace)."""
    c = (snap or {}).get("countries", {}).get(tag)
    return bool(c) and not c.get("capitulated")


_CESSIONS = ("give:state", "ask:state", "cede:", "give:annex")


# Agreement events (chronicle.AGREEMENTS) a letter deal already told: the deal's own event types for each
_BY_LETTER = {"pact": ("give:pact",), "my_guarantee": ("ask:guarantee",), "their_guarantee": ("give:guarantee",),
              "their_access": ("give:access",), "my_access": ("ask:access",), "market": ("give:market", "ask:market"),
              "their_embargo": ("embargo",), "my_embargo": ()}


def _not_by_letter(camp: campaign.Campaign, player: str, snap: Dict, events: List[Dict]) -> List[Dict]:
    """Agreements that began or ended outside the letters (the game's diplomacy, a focus, an event) stay in the
    chronicle; one a letter deal made or ended is told by the deal already, and one that ended with a war between
    the two, or as a treaty's or letter guarantee's term ran out, is told by that."""
    since = campaign.shift(snap["date"], -30)
    recent = [x for x in camp.data["events"] if x["date"] >= since]
    at_war = lambda t: "|".join(sorted([player, t])) in snap.get("wars", [])
    out = []
    for e in events:
        kind = e.get("type", "")
        if not kind.startswith("agreement:"):
            out.append(e)
            continue
        tie, ended = kind.split(":")[1], kind.endswith(":end")
        other = next((t for t in e["tags"] if t != player), "")
        mine = [x for x in recent if other in x["tags"] and player in x["tags"]]
        if ended:
            deal = tie.replace("my_", "").replace("their_", "")
            told = at_war(other) or any(x.get("type", "") in (f"end:{deal}", "end:embargo" if "embargo" in tie else "",
                                                              "treaty:lapsed", "guarantee:expired", "war")
                                        for x in mine)
        else:
            told = any(x.get("type", "") in _BY_LETTER.get(tie, ()) for x in mine)
        if not told:
            out.append(e)
    return out


def _agreed(camp: campaign.Campaign, snap: Dict, events: List[Dict]) -> List[Dict]:
    """A land transfer that a letter deal agreed reads as such: third countries otherwise see only "X took Y from
    Z" (a seizure), and the state may carry another name under its new owner."""
    since = campaign.shift(snap["date"], -30)
    for e in events:
        if e["type"] != "territory":
            continue
        deals = [x for x in camp.data["events"] if x["date"] >= since and x.get("type", "").startswith(_CESSIONS)
                 and all(t in x["tags"] for t in e["tags"])]
        if deals:
            e["text"] += (f" This was the cession agreed by treaty on {deals[-1]['when']}: "
                          + " ".join(x["text"] for x in deals))
    return events


def _with_peace_terms(camp: campaign.Campaign, player: str, old: Dict, snap: Dict, events: List[Dict]) -> List[Dict]:
    """When a war with the player ended, replace the bare 'no longer at war' and 'took ...' lines between the two
    with one account of what the peace did (chronicle.peace_terms)."""
    out = list(events)
    for e in events:
        if e["type"] != "peace" or player not in e["tags"]:
            continue
        other = next((t for t in e["tags"] if t != player), "")
        if other not in snap["countries"]:
            continue                                   # annexed: "ceased to exist" says it
        asked = other if any(x.get("type") == "give:peace" and other in x["tags"] and player in x["tags"]
                             and x["date"] >= old["date"] for x in camp.data["events"]) else ""
        try:
            terms = chronicle.peace_terms(old, snap, player, other, asked)
        except gamestate.StateError as err:
            print(f"  peace terms with {other} unavailable: {err}")
            terms = None
        if terms:
            pair = {player, other}
            out = [x for x in out if x is not e and not (x["type"] == "territory" and set(x["tags"]) == pair)]
            out.append(terms)
    return out


def policy_countries(me: gamestate.CountryState, snap: Dict) -> List[str]:
    """Countries that keep a policy toward the player, most important first (refreshes go in this order):
    neighbours, overlord and subjects, enemies, faction members, then the major powers."""
    majors = [t for t, c in snap["countries"].items() if c["major"]]
    order = me.neighbors + [me.overlord or ""] + me.subjects + me.enemies + me.faction_members + majors
    return [t for t in dict.fromkeys(order) if t and t != me.tag and active(snap, t)]


def relevance(me: gamestate.CountryState, tag: str) -> int:
    """How much `tag` matters to the player when picking who writes in a quiet spell: neighbours most,
    then overlord, subjects, enemies and faction members, and a distant major power least.
    The weight is multiplied by (1 + policy urgency), so a distant power with something urgent still writes."""
    if tag in me.neighbors:
        return 6
    if tag == me.overlord or tag in me.subjects or tag in me.enemies or tag in me.faction_members:
        return 4
    return 1


class _Session:
    """The current campaign and its chronicle, kept in step with the running game."""

    def __init__(self) -> None:
        self.camp: Optional[campaign.Campaign] = None
        self.resumed: Dict[tuple, float] = {}   # (campaign id, country) -> when its waiting letter was resumed
        self.rep_shown: Tuple[str, float] = ("", 0.0)   # the reputation box's text as last set, and when
        self.treaties_shown: Tuple[str, float] = ("", 0.0)   # the same for the diplomacy view's treaty box
        self.steered = 0                         # campaign whose game AI strategies were last brought in line
        self.last_snapshot = 0.0
        self.pending: Dict[str, str] = {}      # country -> why it wants to write ("" = unprompted)
        self.pool = ThreadPoolExecutor(max_workers=POLICY_PARALLEL)  # policy refreshes, side by side
        self.policy_jobs: List[tuple] = []        # [(tag, future, date key, Relation, campaign id, basis, war baseline)]
        self.gm_pool = ThreadPoolExecutor(max_workers=1)   # the reputation game master's LLM calls
        self.gm_job: Optional[tuple] = None       # (kind, item, future, campaign id)
        self.story_pool = ThreadPoolExecutor(max_workers=1)   # story events are written here
        self.story_job: Optional[tuple] = None    # (future, campaign id, other country, its name, party names)
        self.news_job: Optional[tuple] = None     # (future, campaign id, date of the news it reports)
        self.news_draft: Optional[tuple] = None   # a written news event waiting to be shown: (draft, campaign id, date)
        self.story_draft: Optional[tuple] = None  # a written event waiting to be shown: (draft, campaign id, ...)
        self.war_pool = ThreadPoolExecutor(max_workers=1)    # ultimatum decisions
        self.ultimatum_job: Optional[tuple] = None   # (ultimatum, future, campaign id)
        self.ai_pool = ThreadPoolExecutor(max_workers=AI_PARALLEL)   # AI-to-AI exchanges (diplomacy.py), side by side
        self.ai_jobs: List[tuple] = []            # [(stage, queue item, future, campaign id, what the next stage needs)]
        self.plot_job: Optional[tuple] = None     # the plotlines being written: (future, campaign id)
        self.crisis: Optional[tuple] = None       # a crisis try running: (future, campaign id, pairs, countries)
        self.month: Optional[tuple] = None        # the storyteller's month being worked out: (storyteller.Month, campaign id)
        self.date_seen = ""                       # the game date at the last snapshot
        self.active_at = time.monotonic()         # when the date last moved or the player last wrote
        self.retry_at = 0.0                       # no background LLM call before this (after a failure)
        self.was_idle = False
        self.moving = 0                           # snapshots in a row whose date had moved (a loaded save moves it once)
        self.run_from = ""                         # the date the current run of unpaused play began

    def running(self) -> bool:
        """The game is unpaused: its date moved at the last two snapshots. New popups (news, story events, letters
        countries start) wait for this; a paused or just loaded game is the player's to look at first."""
        return self.moving >= 2 and _days(self.date_seen) - _days(self.run_from) >= RUN_DAYS

    def idle(self) -> bool:
        """The game is paused, or was unpaused or loaded fewer than RUN_DAYS days ago. Background AI work
        (policies, the game master, the storyteller, letters countries start) waits; the player's own letters are
        still answered."""
        idle = not self.running()
        if idle != self.was_idle:
            print("game paused: background AI work waits" if idle else "game running: background AI work resumes")
            self.was_idle = idle
        return idle

    def failed(self) -> None:
        """A background LLM call failed: back off before the next one."""
        self.retry_at = time.monotonic() + RETRY_SECONDS

    def campaign_for(self, player: str, cid: int) -> campaign.Campaign:
        if not cid:
            cid = campaign.new_id(player)
            print(f"new campaign {cid}")
        if not self.camp or self.camp.id != cid or self.camp.data["player"] != player:
            self.camp = campaign.Campaign(cid, player)
            self.pending.clear()          # letters queued for the previous game
            self.policy_jobs = []         # refreshes still running belong to the previous game: drop them
            self.gm_job = None
            self.story_job = self.story_draft = self.news_job = self.news_draft = None
            self.ultimatum_job = None
            self.ai_jobs = []
            self.month = None
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
        if self.camp:                                  # those it writes to, and the plot's
            chronicle.WATCH = set(self.camp.threads()) | set(plots.tags(self.camp))
        snap = chronicle.snapshot(player)
        if snap["date"] != self.date_seen:
            self.date_seen, self.active_at = snap["date"], now
            self.moving += 1
            if self.moving == 1:
                self.run_from = snap["date"]          # the date it started to move from
        else:
            self.moving = 0                            # the same date at two snapshots: paused
        camp = self.campaign_for(player, snap["campaign"])
        rewound = camp.at(snap["date"])
        if rewound:
            print(f"{campaign.date_words(snap['date'])}: an earlier save was loaded; later memories dropped")
            self.moving, self.run_from = 1, snap["date"]               # a new run of play begins here
        old = camp.snapshot
        if old is None or snap["date"] > old["date"]:
            if old is not None:
                events = _not_by_letter(camp, player, snap, _agreed(camp, snap, _with_peace_terms(
                    camp, player, old, snap, chronicle.diff(old, snap))))
                for e in events:
                    print(f"  [{e['when']}] {e['text']}")
                camp.add_events(events)
                for d in reputation.detect(camp, player, old, snap, events):
                    print(f"  reputation: deed to judge: {d['facts']}")
            camp.snapshot = snap
            camp.record_surrender(snap)
            camp.save()
            if old is not None:
                self.plan_letters(camp, player, snap, events, old)
                treaties.tick_ai(camp)          # treaties and ultimatums between two AI countries: news only
                ultimatums.tick_ai(camp)
                ticked = treaties.tick(camp, player) + ultimatums.tick(camp, player) + guarantees.tick(camp)
                # A term that ran out is routine: one notice tells the player, no letter per country (a country
                # that wants it renewed says so in its next letter, through its own policy).
                ran_out = {x["text"] for x in camp.data["events"][-60:] if x.get("type") in ("treaty:ended", "guarantee:expired")
                           and x["date"] == snap["date"] and player in x["tags"]}
                for tag, why in ticked:
                    if why in ran_out:
                        continue
                    if active(snap, tag) and settings().letters.incoming:
                        self.pending[tag] = (self.pending.get(tag, "") + " " + why).strip()
                if ran_out:
                    self._expiry_notice(snap, sorted(ran_out))
                if self.steered == camp.id:     # commitments that ended, or a war with their country, since
                    steering.sync_all(camp)
        if rewound or self.steered != camp.id:      # the game's AI follows the policies (steering.py)
            self.steered = camp.id
            steering.sync_all(camp)
        if not self.idle() and time.monotonic() >= self.retry_at:
            self.ultimatum_decision(camp, player)
            self.policies(camp, player, snap)
            self.game_master(camp, player, snap)
            self.story(camp, player)
            self.news(camp, player)
            self.ai_diplomacy(camp)
        return camp

    # ---- AI-to-AI diplomacy (diplomacy.py): two or three short calls per exchange, up to AI_PARALLEL side by side,
    #      in their own queue beside the letters to the player (NanoGPT takes parallel calls)
    def ai_diplomacy(self, camp: campaign.Campaign) -> None:
        cfg = settings().ai
        if cfg.mode not in ("dry", "on"):
            return
        st = diplomacy._state(camp)
        running = []
        for job in self.ai_jobs:
            stage, item, fut, cid, ctx = job
            if not fut.done():
                running.append(job)
            elif cid == camp.id:
                nxt = self._ai_step(camp, stage, item, fut, ctx)
                if nxt:
                    running.append(nxt)
        self.ai_jobs = running
        # ultimatums between AI countries past their deadline: their senders decide, beside the exchanges
        deciding = {job[1]["id"] for job in self.ai_jobs if job[0] == "ultimatum"}
        for u in ultimatums.next_ai_decisions(camp):
            if len(self.ai_jobs) >= AI_PARALLEL:
                break
            if u["id"] in deciding:
                continue
            try:
                msgs = diplomacy.ultimatum_messages(camp, u, gamestate.country(u["from"]), gamestate.country(u["to"]),
                                                    diplomacy.world())
            except gamestate.StateError as e:
                print(f"  [ai] {u['from']} could not decide on its ultimatum to {u['to']}: {e}")
                continue
            self.ai_jobs.append(("ultimatum", u, self.ai_pool.submit(llm.chat, msgs, None, 0.5, None, "ai:ultimatum",
                                                                     u["from"]), camp.id, None))
        if cfg.driver == "storyteller":
            self.storyteller(camp)
        elif diplomacy.due(camp):
            queued = diplomacy.draw(camp, cfg.exchanges_per_month, cfg.cooldown_days)
            print("  [ai] this month: " + (", ".join(f"{x[0]} -> {x[1]}" for x in queued) or "nobody"))
            camp.save()
        snap = camp.snapshot or {}
        w = None
        while st["queue"] and len(self.ai_jobs) < AI_PARALLEL:
            item = st["queue"].pop(0)
            a, b = item[0], item[1]
            if a not in snap.get("countries", {}) or b not in snap.get("countries", {}) \
                    or campaign.pair(a, b) in snap.get("wars", []):
                continue                                 # gone, or at war since the draw
            try:
                w = w or diplomacy.world()
                msgs, offers, asks, threat_ok = diplomacy.dispatch_prompt(camp, gamestate.country(a),
                                                                          gamestate.country(b), w,
                                                                          item[4] if len(item) > 4 else None)
            except gamestate.StateError as e:
                print(f"  [ai] {a} could not write to {b}: {e}")
                continue
            st["pairs"][campaign.pair(a, b)] = st["started"][a] = camp.data.get("last_date") or ""
            camp.save()
            self.ai_jobs.append(("dispatch", item, self.ai_pool.submit(llm.chat, msgs, None, None, None,
                                                                       "ai:dispatch", a), camp.id,
                                 (offers, asks, w, threat_ok)))

    def storyteller(self, camp: campaign.Campaign) -> None:
        """The storyteller's month (storyteller.py): the game queries are worked out a slice per tick so that this loop
        stays responsive, the LLM calls run in the AI pool. The incident it picks is queued as an exchange."""
        self._crisis_step(camp)
        self._plots_step(camp)
        if self.month and self.month[1] != camp.id:
            self.month = None
        if not self.month:
            if not storyteller.due(camp):
                return
            storyteller._state(camp)["tick"] = camp.snapshot["date"]       # one try a month, whatever comes of it
            camp.save()
            self.month = (storyteller.Month(camp, camp.snapshot, self.ai_pool), camp.id)
        try:
            res = self.month[0].step()
        except (gamestate.StateError, bridge.BridgeError, bridge.BridgeUnavailable) as e:
            print(f"  [ai] the storyteller's month failed: {e}")
            self.month = None
            return
        if res is None:
            return
        month, self.month = self.month[0], None
        if res["error"]:
            self.failed()
        item = storyteller.finish(camp, res)
        if item:
            print(f"  [ai] storyteller: {item[2]} {item[0]} -> {item[1]} ({item[3][0]})")
        if not self.plot_job and plots.due(camp):
            plots._store(camp)["tick"] = month.snap.get("date", "")
            self.plot_job = (self.ai_pool.submit(llm.chat, plots.messages(camp, month.snap), None, 0.7, 700,
                                                 "ai:plots", ""), camp.id)
        if not self.crisis and crises.due(camp):
            self._start_crisis(camp, month)
        camp.save()

    def _plots_step(self, camp: campaign.Campaign) -> None:
        """A finished plotlines call: keep what it wrote, and log it."""
        if not self.plot_job:
            return
        fut, cid = self.plot_job
        if cid != camp.id:
            self.plot_job = None
            return
        if not fut.done():
            return
        self.plot_job = None
        try:
            raw = fut.result()
        except llm.LLMError as e:
            print(f"  [ai] the plotlines call failed: {e}")
            self.failed()
            return
        now = camp.data.get("last_date") or ""
        changed = plots.keep(camp, plots.parse(raw), now)
        for x in changed:
            diplomacy.log([f"=== {campaign.date_words(now)}: PLOTLINE: {x['title']} ({x['stage']}; "
                           f"{', '.join(x['tags'])})", f"  goal: {x['goal']}", f"  so far: {x['so_far']}"])
            print(f"  [ai] plotline: {x['title']} ({x['stage']})")
        camp.save()

    def _start_crisis(self, camp: campaign.Campaign, month) -> None:
        """Consider a crisis (crises.py), from the month's world: one LLM call in the AI pool, read by _crisis_step."""
        snap = month.snap
        crises._store(camp)["tick"] = snap.get("date", "")
        pairs, tags = crises.candidates(camp, snap, month.w, month.scored)
        msgs = crises.messages(camp, snap, month.w, pairs, tags, month.now)
        if msgs:
            self.crisis = (self.ai_pool.submit(llm.chat, msgs, None, 0.9, 500, "ai:crisis", ""), camp.id, pairs, tags)

    def _expiry_notice(self, snap: Dict, texts: List[str]) -> None:
        """One popup for the agreements that ran out today, with the reminder that renewing takes a letter."""
        body = " ".join(texts)
        if len(body) > 420:
            body = body[:420].rsplit(" ", 1)[0] + " ..."
        body += " Write to the country if you want it renewed."
        try:
            game.show_news("Agreements Expired" if len(texts) > 1 else "Agreement Expired", body, "Noted.", "treaty")
        except Exception as err:                       # the game may be busy; the chronicle holds it anyway
            print(f"  expiry notice not shown: {err}")

    def _crisis_step(self, camp: campaign.Campaign) -> None:
        """A finished crisis call: open the crisis if it is allowed, and log it."""
        if not self.crisis:
            return
        fut, cid, pairs, tags = self.crisis
        if cid != camp.id:
            self.crisis = None
            return
        if not fut.done():
            return
        self.crisis = None
        try:
            raw = fut.result()
        except llm.LLMError as e:
            print(f"  [ai] the crisis call failed: {e}")
            self.failed()
            return
        pick = crises.parse(raw)
        cn = (camp.snapshot or {}).get("countries", {})
        ok, why = crises.check(camp, pick, pairs, tags, {t: x.get("name", t) for t, x in cn.items()})
        now = camp.data.get("last_date") or ""
        head = f"=== {campaign.date_words(now)}: CRISIS: "
        if not ok:
            if why != "none":
                diplomacy.log([head + f"dropped ({why})", f"  {raw[:300]}"])
            return
        crisis = crises.open_crisis(camp, pick, now)
        c = (camp.snapshot or {}).get("countries", {})
        who = " and ".join(c.get(t, {}).get("name", t) for t in crisis["tags"])
        diplomacy.log([head + f"{crisis['scope']} - {crisis['title']} ({who}; {crisis['severity']}, until "
                       f"{campaign.date_words(crisis['until'])})", f"  {crisis['text']}"])
        print(f"  [ai] crisis: {crisis['title']} ({who})")
        camp.save()

    def _ai_step(self, camp: campaign.Campaign, stage: str, item: List, fut, ctx) -> Optional[tuple]:
        """A finished call of an exchange: the dispatch leads to the answer (returned as the next job), the answer
        to the record; or an ultimatum's sender decided (`item` is then the ultimatum)."""
        if stage == "ultimatum":
            u = item
            try:
                raw = fut.result()
            except llm.LLMError as e:
                print(f"  [ai] {u['from']}'s decision on its ultimatum failed: {e}")
                self.failed()
                return None
            step, months, why = ultimatums.read_decision(raw)
            if step and u["status"] in ("open", "ready"):
                happened = ultimatums.decide(camp, u, step, months, why)
                c = (camp.snapshot or {}).get("countries", {})
                diplomacy.log([f"=== {campaign.date_words(camp.data.get('last_date') or '')}: "
                               f"{c.get(u['from'], {}).get('name', u['from'])}'s ultimatum to "
                               f"{c.get(u['to'], {}).get('name', u['to'])}: {step.upper()}",
                               happened or f"(still readied; reconsidering in {ultimatums.REVIEW_MONTHS} months: {why})"])
            return None
        a, b = item[0], item[1]
        try:
            raw = fut.result()
        except llm.LLMError as e:
            print(f"  [ai] {a} -> {b} failed: {e}")
            self.failed()
            return None
        if stage == "dispatch":
            offers, asks, w, threat_ok = ctx
            text, stance, items, threat = diplomacy.read_dispatch(raw, offers, asks, threat_ok)
            try:
                msgs, c_offers, c_asks = diplomacy.answer_prompt(camp, gamestate.country(b), gamestate.country(a), w,
                                                                 text, items, threat)
            except gamestate.StateError as e:
                print(f"  [ai] {b} could not answer {a}: {e}")
                return None
            return ("answer", item, self.ai_pool.submit(llm.chat, msgs, None, None, None, "ai:answer", b), camp.id,
                    (text, stance, items, w, c_offers, c_asks, threat))
        if stage == "answer":
            text, stance, items, w, c_offers, c_asks, threat = ctx
            reply, answer, b_stance, counter = diplomacy.read_answer(raw, items, c_offers, c_asks)
            if answer == "counter":                     # the proposer has the last word on the counter
                try:
                    msgs = diplomacy.final_prompt(camp, gamestate.country(a), gamestate.country(b), w, text, reply,
                                                  counter)
                except gamestate.StateError as e:
                    print(f"  [ai] {a} could not weigh {b}'s counter: {e}")
                    return None
                return ("final", item, self.ai_pool.submit(llm.chat, msgs, None, None, None, "ai:final", a), camp.id,
                        (text, stance, items, reply, b_stance, counter, threat))
            diplomacy.record(camp, a, b, text, stance, items, reply, answer, b_stance, item[2:4], threat=threat)
            camp.save()
            return None
        text, stance, items, reply, b_stance, counter, threat = ctx
        final, final_answer, final_stance, _ = diplomacy.read_answer(raw, counter)
        diplomacy.record(camp, a, b, text, stance, items, reply, "counter", b_stance, item[2:4], counter=counter,
                         final=final, final_answer=final_answer, final_stance=final_stance, threat=threat)
        camp.save()
        return None

    # ---- ultimatums past their deadline: the country decides between war and backing down (in the background)
    def ultimatum_decision(self, camp: campaign.Campaign, player: str) -> None:
        if self.ultimatum_job:
            u, fut, cid = self.ultimatum_job
            if not fut.done():
                return
            self.ultimatum_job = None
            if cid != camp.id or u not in ultimatums._all(camp) or u["status"] not in ("open", "ready"):
                return
            try:
                text = fut.result()
            except llm.LLMError as e:
                print(f"  ultimatum decision failed: {e}")
                self.failed()
                return
            step, months, why = ultimatums.read_decision(text)
            if not step:
                print(f"  ultimatum decision not readable, asking again later: {text[:300]}")
                self.failed()
                return
            print(f"  ultimatum {u['id']} from {u['from']}: {step.upper()} {months or ''} {why}")
            happened = ultimatums.decide(camp, u, step, months, why)
            if happened and active(camp.snapshot, u["from"]) and settings().letters.incoming:
                self.pending[u["from"]] = (self.pending.get(u["from"], "") + " " + happened).strip()
            return
        u = ultimatums.next_decision(camp)
        if not u:
            return
        try:
            me, them = gamestate.country(player), gamestate.country(u["from"])
        except gamestate.StateError:
            return
        msgs = ultimatums.decision_messages(camp, u, _context(them, me, camp), them.name, me.name,
                                            ultimatums.balance(camp, them.tag, player, them.name, me.name)[0])
        self.ultimatum_job = (u, self.war_pool.submit(llm.chat, msgs, None, 0.5, None, "ultimatum", them.tag),
                              camp.id)

    # ---- story events: the player's country reacts to its letter deals and deeds (written in the background)
    def story(self, camp: campaign.Campaign, player: str) -> None:
        if self.story_job:
            fut, cid, other, other_name, parties = self.story_job
            if not fut.done():
                return
            self.story_job = None
            if cid != camp.id:
                return
            try:
                text = fut.result()
            except llm.LLMError as e:
                print(f"  story event failed: {e}")
                self.failed()
                return
            draft = story.parse(text, list(parties))
            if not draft:
                print(f"  story event: answer not readable, skipped:\n{text[:500]}")
                return
            self.story_draft = (draft, cid, other, other_name, parties)
            return
        if self.story_draft:
            return
        moment = story.due(camp, player)
        if not moment:
            return
        try:
            me = gamestate.country(player)
            them = gamestate.country(moment.other) if active(camp.snapshot, moment.other) else None
        except gamestate.StateError:
            return
        story._story(camp)["last_moment"] = moment.date      # this moment is told, whatever comes of it
        camp.save()
        print(f"  story event: writing one about: {moment.text[:200]}")
        msgs = story.messages(camp, me, them, moment, _relation(them, me) if them else "")
        parties = {p.ideology: p.long_name or p.name for p in me.parties}
        self.story_job = (self.story_pool.submit(llm.chat, msgs, None, None, None, "story", moment.other), camp.id,
                          them.tag if them else "", them.name if them else "", parties)

    def news(self, camp: campaign.Campaign, player: str) -> None:
        """News events (news.py): a report of what the storyteller's incidents came to, written in the story pool."""
        if self.news_job:
            fut, cid, told = self.news_job
            if not fut.done():
                return
            self.news_job = None
            if cid != camp.id:
                return
            try:
                text = fut.result()
            except llm.LLMError as e:
                print(f"  news event failed: {e}")
                self.failed()
                return
            draft = news.parse(text, _clean_for_game)
            if not draft:
                print(f"  news event: answer not readable, skipped: {text[:300]}")
                return
            self.news_draft = (draft, cid, told)
            return
        if self.news_draft or self.story_job:
            return
        ev = news.due(camp)
        if not ev:
            return
        try:
            me = gamestate.country(player)
        except gamestate.StateError:
            return
        news._news(camp)["told"] = ev["date"]               # this news is reported, whatever comes of it
        camp.save()
        print(f"  news event: writing one about: {ev['text'][:200]}")
        msgs = news.messages(camp, ev, me.name, [p.ideology for p in me.parties])
        self.news_job = (self.story_pool.submit(llm.chat, msgs, None, 1.0, 500, "news", ev["tags"][0]), camp.id,
                         ev["date"])

    def news_tick(self, player: str, block: str) -> None:
        """Every poll: open a written news event when no letter window or story event is up."""
        camp = self.camp
        if not camp or not self.news_draft or not self.running() or story._story(camp)["pending"] \
                or re.search(r"\bairp_(reply_from|awaiting_from|outbox_to)=", block):
            return
        draft, cid, told = self.news_draft
        self.news_draft = None
        if cid != camp.id:
            return
        try:
            news.show(camp, draft, told)
            print(f"  news event shown: {draft.title} [{draft.picture}]\n    {draft.body}\n    > {draft.button}")
        except (game.BridgeError, ValueError) as e:
            print(f"  could not show the news event: {e}")

    def story_tick(self, player: str, block: str) -> None:
        """Every poll: apply the pick of an open story event, or open a written one when no letter window is up."""
        camp = self.camp
        if not camp:
            return
        pending = story._story(camp)["pending"]
        if pending:
            n = story.read_pick(block, pending["slot"])
            if n:
                me_name = (camp.snapshot or {}).get("countries", {}).get(player, {}).get("name", player)
                story.resolve(camp, player, n, me_name)
            return
        if not self.story_draft or not self.running() or re.search(r"\bairp_(reply_from|awaiting_from|outbox_to)=", block):
            return
        draft, cid, other, other_name, parties = self.story_draft
        self.story_draft = None
        if cid != camp.id:
            return
        try:
            story.show(camp, draft, player, other, other_name, parties, _clean_for_game)
            print(f"  story event shown: {draft.title}")
        except (game.BridgeError, ValueError) as e:
            print(f"  could not show the story event: {e}")

    # ---- the reputation game master: reads promises in the player's letters and judges deeds, in the background
    def game_master(self, camp: campaign.Campaign, player: str, snap: Dict) -> None:
        names = snap.get("countries", {})
        name = lambda t: names.get(t, {}).get("name", t)
        if self.gm_job:
            kind, item, fut, cid = self.gm_job
            if not fut.done():
                return
            self.gm_job = None
            if cid != camp.id:
                return
            try:
                text = fut.result()
            except llm.LLMError as e:
                print(f"  game master failed: {e}")
                self.failed()
                return
            if kind == "promises":
                found = reputation.read_promises(text)
                for x in camp.letters_with(item["to"]):
                    if x["date"] == item["date"] and x["from"] == player and x["text"] == item["text"]:
                        x["promises_read"] = True
                reputation._rep(camp)["promises"].extend(
                    {"date": item["date"], "when": item["when"], "to": item["to"], "text": p} for p in found)
                threats = reputation.read_threats(text)
                reputation.add_threats(camp, item, threats, name(item["to"]))
                if found:
                    print(f"  game master: promises to {name(item['to'])}: {'; '.join(found)}")
                if threats:
                    print(f"  game master: threats to {name(item['to'])}: {'; '.join(w for _, w in threats)}")
            else:
                item["weight"], item["verdict"] = reputation.read_verdict(text)
                print(f"  game master: {item['weight']:+d} {item['verdict']}")
                if item["weight"] and item.get("by", player) == player and not item.get("opinion_set"):
                    item["opinion_set"] = True
                    actions.reputation_opinion(player, item.get("tags", []), item["weight"])
            camp.save()
            return
        job = reputation.next_job(camp, player)
        if not job:
            return
        kind, item = job
        if kind == "promises":
            msgs = reputation.promise_messages(name(player), name(item["to"]), item)
        else:
            try:                                   # the setting text (vanilla or Kaiserreich) is told by the ideologies
                ideologies = [p.ideology for p in gamestate.country(player).parties]
            except gamestate.StateError:
                ideologies = []
            msgs = reputation.judge_messages(camp, player, item, world.text(ideologies))
        about = item.get("to") or next((t for t in item.get("tags", []) if t != player), "")
        self.gm_job = (kind, item, self.gm_pool.submit(llm.chat, msgs, None, 0.3, None, kind, about), camp.id)

    # ---- neighbours' policies toward the player, refreshed one at a time in the background
    def policies(self, camp: campaign.Campaign, player: str, snap: Dict) -> None:
        running = []
        for job in self.policy_jobs:
            if not job[1].done():
                running.append(job)
            else:
                self._policy_done(camp, player, *job)
        self.policy_jobs = running
        if len(self.policy_jobs) >= POLICY_PARALLEL or time.monotonic() < self.retry_at:
            return
        try:
            me = gamestate.country(player)
        except gamestate.StateError:
            return
        # Most needed first (none or stale, then something happened, then just old); ties in importance order.
        busy = {job[0] for job in self.policy_jobs}
        close = set(me.neighbors + me.enemies + me.faction_members + me.subjects + [me.overlord or ""])
        ranked = [(policy.refresh_priority(camp, tag, player, snap["date"], tag in close), tag)
                  for tag in policy_countries(me, snap) if tag not in busy]
        ranked = sorted([r for r in ranked if r[0]], key=lambda r: -r[0])
        for _, tag in ranked:
            if len(self.policy_jobs) >= POLICY_PARALLEL:
                return
            try:
                them = gamestate.country(tag)
            except gamestate.StateError:
                continue
            rel = actions.relation(tag, player)
            rel.pledged = frozenset(treaties.standing(camp, tag))   # a pledge given isn't wanted again
            baseline, war_part = None, ""
            if player in them.enemies:
                baseline, reasons = war.pressure(them, me, camp)
                war_part = policy.war_instructions(them.name, me.name, baseline, reasons, war.separate_peace_note(them))
            messages = [{"role": "system", "content":
                         f"{_context(them, me, camp)}\n\nYOUR CORRESPONDENCE WITH {me.name.upper()}:\n"
                         f"{_transcript(camp, them, me)}\n\n{policy.instructions(them.name, me.name, rel, war_part)}"},
                        {"role": "user", "content": f"State {them.name}'s policy toward {me.name} now."}]
            self.policy_jobs.append((tag, self.pool.submit(llm.chat, messages, None, 0.7, None, "policy", tag),
                                     snap["date"], rel, camp.id, policy.basis(snap, tag, player), baseline))

    def _policy_done(self, camp: campaign.Campaign, player: str, tag: str, fut, key: str, rel, cid: int, basis: str,
                     baseline) -> None:
        """A finished refresh: keep the policy and steer the game's AI by it."""
        if cid != camp.id or key > (camp.data.get("last_date") or key):
            return                        # started for another game, or an earlier save was loaded since
        try:
            text = fut.result()
        except llm.LLMError as e:
            print(f"  policy for {tag} failed: {e}")
            self.failed()
            return
        p = policy.parse(text, rel, baseline)
        p.update(date=key, when=campaign.date_words(key), basis=basis)
        camp.set_policy(tag, p)
        camp.save()
        words = lambda keys: ", ".join(actions.word(k) for k in keys) or "none"
        print(f"  policy {tag} -> {player}: {p['stance']}, urgency {p['urgency']}, offers {words(p['offers'])}, "
              f"asks {words(p['asks'])}, breaks {words(p['breaks'])}: {p['policy']}")
        if p.get("war"):
            print(f"    war stance {p['war']} (situation: {p['war_baseline']}): {p.get('war_why', '')} "
                  f"Terms: {p.get('terms', '')}")
        for line in steering.sync(camp, tag):
            print(line)

    # ---- who writes to the player on their own
    def _cooling(self, camp: campaign.Campaign, tag: str, today: int) -> bool:
        own = [x for x in camp.letters_with(tag) if x.get("initiated")]
        return bool(own) and today - _days(own[-1]["date"]) < settings().letters.cooldown_days

    def plan_letters(self, camp: campaign.Campaign, player: str, snap: Dict, events: List[Dict],
                     old: Optional[Dict] = None) -> None:
        cfg = settings().letters
        if not cfg.incoming:
            return
        today = _days(snap["date"])
        # Countries an event concerns may write about it, but at most EVENT_LETTERS per batch: a peace that carves
        # out five new puppets shouldn't bring five thank-you letters. Major powers and countries that existed
        # before come first; new countries (just liberated or puppeted) last.
        reasons: Dict[str, str] = {}
        for e in events:
            # market access alone is no reason to write: the game opens it with half the world at once
            if player not in e["tags"] or e.get("type", "").startswith("agreement:market"):
                continue
            for other in e["tags"]:
                if other != player and active(snap, other) and not self._cooling(camp, other, today):
                    reasons[other] = (reasons.get(other, "") + " " + e["text"]).strip()
        before = (old or {}).get("countries", {})
        rank = lambda t: (snap["countries"][t].get("major", False), t in before, len(reasons[t]))
        # The cap holds over EVENT_DAYS, not per batch: a war's allies and subjects join over several days, each
        # in a batch of its own, and would otherwise all write.
        recent = [x for ls in camp.threads().values() for x in ls
                  if x.get("initiated") and today - _days(x["date"]) < EVENT_DAYS]
        room = max(0, EVENT_LETTERS - len(recent))
        for tag in sorted(reasons, key=rank, reverse=True)[:room]:
            self.pending[tag] = (self.pending.get(tag, "") + " " + reasons[tag]).strip()
        if self.pending:
            return
        # Letters countries start without an event (policy or quiet spell) keep letter_gap_days apart.
        started = [x["date"] for ls in camp.threads().values() for x in ls if x.get("initiated")]
        if started and today - _days(max(started)) < cfg.letter_gap_days:
            return
        # A country writes soon, once per policy, when its policy is urgent (3) or it has something to
        # propose or do and some urgency (2): that letter is where it happens.
        for tag in list(camp.policies_toward()):
            p = policy.current(camp, tag)
            own = [x for x in camp.letters_with(tag) if x.get("initiated")]
            if (policy.wants_to_write(p) and active(snap, tag) and not self._cooling(camp, tag, today)
                    and (not own or own[-1]["date"] < p["date"])):
                self.pending[tag] = ""
                return
        letters = [x for ls in camp.threads().values() for x in ls]
        camp.data.setdefault("started", snap["date"])
        last = max([x["date"] for x in letters if x["from"] != player] or [camp.data["started"]])
        if today - _days(last) < cfg.quiet_days:
            return
        try:
            me = gamestate.country(player)
        except gamestate.StateError:
            return
        candidates = [t for t in policy_countries(me, snap) if not self._cooling(camp, t, today)]
        if candidates:
            pols = camp.policies_toward()
            weights = [relevance(me, t) * (1 + pols.get(t, {}).get("urgency", 0)) for t in candidates]
            self.pending[random.choices(candidates, weights)[0]] = ""

    def treaty_prompt(self, player: str, block: str) -> bool:
        """Ask the player about an obligation that fell due under a treaty (an instalment, a war pledge), in
        the reply window with Accept/Decline, when no window is open and no proposal waits. True if one was shown."""
        camp = self.camp
        if not camp or camp.data.get("pending_offer") or not camp.data.get("treaty_due"):
            return False
        if re.search(r"\bairp_(reply_from|awaiting_from|outbox_to)=", block):
            return False
        found = treaties.next_prompt(camp, player)
        if not found:
            camp.save()
            return False
        due, tag, text = found
        camp.data["pending_offer"] = {"from": tag, "keys": [due["key"]], "date": camp.data.get("last_date") or "",
                                      "treaty_due": due}
        camp.save()
        print(f"  treaty: asking the player about {due['kind']} {due['key']} owed to {tag}")
        _show_reply(player, tag, _clean_for_game(text), offer=True)
        return True

    def deliver(self, player: str, block: str) -> None:
        """Let one waiting country write, if the player isn't reading or awaiting a letter."""
        if not self.pending or not self.camp or self.idle() or not self.running():
            return
        if re.search(r"\bairp_(reply_from|awaiting_from|outbox_to)=", block):
            return
        tag = next(iter(self.pending))
        reason = self.pending.pop(tag)
        if not active(self.camp.snapshot, tag):
            return                                     # capitulated (or gone) since it was queued
        write_first(self.camp, player, tag, reason)


def _wait_for_game() -> str:
    """The player's tag, once HOI4 is running with a game loaded (main menu or loading screen: wait)."""
    said = ""
    while True:
        try:
            return bridge.player_tag()
        except bridge.BridgeUnavailable:
            msg = "waiting for HOI4 / the bridge..."
        except RuntimeError:
            msg = "waiting for a game to be loaded..."
        if msg != said:
            print(msg)
            said = msg
        time.sleep(3)


def story_now() -> int:
    """`python -m hoi4_rp story`: write a story event about the latest telling moment now (ignoring the gap and
    how fresh it is), show it, wait for the pick and apply it. For trying events out."""
    player = _wait_for_game()
    camp = campaign.Campaign(campaign.read_id(player), player)
    found = sorted([m for m in story.moments(camp, player) if m.weight >= 2], key=lambda m: m.date)
    if not found:
        print("nothing in this campaign to write a story event about yet (letter deals or judged deeds)")
        return 1
    moment = found[-1]
    me = gamestate.country(player)
    them = gamestate.country(moment.other) if active(camp.snapshot, moment.other) else None
    parties = {p.ideology: p.long_name or p.name for p in me.parties}
    print(f"writing a story event about ({moment.when}): {moment.text[:300]}")
    raw = llm.chat(story.messages(camp, me, them, moment, _relation(them, me) if them else ""),
                   kind="story", about=moment.other)
    draft = story.parse(raw, list(parties))
    if not draft:
        print(f"answer not readable:\n{raw}")
        return 1
    story.show(camp, draft, player, them.tag if them else "", them.name if them else "", parties, _clean_for_game)
    print(f"\"{draft.title}\" is open in the game; pick an option...")
    n = game.wait_for_pick(story._story(camp)["pending"]["slot"])
    story.resolve(camp, player, n, me.name)
    return 0


class _Tee:
    """Writes to the console and to the day's run log, each line stamped with the time."""

    def __init__(self, console, path: str):
        self.console, self.path, self.fresh = console, path, True

    def write(self, text: str) -> int:
        self.console.write(text)
        try:
            with open(self.path, "a", encoding="utf-8") as f:
                for part in text.splitlines(keepends=True):
                    if self.fresh:
                        f.write(time.strftime("[%H:%M:%S] "))
                    f.write(part)
                    self.fresh = part.endswith("\n")
        except OSError:
            pass
        return len(text)

    def flush(self) -> None:
        self.console.flush()

    def __getattr__(self, name):
        return getattr(self.console, name)


def _log_to_file() -> str:
    """Copy everything `run` prints to data/logs/run-YYYY-MM-DD.log (real date)."""
    os.makedirs(llm.LOG_DIR, exist_ok=True)
    path = os.path.join(llm.LOG_DIR, time.strftime("run-%Y-%m-%d.log"))
    sys.stdout, sys.stderr = _Tee(sys.stdout, path), _Tee(sys.stderr, path)
    return path


def run() -> int:
    path = _log_to_file()
    print(f"watching for letters and world events (model {llm.model()}). Ctrl+C to stop. Log: {path}")
    session = _Session()
    # Leader biographies are indexed from the game and mod files (several seconds for big mods): do it now.
    threading.Thread(target=leaders.warm, daemon=True).start()
    while True:
        try:
            player = bridge.player_tag()
            session.chronicle(player)
            if session.camp:
                session.rep_shown = show_reputation(session.camp, player, session.rep_shown)
                session.treaties_shown = show_treaties(session.camp, player, session.treaties_shown)
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
                    bridge.send(f"eval_effect {player} = {{ clear_variable = airp_awaiting_from add_to_variable = {{ airp_awaiting_refresh = 1 }} }}")
                elif to and text.strip():
                    session.active_at = time.monotonic()      # the player is here
                    handle_letter(session.chronicle(player, force=True), player, to, text)
                else:
                    print("empty letter; nothing to answer")
                    bridge.send(f"eval_effect {player} = {{ clear_variable = airp_awaiting_from add_to_variable = {{ airp_awaiting_refresh = 1 }} }}")
            elif (waiting := read_awaiting(block)) and session.camp:
                # Letters are answered here, in this loop, so a wait with no letter in hand came from a loaded save
                mark = (session.camp.id, waiting)
                if time.monotonic() - session.resumed.get(mark, -RESUME_SECONDS) >= RESUME_SECONDS:
                    session.resumed[mark] = time.monotonic()
                    resume_letter(session.chronicle(player, force=True), player, waiting)
            else:
                answer = re.search(r"\bairp_offer_answer=(\d)", block)
                if answer and session.camp:
                    offer = session.camp.data.get("pending_offer") or {}
                    broken = resolve_offer(session.camp, player, int(answer.group(1)))
                    if broken and settings().letters.incoming and active(session.camp.snapshot, offer.get("from")):
                        session.pending[offer["from"]] = broken
                elif not session.treaty_prompt(player, block):
                    session.story_tick(player, block)
                    session.news_tick(player, block)
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
