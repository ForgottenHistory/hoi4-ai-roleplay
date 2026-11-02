"""A country's course: the national focus it is working on and those its AI means to take next, read from the game.

The letters knew only the focuses a country had finished, so a government wrote in the moment, without a direction.
The game has one: the AI of a country follows its strategy plans (common/ai_strategy_plans), each an ordered list of
the focuses it will take (ai_national_focuses); the plans in force are those whose allowed and enable triggers hold
and whose abort trigger doesn't. A country without a plan in force takes what its focus tree opens next, by the
focuses' ai_will_do.

    path(tag, completed, current)     the focuses the country is likely to take next, in order (simulated through its
                                      focus tree: prerequisites met, mutually exclusive ones dropped)
    branches(tag, completed, current) the rival courses, when plans in force pull different ways (the game adds the
                                      plans up, so which way it goes isn't settled until the fork)
    text(state, own=True)             for prompts: the focus under way (days done, days left) and the likely next ones,
                                      or the fork it is undecided at; own=False: the focus under way only (what any
                                      country can see in the game)

    short(tag)                        one line of it, for the short prompts (AI-to-AI cards, storyteller, crises)
    track(camp)                       weekly: a country that took a side at a remembered fork becomes a chronicle
                                      event, type "fork" (news popups, the ledger's World tab, a fresh agenda)

    python -m hoi4_rp course [TAG ...]   print the course of these countries

The course is read from the running game and its files. Only the open forks are stored (data["forks"] = {"checked",
"open": {tag: {"date", "sides": [{"key": the focus where the side begins, "names"}]}}}), so that taking a side can be
noticed. The focus trees and plans are read once per run (a restart of `run` brings in changed mods).
"""

from __future__ import annotations

import re
import time
from functools import lru_cache
from typing import Dict, List, Optional, Set, Tuple

from . import bridge, gamefiles as g, gamestate

NEXT = 5                  # likely next focuses told
FORK_STEPS = 10           # how far each rival course is followed, to find where they split
FORK_SHOWN = 3            # focuses told for each side of a fork
OPEN = 4                  # focuses told as open next, for a country without a plan
DESC_CHARS = 260          # of the description of the focus under way
PLAN_CACHE_S = 600        # which plans are in force, re-checked after this long (real seconds)


class Focus:
    __slots__ = ("id", "prereqs", "exclusive", "days", "ai", "reward")

    def __init__(self, fid: str):
        self.id = fid
        self.prereqs: List[List[str]] = []      # all groups must be met; one focus of a group meets it
        self.exclusive: Set[str] = set()
        self.days = 70
        self.ai = 1.0                            # ai_will_do base factor
        self.reward: Set[str] = set()            # every word of its completion_reward (effects, tags, values)


def _words(node) -> Set[str]:
    out: Set[str] = set()
    for k, _, v in node:
        out.add(k)
        if isinstance(v, list):
            out |= _words(v)
        elif v:
            out.add(v)
    return out


# What a focus must hold to have caused a chronicle event about another country (besides naming its tag)
CAUSES = {
    "subject": {"puppet", "set_autonomy", "annex_country"},
    "independence": {"end_puppet", "release", "release_puppet", "set_autonomy"},
    "faction_joined": {"create_faction", "add_to_faction", "join_faction"},
    "faction_founded": {"create_faction"},
    "territory": {"transfer_state", "set_state_owner", "annex_country", "set_state_controller"},
}


def caused(fid: str, kind: str, other: str = "") -> bool:
    """Could the focus `fid` have brought about a chronicle event of `kind` with `other`? Its completion reward
    names `other`, or holds an effect of that kind. A focus that does it through an event it fires is missed."""
    f = focuses().get(fid)
    if not f:
        return False
    return bool(other and other in f.reward) or bool(f.reward & CAUSES.get(kind, set()))


def _read(path: str):
    with open(path, encoding="utf-8-sig", errors="replace") as f:
        return g.parse(f.read())


def _focus(body) -> Optional[Focus]:
    fid = g.get(body, "id")
    if not isinstance(fid, str):
        return None
    f = Focus(fid)
    for key, _, val in body:
        k = key.lower()
        if k == "prerequisite" and isinstance(val, list):
            group = [v for kk, _, v in val if kk.lower() == "focus" and isinstance(v, str)]
            if group:
                f.prereqs.append(group)
        elif k == "mutually_exclusive" and isinstance(val, list):
            f.exclusive |= {v for kk, _, v in val if kk.lower() == "focus" and isinstance(v, str)}
        elif k == "cost":
            f.days = int((g.num(val) or 10) * 7)
        elif k in ("completion_reward", "select_effect") and isinstance(val, list):
            f.reward |= _words(val)
        elif k == "ai_will_do" and isinstance(val, list):
            base = g.num(g.get(val, "factor"))
            f.ai = base if base is not None else (g.num(g.get(val, "base")) or 1.0)
    return f


@lru_cache(maxsize=None)
def focuses() -> Dict[str, Focus]:
    """Every national focus the game loads (country trees and shared focuses), by id."""
    out: Dict[str, Focus] = {}

    def walk(node):
        for key, _, val in node:
            if not isinstance(val, list):
                continue
            k = key.lower()
            if k in ("focus", "shared_focus", "joint_focus"):
                f = _focus(val)
                if f:
                    out[f.id] = f
            elif k == "focus_tree":
                walk(val)

    for path in g.files("common/national_focus"):
        walk(_read(path))
    for f in out.values():                       # exclusivity is often written on one side only
        for other in f.exclusive:
            if other in out:
                out[other].exclusive.add(f.id)
    return out


@lru_cache(maxsize=None)
def plans() -> List[Tuple[str, str, str, str, List[str]]]:
    """(id, allowed, enable, abort, focuses in order) of every AI strategy plan with focuses."""
    out = []
    for path in g.files("common/ai_strategy_plans"):
        for key, _, body in _read(path):
            if not isinstance(body, list):
                continue
            listed = g.get(body, "ai_national_focuses")
            if not isinstance(listed, list):
                continue
            ids = [k for k, op, _ in listed if not op]
            script = lambda name: g.to_script(v) if isinstance(v := g.get(body, name), list) else ""
            if ids:
                out.append((key, script("allowed"), script("enable"), script("abort"), ids))
    return out


_in_force: Dict[str, Tuple[float, List[str]]] = {}


def plans_of(tag: str) -> List[List[str]]:
    """The focus lists of the strategy plans in force for `tag` (several may be: the game adds them up)."""
    now = time.time()
    hit = _in_force.get(tag)
    if not hit or now - hit[0] > PLAN_CACHE_S:
        # One query for all plans: each logs its id if its triggers hold
        checks = [f"if = {{ limit = {{ {allowed} {enable} " + (f"NOT = {{ {abort} }} " if abort else "")
                  + f"}} log = \"AIRPPLAN|{pid}\" }}"
                  for pid, allowed, enable, abort, _ in plans() if enable]
        try:
            lines = gamestate.query(f"{tag} = {{ {' '.join(checks)} }}")
        except gamestate.StateError:
            return []                                    # not cached: asked again next time
        names = [x[9:].strip() for x in lines if x.startswith("AIRPPLAN|")]
        hit = _in_force[tag] = (now, names)
    by_id = {p[0]: p[4] for p in plans()}
    return [by_id[n] for n in hit[1] if n in by_id]


def _interleave(lists: List[List[str]]) -> List[str]:
    """One order from several plans, each roughly in time order: by position in its own plan."""
    out, seen = [], set()
    for i in range(max((len(x) for x in lists), default=0)):
        for x in lists:
            if i < len(x) and x[i] not in seen:
                seen.add(x[i])
                out.append(x[i])
    return out


def _ancestors(fid: str, tree: Dict[str, Focus], seen: Optional[Set[str]] = None) -> Set[str]:
    seen = set() if seen is None else seen
    for group in (tree[fid].prereqs if fid in tree else []):
        for p in group:
            if p not in seen:
                seen.add(p)
                _ancestors(p, tree, seen)
    return seen


def path(tag: str, completed: List[str], current: str = "", count: int = NEXT) -> Tuple[List[str], bool]:
    """(the focuses `tag` is likely to take after its current one, in order; whether a plan says so), all its plans in
    force taken together (see branches() for plans that pull different ways)."""
    lists = plans_of(tag)
    return _simulate(lists, completed, current, count), bool(lists)


def _simulate(lists: List[List[str]], completed: List[str], current: str = "", count: int = NEXT) -> List[str]:
    """The focuses taken one by one through the focus tree after the current one: the first planned focus whose
    prerequisites are met, else an open focus leading to one, else (no plan) the open focus with the highest
    ai_will_do. Focuses' `available` triggers are not checked."""
    tree = focuses()
    taken = set(completed) | ({current} if current else set())
    closed = set().union(*(tree[f].exclusive for f in taken if f in tree)) if taken else set()
    plan = [f for f in _interleave(lists) if f in tree]
    # The tree the country is on: focuses connected to what it has taken
    mine = {f.id for f in tree.values() if any(p in taken for grp in f.prereqs for p in grp)}
    out: List[str] = []

    def open_now(fid: str) -> bool:
        f = tree[fid]
        return (fid not in taken and fid not in closed
                and all(any(p in taken for p in grp) for grp in f.prereqs))

    for _ in range(count):
        pick = next((f for f in plan if open_now(f)), None)
        if pick is None:
            for f in plan:
                if f in taken or f in closed:
                    continue
                route = [a for a in _ancestors(f, tree) if open_now(a)]
                if route:
                    pick = max(route, key=lambda a: tree[a].ai)
                    break
        if pick is None and not plan:
            opened = [f for f in mine | {x for x in tree if not tree[x].prereqs and x in mine} if open_now(f)]
            if opened:
                pick = max(opened, key=lambda a: tree[a].ai)
        if pick is None:
            break
        out.append(pick)
        taken.add(pick)
        closed |= tree[pick].exclusive
        mine |= {f.id for f in tree.values() if any(pick in grp for grp in f.prereqs)}
    return out


def _clash(a: List[str], b: List[str]) -> bool:
    """Two paths can't both be taken: a focus of one excludes a focus of the other."""
    tree = focuses()
    return any(y in tree[x].exclusive for x in a if x in tree for y in b)


def _side(branch: List[str], others: List[List[str]]) -> List[str]:
    """A branch's own side of the fork: from its first focus that excludes one of another branch on, without the
    focuses the other branches take too."""
    tree = focuses()
    rival = {f for o in others for f in o}
    at = next((i for i, x in enumerate(branch) if x in tree and tree[x].exclusive & rival), None)
    return [] if at is None else [f for f in branch[at:] if f not in rival]


def branches(tag: str, completed: List[str], current: str = "", count: int = FORK_STEPS) -> List[List[str]]:
    """The rival courses of `tag`: each plan in force followed on its own, and plans whose paths don't clash joined
    into one branch. One branch (or none) when the plans agree: the game adds plans up, so where they split, which
    way the AI goes is not settled until it takes the focus at the fork."""
    groups: List[List[List[str]]] = []
    for plan in plans_of(tag):
        own = _simulate([plan], completed, current, count)
        for grp in groups:
            if not _clash(own, _simulate(grp, completed, current, count)):
                grp.append(plan)
                break
        else:
            groups.append([plan])
    return [_simulate(grp, completed, current, count) for grp in groups]


def fork(tag: str, completed: List[str], current: str = "") -> Optional[Tuple[List[str], List[List[str]]]]:
    """(the focuses taken either way, [each side's own focuses, from where they split]) when the plans in force for
    `tag` pull different ways, else None."""
    if len(plans_of(tag)) < 2:
        return None
    forks = branches(tag, completed, current)
    if len(forks) < 2:
        return None
    sides = [_side(b, [o for o in forks if o is not b]) for b in forks]
    if sum(1 for x in sides if x) < 2:
        return None
    shared = [f for f in forks[0] if all(f in b for b in forks[1:])]
    return shared, [x for x in sides if x]


def _names(ids, tag: str = "") -> Dict[str, str]:
    """Display names of focuses, those without one left out (scripted references filled in as `tag` sees them)."""
    ids = sorted(set(ids))
    return ({n.id: n.name for n in gamestate._named(ids, hide_missing=False, scope=tag) if n.name != n.id}
            if ids else {})


def focus_state(tag: str) -> Tuple[List[str], str]:
    """(completed focus ids, the one under way) of `tag`, from one read of its block."""
    block = gamestate._sub_block(bridge.country_block(tag.upper()), "focus", 2) or ""
    cur = re.search(r'\bcurrent="([^"]+)"', block)
    return re.findall(r'completed="([^"]+)"', block), cur.group(1) if cur else ""


@lru_cache(maxsize=1)
def _player() -> str:
    try:
        return gamestate.player_tag()
    except Exception:
        return ""


def short(tag: str, own: bool = True) -> str:
    """One line for the short prompts (AI-to-AI cards, the storyteller, crises): 'working on X; next: A, B' or
    'undecided between A and B'. own=False, and always for the player's country: the focus under way only."""
    try:
        completed, current = focus_state(tag)
        if tag == _player():
            own = False
        split = fork(tag, completed, current) if own else None
        nxt = path(tag, completed, current, count=3)[0] if own and not split else []
        named = _names(([current] if current else []) + nxt
                       + ([f for x in split[1] for f in x[:2]] if split else []), tag)
    except (gamestate.StateError, bridge.BridgeError):
        return ""
    bits = [f"working on the national focus {named[current]}"] if current in named else []
    if split:
        sides = [" then ".join(named[f] for f in x[:2] if f in named) for x in split[1]]
        bits.append("undecided between " + " or ".join(x for x in sides if x))
    elif nxt and any(f in named for f in nxt):
        bits.append("next: " + ", ".join(named[f] for f in nxt if f in named))
    return "; ".join(bits)


# ---------------------------------------------------------------- forks as events

TRACK_DAYS = 7            # game days between checks


def _forks(camp) -> Dict:
    return camp.data.setdefault("forks", {"checked": "", "open": {}})


def track(camp) -> List[Dict]:
    """Every TRACK_DAYS: the forks of the major powers and the countries with agendas or letters. A fork the country
    has now taken a side of (the focus where its side begins completed or under way) becomes a chronicle event, type
    "fork"; new forks are remembered. Returns the new events (not yet added)."""
    from . import campaign, chronicle
    snap = camp.snapshot or {}
    today = snap.get("date", "")
    st = _forks(camp)
    if not today or (st["checked"] and today < campaign.shift(st["checked"], TRACK_DAYS)):
        return []
    st["checked"] = today
    c = snap.get("countries", {})
    player = camp.data["player"]
    tags = ({t for t, x in c.items() if x.get("major")} | set(camp.data.get("agendas") or {}) | chronicle.WATCH)
    events = []
    for tag in sorted(t for t in tags if t in c and t != player and not c[t].get("capitulated")):
        try:
            completed, current = focus_state(tag)
            split = fork(tag, completed, current)
        except (gamestate.StateError, bridge.BridgeError):
            continue
        taken = set(completed) | {current}
        old = st["open"].get(tag)
        chosen = next((x for x in old["sides"] if x["key"] in taken), None) if old else None
        if chosen:
            name = c[tag].get("name", tag)
            toward = f" (toward {', '.join(chosen['names'][1:])})" if len(chosen["names"]) > 1 else ""
            away = " and ".join(x["names"][0] for x in old["sides"] if x is not chosen and x["names"])
            text = (f"{name} settled its course: it took up {chosen['names'][0]}{toward}, turning away from "
                    f"{away}.")
            events.append({"date": today, "when": campaign.date_words(today), "type": "fork", "tags": [tag],
                           "text": text, "major": bool(c[tag].get("major"))})
            print(f"  [course] {text}")
        if split:
            sides = split[1]
            named = _names((f for x in sides for f in x[:FORK_SHOWN]), tag)
            keys = [x[0] for x in sides]
            if not old or chosen or [x["key"] for x in old["sides"]] != keys:
                st["open"][tag] = {"date": today, "sides": [
                    {"key": x[0], "names": [named[f] for f in x[:FORK_SHOWN] if f in named] or [x[0]]}
                    for x in sides]}
        else:
            st["open"].pop(tag, None)
    return events


def drop_after(camp, key: str) -> None:
    """An earlier save was loaded: forks first seen from then on are forgotten, and the next check reads them again."""
    st = _forks(camp)
    st["open"] = {t: x for t, x in st["open"].items() if x["date"] < key}
    st["checked"] = ""


def open_next(completed: List[str], current: str = "") -> List[str]:
    """The focuses open once the current one is done (for a country without a plan): those it unlocks first, then by
    ai_will_do."""
    tree = focuses()
    taken = set(completed) | ({current} if current else set())
    closed = set().union(*(tree[f].exclusive for f in taken if f in tree)) if taken else set()
    found = [f for f in tree.values()
             if f.id not in taken and f.id not in closed and f.prereqs
             and all(any(p in taken for p in grp) for grp in f.prereqs)]
    unlocked = lambda f: any(current in grp for grp in f.prereqs)       # those the current one opens come first
    return [f.id for f in sorted(found, key=lambda f: (not unlocked(f), -f.ai))][:OPEN]


def _short(text: str, n: int) -> str:
    text = " ".join(text.split())
    if len(text) <= n:
        return text
    cut = text[:n].rsplit(". ", 1)[0]
    return (cut if len(cut) > n // 2 else text[:n].rsplit(" ", 1)[0]) + "..."


def now_line(s: gamestate.CountryState, desc: bool = True) -> str:
    """'Rally the Nation (18 of 35 days done)', with the start of its description."""
    if not s.current_focus:
        return ""
    f = focuses().get(s.current_focus)
    named = gamestate._named([s.current_focus], hide_missing=False, scope=s.tag)[0]
    done = f"{s.focus_progress} of {f.days} days done" if f and s.focus_progress is not None else "under way"
    about = f": {_short(gamestate.clean_loc(named.description), DESC_CHARS)}" if desc and named.description else ""
    return f"{named.name} ({done}){about}"


def text(s: gamestate.CountryState, own: bool = True) -> str:
    """For prompts. own: `s`'s own course (the focus under way and the likely next ones); else what another country
    sees of it (the focus under way)."""
    try:
        now = now_line(s, desc=own)
        if not own:
            return f"{s.name} is working on the national focus {now}." if now else ""
        completed = [f.id for f in s.focuses]
        split = fork(s.tag, completed, s.current_focus)
        nxt, planned = path(s.tag, completed, s.current_focus)
        if not planned:
            nxt = open_next(completed, s.current_focus) if s.current_focus else nxt
        named = _names(set(nxt) | ({f for x in split[1] for f in x} | set(split[0]) if split else set()), s.tag)
    except (gamestate.StateError, bridge.BridgeError):
        return ""
    names = lambda fs: [named[f] for f in fs if f in named]
    if not now and not names(nxt):
        return ""
    lines = ["YOUR GOVERNMENT'S COURSE (its national focuses: where it is headed in the months to come; events may "
             "change it):"]
    lines.append(f"- Now: {now}" if now else "- No national focus under way.")
    if split:
        shared, sides = split
        if names(shared):
            lines.append(f"- Either way: {', '.join(names(shared)[:FORK_SHOWN])}.")
        lines.append("- Undecided between rival courses, and the choice is not made yet: "
                     + "; or ".join(", ".join(names(x)[:FORK_SHOWN]) for x in sides) + ".")
        lines.append("Your government is torn between them: let that show (factions pulling different ways, "
                     "caution about committing), without naming focuses as game mechanics.")
        return "\n".join(lines)
    if names(nxt) and planned:
        lines.append(f"- Then, as it plans: {', '.join(names(nxt))}.")
    elif names(nxt):
        lines.append(f"- Open to it next: {', '.join(names(nxt))} (it has not chosen).")
    lines.append("Let this direction show in what you want and say, without naming focuses as game mechanics.")
    return "\n".join(lines)


def main(args: List[str]) -> int:
    tags = [a.upper() for a in args] or [bridge.player_tag()]
    for tag in tags:
        s = gamestate.country(tag)
        completed = [f.id for f in s.focuses]
        nxt, planned = path(tag, completed, s.current_focus)
        print(f"== {s.name} ({tag})")
        print(text(s) or "(no course)")
        print(f"  ids: now {s.current_focus or '-'}; next {nxt} ({'plan' if planned else 'tree'})")
        if len(plans_of(tag)) > 1:
            for b in branches(tag, completed, s.current_focus):
                print(f"  branch: {b}")
        if not planned:
            print(f"  open next: {open_next(completed, s.current_focus)}")
    return 0
