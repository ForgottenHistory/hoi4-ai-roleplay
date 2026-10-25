"""Peace through letters: a country at war with the player offers peace in a letter ([[offer peace]], Accept/Decline);
accepting starts a peace conference through the game's own start_peace_conference. Our own take on the Simple Peace
Deals mod (Workshop 3711217109), which isn't needed.

Who gets how much: the Simple Peace Deals measure (gamestate._war_score): each side's surrender progress, scaled by
the share of its occupied core land the other side and its allies hold. The side beaten more wins, and the margin
is its share of the conference score (0 = white peace). The offer shows that number, and accepting uses it.

When peace can be made (possible()):
    at war with each other, for at least the minimum war length (config [letters] peace_min_months; auto = 3
    months, 6 if one side is a major power, 12 if both are)
    neither is a subject (its overlord decides) and both lead their faction or are in none
    not a civil war
"""

from __future__ import annotations

from typing import Optional, Tuple

from . import bridge, gamestate
from .config import settings


def _trigger(script: str) -> bool:
    return bridge.send(f"eval_trigger {script}")[0].rstrip().endswith("yes")


def min_months(me: str, other: str) -> int:
    configured = settings().letters.peace_min_months
    if configured.isdigit():
        return int(configured)
    majors = sum(_trigger(f"{t} = {{ is_major = yes }}") for t in (me, other))
    return (3, 6, 12)[majors]


def possible(me: str, other: str) -> bool:
    """Peace can be made between the two right now (see the module notes)."""
    n = min_months(me, other)
    side = lambda enemy: (f"is_subject = no OR = {{ is_faction_leader = yes is_in_faction = no }} "
                          f"NOT = {{ civilwar_target = {enemy} }}")
    return _trigger(f"{me} = {{ has_war_with = {other} war_length_with = {{ tag = {other} months > {n - 1} }} "
                    f"{side(other)} {other} = {{ {side(me)} }} }}")


def estimate(me: gamestate.CountryState, them: gamestate.CountryState) -> Tuple[float, str]:
    """(the player's share of the conference score, negative = the other side's; the line for the offer)."""
    margin = gamestate._war_score(them, me) - gamestate._war_score(me, them)
    margin = max(-1.0, min(1.0, margin))
    pct = round(abs(margin) * 100)
    if pct == 0:
        return 0.0, "Outcome: a white peace (neither side gains anything)."
    if margin > 0:
        return margin, f"Outcome: we receive {pct}% of the war score at the peace conference."
    return margin, f"Outcome: {them.name} receives {pct}% of the war score at the peace conference."


def make(me: str, other: str) -> Optional[str]:
    """Start the peace conference, scored now. Returns the outcome line, or None if it couldn't be started."""
    try:
        a, b = gamestate.country(me), gamestate.country(other)
    except gamestate.StateError as e:
        print("  peace: could not read the countries:", e)
        return None
    margin, line = estimate(a, b)
    winner, loser = (me, other) if margin >= 0 else (other, me)
    reply = bridge.send(f"eval_effect {winner} = {{ start_peace_conference = {{ tag = {loser} "
                        f"score_factor = {abs(margin):.3f} }} }}")[0]
    if not reply.startswith("OK"):
        print("  peace failed:", reply)
        return None
    print(f"  peace conference: {winner} wins over {loser}, score {abs(margin):.3f}")
    return line
