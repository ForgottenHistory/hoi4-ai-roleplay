"""Troops on the border: a country sees the player's divisions massing on the tiles next to its own.

Players keep their armies inside their country so as not to look aggressive; a country now notices when they
don't. Tile against tile, not state against state:

    the map (once per run, ~1 s): which provinces touch, from map/provinces.bmp (a pixel of one province's colour
        beside another's), with the sea crossings of map/adjacencies.csv added and its impassable borders taken out;
        each province's state from history/states. The game's files, overridden by each enabled mod in turn.
    each CHECK_DAYS game days: where the player's divisions stand (location= in its live country block, through
        the bridge) and who controls each state (one query). A division stands on a country's border when its tile
        touches a tile that country controls and isn't that country's own. Divisions on a front (a tile touching a
        country at war with the player) count for no one: they are at war, not massing.

More than [world] border_divisions (default 5; 0 = off) on one country's border for HOLD_DAYS is a buildup: an
event (troops:massed) the country remembers and may write about, a threat the storyteller weighs (guarantees:
"troops"), and a line in its prompts for as long as it lasts. Growing to twice what it was told is told again.
Falling to half the limit or less is a withdrawal (troops:withdrawn): remembered, no letter.
Not counted: the player's faction, its subjects and overlord, and countries already at war with it.

Stored in the campaign: data["borders"] = {"checked": date, "at": {tag: {"since", "count", "told", "noticed"}}}.

    python -m hoi4_rp borders     where the player's divisions stand now, per neighbouring country
"""

from __future__ import annotations

import os
import re
import struct
from functools import lru_cache
from typing import Dict, List, Optional, Set, Tuple

from . import bridge, campaign, gamefiles, gamestate

CHECK_DAYS = 3        # game days between two counts
HOLD_DAYS = 7         # a buildup must stand this long before it is noticed (troops passing through aren't)
GROWTH = 2.0          # a noticed buildup grown this many times over is told again

Map = Tuple[Dict[int, Set[int]], Dict[int, int]]   # land province -> touching land provinces; province -> state


def _map_file(name: str) -> Optional[str]:
    """map/<name> as the game loads it: the last enabled mod that has one, else the game's."""
    path = None
    for root in ([gamefiles.game_dir()] if gamefiles.game_dir() else []) + [d for d, _ in gamefiles.mod_dirs()]:
        p = os.path.join(root, "map", name)
        if os.path.isfile(p):
            path = p
    return path


def _definitions(path: str) -> Tuple[Dict[bytes, int], Set[int]]:
    """map/definition.csv -> (BMP colour (b, g, r) -> province id, land provinces)."""
    colour, land = {}, set()
    with open(path, encoding="latin-1") as f:
        for line in f:
            x = line.strip().split(";")
            if len(x) < 5 or not x[0].isdigit() or x[0] == "0":
                continue
            try:
                pid, r, g, b = (int(v) for v in x[:4])
            except ValueError:
                continue
            colour[bytes((b, g, r))] = pid
            if x[4].strip().lower() == "land":
                land.add(pid)
    return colour, land


def _touching(path: str) -> Set[Tuple[bytes, bytes]]:
    """Pairs of colours side by side or one above the other in a 24-bit BMP. Rows are compared whole as integers,
    so only the few pixels where colours change are looked at (the whole map in about a second)."""
    with open(path, "rb") as f:
        d = f.read()
    off = struct.unpack_from("<I", d, 10)[0]
    w, h = struct.unpack_from("<ii", d, 18)
    if struct.unpack_from("<H", d, 28)[0] != 24:
        raise ValueError(f"{path}: not a 24-bit bitmap")
    stride = (w * 3 + 3) & ~3
    rows = [d[off + y * stride: off + y * stride + w * 3] for y in range(abs(h))]
    pairs: Set[Tuple[bytes, bytes]] = set()
    changed = re.compile(rb"[^\x00]")

    def scan(a: bytes, b: bytes) -> None:
        diff = (int.from_bytes(a, "big") ^ int.from_bytes(b, "big")).to_bytes(len(a), "big")
        last = -1
        for m in changed.finditer(diff):
            i = m.start() // 3 * 3
            if i != last:
                last = i
                pa, pb = a[i:i + 3], b[i:i + 3]
                pairs.add((pa, pb) if pa < pb else (pb, pa))

    for y, row in enumerate(rows):
        scan(row[:-3], row[3:])                    # each pixel against the one to its right
        if y + 1 < len(rows):
            scan(row, rows[y + 1])                 # and against the one below
    return pairs


def _states() -> Dict[int, int]:
    """province -> state, from history/states."""
    out: Dict[int, int] = {}
    for path in gamefiles.files("history/states"):
        try:
            with open(path, encoding="utf-8-sig", errors="replace") as f:
                text = re.sub(r"#[^\n]*", "", f.read())
        except OSError:
            continue
        sid = re.search(r"\bid\s*=\s*(\d+)", text)
        provs = re.search(r"\bprovinces\s*=\s*\{([^}]*)\}", text)
        if sid and provs:
            for p in provs.group(1).split():
                if p.isdigit():
                    out[int(p)] = int(sid.group(1))
    return out


@lru_cache(maxsize=1)
def world_map() -> Map:
    defs, bmp, extra = _map_file("definition.csv"), _map_file("provinces.bmp"), _map_file("adjacencies.csv")
    if not defs or not bmp:
        raise gamestate.StateError("map/provinces.bmp or map/definition.csv not found")
    colour, land = _definitions(defs)
    adj: Dict[int, Set[int]] = {p: set() for p in land}
    link = lambda a, b: (adj[a].add(b), adj[b].add(a)) if a in land and b in land and a != b else None
    for ca, cb in _touching(bmp):
        a, b = colour.get(ca), colour.get(cb)
        if a and b:
            link(a, b)
    if extra:
        with open(extra, encoding="latin-1") as f:
            for line in f:
                x = line.split(";")
                if len(x) < 3 or not x[0].strip().isdigit() or not x[1].strip().isdigit():
                    continue
                a, b, kind = int(x[0]), int(x[1]), x[2].strip().lower()
                if kind == "impassable":
                    adj.get(a, set()).discard(b)
                    adj.get(b, set()).discard(a)
                elif kind in ("sea", ""):          # a strait (or a land link the bitmap doesn't show)
                    link(a, b)
    return adj, _states()


# ------------------------------------------------------------------ the count

def divisions(tag: str) -> List[int]:
    """The province each of the country's divisions stands on (its live country block, through the bridge)."""
    block = bridge.country_block(tag)
    i = block.find("units={")
    if i < 0:
        return []
    out = []
    for chunk in block[i:].split("division={")[1:]:
        m = re.search(r"\blocation=(\d+)", chunk)
        if m:
            out.append(int(m.group(1)))
    return out


def controllers() -> Dict[int, str]:
    """state -> the tag controlling it now."""
    out = {}
    for line in gamestate.query('every_state = { log = "AIRPBC|[THIS.GetID]|[THIS.Controller.GetTag]" }'):
        f = line.split("|")
        if f[0] == "AIRPBC" and len(f) >= 3 and f[1].strip().isdigit():
            out[int(f[1])] = f[2].strip()
    return out


def count(player: str, at_war: Set[str]) -> Dict[str, int]:
    """{tag: the player's divisions standing on its border}. at_war: countries the player fights."""
    adj, state_of = world_map()
    held = controllers()
    who = lambda p: held.get(state_of.get(p, -1), "")
    out: Dict[str, int] = {}
    for p in divisions(player):
        if p not in adj:
            continue                               # at sea (transported), or a province the map doesn't know
        here = who(p)
        near = {who(q) for q in adj[p]} - {here, player, ""}
        if near & at_war or here in at_war:
            continue                               # on a front
        for t in near:
            out[t] = out.get(t, 0) + 1
    return out


# ------------------------------------------------------------------ the campaign

def _store(camp: campaign.Campaign) -> Dict:
    return camp.data.setdefault("borders", {"checked": "", "at": {}})


def limit() -> int:
    from .config import settings
    return max(0, getattr(settings().world, "border_divisions", 5))


def _days(key: str) -> int:
    from .letters import _days as days
    return days(key)


def _watched(player: str, snap: Dict) -> Tuple[Set[str], Set[str]]:
    """(the countries a buildup can be against, those the player is at war with)."""
    c = snap.get("countries", {})
    me = c.get(player, {})
    at_war = {t for w in snap.get("wars", []) for t in w.split("|") if player in w.split("|")} - {player}
    ok = {t for t, x in c.items() if t != player and not x.get("capitulated") and t not in at_war
          and not (me.get("faction") and x.get("faction") == me.get("faction"))
          and x.get("overlord") != player and me.get("overlord") != t}
    return ok, at_war


def tick(camp: campaign.Campaign, player: str, snap: Dict) -> List[Dict]:
    """Count the player's divisions on each border (every CHECK_DAYS) and return the events: buildups noticed,
    grown, or withdrawn."""
    n_max = limit()
    store, now = _store(camp), snap["date"]
    if not n_max or (store["checked"] and _days(now) - _days(store["checked"]) < CHECK_DAYS):
        return []
    ok, at_war = _watched(player, snap)
    try:
        found = count(player, at_war)
    except (gamestate.StateError, bridge.BridgeError, OSError, ValueError) as e:
        print(f"borders: {e}")
        return []
    store["checked"] = now
    c = snap.get("countries", {})
    name = lambda t: c.get(t, {}).get("name", t)
    events = []
    event = lambda kind, t, text: events.append({"date": now, "when": campaign.date_words(now), "type": kind,
                                                "tags": [player, t], "text": text, "major": False})
    for t in list(store["at"]):
        if t not in ok:
            del store["at"][t]                     # a war began, or a tie that explains it
    for t in ok:
        n, e = found.get(t, 0), store["at"].get(t)
        if n > n_max:
            if not e:
                store["at"][t] = {"since": now, "count": n, "told": 0, "noticed": ""}
                continue
            e["count"] = n
            if not e["noticed"] and _days(now) - _days(e["since"]) >= HOLD_DAYS:
                e["noticed"], e["told"] = now, n
                event("troops:massed", t, f"{name(player)} has massed {n} divisions on its border with {name(t)}.")
            elif e["noticed"] and n >= GROWTH * e["told"]:
                event("troops:massed", t, f"{name(player)} has reinforced its troops on the border with {name(t)}: "
                                          f"{n} divisions now, up from {e['told']}.")
                e["told"] = n
        elif e:
            e["count"] = n
            if not e["noticed"]:
                del store["at"][t]                 # gone before anyone noticed
            elif n <= n_max // 2:
                event("troops:withdrawn", t, f"{name(player)} has pulled its troops back from the border with "
                                             f"{name(t)}" + (f" ({n} divisions remain)." if n else "."))
                del store["at"][t]
    for e in events:
        print(f"  [{e['when']}] {e['text']}")
    return events


def massed(camp: campaign.Campaign) -> Dict[str, Dict]:
    """The buildups noticed and still standing: {tag: entry}."""
    return {t: e for t, e in _store(camp)["at"].items() if e.get("noticed")}


def describe(camp: campaign.Campaign, tag: str, them_name: str, me_name: str) -> str:
    """For `tag`'s prompts: the player's troops on its border now ('' if none to speak of)."""
    e = massed(camp).get(tag)
    if not e:
        return ""
    return (f"TROOPS ON YOUR BORDER: {me_name} keeps {e['count']} divisions on the tiles along your border, massed "
            f"there since {campaign.date_words(e['noticed'])}. A country that means its neighbours no harm keeps its "
            f"armies back from their borders; armies held this close are a warning, or the start of an invasion.")


def drop_after(camp: campaign.Campaign, key: str) -> None:
    """An earlier save was loaded: forget what was seen from then on (the next count sees the border afresh)."""
    store = _store(camp)
    store["checked"] = ""
    for t, e in list(store["at"].items()):
        if e["since"] >= key:
            del store["at"][t]
        elif e["noticed"] and e["noticed"] >= key:
            e["noticed"], e["told"] = "", 0


def main(args: List[str]) -> int:
    player = bridge.player_tag()
    from . import chronicle
    snap = chronicle.snapshot(player)
    ok, at_war = _watched(player, snap)
    found = count(player, at_war)
    total = len(divisions(player))
    name = lambda t: snap["countries"].get(t, {}).get("name", t)
    print(f"{name(player)}: {total} divisions; more than {limit()} on one border is a buildup")
    for t, n in sorted(found.items(), key=lambda x: -x[1]):
        print(f"  {n:4d}  {name(t)} ({t}){'' if t in ok else '  (not counted: ally, subject or overlord)'}")
    if at_war:
        print(f"  on a front, not counted: at war with {', '.join(sorted(at_war))}")
    return 0
