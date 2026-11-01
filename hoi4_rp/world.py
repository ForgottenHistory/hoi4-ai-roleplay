"""The world the game is set in, for the LLM: vanilla history or a mod's world, from the lore files (lore/*.txt, one
per setting: vanilla, Kaiserreich, Half-Life...; see lore/README.md), plus optional setting notes from a file.
Chosen in config.ini [world]:

    setting = auto          auto (default) picks the lore file whose DETECT ideology groups the game has
    lore_file =             optional text file with extra setting notes, added to every prompt

The setting text opens every prompt (letters, replies, policies). The live game data that follows is
always authoritative; the setting only tells the LLM which history to reason from.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from typing import Dict, FrozenSet, Optional

from .config import settings

LORE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lore")


@dataclass(frozen=True)
class Setting:
    key: str                                   # file name without .txt
    name: str
    text: str
    detect: FrozenSet[str] = frozenset()       # ideology groups only this setting has
    voiceless: FrozenSet[str] = frozenset()    # ideology groups of creatures, not governments
    default: bool = False


def _parse(key: str, raw: str) -> Setting:
    """'KEY: value' header lines, a '---' line, then the lore text."""
    head, sep, body = raw.partition("\n---")
    if not sep:
        head, body = "", raw
    fields: Dict[str, str] = {}
    for line in head.splitlines():
        k, colon, v = line.partition(":")
        if colon and not line.lstrip().startswith("#"):
            fields[k.strip().upper()] = v.strip()
    ids = lambda k: frozenset(x.strip() for x in fields.get(k, "").replace(";", ",").split(",") if x.strip())
    return Setting(key, fields.get("NAME", key), body.lstrip("-").strip(), ids("DETECT"), ids("VOICELESS"),
                   fields.get("DEFAULT", "").lower() in ("yes", "true", "1"))


def lore() -> Dict[str, Setting]:
    """Every lore file, by key. Read afresh each time (they are small), so edits apply to the next prompt."""
    out: Dict[str, Setting] = {}
    try:
        names = sorted(n for n in os.listdir(LORE_DIR) if n.lower().endswith(".txt"))
    except OSError:
        names = []
    for n in names:
        try:
            with open(os.path.join(LORE_DIR, n), encoding="utf-8-sig") as f:
                out[n[:-4].lower()] = _parse(n[:-4].lower(), f.read())
        except OSError as e:
            print(f"lore file {n} could not be read: {e}")
    return out


_FALLBACK = Setting("vanilla", "Vanilla", "This game follows real history from 1936, but the live situation below may "
                    "already have diverged from it: trust the situation over your own historical knowledge.")

_LORE = ("Stay true to this world's lore in everything you write: who your country is decides how it speaks, what "
         "it knows and what it would never do.")


def detect(party_ideologies) -> Setting:
    """The lore file whose DETECT ideology groups the parties use (the most matches wins), else the default one."""
    found = set(party_ideologies)
    files = lore()
    best = max(files.values(), key=lambda s: len(s.detect & found), default=None)
    if best and best.detect & found:
        return best
    return next((s for s in files.values() if s.default), files.get("vanilla", _FALLBACK))


def setting_for(party_ideologies) -> Setting:
    chosen = settings().world.setting
    if chosen != "auto":
        files = lore()
        if chosen in files:
            return files[chosen]
        print(f"no lore file lore/{chosen}.txt; detecting the setting instead")
    return detect(party_ideologies)


def text(party_ideologies) -> str:
    """The setting paragraph(s) for the top of a prompt."""
    body = setting_for(party_ideologies).text
    notes = _lore_notes()
    return body + f"\n{_LORE}" + (f"\n\nSETTING NOTES FROM THE PLAYER:\n{notes}" if notes else "")


def voiceless_groups() -> FrozenSet[str]:
    """Ideology groups of creatures, not governments, from every lore file (VOICELESS lines)."""
    return frozenset().union(*(s.voiceless for s in lore().values()))


def voiceless(s) -> bool:
    """A country of creatures, not a government (its ruling party's ideology group is VOICELESS in a lore file)."""
    groups = voiceless_groups()
    return any(p.ruling and p.ideology in groups for p in s.parties)


@lru_cache(maxsize=None)
def _game_ideologies() -> FrozenSet[str]:
    """The ideology groups the loaded game defines."""
    from . import gamefiles
    found = set()
    for path in gamefiles.files("common/ideologies"):
        try:
            with open(path, encoding="utf-8-sig", errors="replace") as f:
                tree = gamefiles.parse(f.read())
        except OSError:
            continue
        for key, _, groups in tree:
            if key == "ideologies" and isinstance(groups, list):
                found |= {g for g, _, v in groups if isinstance(v, list)}
    return frozenset(found)


def voiceless_ideologies() -> tuple:
    """The voiceless ideology groups the loaded game defines (asking the game about one it doesn't know is an
    error)."""
    return tuple(sorted(_game_ideologies() & voiceless_groups()))


def voiceless_script() -> str:
    """Script logging "V|TAG" for a voiceless country (THIS = the country); empty when the game has none."""
    ideos = voiceless_ideologies()
    if not ideos:
        return ""
    cond = " ".join(f"has_government = {i}" for i in ideos)
    return f'if = {{ limit = {{ OR = {{ {cond} }} }} log = "V|[THIS.GetTag]" }} '


def _lore_notes() -> Optional[str]:
    path = settings().world.lore_file
    if not path:
        return None
    if not os.path.isabs(path):
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), path)
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip() or None
    except OSError as e:
        print(f"lore_file {path} could not be read: {e}")
        return None


# ---------------------------------------------------------------- world tension
# How tense the world is (0 to 1, shown as %), and what that lets each government do: HOI4 ties guarantees,
# factions, lend-lease, volunteers and war goals to it, with thresholds set per ideology and moved by national spirits
# (the USA's neutrality acts put a guarantee at 150%: never). Read live, so mods' values count.
#     [?global.threat]                 world tension
#     [?THIS.modifier@guarantee_tension] ...   a country's own thresholds (0 or less: always)

TENSION_RULES = [("guarantee_tension", "guarantee another country"), ("join_faction_tension", "join or form a faction"),
                 ("lend_lease_tension", "send lend-lease"), ("send_volunteers_tension", "send volunteers"),
                 ("generate_wargoal_tension", "justify war goals")]


def tension(tags) -> tuple:
    """(world tension 0..1, {tag: {rule: threshold}}) for these countries, from the running game."""
    from . import gamestate
    eff = 'log = "AIRPWT|[?global.threat]" ' + " ".join(
        f'{t} = {{ log = "AIRPWR|{t}|' + "|".join(f"[?THIS.modifier@{k}]" for k, _ in TENSION_RULES) + '" }'
        for t in dict.fromkeys(tags) if t)
    level, rules = 0.0, {}
    num = lambda x: float(x) if x.strip().replace(".", "", 1).replace("-", "", 1).isdigit() else 0.0
    for line in gamestate.query(eff):
        f = [x.strip() for x in line.split("|")]
        if f[0] == "AIRPWT" and len(f) > 1:
            level = num(f[1])
        elif f[0] == "AIRPWR" and len(f) == 2 + len(TENSION_RULES):
            rules[f[1]] = {k: num(v) for (k, _), v in zip(TENSION_RULES, f[2:])}
    return level, rules


def _pct(x: float) -> str:
    return f"{round(x * 100)}%"


def tension_line(name: str, level: float, rules: dict) -> str:
    """'France may now send lend-lease; needs more tension to guarantee another country (25%), ...'"""
    may = [label for k, label in TENSION_RULES if rules.get(k, 0) <= level]
    later = [f"{label} ({_pct(rules[k])})" for k, label in sorted(TENSION_RULES, key=lambda r: rules.get(r[0], 0))
             if level < rules.get(k, 0) <= 1]
    never = [label for k, label in TENSION_RULES if rules.get(k, 0) > 1]
    parts = [f"may now {', '.join(may)}" if may else "may do none of these yet"]
    if later:
        parts.append("needs more tension to " + ", ".join(later))
    if never:
        parts.append("may not " + ", ".join(never) + " at any tension (its national policy forbids it)")
    return f"- {name} " + "; ".join(parts) + "."


def tension_text(names: dict) -> str:
    """For prompts: world tension, and what it allows each of these countries ({tag: name})."""
    try:
        level, rules = tension(list(names))
    except Exception as e:                          # the game not answering: leave it out rather than fail a letter
        print(f"  world tension could not be read: {e}")
        return ""
    lines = [tension_line(n, level, rules[t]) for t, n in names.items() if t in rules]
    if settings().world.tension_rules:
        lines.append("Deals by letter keep to these rules too: a guarantee only once the guarantor may give one, a "
                     "faction only once both sides may join one. Offer, ask or promise nothing else of the kind.")
    return (f"WORLD TENSION: {_pct(level)}. It rises with every war goal justified, annexation and war, and falls "
            f"with guarantees and lasting peace; the game ties what governments may do to it (low tension: a quiet "
            f"world that would see guarantees, alliances and war preparations as alarmism; high: a world bracing for "
            f"war):\n" + "\n".join(lines))
