"""What a country's own game AI is set on doing toward other countries, read from its AI strategies, so that what its
government writes doesn't contradict what its state does.

The direction runs the other way: our agendas and storyteller decide where a country goes, and steering pushes that
into its AI. So this is for realism in letters and AI-to-AI cards, and as feedback on whether the pushes took hold;
never as input that bends a country's aims (agendas.py doesn't read it).

The game keeps them in the country's save-format block, under ai = { }:
    ai_strategy = { type=1 id=5 value=100 }          worked out by the AI (ideology, threat, its strategy files)
    persistent_strategy = { type=1 id=5 value=50 }   added by script (add_ai_strategy), by the game's files or ours
`type` is the strategy, `id` the country it is about: its place in the game's country list (common/country_tags,
in load order, counted from 1), dynamic tags included. Both decoded live (1936, by adding each strategy type with a
marked value and reading it back): see TYPES. Several entries of a kind add up.

What steering.py pushed (the letter policy, commitments made by letter) is taken out again: the prompts already
tell the policy, and this is what the country's AI does of its own accord. The diplomatic-action strategies (types
73, 74: a hashed action as target) are left out for the same reason.

Each kind has a threshold, below which it is the AI's background (every fascist state antagonizes every democracy a
little, every country contains every other a little).

    read(tag)                 {kind: {tag: value}} over the thresholds, the player's pushes taken out
    text(s, other, camp)      for a letter prompt: what the country's AI is doing, and toward `other` in particular
    short(s, other, camp)     one clause for the AI-to-AI cards: its AI's stance toward `other`, or ''

    python -m hoi4_rp intent [TAG ...]   print it
"""

from __future__ import annotations

import re
import time
from functools import lru_cache
from typing import Dict, List, Optional, Set

from . import bridge, gamefiles as g, gamestate

TYPES = {0: "conquer", 1: "antagonize", 2: "befriend", 3: "protect", 5: "influence", 6: "alliance", 13: "contain",
         14: "invade", 39: "send_volunteers_desire", 41: "support", 43: "prepare_for_war", 46: "declare_war"}

# kind: (words, threshold); shown in this order. alliance below NO_ALLIANCE: it will not ally with them.
KINDS = {
    "declare_war": ("means to declare war on", 1),
    "prepare_for_war": ("prepares for war with", 10),
    "conquer": ("means to conquer", 10),
    "invade": ("plans to invade", 10),
    "antagonize": ("works against", 50),
    "contain": ("seeks to contain", 20),
    "alliance": ("seeks an alliance with", 20),
    "protect": ("protects", 30),
    "support": ("supports", 30),
    "send_volunteers_desire": ("wants to send volunteers to", 50),
    "befriend": ("courts", 20),
    "influence": ("seeks influence over", 30),
}
NO_ALLIANCE = -1000          # scripted refusals (Germany never allies with Austria: -2000); -500 is ideology filler
SHOWN = 6                    # countries named per kind; the rest counted
CACHE_S = 60


@lru_cache(maxsize=None)
def tags_by_index() -> Dict[int, str]:
    """The game's country list: place (from 1) -> tag."""
    order: List[str] = []
    seen: Set[str] = set()
    for path in g.files("common/country_tags"):
        with open(path, encoding="utf-8-sig", errors="replace") as f:
            for key, op, _ in g.parse(f.read()):
                if op and re.fullmatch(r"[A-Z0-9]{3}", key) and key not in seen:
                    seen.add(key)
                    order.append(key)
    return {i + 1: t for i, t in enumerate(order)}


_cache: Dict[str, tuple] = {}


def _raw(tag: str) -> Dict[str, Dict[str, int]]:
    """{kind: {tag: summed value}} of every strategy in TYPES, ai and persistent together."""
    hit = _cache.get(tag)
    if hit and time.time() - hit[0] < CACHE_S:
        return hit[1]
    ai = gamestate._sub_block(bridge.country_block(tag), "ai", 2) or ""
    index = tags_by_index()
    out: Dict[str, Dict[str, int]] = {}
    for body in re.findall(r"(?:persistent_strategy|ai_strategy)=\{ ([^}]*)\}", ai):
        f = dict(re.findall(r"(\w+)=(\S+)", body))
        kind = TYPES.get(int(f.get("type", -1)))
        cid = index.get(int(f["id"])) if f.get("id", "").isdigit() else None
        if kind and cid and "target" not in f:
            try:
                out.setdefault(kind, {})[cid] = out.get(kind, {}).get(cid, 0) + int(float(f.get("value", 0)))
            except ValueError:
                pass
    _cache[tag] = (time.time(), out)
    return out


def read(tag: str, camp=None) -> Dict[str, Dict[str, int]]:
    """{kind: {tag: value}} of what `tag`'s AI is set on, over the thresholds, strongest first; steering.py's pushes
    (in `camp`) taken out."""
    raw = {k: dict(v) for k, v in _raw(tag).items()}
    if camp is not None:
        from . import steering
        entry = (camp.data.get("steering") or {}).get(tag)
        if entry:
            for (cid, kind, extra), v in steering._totals(entry, camp.data["player"]).items():
                if not extra and kind in raw and cid in raw[kind]:
                    raw[kind][cid] -= v
    out: Dict[str, Dict[str, int]] = {}
    for kind, (_, least) in KINDS.items():
        found = {t: v for t, v in raw.get(kind, {}).items() if t != tag and v >= least}
        if found:
            out[kind] = dict(sorted(found.items(), key=lambda x: -x[1]))
    refused = {t: v for t, v in raw.get("alliance", {}).items() if t != tag and v <= NO_ALLIANCE}
    if refused:
        out["no_alliance"] = refused
    return out


def _names(found: Dict[str, int], s: gamestate.CountryState, first: str, majors: Set[str], name) -> str:
    """The countries of one kind: `first` (the correspondent), the major powers and neighbours, then the strongest;
    SHOWN named and the rest counted."""
    rank = lambda t: (t != first, t not in majors, t not in s.neighbors, -found[t])
    order = sorted(found, key=rank)
    shown = ", ".join(name(t) for t in order[:SHOWN])
    rest = len(order) - SHOWN
    return shown + (f" and {rest} other{'s' if rest > 1 else ''}" if rest > 0 else "")


def _name_of(s: gamestate.CountryState, camp):
    countries = ((camp.snapshot if camp is not None else None) or {}).get("countries", {})
    return lambda t: s.names.get(t) or countries.get(t, {}).get("name") or t


def text(s: gamestate.CountryState, other: str = "", camp=None) -> str:
    """For `s`'s letter prompt: what its own AI is doing in the world, and toward `other`."""
    try:
        found = read(s.tag, camp)
        majors = gamestate._majors()
    except (gamestate.StateError, bridge.BridgeError):
        return ""
    if not found:
        return ""
    name = _name_of(s, camp)
    lines = [f"- {KINDS[k][0] if k in KINDS else 'will not ally with'}: {_names(v, s, other, majors, name)}"
             for k, v in found.items() if k in KINDS or k == "no_alliance"]
    toward = [KINDS[k][0] if k in KINDS else "will not ally with" for k, v in found.items() if other in v]
    head = ("WHAT YOUR STATE IS DOING (your country's ministries, general staff and diplomats, as the game runs them; "
            "this is what your country actually does, whatever is written in letters):")
    tail = (f"Toward {name(other)} in particular, your state {' and '.join(toward)} it." if toward and other else
            f"Toward {name(other)}, your state has no particular design." if other else "")
    return "\n".join([head] + lines + ([tail] if tail else []) + [
        "Your letters may soften or hide this, as diplomats do, but don't promise the opposite of it unless your "
        "government truly means to change course."])


def short(s: gamestate.CountryState, other: str, camp=None) -> str:
    """One clause for an AI-to-AI card: what `s`'s AI does toward `other`, or ''."""
    try:
        found = read(s.tag, camp)
    except (gamestate.StateError, bridge.BridgeError):
        return ""
    toward = [KINDS[k][0] if k in KINDS else "will not ally with" for k, v in found.items() if other in v]
    if not toward:
        return ""
    name = s.names.get(other) or gamestate.names_for([other]).get(other) or other
    return f"its state {' and '.join(toward)} {name}"


def main(args: List[str]) -> int:
    tags = [a.upper() for a in args] or ["GER", "ENG", "FRA", "ITA", "SOV", "USA", "JAP"]
    player = gamestate.player_tag()
    from . import campaign
    camp = campaign.Campaign(campaign.read_id(player), player)
    for tag in tags:
        s = gamestate.country(tag)
        print(f"== {s.name} ({tag})")
        print(text(s, player, camp) or "(nothing over the thresholds)")
    return 0
