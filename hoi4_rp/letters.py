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

import random
import threading
import re
import time
import unicodedata
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Dict, List, Optional

from . import actions, bridge, campaign, chronicle, gamestate, leaders, llm, peace, policy, style, subjects, war, world
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
    """Make LLM text safe for HOI4 localisation: no markup characters, only Latin-1 letters. If the model wrote
    several drafts separated by a rule line (---), only the first is kept."""
    text = re.split(r"\n\s*(?:-{3,}|\*{3,}|_{3,})\s*\n", text.strip())[0]
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
    lines.append("Your government knows all this. Whatever your tone, don't pretend the war stands otherwise.")
    return "THE WAR BETWEEN YOU:\n" + "\n".join(lines)


def _memory(camp: campaign.Campaign, them: gamestate.CountryState, me: gamestate.CountryState) -> str:
    events = [e for e in camp.data["events"]
              if them.tag in e["tags"] or me.tag in e["tags"] or e.get("major")]
    if not events:
        return "Nothing notable recorded yet."
    return "\n".join(f"- {e['when']}: {e['text']}" for e in events)


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
            f"{them.name}'s interests, ideology and situation. {why} {st.manner}")


def _context(them: gamestate.CountryState, me: gamestate.CountryState, camp: campaign.Campaign) -> str:
    """Everything `them` knows when dealing with `me`."""
    pol = policy.describe(policy.current(camp, them.tag), them.name, me.name)
    return (
        f"You are the government of {them.name} in the grand strategy game Hearts of Iron IV. "
        f"The date is {them.date.strip()}.\n\n"
        f"{world.text([p.ideology for p in me.parties + them.parties])}\n\n"
        f"YOUR COUNTRY:\n{gamestate.describe(them)}\n\n"
        f"THE COUNTRY YOU CORRESPOND WITH:\n{gamestate.describe(me)}\n\n"
        f"BETWEEN YOU: {_relation(them, me)}\n\n"
        + (f"{_war(camp, them, me)}\n\n" if me.tag in them.enemies else "")
        + (f"{pol}\n\n" if pol else "")
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
        f"{style.current().rules.format(name=them.name, other=me.name)}\n\n{consequences}"
    )


def _show_reply(player: str, sender: str, text: str, offer: bool = False) -> None:
    body = text.replace("\r", "").replace("\n", "\\n")
    size = reply_size_for(text)       # which of the reply window's heights fits the letter
    offer_var = "set_variable = { airp_offer = 1 }" if offer else "clear_variable = airp_offer"
    replies = bridge.send(
        f"bridge.setloc airp_reply_body {body}",
        f"eval_effect {player} = {{ clear_variable = airp_awaiting_from {offer_var} "
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
    if them.capitulated:                               # no government left to answer: no LLM call
        print(f"  {them.name} has capitulated; no reply")
        _show_reply(player, to, f"(No answer comes. {them.name} has capitulated, and no government remains "
                                f"to receive your letter.)")
        return
    key = campaign.date_key(them.date)
    camp.at(key)
    camp.add_letter(to, player, text, key)
    camp.save()
    rel = actions.relation(to, player)
    offers, asks = _proposals(camp, to, rel, key, reply=True)
    help_text = (actions.reply_help(them.name, me.name, rel) + "\n\n"
                 + actions.proposal_help(them.name, me.name, offers, asks, expected=False)
                 + ("\n\n" + _peace_note(them, me, camp, offers, reply=True) if rel.at_war else ""))
    try:
        prompt = ([{"role": "system", "content": _system_prompt(them, me, camp, actions_help=help_text)}]
                  + _conversation(camp, them, me))
        raw = llm.chat(prompt)
    except llm.LLMError as e:
        print("  LLM error:", e)
        _show_reply(player, to, _clean_for_game(f"(The courier returned without an answer: {e})"[:300]))
        return
    letter, commands, proposals = actions.parse(raw)
    reply = _clean_for_game(letter)
    camp.add_letter(to, to, reply, key)
    outcomes, events = actions.apply(to, player, them.name, me.name, commands,
                                     allowed=["opinion"] + actions.GIVES + actions.UNILATERAL + actions.state_gives(rel))
    for e in events:
        camp.add_events([{**e, "date": key, "when": campaign.date_words(key), "major": False}])
    # Proposal items are only kept if still possible after what was just applied.
    after = actions.relation(to, player) if events else rel
    items = _attach_proposal(camp, to, actions.package([p for p in proposals if actions.possible(after, p)],
                                                       offers + asks), key)
    camp.save()
    print(f"{to} replies{' (proposes ' + ', '.join(items) + ')' if items else ''}:\n{reply}")
    if commands:
        print(f"  commands: {commands} -> {[o for o, _ in outcomes] or 'nothing applied'}")
    shown = reply + ("\n\n" + actions.outcome_text(outcomes) if outcomes else "")
    if items:
        shown += "\n\n" + actions.proposal_line(items, them.name) + _peace_line(items, me, them)
    _show_reply(player, to, shown, offer=bool(items))


def _peace_line(items: List[str], me: gamestate.CountryState, them: gamestate.CountryState) -> str:
    """Under a peace offer: what the peace conference would bring."""
    return "\n§Y" + peace.estimate(me, them)[1] + "§!" if "give:peace" in items else ""


def _attach_proposal(camp: campaign.Campaign, tag: str, items: List[str], key: str) -> List[str]:
    """Remember the proposal package on the letter just added and as the pending Accept/Decline."""
    if items:
        camp.letters_with(tag)[-1]["proposal"] = items
        camp.data["pending_offer"] = {"from": tag, "keys": items, "date": key}
    return items


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
               f"{me.name} would receive about {pct}% of the war score, to take from you" if margin > 0 else
               f"you would receive about {pct}% of the war score, to take from {me.name}")
    pol = policy.current(camp, them.tag) or {}
    when = (f"if {me.name} asked for peace or terms and your war stance accepts it, or if your stance is to seek "
            f"peace" if reply else "your war stance is to seek peace, so this is the moment to offer it")
    return (f"PEACE. You may offer {me.name} peace with [[offer peace]], on its own (no other items): "
            f"{when}. If {me.name} accepts, a peace conference settles the terms by how the war stands; right now "
            f"that would be {outcome}. {_TERMS_NOTE} Your war stance: {pol.get('war', 'unknown')}. Never offer "
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
    proposed = [x["date"] for ls in camp.data["letters"].values() for x in ls if x.get("proposal")]
    if proposed and today - _days(max(proposed)) < cfg.proposal_gap_days:
        return peace_offer, []
    player = camp.data["player"]
    refused = set()
    for r in camp.data.get("refusals", []):
        if r["from"] != tag or today - _days(r["date"]) >= cfg.refusal_days:
            continue
        if any(e["date"] > r["date"] and tag in e["tags"] and player in e["tags"]
               and e.get("type") in chronicle.WORLD_TYPES for e in camp.data["events"]):
            continue                                       # things changed since: they may ask again
        refused.update(r["keys"])
    pol = policy.current(camp, tag)
    if not pol and policy.stale(camp, tag):
        return [], []                                      # its old aims no longer apply; wait for the new ones
    offers = pol["offers"] if pol else actions.GIVES + actions.state_gives(rel)
    asks = pol["asks"] if pol else actions.ASKS + actions.state_asks(rel)
    ok = lambda k: k not in refused and k != "give:peace" and actions.possible(rel, k)
    return peace_offer + [k for k in offers if ok(k)], [k for k in asks if ok(k)]


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
    steps = [k for k in (pol["breaks"] if pol else []) if actions.possible(rel, k)]
    if actions.possible(rel, "end:embargo") and "end:embargo" not in steps:
        steps.append("end:embargo")                       # lifting an embargo is always allowed
    # Only an urgent policy (3) makes a proposal or step the point of the letter; otherwise the country
    # mostly just writes, and proposes only if it fits.
    expected = (bool(pol) and pol["urgency"] >= 3 and not reason
                and bool(offers or asks or [s for s in steps if s != "end:embargo"]))
    task = (_initiative_task(them, me, reason) + "\n\n"
            + actions.letter_help(them.name, me.name, offers, asks, steps, expected)
            + ("\n\n" + _peace_note(them, me, camp, offers, reply=False) if rel.at_war else ""))
    prompt = ([{"role": "system", "content": _system_prompt(them, me, camp, task)}]
              + _conversation(camp, them, me)
              + [{"role": "user", "content": f"(No new letter from {me.name}. It is {campaign.date_words(key)}. "
                                             f"Write your letter to them now.)"}])
    try:
        raw = llm.chat(prompt)
    except llm.LLMError as e:
        print(f"  {sender} could not write (LLM error): {e}")
        return
    letter, commands, proposals = actions.parse(raw)
    text = _clean_for_game(letter)
    camp.add_letter(sender, sender, text, key)
    camp.letters_with(sender)[-1]["initiated"] = True
    outcomes, events = actions.apply(sender, player, them.name, me.name, commands, allowed=steps)
    camp.add_events([{**e, "date": key, "when": campaign.date_words(key), "major": False} for e in events])
    after = actions.relation(sender, player) if events else rel
    items = _attach_proposal(camp, sender, actions.package([p for p in proposals if actions.possible(after, p)],
                                                           offers + asks), key)
    camp.save()
    print(f"\n{sender} writes to {player}{' about: ' + reason if reason else ''}"
          f"{' (proposes ' + ', '.join(items) + ')' if items else ''}\n{text}")
    if outcomes:
        print(f"  steps: {[o for o, _ in outcomes]}")
    shown = text + ("\n\n" + actions.outcome_text(outcomes) if outcomes else "")
    if items:
        shown += "\n\n" + actions.proposal_line(items, them.name) + _peace_line(items, me, them)
    _show_reply(player, sender, shown, offer=bool(items))


def resolve_offer(camp: campaign.Campaign, player: str, answer: int) -> None:
    """The player clicked Accept (1) or Decline (2) on a proposal package (offers and requests)."""
    bridge.send(f"eval_effect {player} = {{ clear_variable = airp_offer_answer clear_variable = airp_offer }}")
    offer = camp.data.get("pending_offer")
    camp.data["pending_offer"] = None
    if not offer:
        return
    tag = offer["from"]
    items = (offer.get("keys") or ([offer["key"]] if offer.get("key") else None)   # older saves: one item,
             or [f"give:{offer['command']}"])                                     # or only the word
    names = (camp.snapshot or {}).get("countries", {})
    them_name = names.get(tag, {}).get("name", tag)
    me_name = names.get(player, {}).get("name", player)
    key = camp.data.get("last_date") or offer["date"]
    rel = actions.relation(tag, player)                     # also loads state names for territory items
    actions.state_gives(rel), actions.state_asks(rel)
    what = actions.package_text(items, them_name)          # "France offers ...; France asks for ..."
    if answer == 1:
        outcomes, events = actions.apply(tag, player, them_name, me_name, [(k, 0) for k in items], allowed=items)
        camp.add_events([{**e, "date": key, "when": campaign.date_words(key), "major": False} for e in events])
        missed = len(items) - len(events)
        text = (f"{me_name} accepted the proposal ({what})."
                + (f" {missed} part(s) could no longer be put in place." if missed else ""))
        print(f"accepted {tag}'s proposal: {[o for o, _ in outcomes] or 'nothing applied'}")
    else:
        text = f"{me_name} declined the proposal ({what})."
        camp.data.setdefault("refusals", []).append({"from": tag, "keys": items, "date": key,
                                                     "basis": policy.basis(camp.snapshot, tag, player)})
        print(f"declined {tag}'s proposal: {items}")
    camp.add_events([{"date": key, "when": campaign.date_words(key), "type": "offer_answer",
                      "tags": [tag, player], "text": text, "major": False}])
    camp.save()


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
            terms = chronicle.peace_terms(old, snap, player, other, chronicle.front([player, other]), asked)
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
        self.last_snapshot = 0.0
        self.pending: Dict[str, str] = {}      # country -> why it wants to write ("" = unprompted)
        self.pool = ThreadPoolExecutor(max_workers=1)  # policy refreshes: the slow LLM part runs here
        self.policy_job: Optional[tuple] = None   # (tag, future, date key, Relation, campaign id, basis, war baseline)

    def campaign_for(self, player: str, cid: int) -> campaign.Campaign:
        if not cid:
            cid = campaign.new_id(player)
            print(f"new campaign {cid}")
        if not self.camp or self.camp.id != cid or self.camp.data["player"] != player:
            self.camp = campaign.Campaign(cid, player)
            self.pending.clear()          # letters queued for the previous game
            self.policy_job = None        # a refresh still running belongs to the previous game: drop it
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
                events = _with_peace_terms(camp, player, old, snap, chronicle.diff(old, snap))
                for e in events:
                    print(f"  [{e['when']}] {e['text']}")
                camp.add_events(events)
            camp.snapshot = snap
            camp.record_surrender(snap)
            camp.save()
            if old is not None:
                self.plan_letters(camp, player, snap, events)
        self.policies(camp, player, snap)
        return camp

    # ---- neighbours' policies toward the player, refreshed one at a time in the background
    def policies(self, camp: campaign.Campaign, player: str, snap: Dict) -> None:
        if self.policy_job:
            tag, fut, key, rel, cid, basis, baseline = self.policy_job
            if not fut.done():
                return
            self.policy_job = None
            if cid != camp.id or key > (camp.data.get("last_date") or key):
                return                    # started for another game, or an earlier save was loaded since
            try:
                text = fut.result()
            except llm.LLMError as e:
                print(f"  policy for {tag} failed: {e}")
                return
            p = policy.parse(text, rel, baseline)
            p.update(date=key, when=campaign.date_words(key), basis=basis)
            camp.data.setdefault("policies", {})[tag] = p
            camp.save()
            words = lambda keys: ", ".join(actions.word(k) for k in keys) or "none"
            print(f"  policy {tag} -> {player}: {p['stance']}, urgency {p['urgency']}, offers {words(p['offers'])}, "
                  f"asks {words(p['asks'])}, breaks {words(p['breaks'])}: {p['policy']}")
            if p.get("war"):
                print(f"    war stance {p['war']} (situation: {p['war_baseline']}): {p.get('war_why', '')} "
                      f"Terms: {p.get('terms', '')}")
            return
        try:
            me = gamestate.country(player)
        except gamestate.StateError:
            return
        # Most needed first (none or stale, then something happened, then just old); ties in importance order.
        ranked = [(policy.refresh_priority(camp, tag, player, snap["date"]), tag) for tag in policy_countries(me, snap)]
        ranked = sorted([r for r in ranked if r[0]], key=lambda r: -r[0])
        for _, tag in ranked:
            try:
                them = gamestate.country(tag)
            except gamestate.StateError:
                continue
            rel = actions.relation(tag, player)
            baseline, war_part = None, ""
            if player in them.enemies:
                baseline, reasons = war.pressure(them, me, camp)
                war_part = policy.war_instructions(them.name, me.name, baseline, reasons, war.separate_peace_note(them))
            messages = [{"role": "system", "content":
                         f"{_context(them, me, camp)}\n\nYOUR CORRESPONDENCE WITH {me.name.upper()}:\n"
                         f"{_transcript(camp, them, me)}\n\n{policy.instructions(them.name, me.name, rel, war_part)}"},
                        {"role": "user", "content": f"State {them.name}'s policy toward {me.name} now."}]
            self.policy_job = (tag, self.pool.submit(llm.chat, messages, None, 0.7), snap["date"], rel, camp.id,
                               policy.basis(snap, tag, player), baseline)
            return

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
                if other != player and active(snap, other) and not self._cooling(camp, other, today):
                    self.pending[other] = (self.pending.get(other, "") + " " + e["text"]).strip()
        if self.pending:
            return
        # Letters countries start without an event (policy or quiet spell) keep letter_gap_days apart.
        started = [x["date"] for ls in camp.data["letters"].values() for x in ls if x.get("initiated")]
        if started and today - _days(max(started)) < cfg.letter_gap_days:
            return
        # A country writes soon, once per policy, when its policy is urgent (3) or it has something to
        # propose or do and some urgency (2): that letter is where it happens.
        for tag in list(camp.data.get("policies", {})):
            p = policy.current(camp, tag)
            own = [x for x in camp.letters_with(tag) if x.get("initiated")]
            if (policy.wants_to_write(p) and active(snap, tag) and not self._cooling(camp, tag, today)
                    and (not own or own[-1]["date"] < p["date"])):
                self.pending[tag] = ""
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
        candidates = [t for t in policy_countries(me, snap) if not self._cooling(camp, t, today)]
        if candidates:
            pols = camp.data.get("policies", {})
            weights = [relevance(me, t) * (1 + pols.get(t, {}).get("urgency", 0)) for t in candidates]
            self.pending[random.choices(candidates, weights)[0]] = ""

    def deliver(self, player: str, block: str) -> None:
        """Let one waiting country write, if the player isn't reading or awaiting a letter."""
        if not self.pending or not self.camp:
            return
        if re.search(r"\bairp_(reply_from|awaiting_from|outbox_to)=", block):
            return
        tag = next(iter(self.pending))
        reason = self.pending.pop(tag)
        if not active(self.camp.snapshot, tag):
            return                                     # capitulated (or gone) since it was queued
        write_first(self.camp, player, tag, reason)


def run() -> int:
    print(f"watching for letters and world events (model {llm.model()}). Ctrl+C to stop.")
    session = _Session()
    # Leader biographies are indexed from the game and mod files (several seconds for big mods): do it now.
    threading.Thread(target=leaders.warm, daemon=True).start()
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
                answer = re.search(r"\bairp_offer_answer=(\d)", block)
                if answer and session.camp:
                    resolve_offer(session.camp, player, int(answer.group(1)))
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
