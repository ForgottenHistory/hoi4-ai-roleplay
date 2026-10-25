"""Subject levels (HOI4 "autonomy states": dominion, colony, puppet, integrated puppet, reichskommissariat, a mod's
own levels...): what each one means, read from the game's and the enabled mods' common/autonomous_states files,
and which levels are open to a given subject, checked in the live game.

    from hoi4_rp import subjects
    subjects.level("autonomy_colony").name        # "Colony"
    subjects.rules_text("autonomy_colony", "the Raj", "Britain")
    subjects.ladder("RAJ", "ENG", "autonomy_colony")   # levels open to it, least to most free, and its progress

Which levels a subject can be on depends on both governments (a fascist overlord has Reichskommissariats where
others have integrated puppets), DLC, focuses and the mod. That is each level's `allowed` block, game script
that runs in the subject's scope, so instead of reimplementing it the live game evaluates it (bridge.query).

The files are the game's and the enabled mods' (gamefiles.py).

    python -m hoi4_rp subjects [TAG]   # list every level, or the ladder of one subject
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Dict, List, Optional, Tuple

from .gamefiles import files, game_dir, get as _get, num as _num, parse, to_script  # noqa: F401 (game_dir: CLI)

FOLDER = "common/autonomous_states"


# ---------------------------------------------------------------- levels

@dataclass
class Level:
    id: str
    freedom: float                       # min_freedom_level: higher is freer
    is_puppet: bool
    rules: Dict[str, bool] = field(default_factory=dict)
    modifiers: Dict[str, float] = field(default_factory=dict)
    allowed: str = ""                    # script trigger, subject scope
    only: List[str] = field(default_factory=list)   # allowed_levels_filter: the levels it may move to
    name: str = ""


@lru_cache(maxsize=None)
def levels() -> Dict[str, Level]:
    out: Dict[str, Level] = {}
    for path in files(FOLDER):
        try:
            with open(path, encoding="utf-8-sig", errors="replace") as f:
                tree = parse(f.read())
        except OSError:
            continue
        for key, _, body in tree:
            if key != "autonomy_state" or not isinstance(body, list) or not _get(body, "id"):
                continue
            rule = _get(body, "rule") or []
            mods = _get(body, "modifier") or []
            allowed = _get(body, "allowed")
            only = _get(body, "allowed_levels_filter")
            lv = Level(
                id=_get(body, "id"),
                freedom=_num(_get(body, "min_freedom_level")) or 0.0,
                is_puppet=_get(body, "is_puppet") == "yes",
                rules={k: v == "yes" for k, _, v in rule if isinstance(v, str) and v in ("yes", "no")},
                modifiers={k: _num(v) for k, _, v in mods if _num(v) is not None},
                allowed=to_script(allowed) if isinstance(allowed, list) else "",
                only=[k for k, _, _ in only] if isinstance(only, list) else [],
            )
            out[lv.id] = lv                                   # a later file with the same id wins
    _name_levels(out)
    return out


def _name_levels(lvls: Dict[str, Level]) -> None:
    try:
        from .gamestate import clean_loc, loc
        names = loc(*lvls) if lvls else []
    except Exception:                                         # no game running: fall back to the id
        names = []
    names += [""] * (len(lvls) - len(names))
    for lv, text in zip(lvls.values(), names):
        text = clean_loc(text) if text != lv.id else ""
        lv.name = text or lv.id.replace("autonomy_", "").replace("_", " ").title()


def level(level_id: str) -> Optional[Level]:
    return levels().get(level_id) or levels().get(f"autonomy_{level_id}")


def name(level_id: str) -> str:
    lv = level(level_id)
    return lv.name if lv else level_id.replace("autonomy_", "").replace("_", " ")


def _pct(x: float) -> str:
    return f"{round(x * 100)}%"


def rules_text(level_id: str, sub: str, lord: str) -> str:
    """What being on this level means, in plain words (empty if the level is unknown)."""
    lv = level(level_id)
    if not lv:
        return ""
    r, m = lv.rules, lv.modifiers
    parts = []
    if r.get("can_not_declare_war", True):
        parts.append(f"{sub} cannot declare war on its own")
    parts.append(f"it may refuse {lord}'s calls to war" if r.get("can_decline_call_to_war")
                 else f"it must join {lord}'s wars when called")
    if r.get("units_deployed_to_overlord"):
        parts.append(f"the divisions it raises are handed to {lord}")
    cic, mic = m.get("cic_to_overlord_factor", 0), m.get("mic_to_overlord_factor", 0)
    if cic or mic:
        parts.append(f"{lord} takes " + " and ".join(
            p for p in (f"{_pct(cic)} of its civilian factories" if cic else "",
                        f"{_pct(mic)} of its military factories" if mic else "") if p))
    if m.get("autonomy_manpower_share"):
        parts.append(f"{lord} recruits from {_pct(m['autonomy_manpower_share'])} of its manpower")
    if m.get("extra_trade_to_overlord_factor", 0) >= 0.5:
        parts.append(f"its resources go to {lord}'s trade first")
    if r.get("can_be_spymaster") is False and r.get("contributes_operatives") is False:
        parts.append("it runs no intelligence service of its own")
    return "; ".join(parts) + "."


# ---------------------------------------------------------------- one subject, live

@dataclass
class Ladder:
    current: str                         # level id
    progress: Optional[float]            # 0..1 toward the next freer level
    open: List[str]                      # level ids open to this subject, least to most free (incl. current)

    def neighbours(self) -> Tuple[Optional[str], Optional[str]]:
        """(the level below, the level above), or None at an end."""
        if self.current not in self.open:
            return None, None
        i = self.open.index(self.current)
        return (self.open[i - 1] if i > 0 else None), (self.open[i + 1] if i + 1 < len(self.open) else None)


def _scoped(trigger: str, sub: str, lord: str) -> str:
    """An allowed block runs with ROOT = the subject and FROM = the overlord; in a query ROOT is someone else."""
    return re.sub(r"\bFROM\b", lord, re.sub(r"\bROOT\b", sub, trigger))


def ladder(sub: str, lord: str, current: str) -> Ladder:
    """The levels open to `sub` under `lord` right now (each level's `allowed`, evaluated in the game), and how
    far its autonomy has progressed."""
    from .gamestate import StateError, query
    lvls = levels()
    candidates = list(lvls.values())
    cur = level(current)
    if cur and cur.only:
        candidates = [lv for lv in candidates if lv.id in cur.only or lv.id == cur.id]

    def run(group: List[Level]) -> Tuple[List[str], Optional[float]]:
        body = " ".join(f'if = {{ limit = {{ {_scoped(lv.allowed, sub, lord)} }} log = "AIRPL|{lv.id}" }}'
                        if lv.allowed else f'log = "AIRPL|{lv.id}"' for lv in group)
        lines = query(f'{sub} = {{ log = "AIRPR|[?THIS.autonomy_ratio]" {body} log = "AIRPE|" }}')
        if not any(x.startswith("AIRPE|") for x in lines):
            raise StateError("level query did not run")
        ratio = next((_num(x[6:]) for x in lines if x.startswith("AIRPR|")), None)
        return [x[6:].strip() for x in lines if x.startswith("AIRPL|")], ratio

    try:
        found, ratio = run(candidates)
    except StateError:                     # one level's script broke the query: check them one at a time
        found, ratio = [], None
        for lv in candidates:
            try:
                ids, r = run([lv])
                found += ids
                ratio = r if r is not None else ratio
            except StateError:
                pass
    if cur and cur.id not in found:
        found.append(cur.id)
    found = sorted(dict.fromkeys(found), key=lambda i: (lvls[i].freedom if i in lvls else 0, i))
    return Ladder(current=cur.id if cur else current, progress=ratio if ratio is not None and ratio >= 0 else None,
                  open=found)


def relation_text(sub_tag: str, sub: str, lord_tag: str, lord: str, current: str) -> str:
    """The full picture for a prompt: the level, what it means, the levels open to it and its progress."""
    lv = level(current)
    title = lv.name if lv else name(current)
    text = f"{sub}'s subject level is {title}: {rules_text(current, sub, lord) or '(its rules are unknown)'}"
    try:
        lad = ladder(sub_tag, lord_tag, current)
    except Exception as e:                                   # game state unavailable: the level alone
        print(f"  subject ladder for {sub_tag} unavailable: {e}")
        return text
    if len(lad.open) > 1:
        text += (" Levels open to it under these two governments, from most controlled to freest: "
                 + ", ".join(name(i) + (" (current)" if i == lad.current else "") for i in lad.open) + ".")
    down, up = lad.neighbours()
    if lad.progress is not None:
        text += f" Its autonomy has progressed {_pct(lad.progress)} of the way toward the next level."
    if up:
        text += (f" If that reaches 100% it can rise to {name(up)} ({rules_text(up, sub, lord)}")[:-1] + ")."
    elif lad.open:
        text += " It is already on the freest level open to it; beyond it lies only independence."
    if down:
        text += (f" If it falls to 0% {lord} can lower it to {name(down)} ({rules_text(down, sub, lord)}")[:-1] + ")."
    elif lad.open:
        text += f" It is already on the most controlled level open to it; the only step further down is annexation."
    return text
