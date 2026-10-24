"""Policies: what a neighbouring country currently wants from the player, written by the LLM from the full
game state and memory, and stored in the campaign (data["policies"][TAG]).

A policy is plain text in four lines (no JSON: it breaks). Parsing is lenient; anything missing falls back
to a default:
    STANCE: wary
    URGENCY: 2
    OFFERS: pact, market
    POLICY: Two or three sentences.

It feeds every letter that country writes, decides who writes first when it's quiet (urgency), and lists
the agreements it may offer the player (Accept/Decline). Refreshed every REFRESH_DAYS of game time, or sooner
when something happens between the two; refreshes run in the background (letters.py).
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional

STANCES = ["friendly", "cordial", "indifferent", "wary", "hostile"]
REFRESH_DAYS = 90


def instructions(them_name: str, me_name: str, offerable: List[str]) -> str:
    offers = ", ".join(offerable) if offerable else "(none possible right now)"
    return (
        f"Decide {them_name}'s current foreign policy toward {me_name}, as {them_name}'s government would "
        f"see it today: its interests, fears, ideology, alliances, the situation above and what you remember. "
        f"This is internal policy, not a letter.\n\n"
        f"Answer with exactly these four lines and nothing else:\n"
        f"STANCE: one of {', '.join(STANCES)}\n"
        f"URGENCY: 0 to 3, how pressing it is for {them_name} to contact {me_name} now "
        f"(0 nothing to say, 3 something must be said soon)\n"
        f"OFFERS: agreements {them_name} would be willing to propose to {me_name} now, comma-separated, "
        f"chosen from: {offers}; or none\n"
        f"POLICY: two or three sentences on what {them_name} wants from {me_name}, fears from them, and how "
        f"it means to deal with them"
    )


def _days(key: str) -> int:
    import datetime
    try:
        y, m, d = (int(x) for x in key.split(".")[:3])
        return datetime.date(y, m, d).toordinal()
    except ValueError:
        return 0


def parse(text: str, offerable: List[str]) -> Dict:
    """Read the four lines leniently. Unknown stances become 'indifferent', urgency is clamped to 0..3,
    offers are kept only if they're in `offerable`."""
    def line(key: str) -> str:
        # tolerate markdown (**Stance:**) and a dash instead of a colon
        m = re.search(rf"^[\W_]*{key}[*_\s]*[:\-–]+\s*(.+)$", text, re.I | re.M)
        return m.group(1).strip(" *_") if m else ""

    stance = line("stance").lower()
    stance = next((s for s in STANCES if s in stance), "indifferent")
    m = re.search(r"\d", line("urgency"))
    urgency = max(0, min(3, int(m.group(0)))) if m else 0
    offers = [o for o in re.findall(r"[a-z_]+", line("offers").lower()) if o in offerable]
    policy = line("policy")
    if not policy:          # tolerate a policy written as a paragraph after the other lines
        rest = re.sub(r"^[\W_]*(stance|urgency|offers)[*_\s]*[:\-–].*$", "", text, flags=re.I | re.M).strip()
        policy = re.sub(r"\s+", " ", rest)[:600]
    return {"stance": stance, "urgency": urgency, "offers": list(dict.fromkeys(offers)), "policy": policy}


def current(camp, tag: str) -> Optional[Dict]:
    return camp.data.setdefault("policies", {}).get(tag)


def needs_refresh(camp, tag: str, player: str, today: str) -> bool:
    p = current(camp, tag)
    if not p:
        return True
    if _days(today) - _days(p["date"]) >= REFRESH_DAYS:
        return True
    # something happened between the two since the policy was set
    return any(e["date"] > p["date"] and tag in e["tags"] and player in e["tags"] for e in camp.data["events"])


def describe(p: Optional[Dict], them_name: str, me_name: str) -> str:
    """For letter prompts."""
    if not p:
        return ""
    offers = ", ".join(p["offers"]) or "none"
    return (f"YOUR CURRENT POLICY TOWARD {me_name.upper()} (set {p.get('when', p['date'])}): stance {p['stance']}. "
            f"{p['policy']} Agreements you are willing to offer them: {offers}.")
