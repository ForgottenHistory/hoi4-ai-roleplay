"""The game's and the enabled mods' script files, and a lenient parser for them.

    from hoi4_rp import gamefiles
    gamefiles.files("common/autonomous_states")   # the files the game loads for that folder, in order
    gamefiles.parse(text)                          # [(key, operator, value or child list), ...]

The game folder is config [world] game_dir, else the running hoi4.exe, else the Steam libraries. Mods are the
launcher's enabled list (dlc_load.json) in load order: a mod file with the same name replaces the game's, and
replace_path drops the game's folder.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from functools import lru_cache
from typing import Dict, List, Optional, Tuple, Union

from .config import settings

DOCS_DIR = os.path.join(os.path.expanduser("~"), "Documents", "Paradox Interactive", "Hearts of Iron IV")


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


def get(node: Node, key: str):
    for k, _, v in node:
        if k.lower() == key:
            return v
    return None


def num(v) -> Optional[float]:
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


def mod_dirs() -> List[Tuple[str, List[str]]]:
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


def files(folder: str, ext: str = ".txt") -> List[str]:
    """The files the game loads from `folder` (e.g. "common/autonomous_states"): the game's, overridden by name
    by each mod in turn."""
    chosen: Dict[str, str] = {}
    roots = ([(game_dir(), [])] if game_dir() else []) + mod_dirs()
    for root, replaces in roots:
        if any(r.strip("/").lower() == folder.lower() for r in replaces):
            chosen.clear()
        d = os.path.join(root, *folder.split("/"))
        if os.path.isdir(d):
            for name in os.listdir(d):
                if name.lower().endswith(ext):
                    chosen[name] = os.path.join(d, name)
    return [chosen[n] for n in sorted(chosen)]
