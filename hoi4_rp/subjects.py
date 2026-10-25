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

Files: the game folder is config [world] game_dir, else the running hoi4.exe, else the Steam libraries. Mods
are the launcher's enabled list (dlc_load.json) in load order: a mod file with the same name replaces the
game's, and replace_path drops the game's folder.

    python -m hoi4_rp subjects [TAG]   # list every level, or the ladder of one subject
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Dict, List, Optional, Tuple, Union

from .config import settings

APP_ID = "394360"
DOCS_DIR = os.path.join(os.path.expanduser("~"), "Documents", "Paradox Interactive", "Hearts of Iron IV")
FOLDER = "common/autonomous_states"


# ---------------------------------------------------------------- Paradox script

Node = List[Tuple[str, str, Union[str, list]]]     # (key, operator, value or child block)

_TOKEN = re.compile(r'"(?:[^"\\]|\\.)*"|[<>!]?=|[<>]|[{}]|[^\s{}=<>"#]+')


def parse(text: str) -> Node:
    """Lenient parser for game script: `key = value`, `key = { ... }`, `key > 1`, bare list items."""
    text = re.sub(r"#[^\n]*", "", text)
    tokens = _TOKEN.findall(text)
    pos = 0

    def block() -> Node:
        nonlocal pos
        out: Node = []
        while pos < len(tokens):
            tok = tokens[pos]
            pos += 1
            if tok == "}":
                return out
            if tok == "{":                                   # anonymous block
                out.append(("", "", block()))
                continue
            if pos < len(tokens) and re.fullmatch(r"[<>!]?=|[<>]", tokens[pos]):
                op = tokens[pos]
                pos += 1
                if pos < len(tokens) and tokens[pos] == "{":
                    pos += 1
                    out.append((tok, op, block()))
                elif pos < len(tokens):
                    out.append((tok, op, tokens[pos]))
                    pos += 1
            else:
                out.append((tok, "", ""))                    # bare item (e.g. inside allowed_levels_filter)
        return out

    return block()


def to_script(node: Node) -> str:
    parts = []
    for key, op, val in node:
        if not op:
            parts.append(key if not isinstance(val, list) else "{ " + to_script(val) + " }")
        elif isinstance(val, list):
            parts.append(f"{key} {op} {{ {to_script(val)} }}")
        else:
            parts.append(f"{key} {op} {val}")
    return " ".join(parts)


def _get(node: Node, key: str):
    for k, _, v in node:
        if k.lower() == key:
            return v
    return None


def _num(v) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- game and mod files

def _steam_libraries() -> List[str]:
    roots = [r"C:\Program Files (x86)\Steam", r"C:\Program Files\Steam"]
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as k:
            roots.insert(0, winreg.QueryValueEx(k, "SteamPath")[0])
    except OSError:
        pass
    libs = []
    for root in roots:
        vdf = os.path.join(root, "steamapps", "libraryfolders.vdf")
        if os.path.exists(vdf):
            with open(vdf, encoding="utf-8", errors="replace") as f:
                libs += [p.replace("\\\\", "\\") for p in re.findall(r'"path"\s+"([^"]+)"', f.read())]
        libs.append(root)
    return list(dict.fromkeys(os.path.normpath(p) for p in libs))


@lru_cache(maxsize=None)
def game_dir() -> Optional[str]:
    configured = settings().world.game_dir
    if configured and os.path.isdir(configured):
        return configured
    try:   # the running game
        out = subprocess.run(["powershell", "-NoProfile", "-Command",
                              "(Get-Process hoi4 -ErrorAction SilentlyContinue | Select-Object -First 1).Path"],
                             capture_output=True, text=True, timeout=15).stdout.strip()
        if out and os.path.isfile(out):
            return os.path.dirname(out)
    except (OSError, subprocess.SubprocessError):
        pass
    for lib in _steam_libraries():
        d = os.path.join(lib, "steamapps", "common", "Hearts of Iron IV")
        if os.path.isdir(os.path.join(d, "common")):
            return d
    return None


def _mod_dirs() -> List[Tuple[str, List[str]]]:
    """(folder, replace_paths) of each enabled mod, in load order."""
    try:
        with open(os.path.join(DOCS_DIR, "dlc_load.json"), encoding="utf-8") as f:
            enabled = json.load(f).get("enabled_mods", [])
    except (OSError, ValueError):
        return []
    out = []
    for rel in enabled:
        try:
            with open(os.path.join(DOCS_DIR, rel), encoding="utf-8", errors="replace") as f:
                desc = f.read()
        except OSError:
            continue
        m = re.search(r'^\s*path\s*=\s*"([^"]+)"', desc, re.M)
        if not m:
            continue                                         # zipped (archive=) mods aren't read
        path = m.group(1) if os.path.isabs(m.group(1)) else os.path.join(DOCS_DIR, m.group(1))
        out.append((os.path.normpath(path), re.findall(r'replace_path\s*=\s*"([^"]+)"', desc)))
    return out


def _files() -> List[str]:
    """The level files the game loads: the game's, overridden by name by each mod in turn."""
    chosen: Dict[str, str] = {}
    roots = ([(game_dir(), [])] if game_dir() else []) + _mod_dirs()
    for root, replaces in roots:
        if any(r.strip("/").lower() == FOLDER for r in replaces):
            chosen.clear()
        d = os.path.join(root, *FOLDER.split("/"))
        if os.path.isdir(d):
            for name in os.listdir(d):
                if name.endswith(".txt"):
                    chosen[name] = os.path.join(d, name)
    return [chosen[n] for n in sorted(chosen)]


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
    for path in _files():
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
