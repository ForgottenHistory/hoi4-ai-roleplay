"""War stance: how a country at war with the player means to end that war.

    fight on       no talk of peace (a winner pressing for victory, or a loser refusing to yield)
    open to terms  would listen to a reasonable offer, but won't beg
    seek peace     wants out of the war and will offer concessions
    any terms      will accept nearly anything to end it

The numbers set a baseline (pressure()): how close it is to capitulation and how fast that is moving, its capital,
war support (the game's own measure of its people's will to fight), stability, the armies and how long the war has
lasted. The policy LLM then picks the stance, at most one step from the baseline, from who the country is: its
leader, traits, ideology and people. A fanatic may hold on a step longer; a liberal council may sue a step sooner.
"""

from __future__ import annotations

from typing import List, Optional, Tuple

from . import campaign, gamestate

STANCES = ["fight on", "open to terms", "seek peace", "any terms"]

MEANING = {
    "fight on": "no talk of peace: it means to win, or at least not to yield",
    "open to terms": "it would listen to a reasonable offer, but won't beg",
    "seek peace": "it wants out of the war and is ready to offer concessions",
    "any terms": "it will accept nearly anything to end the war",
}


def band(surrender: Optional[float]) -> int:
    """0..4: surrender progress in quarters (for noticing that a war has turned)."""
    return min(4, int((surrender or 0) * 4))


def pressure(them: gamestate.CountryState, me: gamestate.CountryState,
             camp: "campaign.Campaign") -> Tuple[int, List[str]]:
    """Baseline stance index for `them` in its war with `me`, and the reasons, in words."""
    if them.capitulated:
        return 3, [f"{them.name} has capitulated"]
    why: List[str] = []
    p = them.surrender or 0.0
    if p >= 0.05:
        why.append(f"surrender progress {round(p * 100)}%")
    today = campaign.date_key(them.date)
    before = camp.surrender_at(them.tag, campaign.shift(today, -30)) if today else None
    if before is not None and (them.surrender or 0) - before >= 0.1:
        p += min(0.2, (them.surrender or 0) - before)
        why.append(f"up from {round(before * 100)}% a month ago")
    if them.capital_held_by:
        p += 0.15
        why.append("its capital has fallen")
    ws = them.war_support
    if ws is not None:
        p += (0.5 - ws) * 0.4
        if ws < 0.35 or ws > 0.7:
            why.append(f"war support {round(ws * 100)}%")
    st = them.stability
    if st is not None:
        p += (0.5 - st) * 0.2
        if st < 0.35:
            why.append(f"stability {round(st * 100)}%")
    esr = gamestate.odds(them)            # its whole side against the enemy's, when it has allies
    allied = bool(them.side_strength) and gamestate.has_allies(them)
    if esr:
        if esr > 1.5:
            p += min(0.15, (esr - 1) * 0.05)
            why.append(f"enemy armies {esr:.1f} times {'its side' + chr(39) + 's' if allied else 'its own'}")
        elif esr < 0.8:
            p -= 0.1
            why.append("its side's armies are the stronger" if allied else "its armies are the stronger")
    whole = gamestate.whole_war(them, me)     # its side's war, as the game scores it
    if whole and whole["favour"] <= -0.5:
        p += 0.1
        why.append(f"the war as a whole is going {round(-whole['favour'] * 100)}% against its side")
    elif whole and whole["favour"] >= 0.5:
        p -= 0.1
        why.append(f"the war as a whole is going {round(whole['favour'] * 100)}% its side's way")
    if (me.surrender or 0) > (them.surrender or 0) + 0.2:
        p -= 0.2
        why.append(f"{me.name} is the one losing ({round((me.surrender or 0) * 100)}%)")
    war = next((w for w in them.wars if w.enemy == me.tag), None)
    start = campaign.save_key(war.start_date) if war and war.start_date else ""
    if start and today:
        months = (_days(today) - _days(start)) // 30
        if months >= 12:
            p += 0.05 if months < 24 else 0.1
            why.append(f"{months} months of war")
    level = 0 if p < 0.25 else 1 if p < 0.5 else 2 if p < 0.75 else 3
    return level, why or ["the war has not yet gone clearly either way"]


def separate_peace_note(them: gamestate.CountryState) -> str:
    """A subject or faction member can't make peace on its own: its overlord or faction leader decides."""
    if them.overlord:
        return (f"{them.name} is a subject of {them.name_of(them.overlord)}: it cannot make peace on its own; "
                f"{them.name_of(them.overlord)} decides, and {them.name} can only plead or ask for terms through it.")
    if them.faction and them.faction_leader and them.faction_leader != them.tag:
        return (f"{them.name} is a member of the {them.faction}, not its leader: it cannot make a separate peace "
                f"while {them.name_of(them.faction_leader)} fights on, only urge it or ask for terms through it.")
    return ""


def parse_stance(text: str) -> Optional[int]:
    t = text.lower()
    for i, s in reversed(list(enumerate(STANCES))):     # "any terms" before "terms"
        if s in t:
            return i
    if "peace" in t:
        return 2
    if "fight" in t or "victory" in t:
        return 0
    return None


def _days(key: str) -> int:
    import datetime
    try:
        y, m, d = (int(x) for x in key.split(".")[:3])
        return datetime.date(y, m, d).toordinal()
    except ValueError:
        return 0
