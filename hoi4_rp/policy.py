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
    POLICY: Two or three sentences.

It feeds every letter that country writes, decides who writes first when it's quiet (urgency), and holds
what it may propose (Accept/Decline) or do on its own. Refreshed every REFRESH_DAYS of game time, or sooner
when something happens between the two; refreshes run in the background (letters.py).
Stored lists hold deal keys from actions.py ("give:pact", "ask:access", "end:access", "embargo").
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional

from . import actions
from .chronicle import WORLD_TYPES

STANCES = ["friendly", "cordial", "indifferent", "wary", "hostile"]
REFRESH_DAYS = 90


def instructions(them_name: str, me_name: str, rel: "actions.Relation") -> str:
    def words(keys: List[str]) -> str:
        return ", ".join(actions.described(k) for k in keys) or "(none possible right now)"
    gives = actions.possible_keys(rel, actions.GIVES) + actions.state_gives(rel)
    asks = actions.possible_keys(rel, actions.ASKS) + actions.state_asks(rel)
    breaks = actions.possible_keys(rel, actions.UNILATERAL)
    return (
        f"Decide {them_name}'s current foreign policy toward {me_name}, as {them_name}'s government would "
        f"see it today: its interests, fears, ideology, alliances, the situation above and what you remember. "
        f"This is internal policy, not a letter.\n\n"
        f"Answer with exactly these six lines and nothing else:\n"
        f"STANCE: one of {', '.join(STANCES)}\n"
        f"URGENCY: 0 to 3, how pressing it is for {them_name} to contact {me_name} now "
        f"(0 nothing to say, 3 something must be said soon)\n"
        f"OFFERS: agreements {them_name} would be willing to give {me_name} now, chosen from: {words(gives)}; "
        f"or none\n"
        f"ASKS: agreements {them_name} wants {me_name} to give it, chosen from: {words(asks)}; or none\n"
        f"BREAKS: agreements {them_name} means to end, or measures against {me_name}, chosen from: "
        f"{words(breaks)}; or none (only for real cause)\n"
        f"POLICY: two or three sentences on what {them_name} wants from {me_name}, fears from them, and how "
        f"it means to deal with them"
        + (f"\n\nStates are listed with their number (state N). {actions.TERRITORY_NOTE}"
           if rel.them_border or rel.me_border else "")
    )


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
        st = re.search(r"state\s*(\d+)", part)
        if st:                                  # "state 35 (Brabant)"
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


def parse(text: str, rel: "actions.Relation") -> Dict:
    """Read the lines leniently. Unknown stances become 'indifferent', urgency is clamped to 0..3, and each
    list keeps only deals possible right now."""
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
        rest = re.sub(r"^[\W_]*(stance|urgency|offers|asks|breaks)[*_\s]*[:\-–].*$", "", text,
                      flags=re.I | re.M).strip()
        policy = re.sub(r"\s+", " ", rest)[:600]
    return {
        "stance": stance, "urgency": urgency, "policy": policy,
        "offers": _keys(line("offers"), actions.possible_keys(rel, actions.GIVES) + actions.state_gives(rel)),
        "asks": _keys(line("asks"), actions.possible_keys(rel, actions.ASKS) + actions.state_asks(rel)),
        "breaks": _keys(line("breaks"), actions.possible_keys(rel, actions.UNILATERAL)),
    }


def _future(camp, p: Dict) -> bool:
    """Dated after the campaign's current game date: written for another game or a later save."""
    return p["date"] > (camp.data.get("last_date") or p["date"])


def current(camp, tag: str) -> Optional[Dict]:
    p = camp.data.setdefault("policies", {}).get(tag)
    if p and _future(camp, p):
        del camp.data["policies"][tag]
        return None
    if p:   # policies saved before asks/breaks held bare words in "offers"
        p["offers"] = [o if ":" in o else f"give:{o}" for o in p.get("offers", [])]
        p.setdefault("asks", [])
        p.setdefault("breaks", [])
    return p


def needs_refresh(camp, tag: str, player: str, today: str) -> bool:
    raw = camp.data.setdefault("policies", {}).get(tag)
    if not raw or "asks" not in raw or _future(camp, raw):   # none, old format, or from another game/save
        return True
    p = current(camp, tag)
    if _days(today) - _days(p["date"]) >= REFRESH_DAYS:
        return True
    # something happened in the world between the two since the policy was set (not just letter outcomes,
    # or every answered proposal would refresh the policy and start the next one)
    return any(e["date"] > p["date"] and tag in e["tags"] and player in e["tags"] and e.get("type") in WORLD_TYPES
               for e in camp.data["events"])


def wants_to_write(p: Dict) -> bool:
    """Urgent, or something to propose or do with some urgency."""
    return p["urgency"] >= 3 or (p["urgency"] >= 2 and bool(p["offers"] or p["asks"] or p["breaks"]))


def describe(p: Optional[Dict], them_name: str, me_name: str) -> str:
    """For letter prompts."""
    if not p:
        return ""
    def words(keys: List[str]) -> str:
        return ", ".join(actions.described(k) for k in keys) or "none"
    return (f"YOUR CURRENT POLICY TOWARD {me_name.upper()} (set {p.get('when', p['date'])}): stance {p['stance']}. "
            f"{p['policy']} Would be willing to offer them (not offered yet unless the letters show it): "
            f"{words(p['offers'])}. Want from them: {words(p['asks'])}. "
            f"Mean to end or impose: {words(p['breaks'])}.")
