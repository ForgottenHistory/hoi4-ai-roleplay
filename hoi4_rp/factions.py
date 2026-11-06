"""Factions between AI countries by agreement ([ai_diplomacy] factions = ours; default game).

The game's AI may then not invite, ask to join, expel or leave (gates.py); the storyteller forms and breaks the
world's factions instead, one move a month at most (storyteller.FactionMonth), from what Python finds possible here.
Focuses, events and decisions still found, fill and dissolve factions on their own (the historical ones among them):
the storyteller sees the result and works around it.

    join     a country outside any faction and a faction's leader: an aim against the faction's foe, at war with
             its enemy, backing the leader, threatened by the same country; the same ideology helps, another one
             needs a war or a threat in common
    found    two countries outside any faction with a foe, a war or a threat in common, the founder a major power or
             the stronger, never one whose own national focuses are about to found a faction (the Axis, the
             Comintern... would be pre-empted)
    break    a member and its leader that no longer belong together: an aim against each other, backing the
             faction's enemy (or the leader backing the member's), another ideology without a war in common (two of
             democratic, communist, fascist count double, and the two calling each other friends doesn't cancel
             that); the member leaves or the leader expels it.
             Rare: one in the world every BREAK_GAP_DAYS

Size: a faction led by an AI country holds at most cap() of the world's industry (20% in a calm world, 35% at full
tension) and two major powers, unless the joiner is already at war beside it (room(), checked for letters and
exchanges too through actions.possible); a fuller faction scores lower here, so the world splits into blocs.

Distance as for guarantees (guarantees.reach): near (capitals on one continent), present (land there or a border),
far (only at war together, or with an aim to back the leader). The faction DLC's joining rules (same continent,
ideology...) are checked when a faction has them (has_active_rule; the rule's own trigger).

A new faction's name is a loc key airp_faction_N set through the bridge (name_key()); the names are kept in
data/faction_names.json and set again when `run` loads a game (restore()). The storyteller names the factions it
founds; one founded by letter takes an unused name the game has for its leader's ideology.
"""

from __future__ import annotations

import json
import os
import re
from functools import lru_cache
from typing import Dict, List, Optional, Tuple

from . import bridge, campaign, gamefiles, gamestate

NAMES_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "faction_names.json")
BREAK_GAP_DAYS = 180         # the world sees one faction broken by us this often at most
CLASHING = {"democratic", "communism", "fascism"}    # two of these in one faction count double for a break
CANDIDATES = 10
PER_COUNTRY = 2
REACH_WEIGHT = {"near": 1.0, "present": 0.5, "far": 0.2}
FOUNDING = {"create_faction", "create_faction_from_template"}
SHARE_CALM = 0.20            # the most of the world's industry a faction led by the AI holds in a calm world,
SHARE_TENSE = 0.35           # and at full world tension (linear between)
MAJORS = 2                   # major powers in one faction at most


# ---------------------------------------------------------------- how large a faction may grow
# A faction led by an AI country holds at most cap(tension) of the world's factories (civilian and military) and
# MAJORS major powers: in peacetime blocs stay small, and they grow as war nears. A joiner already at war with one of
# the faction's enemies is let in regardless. The player's own faction is never capped (their buttons are theirs).

def the(name: str) -> str:
    """'the Axis', and 'The European Combine' left as it is (no 'the The')."""
    return name if name.lower().startswith("the ") else f"the {name}"


def cap(tension: float) -> float:
    return SHARE_CALM + (SHARE_TENSE - SHARE_CALM) * max(0.0, min(1.0, tension))


def too_large(share: float, majors: int, joiner_major: bool, tension: float) -> str:
    """Why a faction holding `share` of the world's industry and `majors` major powers with the joiner can't take it
    in ('' if it can)."""
    if joiner_major and majors > MAJORS:
        return f"the faction already has {majors - 1} major powers, and {MAJORS} is the most one holds"
    if share > cap(tension):
        return (f"with it the faction would hold {share:.0%} of the world's industry, more than the "
                f"{cap(tension):.0%} a faction holds at this world tension")
    return ""


def room(leader: str, joiner: str, tension: float) -> str:
    """Why `joiner` can't join `leader`'s faction, or found one with it ('' if it can). One game query."""
    try:
        if (bridge.trigger(f"{leader} = {{ is_ai = no }}")
                or bridge.trigger(f"any_country = {{ has_war_with = {leader} has_war_with = {joiner} }}")):
            return ""
        lines = gamestate.query(
            f'every_country = {{ limit = {{ exists = yes }} '
            f'log = "AIRPFS|[THIS.GetTag]|[?THIS.num_of_civilian_factories]|[?THIS.num_of_military_factories]" '
            f'if = {{ limit = {{ OR = {{ tag = {leader} tag = {joiner} is_in_faction_with = {leader} }} }} '
            f'log = "AIRPFM|[THIS.GetTag]" if = {{ limit = {{ is_major = yes }} log = "AIRPFJ|[THIS.GetTag]" }} }} }}')
    except (gamestate.StateError, bridge.BridgeError):
        return ""                                      # the game not answering: the game's own checks still hold
    num = lambda x: float(x) if x.strip().replace(".", "", 1).isdigit() else 0.0
    power, members, majors = {}, set(), set()
    for line in lines:
        f = [x.strip() for x in line.split("|")]
        if f[0] == "AIRPFS" and len(f) >= 4:
            power[f[1]] = num(f[2]) + num(f[3])
        elif f[0] == "AIRPFM" and len(f) >= 2:
            members.add(f[1])
        elif f[0] == "AIRPFJ" and len(f) >= 2:
            majors.add(f[1])
    total = sum(power.values())
    if not total:
        return ""
    return too_large(sum(power.get(t, 0) for t in members) / total, len(majors), joiner in majors, tension)


# ---------------------------------------------------------------- names

def _names() -> Dict:
    try:
        with open(NAMES_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    data.setdefault("names", {})
    data.setdefault("pending", {})
    return data


def _save(data: Dict) -> None:
    os.makedirs(os.path.dirname(NAMES_FILE), exist_ok=True)
    with open(NAMES_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1, ensure_ascii=False)


def _clean(name: str) -> str:
    name = re.sub(r"[\"\[\]§$\\]", "", name or "").strip(" .'*_")
    return re.sub(r"(?i)^the\s+", "", name)[:60]


def propose_name(a: str, b: str, name: str) -> None:
    """The storyteller's name for the faction a and b may found (either may end up its leader)."""
    name = _clean(name)
    if name:
        data = _names()
        data["pending"][campaign.pair(a, b)] = name
        _save(data)


def _set(key: str, name: str) -> None:
    bridge.send(f"bridge.setloc {key} {name}")


def name_key(leader: str, member: str) -> Tuple[str, str]:
    """(loc key, name) for the faction `leader` founds now with `member`: the storyteller's name for the pair, or an
    unused one of the game's names for the leader's ideology. The key's text is set in the game before the faction
    is made (the game reads it then)."""
    data = _names()
    name = data["pending"].pop(campaign.pair(leader, member), "") or _ideology_name(leader, data)
    n = 1 + max([int(k.rsplit("_", 1)[1]) for k in data["names"] if k.rsplit("_", 1)[1].isdigit()] or [0])
    key = f"airp_faction_{n}"
    data["names"][key] = name
    _save(data)
    _set(key, name)
    return key, name


def _ideology_name(leader: str, data: Dict) -> str:
    used = set(data["names"].values()) | {r.split("|", 1)[1] for r in _faction_names()}
    group = ideologies([leader]).get(leader, "")
    keys = _dynamic_names().get(group, [])
    try:
        texts = gamestate.loc(*keys) if keys else []
    except gamestate.StateError:
        texts = []
    for t in texts:
        if t and not t.startswith("FACTION_NAME") and _clean(t) not in used:
            return _clean(t)
    try:
        own = gamestate.query(f'{leader} = {{ log = "AIRPFA|[THIS.GetAdjective]" }}')
        adj = own[0].split("|", 1)[1] if own else leader
    except gamestate.StateError:
        adj = leader
    return f"{adj} Alliance"


def _faction_names() -> List[str]:
    """'LEADER|name' for every faction now."""
    try:
        return [x.split("|", 1)[1] for x in gamestate.query(
            'every_country = { limit = { is_faction_leader = yes } log = "AIRPFN|[THIS.GetTag]|[THIS.GetFactionName]" }')
            if x.startswith("AIRPFN|")]
    except gamestate.StateError:
        return []


def restore() -> None:
    """After loading: our factions' names are only in the game while `run` sets them; set them again, and have the
    factions read them again (a faction made before shows its key until then)."""
    data = _names()
    for key, name in data["names"].items():
        _set(key, name)
    from . import actions
    for row in _faction_names():
        tag, _, shown = row.partition("|")
        if shown in data["names"]:
            actions._effect(f"{tag} = {{ set_faction_name = {shown} }}")


# ---------------------------------------------------------------- the game's files: ideologies, joining rules

@lru_cache(maxsize=1)
def _ideology_files() -> Dict[str, List[str]]:
    """{ideology group: its dynamic_faction_names loc keys}, game and mods."""
    out: Dict[str, List[str]] = {}
    for path in gamefiles.files("common/ideologies"):
        with open(path, encoding="utf-8-sig", errors="replace") as f:
            node = gamefiles.parse(f.read())
        for key, _, val in node:
            if key == "ideologies" and isinstance(val, list):
                for group, _, body in val:
                    if isinstance(body, list):
                        names = gamefiles.get(body, "dynamic_faction_names")
                        out[group] = ([k.strip('"') for k, _, _ in names] if isinstance(names, list)
                                      else out.get(group, []))
    return out


def _dynamic_names() -> Dict[str, List[str]]:
    return _ideology_files()


def ideologies(tags: Optional[List[str]] = None) -> Dict[str, str]:
    """{tag: ideology group} (democratic, fascism...: the groups of the game and mods), one query."""
    groups = list(_ideology_files())
    limit = (f"limit = {{ OR = {{ {' '.join(f'tag = {t}' for t in tags)} }} }} " if tags else "limit = { exists = yes } ")
    eff = f"every_country = {{ {limit}" + " ".join(
        f'if = {{ limit = {{ has_government = {g} }} log = "AIRPFI|[THIS.GetTag]|{g}" }}' for g in groups) + " }"
    out = {}
    try:
        for line in gamestate.query(eff):
            f = line.split("|")
            if f[0] == "AIRPFI" and len(f) >= 3:
                out[f[1]] = f[2]
    except gamestate.StateError:
        pass
    return out


def _blocks(text: str):
    """(name, body) of each top-level `name = { ... }` block."""
    i = 0
    for m in re.finditer(r"(?m)^([A-Za-z0-9_]+)\s*=\s*\{", text):
        if m.start() < i:
            continue
        depth, j = 1, m.end()
        while j < len(text) and depth:
            depth += {"{": 1, "}": -1}.get(text[j], 0)
            j += 1
        i = j
        yield m.group(1), text[m.end():j - 1]


def _inner(body: str, key: str) -> Optional[str]:
    """The text of `key = { ... }` directly inside `body`."""
    depth = 0
    for m in re.finditer(r"[{}]|\b" + key + r"\s*=\s*\{", body):
        tok = m.group(0)
        if tok == "{":
            depth += 1
        elif tok == "}":
            depth -= 1
        elif depth == 0:
            d, j = 1, m.end()
            while j < len(body) and d:
                d += {"{": 1, "}": -1}.get(body[j], 0)
                j += 1
            return body[m.end():j - 1]
        else:
            depth += 1
    return None


@lru_cache(maxsize=1)
def joining_rules() -> Dict[str, str]:
    """{rule id: its trigger} for the faction DLC's joining rules (game and mods; none without the files)."""
    out = {}
    for path in gamefiles.files("common/factions/rules"):
        with open(path, encoding="utf-8-sig", errors="replace") as f:
            text = re.sub(r"#[^\n]*", "", f.read())
        for name, body in _blocks(text):
            if re.search(r"\btype\s*=\s*joining_rules\b", body):
                trig = _inner(body, "trigger")
                if trig and trig.strip():
                    out[name] = trig
    return out


def active_rules(leaders: List[str]) -> Dict[str, List[str]]:
    """{leader: the joining rules its faction has}, one query (empty without the DLC)."""
    rules = joining_rules()
    if not rules or not leaders:
        return {}
    eff = " ".join(f"{L} = {{ " + " ".join(f'if = {{ limit = {{ has_active_rule = {r} }} log = "AIRPFR|{L}|{r}" }}'
                                           for r in rules) + " }" for L in leaders)
    out: Dict[str, List[str]] = {}
    try:
        for line in gamestate.query(eff):
            f = line.split("|")
            if f[0] == "AIRPFR" and len(f) >= 3:
                out.setdefault(f[1], []).append(f[2])
    except gamestate.StateError:
        pass
    return out


def may_join(joiner: str, leader: str, rules: List[str]) -> bool:
    """Does `joiner` meet the joining rules of `leader`'s faction (ROOT the joiner, FROM the faction)?"""
    for r in rules:
        trig = re.sub(r"\bFROM\b", leader, joining_rules().get(r, ""))
        if trig and not bridge.trigger(f"{joiner} = {{ {trig} }}"):
            return False
    return True


def founds_by_focus(tag: str) -> bool:
    """May `tag`'s national focuses still found a faction: one on the course its AI plans, or one of its own tree
    (focus ids TAG_...) not taken and not shut out by a focus it took? Then it doesn't found one by agreement:
    a faction of ours could keep the focus from firing (the Axis, the Co-Prosperity Sphere...)."""
    from . import course
    try:
        completed, current = course.focus_state(tag)
        nxt = course.path(tag, completed, current, count=40)[0]
    except Exception:
        return False
    fs, done = course.focuses(), set(completed)
    if any(fs[f].reward & FOUNDING for f in ([current] if current else []) + nxt if f in fs):
        return True
    return any(f.reward & FOUNDING and f.id.startswith(f"{tag}_") and f.id not in done and not (f.exclusive & done)
               for f in fs.values())


# ---------------------------------------------------------------- who might join, found or break

def _ok(camp: campaign.Campaign, tag: str, w: Dict[str, Dict]) -> bool:
    from . import guarantees
    return guarantees._ok(camp, tag, w)


def world_factions(camp: campaign.Campaign) -> Dict[str, Dict]:
    """{faction name: {"leader", "members": [tags, the leader first]}} from the snapshot."""
    out: Dict[str, Dict] = {}
    for t, x in sorted((camp.snapshot or {}).get("countries", {}).items()):
        if x.get("faction"):
            f = out.setdefault(x["faction"], {"leader": x.get("faction_leader", ""), "members": []})
            f["members"].append(t)
    for f in out.values():
        f["members"].sort(key=lambda t: t != f["leader"])
    return out


def _enemies(snap: Dict, tag: str) -> set:
    return {b if a == tag else a for a, b in (p.split("|") for p in snap.get("wars", [])) if tag in (a, b)}


def candidates(camp: campaign.Campaign, w: Dict[str, Dict]) -> List[Dict]:
    """Possible faction moves: [{"move": join | found | break, "a", "b" (join: the joiner and the leader; found: the
    founder and the other; break: the member and the leader), "faction", "stake": [words], "aim", "reach",
    "score"}], best first. The game's rules (world tension, the deal menu) are checked later by the storyteller."""
    from . import agendas, guarantees, storyteller
    snap = camp.snapshot or {}
    c = snap.get("countries", {})
    name = lambda t: c.get(t, {}).get("name", t)
    player = camp.data["player"]
    power = lambda t: w.get(t, {}).get("civ", 0) + w.get(t, {}).get("mil", 0)
    aims = {t: agendas.aims_of(camp, t) for t in w if t in c}
    against = lambda t: {a["target"] for a in aims.get(t, []) if a["kind"] in ("oppose", "land") and a["target"]}
    friends = lambda t: {a["target"] for a in aims.get(t, []) if a["kind"] in ("back", "befriend") and a["target"]}
    enemies = {t: _enemies(snap, t) for t in w}
    threat_by: Dict[str, set] = {}
    for x in guarantees.threats(camp, w):
        threat_by.setdefault(x["of"], set()).add(x["by"])
    facs = world_factions(camp)
    ideo = ideologies()
    rules = active_rules([f["leader"] for f in facs.values() if f["leader"] and f["leader"] != player])
    try:
        from . import world
        level = world.tension([player])[0]
    except Exception:
        level = 0.0
    total = sum(power(t) for t in w) or 1
    share = lambda tags: sum(power(t) for t in tags) / total
    majors = lambda tags: sum(1 for t in tags if c.get(t, {}).get("major"))
    out: List[Dict] = []

    def aim_line(t: str, target: str, kinds) -> Optional[Dict]:
        return next((a for a in aims.get(t, []) if a["target"] == target and a["kind"] in kinds), None)

    # join: a country outside any faction, and a faction led by an AI country
    loose = [t for t in w if _ok(camp, t, w) and not c[t].get("faction")]
    for fname, f in facs.items():
        L, members = f["leader"], f["members"]
        if not L or L == player or player in members or not _ok(camp, L, w):
            continue
        foes = set().union(*(enemies.get(m, set()) for m in members)) | against(L)
        at_war = set().union(*(enemies.get(m, set()) for m in members))
        for x in loose:
            if x in members or at_war & {x} or against(x) & set(members) or against(L) & {x}:
                continue
            s, stake, aim = 0, [], None
            shared_war = enemies.get(x, set()) & at_war
            if shared_war:
                s, stake = s + 4, stake + [f"at war with {', '.join(name(e) for e in sorted(shared_war))} as the "
                                           f"faction is"]
            for t in sorted(against(x) & foes):
                a = aim_line(x, t, ("oppose", "land"))
                s, aim = s + 3, aim or a
                stake.append(f"its aim: {a['aim']}")
            for t in sorted(friends(x) & set(members)):
                a = aim_line(x, t, ("back", "befriend"))
                s, aim = s + 3, aim or a
                stake.append(f"its aim: {a['aim']}")
            if x in friends(L):
                s, stake = s + 2, stake + [f"the leader's aim: {aim_line(L, x, ('back', 'befriend'))['aim']}"]
            common = threat_by.get(x, set()) & foes
            if common:
                s, stake = s + 2, stake + [f"threatened by {', '.join(name(t) for t in sorted(common))}"]
            if w[x]["neighbours"] & set(members):
                s += 1
            same = ideo.get(x) and ideo.get(x) == ideo.get(L)
            if same:
                s, stake = s + 1, stake + ["the same ideology"]
            elif not (shared_war or common):
                s -= 2                                 # another ideology joins only for a war or a threat in common
            if s < 3:
                continue
            tier = guarantees.reach(L, x, w)
            if tier == "present" and not (shared_war or aim):
                continue
            if tier == "far" and not (shared_war or x in friends(L) or friends(x) & {L}):
                continue
            if not may_join(x, L, rules.get(L, [])):
                continue
            fill = 0.0                                 # a faction near its cap takes members less readily
            if not shared_war:
                if too_large(share(members + [x]), majors(members + [x]), bool(c[x].get("major")), level):
                    continue
                fill = share(members) / cap(level)
            out.append({"move": "join", "a": x, "b": L, "faction": fname, "stake": stake, "aim": aim, "reach": tier,
                        "score": s * REACH_WEIGHT[tier] * (1 - fill / 2)})

    # found: two countries outside any faction; the founder a major power or the stronger one
    for i, a in enumerate(loose):
        for b in loose[i + 1:]:
            if b in enemies.get(a, set()) or b in against(a) or a in against(b):
                continue
            L, x = (a, b) if (c[a].get("major"), power(a)) >= (c[b].get("major"), power(b)) else (b, a)
            s, stake, aim = 0, [], None
            shared_war = enemies.get(a, set()) & enemies.get(b, set())
            if shared_war:
                s, stake = s + 4, stake + [f"both at war with {', '.join(name(e) for e in sorted(shared_war))}"]
            for t in sorted(against(a) & against(b)):
                s, aim = s + 3, aim or aim_line(L, t, ("oppose", "land")) or aim_line(x, t, ("oppose", "land"))
                stake.append(f"both aim against {name(t)}")
            if b in friends(a) or a in friends(b):
                s, stake = s + 2, stake + ["one aims to stand by the other"]
                aim = aim or aim_line(L, x, ("back", "befriend"))
            # A threat in common is what founds an alliance of neighbours (the Little Entente, the Balkan Pact), and
            # it counts for minor countries, which mostly have no aims of their own (agendas cover the few
            # dozen that matter most): with the same ideology, or a border, it is enough.
            common = threat_by.get(a, set()) & threat_by.get(b, set())
            if common:
                s, stake = s + 3, stake + [f"both threatened by {', '.join(name(t) for t in sorted(common))}"]
            if b in w[a]["neighbours"]:
                s, stake = s + 1, stake + ["neighbours"]
            same = ideo.get(a) and ideo.get(a) == ideo.get(b)
            if same:
                s, stake = s + 1, stake + ["the same ideology"]
            elif not (shared_war or common):
                continue
            if s < 4:
                continue
            tier = guarantees.reach(L, x, w)
            if tier == "far" or (tier == "present" and not shared_war):
                continue                               # a new alliance starts among neighbours
            if not shared_war and too_large(share([a, b]), majors([a, b]), False, level):
                continue
            out.append({"move": "found", "a": L, "b": x, "faction": "", "stake": stake, "aim": aim, "reach": tier,
                        "score": s * REACH_WEIGHT[tier]})

    # break: a member and its leader that no longer belong together (rare)
    gap_open = not any(x.get("slot") == "faction" and x["kind"] == "break"
                       and 0 <= storyteller._days(x["date"], snap.get("date", "")) < BREAK_GAP_DAYS
                       for x in storyteller._state(camp)["incidents"])
    for fname, f in facs.items() if gap_open else ():
        L = f["leader"]
        if not L or L == player or player in f["members"] or not _ok(camp, L, w):
            continue
        at_war = enemies.get(L, set())
        for x in f["members"]:
            if x == L or not _ok(camp, x, w) or enemies.get(x, set()) & at_war:
                continue                               # nobody leaves a war it fights beside its leader
            s, stake, aim = 0, [], None
            hostile = against(x) & set(f["members"])
            if hostile:
                a = aim_line(x, sorted(hostile)[0], ("oppose", "land"))
                s, aim = s + 4, a
                stake.append(f"its aim: {a['aim']}")
            if x in against(L):
                s, aim = s + 3, aim or aim_line(L, x, ("oppose", "land"))
                stake.append(f"the leader's aim: {aim_line(L, x, ('oppose', 'land'))['aim']}")
            backs = friends(x) & (against(L) | at_war)
            if backs:
                s, stake = s + 3, stake + [f"it stands by {', '.join(name(t) for t in sorted(backs))}, whom the "
                                           f"leader opposes"]
            led = friends(L) & against(x)
            if led:
                s, stake = s + 3, stake + [f"the leader stands by {', '.join(name(t) for t in sorted(led))}, whom "
                                           f"it opposes"]
            clash = bool(ideo.get(x) and ideo.get(L) and ideo[x] != ideo[L] and {ideo[x], ideo[L]} <= CLASHING)
            if ideo.get(x) and ideo.get(L) and ideo[x] != ideo[L]:
                s += 4 if clash else 2
                stake.append(f"its government is now {ideo[x]}, the leader's {ideo[L]}")
            if x in friends(L) or L in friends(x):
                # friendship outweighs a lesser difference; a democracy under a fascist (or the like) stays a
                # question for the storyteller, who sees both sides
                if clash:
                    stake.append("yet the two still call each other friends")
                else:
                    s -= 3
            if s < 4:
                continue
            out.append({"move": "break", "a": x, "b": L, "faction": fname, "stake": stake, "aim": aim,
                        "reach": guarantees.reach(L, x, w), "score": float(s)})

    out.sort(key=lambda x: -x["score"])
    kept, per = [], {}
    for x in out:
        if per.get(x["a"], 0) >= PER_COUNTRY:
            continue
        per[x["a"]] = per.get(x["a"], 0) + 1
        kept.append(x)
        if len(kept) >= CANDIDATES:
            break
    return [x for x in kept if x["move"] != "found" or not founds_by_focus(x["a"])]


def break_effect(member: str, leader: str, by_leader: bool) -> str:
    return (f"{leader} = {{ remove_from_faction = {member} }}" if by_leader
            else f"{member} = {{ leave_faction = yes }}")
