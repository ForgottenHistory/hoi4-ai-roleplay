"""World chronicle: snapshot the whole world in one query and turn the differences between two
snapshots into dated events (wars, peace, capitulations, factions, subjects, governments, territory).

A snapshot costs one bridge query (~30 ms for the vanilla world), so it can run every few seconds.

The player and the countries at war with them also get their "front" in each snapshot: ships, factories, and per
owned state its controller, factories, resources and whether it is a demilitarized zone. When a war with the
player ends, peace_terms() compares that with the same detail right after, to say what the peace conference did:
states taken and occupied states returned, puppets, forced governments, liberated countries, the navy taken,
demilitarized zones, dismantled military industry, war reparations and resource rights.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Dict, List, Optional, Set, Tuple

from .campaign import ID_VARIABLE, date_key, date_words, save_key
from .gamestate import StateError, query, save_date, timed_activities

_EFFECT = (
    'log = "I|[?' + ID_VARIABLE + ']" '
    'log = "D|[GetDateText]" '
    'every_country = { '
    'log = "C|[THIS.GetTag]|[THIS.GetName]|[THIS.GetLeader]|[THIS.GetRulingParty]|[THIS.GetRulingIdeology]" '
    'if = { limit = { is_major = yes } log = "M|[THIS.GetTag]" } '
    'if = { limit = { is_in_faction = yes } log = "F|[THIS.GetTag]|[THIS.GetFactionName]|[THIS.FactionLeader.GetTag]" } '
    'if = { limit = { is_subject = yes } log = "O|[THIS.GetTag]|[THIS.Overlord.GetTag]" } '
    'if = { limit = { has_capitulated = yes } log = "K|[THIS.GetTag]" } '
    'if = { limit = { has_war = yes } log = "P|[THIS.GetTag]|[?THIS.surrender_progress]" } '
    'every_enemy_country = { log = "W|[PREV.GetTag]|[THIS.GetTag]" } '
    '} '
    'every_state = { log = "S|[THIS.GetID]|[THIS.GetName]|[THIS.Owner.GetTag]" }'
)

RESOURCES = ["oil", "aluminium", "rubber", "tungsten", "steel", "chromium", "coal"]

# One country's front (THIS = the country; PREV inside the state loop is the country too).
_FRONT = (
    'log = "Q|[THIS.GetTag]|[?THIS.num_ships]|[?THIS.num_of_civilian_factories]|[?THIS.num_of_military_factories]" '
    'every_owned_state = { '
    'log = "QS|[PREV.GetTag]|[THIS.GetID]|[THIS.Controller.GetTag]|[?THIS.arms_factory_level]|'
    '[?THIS.industrial_complex_level]|' + "|".join(f"[?THIS.resource@{r}]" for r in RESOURCES) + '" '
    'if = { limit = { is_demilitarized_zone = yes } log = "QD|[PREV.GetTag]|[THIS.GetID]" } '
    '} '
)


def _num(x: str) -> float:
    try:
        return float(x)
    except ValueError:
        return 0.0


def _read_front(lines: List[str]) -> Dict[str, Dict]:
    """{tag: {ships, civ, mil, states: {id: {controller, arms, civ, res: [..], dmz}}}}"""
    out: Dict[str, Dict] = {}
    for line in lines:
        kind, _, rest = line.partition("|")
        f = [x.strip() for x in rest.split("|")]
        if kind == "Q" and len(f) >= 4:
            out[f[0]] = {"ships": _num(f[1]), "civ": _num(f[2]), "mil": _num(f[3]), "states": {}}
        elif kind == "QS" and len(f) >= 6 and f[0] in out:
            out[f[0]]["states"][f[1]] = {"controller": f[2], "arms": _num(f[3]), "civ": _num(f[4]),
                                         "res": [_num(x) for x in f[5:5 + len(RESOURCES)]], "dmz": False}
        elif kind == "QD" and len(f) >= 2 and f[1] in out.get(f[0], {}).get("states", {}):
            out[f[0]]["states"][f[1]]["dmz"] = True
    return out


# The player's ties to other countries, kept in each snapshot so they are still known after a war ends them
# (reputation.py: what the player had with a country it attacked). Inside every_country, PREV in a nested
# `TAG = { ... }` is the country looped over.
TIES = {
    "pact": "has_non_aggression_pact_with = {p}",
    "my_guarantee": "{p} = {{ has_guaranteed = PREV }}",          # the player guarantees it
    "their_guarantee": "has_guaranteed = {p}",                     # it guarantees the player
    "their_access": "gives_military_access_to = {p}",              # it lets the player's troops through
    "my_access": "{p} = {{ gives_military_access_to = PREV }}",
    "faction": "is_in_faction_with = {p}",
    "subject": "is_subject_of = {p}",
}


def _ties(player: str) -> str:
    checks = " ".join(f'if = {{ limit = {{ {cond.format(p=player)} }} log = "T|[THIS.GetTag]|{kind}" }}'
                      for kind, cond in TIES.items())
    return f"every_country = {{ limit = {{ NOT = {{ tag = {player} }} }} {checks} }}"


def _read_ties(lines: List[str], player: str) -> Dict[str, List[str]]:
    out: Dict[str, List[str]] = defaultdict(list)
    for line in lines:
        if line.startswith("T|"):
            tag, _, kind = line[2:].partition("|")
            if tag.strip() != player:
                out[tag.strip()].append(kind.strip())
    return dict(out)


def front(tags: List[str]) -> Dict[str, Dict]:
    """The front detail of these countries, now."""
    tags = [t for t in dict.fromkeys(tags) if t]
    return _read_front(query(" ".join(f"{t} = {{ {_FRONT} }}" for t in tags))) if tags else {}


# Event types diff() produces: things that happened in the world, as opposed to letter outcomes
# (agreements granted, proposals answered). Only these refresh policies or lift a refusal.
WORLD_TYPES = {"country_new", "country_gone", "government", "leader", "faction_left", "faction_founded",
               "faction_joined", "independence", "subject", "capitulation", "war", "peace", "territory"}


def snapshot(player: str) -> Dict:
    """{campaign, date, date_text, countries: {tag: {...}}, wars: [A|B], states: {id: [name, owner]},
    front: {tag: ...} (the player and their enemies, see _read_front)}"""
    snap: Dict = {"campaign": 0, "date": "", "date_text": "", "countries": {}, "wars": [], "states": {}}
    wars: Set[str] = set()
    lines = query(f"{player} = {{ {_EFFECT} {_FRONT} every_enemy_country = {{ {_FRONT} }} {_ties(player)} }}")
    snap["ties"] = _read_ties(lines, player)
    snap["front"] = _read_front(lines)
    for line in lines:
        kind, _, rest = line.partition("|")
        f = [x.strip() for x in rest.split("|")]
        c = snap["countries"]
        if kind == "I":
            try:
                snap["campaign"] = int(float(f[0]))
            except ValueError:
                pass
        elif kind == "D":
            snap["date_text"], snap["date"] = f[0], date_key(f[0])
        elif kind == "C" and len(f) >= 5:
            c[f[0]] = {"name": f[1], "leader": f[2], "party": f[3], "ideology": f[4], "major": False,
                       "faction": "", "faction_leader": "", "overlord": "", "capitulated": False}
        elif kind == "M" and f[0] in c:
            c[f[0]]["major"] = True
        elif kind == "F" and f[0] in c and len(f) >= 3:
            c[f[0]]["faction"], c[f[0]]["faction_leader"] = f[1], f[2]
        elif kind == "O" and f[0] in c and len(f) >= 2:
            c[f[0]]["overlord"] = f[1]
        elif kind == "K" and f[0] in c:
            c[f[0]]["capitulated"] = True
        elif kind == "P" and f[0] in c and len(f) >= 2:
            try:
                c[f[0]]["surrender"] = float(f[1])
            except ValueError:
                pass
        elif kind == "W" and len(f) >= 2:
            wars.add("|".join(sorted(f[:2])))
        elif kind == "S" and len(f) >= 3:
            snap["states"][f[0]] = [f[1], f[2]]
    snap["wars"] = sorted(wars)
    return snap


def diff(old: Dict, new: Dict) -> List[Dict]:
    """Events that happened between two snapshots, dated at the newer one."""
    oc, nc = old["countries"], new["countries"]
    name = lambda t: (nc.get(t) or oc.get(t) or {}).get("name") or t
    major = lambda *tags: any((nc.get(t) or oc.get(t) or {}).get("major") for t in tags)
    events: List[Dict] = []

    def add(tags: List[str], text: str, kind: str, world: bool = False) -> None:
        tags = [x for x in tags if x]
        events.append({"date": new["date"], "when": date_words(new["date"]), "type": kind,
                       "tags": tags, "text": text, "major": world or major(*tags)})

    for t in nc.keys() - oc.keys():
        lord = nc[t]["overlord"]
        if lord:
            # e.g. annexed in a peace deal and released as a puppet: the overlord is part of the event
            add([t, lord], f"{name(t)} was established as a country, a subject of {name(lord)}.", "subject")
        else:
            add([t], f"{name(t)} appeared as a country.", "country_new")
    for t in oc.keys() - nc.keys():
        add([t], f"{name(t)} ceased to exist.", "country_gone")

    for t in nc.keys() & oc.keys():
        a, b = oc[t], nc[t]
        if a["party"] != b["party"] or a["ideology"] != b["ideology"]:
            add([t], f"In {b['name']}, the {b['party']} ({b['ideology']}) took power from the "
                     f"{a['party']} ({a['ideology']}); {b['leader']} now leads the country.", "government")
        elif a["leader"] != b["leader"]:
            add([t], f"In {b['name']}, {b['leader']} succeeded {a['leader']} as leader.", "leader")
        if a["faction"] != b["faction"]:
            if a["faction"]:
                add([t, a["faction_leader"]], f"{b['name']} left the {a['faction']}.", "faction_left")
            if b["faction"]:
                if b["faction_leader"] == t:
                    add([t], f"{b['name']} founded the {b['faction']}.", "faction_founded")
                else:
                    add([t, b["faction_leader"]], f"{b['name']} joined the {b['faction']}, "
                                                  f"led by {name(b['faction_leader'])}.", "faction_joined")
        if a["overlord"] != b["overlord"]:
            if a["overlord"]:
                add([t, a["overlord"]], f"{b['name']} is no longer a subject of {name(a['overlord'])}.", "independence")
            if b["overlord"]:
                add([t, b["overlord"]], f"{b['name']} became a subject of {name(b['overlord'])}.", "subject")
        if b["capitulated"] and not a["capitulated"]:
            add([t], f"{b['name']} capitulated.", "capitulation")

    ow, nw = set(old["wars"]), set(new["wars"])
    for pair in sorted(nw - ow):
        x, y = pair.split("|")
        add([x, y], f"War broke out between {name(x)} and {name(y)}.", "war")
    for pair in sorted(ow - nw):
        x, y = pair.split("|")
        if x in nc and y in nc:
            add([x, y], f"{name(x)} and {name(y)} are no longer at war.", "peace")
        elif x in nc or y in nc:
            lost = y if x in nc else x
            add([x, y], f"The war between {name(x)} and {name(y)} ended: {name(lost)} was defeated and "
                        f"ceased to exist.", "peace")

    moved: Dict[Tuple[str, str], List[str]] = defaultdict(list)
    for sid, (sname, owner) in new["states"].items():
        before = old["states"].get(sid)
        if before and before[1] != owner:
            moved[(before[1], owner)].append(sname)
    for (frm, to), names in moved.items():
        add([to, frm], f"{name(to)} took {', '.join(names)} from {name(frm)}.", "territory")
    return events


def peace_terms(old: Dict, new: Dict, a: str, b: str, asked: str = "") -> Optional[Dict]:
    """What the peace between a and b did, as one event (None if the snapshot before has no front detail for both).
    `asked` is the tag that sued for peace, if known. Reads the front of everyone who got the loser's land, now.

    Only the loser's losses are described: the loser is the side that capitulated, else the one further along in
    surrender. Forcing a government on part of a country creates a new country with the loser's original tag; a
    puppet is a new or existing country that is now the winner's subject. War reparations and dismantled military
    industry shows as military factory levels gone from states. Demilitarized zones, war reparations and resource
    rights are read exactly from each country's timed_activities (gamestate.timed_activities): states, factories,
    who benefits and until when."""
    before = old.get("front", {})
    if a not in before or b not in before:
        return None
    oc, nc = old["countries"], new["countries"]
    name = lambda t: (nc.get(t) or oc.get(t) or {}).get("name") or t
    sname = lambda sid: (new["states"].get(sid) or old["states"].get(sid) or [sid])[0]
    owner_now = lambda sid: (new["states"].get(sid) or ["", ""])[1]
    beaten = lambda t: (2 if oc.get(t, {}).get("capitulated") else 0) + oc.get(t, {}).get("surrender", 0)
    lose, win = (a, b) if beaten(a) > beaten(b) else (b, a)
    lb = before[lose]

    # Everyone who holds the loser's land now, and what kind of country they are.
    holders: Dict[str, List[str]] = defaultdict(list)
    for sid in lb["states"]:
        holders[owner_now(sid)].append(sid)
    holders.pop("", None)
    after = front(list(holders) + [win])
    same_country = set()
    check = " ".join(f'{t} = {{ if = {{ limit = {{ original_tag = {lose} }} log = "OT|{t}" }} }}'
                     for t in holders if t != lose)
    for line in query(check) if check else []:
        if line.startswith("OT|"):
            same_country.add(line[3:].strip())

    out: List[str] = []
    for owner, sids in holders.items():
        names = ", ".join(sname(x) for x in sids)
        c = nc.get(owner, {})
        if owner == lose:
            returned = [x for x in sids if lb["states"][x]["controller"] not in (lose, "")]
            if returned:
                out.append(f"returned the occupied {', '.join(sname(x) for x in returned)} to {name(lose)}")
        elif owner == win:
            out.append(f"took {names}")
        elif owner in same_country:
            out.append(f"installed a new {c.get('ideology', '')} government of {name(lose)} in {names}, as "
                       f"{name(owner)} ({c.get('party', '')})")
        elif c.get("overlord") == win:
            out.append(f"made {names} into its puppet {name(owner)}")
        elif owner not in oc:
            out.append(f"liberated {names} as {name(owner)}")
        else:
            out.append(f"handed {names} to {name(owner)}")
    if nc.get(lose, {}).get("overlord") == win and oc.get(lose, {}).get("overlord") != win:
        out.append(f"made {name(lose)} its puppet")
    was, now = oc.get(lose, {}), nc.get(lose, {})
    if now and (was.get("party"), was.get("ideology")) != (now.get("party"), now.get("ideology")):
        out.append(f"forced a new government on {name(lose)} ({now.get('party')}, {now.get('ideology')})")

    la, wb, wa = after.get(lose, {"ships": 0}), before[win], after.get(win, {"ships": 0})
    ships = lb["ships"] - la["ships"]
    if ships >= 1 and wa["ships"] > wb["ships"]:
        out.append(f"took {int(min(ships, wa['ships'] - wb['ships']))} ships of {name(lose)}'s navy")
    for owner in holders:
        if owner == win or owner not in after:
            continue
        fa = after[owner]
        mine = [x for x in holders[owner] if x in fa["states"]]
        dismantled = {x: lb["states"][x]["arms"] - fa["states"][x]["arms"] for x in mine}
        dismantled = {x: v for x, v in dismantled.items() if v >= 1}
        if dismantled:
            out.append(f"dismantled {int(sum(dismantled.values()))} military factories in "
                       f"{', '.join(sname(x) for x in dismantled)}")
        try:
            terms = timed_activities(owner)
        except (StateError, RuntimeError):
            terms = []
        for t in terms:
            if t["target"] != win or save_key(t["start"])[:10] < old["date"][:10]:
                continue                               # imposed by someone else, or by an earlier peace
            until = f" until {save_date(t['end'])}" if t["end"] else ""
            where = ", ".join(sname(x) for x in t["states"])
            if t["kind"] == "demilitarized_zone":
                out.append(f"demilitarized {where if len(t['states']) < len(fa['states']) else 'all of ' + name(owner)}"
                           f"{until}")
            elif t["kind"] == "war_reparation":
                out.append(f"imposed war reparations on {name(owner)}: {int(t['civilian_factories'])} civilian "
                           f"factories{until}")
            elif t["kind"] == "resource_rights":
                out.append(f"took the resource rights in {name(owner)}'s {where}{until}")
            else:
                out.append(f"imposed {t['kind'].replace('_', ' ')} on {name(owner)}"
                           + (f" in {where}" if where else "") + until)

    who = f" ({name(asked)} had sued for peace)" if asked else ""
    text = (f"Peace between {name(a)} and {name(b)}{who}: "
            + (f"{name(win)} " + "; ".join(out) if out else "a white peace, nothing changed hands") + ".")
    return {"date": new["date"], "when": date_words(new["date"]), "type": "peace", "tags": [a, b], "text": text,
            "major": any((nc.get(t) or oc.get(t) or {}).get("major") for t in (a, b))}
