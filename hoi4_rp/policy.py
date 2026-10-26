"""Policies: what a country that matters to the player (neighbour, overlord or subject, enemy, faction
member, major power) currently wants from the player, written by the LLM from the full
game state and memory, and stored in the campaign (data["policies"][TAG]).

A policy is plain text in six lines (no JSON: it breaks). Parsing is lenient; anything missing falls back
to a default, and only things possible right now are kept:
    STANCE: wary
    URGENCY: 2
    OFFERS: pact, market                 agreements it would offer the player
    ASKS: access                         agreements it would ask the player to grant it
    BREAKS: end access, embargo          agreements it means to end, or an embargo
    POLICY: One or two short sentences (at most POLICY_WORDS words).
At war with the player, three more (war.py):
    WAR: seek peace                      fight on | open to terms | seek peace | any terms
    WHY: One sentence: why, given who leads it.
    TERMS: One sentence: what it would give up or demand.
The war stance starts from a baseline the numbers set (war.pressure) and may move at most one step from it.

It feeds every letter that country writes, decides who writes first when it's quiet (urgency), and holds
what it may propose (Accept/Decline) or do on its own. Refreshed every REFRESH_DAYS of game time, or sooner
when something happens between the two; refreshes run in the background (letters.py).
A policy remembers the relationship it was written under (basis(): overlords, war, capitulation). Once that
changes (say the country was beaten and became the player's puppet) the policy is stale: it is left out of
prompts and refreshed before anything else.
Stored lists hold deal keys from actions.py ("give:pact", "ask:access", "end:access", "embargo").
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional

from . import actions, style, treaties, war
from .chronicle import WORLD_TYPES

STANCES = ["friendly", "cordial", "indifferent", "wary", "hostile"]
REFRESH_DAYS = 90
VERSION = 4                   # older policies are rewritten once (v4: no war pledges, docking only with a coast)
FAR_REFRESH_DAYS = 360        # a distant country with nothing to say (indifferent, urgency 0) thinks again rarely
POLICY_WORDS = 40
TERRITORY_REFRESH_DAYS = 30   # states changing hands between the two refresh a policy at most this often
# Events that change what the two countries are to each other (a policy written before one is stale).
RELATION_TYPES = {"subject", "independence", "capitulation", "war", "peace", "country_gone", "country_new"}


def war_instructions(them_name: str, me_name: str, baseline: int, reasons: List[str], note: str) -> str:
    """The extra lines asked of a country at war with the player."""
    lo, hi = max(0, baseline - 1), min(len(war.STANCES) - 1, baseline + 1)
    allowed = war.STANCES[lo:hi + 1]
    return (
        f"\n\nYOU ARE AT WAR WITH {me_name.upper()}. Also decide how {them_name} means to end this war. The war "
        f"situation points to: {war.STANCES[baseline].upper()} ({', '.join(reasons)}). Choose from "
        f"{', '.join(allowed)}: the situation's stance, or one step from it if {them_name}'s leader, their traits, "
        f"ideology and people would really take it differently (a fanatical regime may hold on a step longer; a "
        f"government answerable to a war-weary public may sue a step sooner). Meanings: "
        + "; ".join(f"{s}: {war.MEANING[s]}" for s in war.STANCES) + "."
        + (f" {note}" if note else "")
        + f"\nAdd these three lines:\n"
        f"WAR: one of {', '.join(allowed)}\n"
        f"WHY: one sentence on why, given who leads {them_name}\n"
        f"TERMS: one sentence on what {them_name} would give up, or demand, to end the war"
    )


def instructions(them_name: str, me_name: str, rel: "actions.Relation", war_part: str = "") -> str:
    def words(keys: List[str]) -> str:
        return ", ".join(actions.described(k) for k in keys) or "(none possible right now)"
    gives = (actions.possible_keys(rel, actions.GIVES) + actions.state_gives(rel)
             + actions.possible_keys(rel, actions.rights_gives(rel) + actions.dmz_gives(rel) + _war_gives(rel)))
    asks = (actions.possible_keys(rel, actions.ASKS) + actions.state_asks(rel)
            + actions.possible_keys(rel, actions.rights_asks(rel) + actions.dmz_asks(rel) + _war_asks(rel)))
    breaks = actions.possible_keys(rel, actions.UNILATERAL)
    return (
        f"Decide {them_name}'s current foreign policy toward {me_name}, as {them_name}'s government would "
        f"see it today: its interests, fears, ideology, alliances, the situation above and what you remember. "
        f"This is internal policy, not a letter. {style.diplomacy()}\n\n"
        f"Answer with exactly these lines and nothing else:\n"
        f"STANCE: one of {', '.join(STANCES)}\n"
        f"URGENCY: 0 to 3, how pressing it is for {them_name} to contact {me_name} now "
        f"(0 nothing to say, 3 something must be said soon)\n"
        f"OFFERS: agreements {them_name} would be willing to give {me_name} now, chosen from: {words(gives)}; "
        f"or none\n"
        f"ASKS: agreements {them_name} wants {me_name} to give it, chosen from: {words(asks)}; or none\n"
        f"BREAKS: agreements {them_name} means to end, or measures against {me_name}, chosen from: "
        f"{words(breaks)}; or none (only for real cause)\n"
        f"POLICY: one or two short sentences, at most {POLICY_WORDS} words in all: what {them_name} wants from "
        f"{me_name}, fears from them, and how it means to deal with them"
        + (f"\n\nStates are listed with their number (state N). {actions.TERRITORY_NOTE} {actions.RIGHTS_NOTE}"
           if rel.them_border or rel.me_border else "")
        + (f"\n\n{actions.bound_note(them_name, me_name, rel.bound)}" if rel.bound else "")
        + (f"\n\n{actions.WAR_NOTE}" if any(actions.is_war(k) or actions.is_pledge(k) for k in gives + asks) else "")
        + war_part
    )


def _war_gives(rel: "actions.Relation") -> List[str]:
    """Joining the player's wars, and pledges to join its future wars."""
    return actions.war_gives(rel) + actions.pledge_gives(rel)


def _war_asks(rel: "actions.Relation") -> List[str]:
    return actions.war_asks(rel) + actions.pledge_asks(rel)


def _days(key: str) -> int:
    import datetime
    try:
        y, m, d = (int(x) for x in key.split(".")[:3])
        return datetime.date(y, m, d).toordinal()
    except ValueError:
        return 0


def _keys(value: str, allowed: List[str]) -> List[str]:
    """Map a comma-separated line of policy words to deal keys, keeping only `allowed` ones."""
    out = []
    for part in re.split(r"[,;/]|\band\b", value.lower()):
        wr = re.search(r"\b(war|pledge)\s+([a-z0-9]{3})\b", part)
        if wr:                                  # "war ETH (Ethiopia)", "pledge GER (German Reich)"
            tag = wr.group(2).upper()
            out += [c for c in (f"give:{wr.group(1)}:{tag}", f"ask:{wr.group(1)}:{tag}") if c in allowed][:1]
            continue
        dz = re.search(r"dmz\s*(\d+)", part)
        if dz:                                  # "dmz 57 (a demilitarized Ostpreussen)"
            out += [c for c in (f"give:dmz:{dz.group(1)}", f"ask:dmz:{dz.group(1)}") if c in allowed][:1]
            continue
        rt = re.search(r"rights\s*(\d+)", part)
        if rt:                                  # "rights 43 (the resource rights in Northern Hungary ...)"
            out += [c for c in (f"give:rights:{rt.group(1)}", f"ask:rights:{rt.group(1)}") if c in allowed][:1]
            continue
        st = re.search(r"state\s*(\d+)", part)
        if st:                                # "state 35 (Brabant)"
            out += [c for c in (f"give:state:{st.group(1)}", f"ask:state:{st.group(1)}") if c in allowed][:1]
            continue
        w = re.sub(r"[^a-z ]", " ", part).split()
        if not w or w[0] == "none":
            continue
        if w[0] in ("end", "lift", "cancel", "withdraw", "revoke") and len(w) > 1:
            candidates = [f"end:{w[1]}"]
        elif w[0] == "embargo":
            candidates = ["embargo"]            # a bare "embargo" only ever means imposing one
        else:
            candidates = [f"give:{w[0]}", f"ask:{w[0]}", w[0], f"end:{w[0]}"]
        out += [c for c in candidates if c in allowed][:1]
    return list(dict.fromkeys(out))


def parse(text: str, rel: "actions.Relation", war_baseline: Optional[int] = None) -> Dict:
    """Read the lines leniently. Unknown stances become 'indifferent', urgency is clamped to 0..3, and each
    list keeps only deals possible right now. At war (war_baseline given), the war stance is kept within one step
    of the baseline, and a country seeking peace has something urgent to say."""
    def line(key: str) -> str:
        # tolerate markdown (**Stance:**) and a dash instead of a colon
        m = re.search(rf"^[\W_]*{key}[*_\s]*[:\-–]+\s*(.+)$", text, re.I | re.M)
        return m.group(1).strip(" *_") if m else ""

    stance = line("stance").lower()
    stance = next((s for s in STANCES if s in stance), "indifferent")
    m = re.search(r"\d", line("urgency"))
    urgency = max(0, min(3, int(m.group(0)))) if m else 0
    policy = line("policy")
    if not policy:          # tolerate a policy written as a paragraph after the other lines
        rest = re.sub(r"^[\W_]*(stance|urgency|offers|asks|breaks|war|why|terms)[*_\s]*[:\-–].*$", "", text,
                      flags=re.I | re.M).strip()
        policy = re.sub(r"\s+", " ", rest)[:600]
    out_war = {}
    if war_baseline is not None:
        got = war.parse_stance(line("war"))
        level = war_baseline if got is None else max(war_baseline - 1, min(war_baseline + 1, got))
        out_war = {"war": war.STANCES[level], "war_baseline": war.STANCES[war_baseline],
                   "war_why": line("why"), "terms": line("terms")}
        if level >= 2:
            urgency = 3                                  # suing for peace is worth a letter
    return {
        **out_war, "v": VERSION,
        "stance": stance, "urgency": urgency, "policy": policy,
        "offers": _keys(line("offers"), actions.possible_keys(rel, actions.GIVES) + actions.state_gives(rel)
                         + actions.rights_gives(rel) + actions.dmz_gives(rel) + _war_gives(rel)),
        "asks": _keys(line("asks"), actions.possible_keys(rel, actions.ASKS) + actions.state_asks(rel)
                       + actions.rights_asks(rel) + actions.dmz_asks(rel) + _war_asks(rel)),
        "breaks": _keys(line("breaks"), actions.possible_keys(rel, actions.UNILATERAL)),
    }


def _future(camp, p: Dict) -> bool:
    """Dated after the campaign's current game date: written for another game or a later save."""
    return p["date"] > (camp.data.get("last_date") or p["date"])


def basis(snap: Optional[Dict], tag: str, player: str) -> str:
    """The relationship between the two in a chronicle snapshot: who is whose subject, war, capitulation."""
    c = (snap or {}).get("countries", {})
    if tag not in c or player not in c:
        return ""
    at_war = "|".join(sorted([tag, player])) in snap.get("wars", [])
    out = (f"{c[tag]['overlord']}/{c[player]['overlord']}/{'war' if at_war else 'peace'}/"
           f"{int(c[tag]['capitulated'])}{int(c[player]['capitulated'])}")
    if at_war:   # a war that turns (surrender progress crossing a quarter on either side) needs a new stance
        out += f"/{war.band(c[tag].get('surrender'))}{war.band(c[player].get('surrender'))}"
    return out


def stale(camp, tag: str) -> bool:
    """Written under a relationship that no longer holds."""
    p = camp.data.setdefault("policies", {}).get(tag)
    if not p:
        return False
    # A country that vanished or appeared since is another country now: dynamic tags (D01...) are reused, and
    # the D04 that was Hitler's Reich in a civil war can be Vapsid Estonia a year later
    if any(e["date"] > p["date"] and tag in e["tags"][:1] and e.get("type") in ("country_gone", "country_new")
           for e in camp.data["events"]):
        return True
    if p.get("basis"):
        now = basis(camp.snapshot, tag, camp.data["player"])
        return bool(now) and now != p["basis"]
    # Older policies don't store their basis: a relation change with the player since counts, and so does the
    # country itself capitulating, vanishing or coming back (older chronicles didn't tie those to the player).
    player = camp.data["player"]
    return any(e["date"] > p["date"] and tag in e["tags"] and e.get("type") in RELATION_TYPES
               and (player in e["tags"] or e["type"] in ("capitulation", "country_gone", "country_new"))
               for e in camp.data["events"])


def current(camp, tag: str) -> Optional[Dict]:
    """The country's policy toward the player, or None (none yet, or stale until it is refreshed)."""
    p = camp.data.setdefault("policies", {}).get(tag)
    if p and _future(camp, p):
        del camp.data["policies"][tag]
        return None
    if p and stale(camp, tag):
        return None
    if p:   # policies saved before asks/breaks held bare words in "offers"
        p["offers"] = [o if ":" in o else f"give:{o}" for o in p.get("offers", [])]
        p.setdefault("asks", [])
        p.setdefault("breaks", [])
        given = treaties.standing(camp, tag)        # a pledge made since the policy was set isn't wanted again
        keep = lambda k: k not in given and (actions.PLEDGES or not actions.is_pledge(k))   # pledges switched off
        p = {**p, "offers": [k for k in p["offers"] if keep(k)], "asks": [k for k in p["asks"] if keep(k)]}
    return p


def refresh_priority(camp, tag: str, player: str, today: str, close: bool = True) -> int:
    """How badly the policy needs rewriting: 3 none or stale, 2 something happened between the two,
    1 just old, 0 not needed. `close`: a neighbour, enemy, faction member, overlord or subject of the player; a
    country that isn't, with an indifferent policy and nothing urgent, is old only after FAR_REFRESH_DAYS."""
    raw = camp.data.setdefault("policies", {}).get(tag)
    if not raw or "asks" not in raw or _future(camp, raw) or stale(camp, tag) or raw.get("v", 1) < VERSION:
        return 3
    age = _days(today) - _days(raw["date"])
    # something happened in the world between the two since the policy was set (not just letter outcomes,
    # or every answered proposal would refresh the policy and start the next one). States changing hands
    # happen all through a war, so they alone refresh it only now and then.
    since = {e["type"] for e in camp.data["events"]
             if e["date"] > raw["date"] and tag in e["tags"] and player in e["tags"] and e.get("type") in WORLD_TYPES}
    if since - {"territory"}:
        return 2
    far = not close and raw.get("stance") == "indifferent" and not raw.get("urgency")
    if age >= (FAR_REFRESH_DAYS if far else REFRESH_DAYS) or (since and age >= TERRITORY_REFRESH_DAYS):
        return 1
    return 0


def wants_to_write(p: Optional[Dict]) -> bool:
    """Urgent, or something to propose or do with some urgency (no policy, or a stale one: no)."""
    if not p:
        return False
    return p["urgency"] >= 3 or (p["urgency"] >= 2 and bool(p["offers"] or p["asks"] or p["breaks"]))


def describe(p: Optional[Dict], them_name: str, me_name: str) -> str:
    """For letter prompts."""
    if not p:
        return ""
    def words(keys: List[str]) -> str:
        return ", ".join(actions.described(k) for k in keys) or "none"
    out = (f"YOUR CURRENT POLICY TOWARD {me_name.upper()} (set {p.get('when', p['date'])}): stance {p['stance']}. "
           f"{p['policy']} Would be willing to offer them (not offered yet unless the letters show it): "
           f"{words(p['offers'])}. Want from them: {words(p['asks'])}. "
           f"Mean to end or impose: {words(p['breaks'])}.")
    if p.get("war"):
        out += (f"\nYOUR WAR STANCE: {p['war'].upper()}: {war.MEANING.get(p['war'], '')}."
                + (f" {p['war_why']}" if p.get("war_why") else "")
                + (f" Terms: {p['terms']}" if p.get("terms") else "")
                + " Your letters follow this stance: the tone is yours, but don't vow to fight to the end while "
                  "seeking peace, or beg for peace while meaning to fight on.")
    return out
