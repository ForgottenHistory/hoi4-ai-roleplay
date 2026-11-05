"""Peace between AI countries: wars the game's AI would fight forever get a way out.

HOI4's AI ends a war only by capitulation, so a war between two countries that can't reach each other (Germany and
Hungary, no border, Hungary without a coast) or that has frozen on its front runs on for good. Once a game month
(due()), wars between two AI countries that could make peace (peace.possible: faction leaders or countries alone,
no civil war, the player not fighting in the war at all: player_at_table) are looked at (candidates()):

    stalled     no front between the two sides (gamestate.reach), or neither side's surrender progress moved
                STALL_MOVE in the last STALL_DAYS
    lopsided    one side at LOPSIDED surrender progress or more

and no other front of the war live (live_elsewhere): the conference ends the whole war, so a quiet front's peace
would take a won war from a third country. Both factions come to the table whole (peace.make), members fighting
on their own included: a peace with a faction's leader alone left its members at war, and the leader was called
back in weeks later.

with the war at least MIN_DAYS old and the pair not talked to in COOLDOWN_DAYS. At most PER_MONTH of them, the
stalled first. The side worse off writes (in a stalemate, either): two short LLM calls, an offer (OFFER: yes or no,
it may decide to fight on) and the answer (ANSWER: accept or decline). Both see the same picture: the war's
balance, whether they can reach each other, its length, how surrender progress has moved, and the terms, which are
the game's own measure (peace.estimate: a white peace when even, else the winner's share of a peace conference
the game's AI runs). Accepted, with [ai_diplomacy] mode = on, the conference starts (peace.make) and the peace is
news in the chronicle; a refusal stays between the two.

Kept in the campaign: data["ai_peace"] = {"tick": month, "pairs": {"A|B": date}}. Logged to ai_diplomacy.txt.
"""

from __future__ import annotations

import random
import re
from typing import Dict, List, Optional, Tuple

from . import campaign, gamestate

PER_MONTH = 2
COOLDOWN_DAYS = 180
MIN_DAYS = 90
STALL_DAYS = 90
STALL_MOVE = 0.05
LOPSIDED = 0.5


def _state(camp: campaign.Campaign) -> Dict:
    st = camp.data.setdefault("ai_peace", {})
    st.setdefault("tick", "")
    st.setdefault("pairs", {})
    return st


def _days(key: str) -> int:
    import datetime
    try:
        y, m, d = (int(x) for x in key.split(".")[:3])
        return datetime.date(y, m, d).toordinal()
    except ValueError:
        return 0


def due(camp: campaign.Campaign) -> bool:
    """A new game month since the last look."""
    today = camp.data.get("last_date") or ""
    return bool(today) and today[:7] != _state(camp)["tick"][:7]


def _moved(camp: campaign.Campaign, tag: str, today: str) -> Optional[float]:
    then = camp.surrender_at(tag, campaign.shift(today, -STALL_DAYS))
    now = camp.surrender_at(tag, today)
    return None if then is None or now is None else abs(now - then)


def live_elsewhere(camp: campaign.Campaign, a: str, b: str, today: str) -> str:
    """A country other than the two fighting either of them whose front is live: capitulated, or its surrender
    progress moved STALL_MOVE in STALL_DAYS (its name, or ""). The conference ends that war too: an Italy-USA white
    peace across the ocean took Britain's win over a capitulated Ireland, twice."""
    snap = camp.snapshot or {}
    c = snap.get("countries", {})
    others = {t for key in snap.get("wars", []) for t in key.split("|")
              if {a, b} & set(key.split("|"))} - {a, b}
    for t in sorted(others):
        if c.get(t, {}).get("capitulated"):
            return f"{c[t].get('name', t)} has capitulated"
        m = _moved(camp, t, today)
        if m is not None and m >= STALL_MOVE:
            return f"{c.get(t, {}).get('name', t)}'s front is moving"
    return ""


def player_at_table(player: str, a: str, b: str) -> bool:
    """Would the player sit at a conference between a and b? The game's conference ends the whole war: everyone
    fighting either of them, not only their factions and subjects (the player fighting Britain beside the USA lost
    its war score and its occupied states to an Australia-Britain peace). Such a peace is the player's to make."""
    from . import bridge
    return bridge.trigger(
        f"{player} = {{ OR = {{ has_war_with = {a} has_war_with = {b} has_war_together_with = {a} "
        f"has_war_together_with = {b} is_in_faction_with = {a} is_in_faction_with = {b} is_subject_of = {a} "
        f"is_subject_of = {b} }} }}")


def candidates(camp: campaign.Campaign) -> List[Tuple[str, str, str]]:
    """Wars between AI countries worth peace talks now: [(writer, other, why)], at most PER_MONTH, stalled first.
    Marks the month as looked at."""
    from . import peace
    st = _state(camp)
    today = camp.data.get("last_date") or ""
    st["tick"] = today
    snap = camp.snapshot or {}
    c = snap.get("countries", {})
    player = camp.data["player"]
    found = []
    for key in snap.get("wars", []):
        a, b = key.split("|")
        if player in (a, b) or a not in c or b not in c or c[a].get("capitulated") or c[b].get("capitulated"):
            continue
        if today and st["pairs"].get(key) and _days(today) - _days(st["pairs"][key]) < COOLDOWN_DAYS:
            continue
        if not peace.possible(a, b) or player_at_table(player, a, b) or live_elsewhere(camp, a, b, today):
            continue
        try:
            sa, sb = gamestate.country(a), gamestate.country(b)
        except gamestate.StateError:
            continue
        war = next((w for w in sa.wars if w.enemy == b), None)
        start = campaign.save_key(war.start_date) if war and war.start_date else ""
        if not start or _days(today) - _days(start) < MIN_DAYS:
            continue
        ma, mb = _moved(camp, a, today), _moved(camp, b, today)
        unreachable = "meet anywhere" in gamestate.reach(sa, sb)
        frozen = ma is not None and mb is not None and ma < STALL_MOVE and mb < STALL_MOVE
        worse = max((sa, sb), key=lambda s: s.surrender or 0)
        if unreachable or frozen:
            writer = random.choice((a, b)) if (sa.surrender or 0) == (sb.surrender or 0) else worse.tag
            found.append((0, writer, b if writer == a else a, "no front" if unreachable else "frozen front"))
        elif (worse.surrender or 0) >= LOPSIDED:
            found.append((1, worse.tag, b if worse.tag == a else a, "lopsided"))
    found.sort(key=lambda x: x[0])
    for _, w, o, _ in found[:PER_MONTH]:
        st["pairs"][campaign.pair(w, o)] = today    # talks under way: a month ticking over before they end starts none
    return [(w, o, why) for _, w, o, why in found[:PER_MONTH]]


# ---------------------------------------------------------------- the two calls

def _picture(camp: campaign.Campaign, me: gamestate.CountryState, other: gamestate.CountryState,
             w: Dict[str, Dict]) -> str:
    """The war as both governments see it, from `me`'s side."""
    from . import diplomacy, peace
    today = camp.data.get("last_date") or ""
    war = next((x for x in me.wars if x.enemy == other.tag), None)
    start = campaign.save_key(war.start_date) if war and war.start_date else ""
    lines = [diplomacy.card(me, w.get(me.tag, {}), other.tag, camp),
             diplomacy.card(other, w.get(other.tag, {}), me.tag, camp),
             "", "THE WAR BETWEEN YOU:", gamestate.war_balance(me, other)]
    if (where := gamestate.reach(me, other)):
        lines.append(where)
    if start:
        lines.append(f"It began on {campaign.date_words(start)}, {(_days(today) - _days(start)) // 30} months ago.")
    for s in (me, other):
        m = _moved(camp, s.tag, today)
        if m is not None:
            lines.append(f"{s.name}'s surrender progress is {round((s.surrender or 0) * 100)}%, "
                         f"{'barely moved' if m < STALL_MOVE else f'moved {round(m * 100)} points'} in three months.")
    _, terms = peace.estimate(me, other, player_leads=False)
    lines += ["", f"A peace now: {terms} The game's peace conference settles the demands of whichever side gains."]
    mine, theirs = gamestate.war_side(me, other), gamestate.war_side(other, me)
    if len(mine) > 1 or len(theirs) > 1:
        n = lambda k: f"{k} countr{'y' if k == 1 else 'ies'}"
        lines.append(f"A peace between you ends the war for both sides at once: your {n(len(mine))} and their "
                     f"{n(len(theirs))} all come to the one peace conference.")
    return "\n".join(lines)


def _system(me: gamestate.CountryState) -> str:
    return (f"You are the government of {me.name} in Hearts of Iron IV. The date is {me.date.strip()}. Act as a "
            f"shrewd player of {me.name} would: its interests first, true to its leader and ideology. Write in "
            f"English, whatever your country's language.")


def offer_messages(camp: campaign.Campaign, me: gamestate.CountryState, other: gamestate.CountryState,
                   w: Dict[str, Dict]) -> List[Dict]:
    task = (f"Your government is weighing whether to offer {other.name} peace on these terms. A war you can't win, "
            f"can't lose or can't even reach costs men, factories and stability for nothing; a war you are winning "
            f"may be worth finishing; one you are losing may be worth ending before it costs more. Decide as "
            f"{me.name}'s government would.\n\nIf you offer it, write the offer to {other.name}'s government, "
            f"addressed to them: 2 to 4 sentences in {me.name}'s own voice (no heading, date or place line), "
            f"and only the offer: they read every word of it, so none of your own reckoning. Then, on a line of "
            f"its own:\nOFFER: yes or no (no: you fight on, and write one sentence on why instead of the offer)")
    return [{"role": "system", "content": f"{_system(me)}\n\n{_picture(camp, me, other, w)}\n\n{task}"},
            {"role": "user", "content": "Decide now."}]


def answer_messages(camp: campaign.Campaign, me: gamestate.CountryState, other: gamestate.CountryState,
                    w: Dict[str, Dict], offer: str) -> List[Dict]:
    task = (f"{other.name} offers you peace on these terms:\n\"{offer}\"\n\nAnswer it in 1 to 3 sentences in "
            f"{me.name}'s own voice (no heading, date or place line), weighing it as {me.name}'s government would: "
            f"what more war could still win or lose, and what it costs. Then, on a line of its own:\nANSWER: accept "
            f"or decline")
    return [{"role": "system", "content": f"{_system(me)}\n\n{_picture(camp, me, other, w)}\n\n{task}"},
            {"role": "user", "content": "Answer now."}]


def _line(text: str, key: str) -> str:
    m = re.search(rf"^[\W_]*{key}[*_\s]*[:\-–]+\s*(.+)$", text, re.I | re.M)
    return m.group(1).strip(" *_.").lower() if m else ""


def _clean(text: str) -> str:
    return re.sub(r"^[\W_]*(offer|answer)[*_\s]*[:\-–].*$", "", text, flags=re.I | re.M).strip()


def read_offer(raw: str) -> Tuple[bool, str]:
    """(offers peace?, its text, or why not)."""
    said = _line(raw, "offer")
    return said.startswith("yes"), _clean(raw)


def read_answer(raw: str) -> Tuple[bool, str]:
    said = _line(raw, "answer")
    return "accept" in said and not re.search(r"\b(declin|reject|refus)", said), _clean(raw)


def record(camp: campaign.Campaign, a: str, b: str, why: str, offered: bool, offer: str,
           accepted: bool = False, reply: str = "") -> None:
    """Log the talks; an accepted peace is made (mode = on) and is news. `a` offered, `b` answered."""
    from . import diplomacy, peace
    from .config import settings
    key = camp.data.get("last_date") or ""
    _state(camp)["pairs"][campaign.pair(a, b)] = key
    c = (camp.snapshot or {}).get("countries", {})
    name = lambda t: c.get(t, {}).get("name", t)
    lines = [f"=== {campaign.date_words(key)}: PEACE TALKS {name(a)} -> {name(b)}   ({why})"]
    if not offered:
        lines.append(f"{name(a).upper()} fights on: {offer}")
        diplomacy.log(lines)
        return
    lines += [f"{name(a).upper()} offers peace: {offer}",
              f"{name(b).upper()} ({'ACCEPT' if accepted else 'DECLINE'}): {reply}"]
    if accepted and player_at_table(camp.data["player"], a, b):     # the player joined the war since
        lines.append("  peace not made: the player now fights in this war, and its peace is theirs to make")
        diplomacy.log(lines)
        return
    if accepted:
        outcome = peace.make(a, b, player_leads=False) if settings().ai.mode == "on" else None
        if outcome:
            outcome = outcome.replace("our side", f"{name(a)}'s side")      # the line speaks for the offerer
            lines.append(f"  [live] peace conference: {outcome}")
            camp.add_events([{"date": key, "when": campaign.date_words(key), "type": "news", "tags": [a, b],
                              "text": f"{name(a)} and {name(b)} agreed to make peace. {outcome}",
                              "major": any(c.get(t, {}).get("major") for t in (a, b))}])
        else:
            lines.append("  [dry run] would make peace" if settings().ai.mode != "on" else
                         "  peace could not be started")
    diplomacy.log(lines)
