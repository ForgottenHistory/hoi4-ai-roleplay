"""World chronicle: snapshot the whole world in one query and turn the differences between two
snapshots into dated events (wars, peace, capitulations, factions, subjects, governments, territory).

A snapshot costs one bridge query (~30 ms for the vanilla world), so it can run every few seconds.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Dict, List, Set, Tuple

from .campaign import ID_VARIABLE, date_key, date_words
from .gamestate import query

_EFFECT = (
    'log = "I|[?' + ID_VARIABLE + ']" '
    'log = "D|[GetDateText]" '
    'every_country = { '
    'log = "C|[THIS.GetTag]|[THIS.GetName]|[THIS.GetLeader]|[THIS.GetRulingParty]|[THIS.GetRulingIdeology]" '
    'if = { limit = { is_major = yes } log = "M|[THIS.GetTag]" } '
    'if = { limit = { is_in_faction = yes } log = "F|[THIS.GetTag]|[THIS.GetFactionName]|[THIS.FactionLeader.GetTag]" } '
    'if = { limit = { is_subject = yes } log = "O|[THIS.GetTag]|[THIS.Overlord.GetTag]" } '
    'if = { limit = { has_capitulated = yes } log = "K|[THIS.GetTag]" } '
    'every_enemy_country = { log = "W|[PREV.GetTag]|[THIS.GetTag]" } '
    '} '
    'every_state = { log = "S|[THIS.GetID]|[THIS.GetName]|[THIS.Owner.GetTag]" }'
)


# Event types diff() produces: things that happened in the world, as opposed to letter outcomes
# (agreements granted, proposals answered). Only these refresh policies or lift a refusal.
WORLD_TYPES = {"country_new", "country_gone", "government", "leader", "faction_left", "faction_founded",
               "faction_joined", "independence", "subject", "capitulation", "war", "peace", "territory"}


def snapshot(player: str) -> Dict:
    """{campaign, date, date_text, countries: {tag: {...}}, wars: [A|B], states: {id: [name, owner]}}"""
    snap: Dict = {"campaign": 0, "date": "", "date_text": "", "countries": {}, "wars": [], "states": {}}
    wars: Set[str] = set()
    for line in query(f"{player} = {{ {_EFFECT} }}"):
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
