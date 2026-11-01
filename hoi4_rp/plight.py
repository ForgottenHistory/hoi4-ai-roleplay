"""How much trouble a country is in, for prompts: secure, strained, dire or desperate, and why.

The prompts carry the facts already (surrender progress, lost land, spirits), but a model given facts alone writes
as if all were well. This turns them into one judgement, and says what it means for the voice and for deals:
a country in a bad way asks plainly, pays more for help and settles for less.

    level, reasons = plight.assess(state)
    plight.text(state)                       # the YOUR SITUATION section ('' when secure)
    plight.short(state)                      # 'dire (...)' for the other side of a correspondence
"""

from __future__ import annotations

from typing import List, Optional, Tuple

from . import gamestate, spirits

LEVELS = ["secure", "strained", "dire", "desperate"]

MEANING = {
    1: "It is under strain and it shows at the edges. Its government wants help more than it admits, and an offer "
       "of support weighs more than in calmer times.",
    2: "It is in serious danger and its government knows it. Need outweighs pride: it asks plainly for what it "
       "needs, pays more for help than it would in calmer times, and settles for less.",
    3: "It is fighting for its survival and may not last. Its government is desperate: it will pay nearly any "
       "price that does not end it as a nation for help that comes soon, and takes deals it would scorn in better "
       "times. Its fear is real, whatever face it puts on it.",
}


def assess(s: gamestate.CountryState) -> Tuple[int, List[str]]:
    """(level 0..3, the reasons), from the war, the home front and the national spirits."""
    signals: List[Tuple[int, str]] = []
    pct = lambda v: f"{round(v * 100)}%"
    if s.capitulated:
        signals.append((3, "it has capitulated"))
    if s.capital_held_by:
        signals.append((3, f"its capital {s.capital} is occupied by {s.name_of(s.capital_held_by)}"))
    sp = s.surrender or 0
    if sp >= 0.1:
        signals.append((3 if sp >= 0.6 else 2 if sp >= 0.3 else 1, f"surrender progress is at {pct(sp)}"))
    if s.enemies:
        odds = gamestate.odds(s) or 0
        if odds >= 1.5:     # weighs most once it tells: a world-wide enemy (UC's Xen) far away is only a worry
            signals.append((2 if odds >= 3 and sp >= 0.1 else 1, f"the enemy is {odds:.1f} times as strong"))
        lost = [o for o in s.occupied if o.core]
        if lost:
            signals.append((2 if len(lost) * 4 >= max(s.core_states, 1) else 1,
                            f"the enemy holds {len(lost)} of its {s.core_states or len(s.states)} core states"))
    if s.stability is not None and s.stability < 0.35:
        signals.append((2 if s.stability < 0.2 else 1, f"stability is only {pct(s.stability)}"))
    threats = list(dict.fromkeys(s.justified_by + [w.other for w in s.wargoals_against]))
    if threats and not s.enemies:
        signals.append((1, f"{s.names_of(threats)} prepare{'s' if len(threats) == 1 else ''} war on it"))
    try:
        hurt = spirits.harm([i.id for i in s.ideas])
    except Exception:                                   # the files unreadable: judge without the spirits
        hurt = []
    total = sum(h for _, h in hurt)
    if total >= 1:
        names = {i.id: i.name for i in s.ideas}
        worst = ", ".join(names.get(i, i) for i, _ in hurt[:4])
        signals.append((2 if total >= 2 else 1, f"crippling national spirits weigh on it ({worst})"))
    if not signals:
        return 0, []
    level = max(lv for lv, _ in signals)
    if level < 3 and sum(lv >= 1 for lv, _ in signals) >= 3:      # troubles compound
        level += 1
    return level, [r for _, r in sorted(signals, key=lambda x: -x[0])]


def short(s: gamestate.CountryState) -> str:
    """'dire: surrender progress is at 40%, ...' or '' when secure."""
    level, reasons = assess(s)
    return f"{LEVELS[level]}: {'; '.join(reasons)}" if level else ""


def text(s: gamestate.CountryState, voice: Optional[str] = None) -> str:
    """The YOUR SITUATION section for a country's own prompt ('' when secure). `voice`: how it shows in what this
    country writes, in the letter style."""
    level, reasons = assess(s)
    if not level:
        return ""
    why = "; ".join(reasons)
    out = f"YOUR SITUATION: {LEVELS[level].upper()}. {why[:1].upper() + why[1:]}. {MEANING[level]}"
    return out + (f"\nLet it show in what you write: {voice}" if voice else
                  "\nLet it show in what you write and in what you agree to.")
