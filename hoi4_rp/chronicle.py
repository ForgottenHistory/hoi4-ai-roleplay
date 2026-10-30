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

import re
from collections import defaultdict
from typing import Dict, List, Optional, Set, Tuple

from .campaign import ID_VARIABLE, date_key, date_words, save_key
from .gamestate import StateError, completed_focuses, query, save_date, timed_activities

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
    "my_justifying": "{p} = {{ is_justifying_wargoal_against = PREV }}",   # the player prepares a war goal on it
    "their_justifying": "is_justifying_wargoal_against = {p}",
    "my_wargoal": "{p} = {{ has_wargoal_against = PREV }}",
    "their_wargoal": "has_wargoal_against = {p}",
    "market": "has_market_access_with = {p}",
    "their_embargo": "is_embargoing = {p}",                        # it embargoes the player
    "my_embargo": "{p} = {{ is_embargoing = PREV }}",
}

# Agreements whose start and end are news, however they came about (the game's own diplomacy, a focus, an event, a
# letter): (start text, end text). letters._not_by_letter drops those a letter deal already told, and ends a war
# or a term explains.
AGREEMENTS = {
    "pact": ("{p} and {o} signed a non-aggression pact.", "The non-aggression pact between {p} and {o} ended."),
    "my_guarantee": ("{p} guaranteed the independence of {o}.", "{p}'s guarantee of {o}'s independence ended."),
    "their_guarantee": ("{o} guaranteed the independence of {p}.", "{o}'s guarantee of {p}'s independence ended."),
    "their_access": ("{o} granted {p} military access.", "{o} no longer gives {p} military access."),
    "my_access": ("{p} granted {o} military access.", "{p} no longer gives {o} military access."),
    "market": ("{p} and {o} opened market access to each other.", "Market access between {p} and {o} closed."),
    "their_embargo": ("{o} imposed a trade embargo on {p}.", "{o} lifted its trade embargo on {p}."),
    "my_embargo": ("{p} imposed a trade embargo on {o}.", "{p} lifted its trade embargo on {o}."),
}

# Ties whose start is news (diff() turns them into events): "{a}" begins it against "{b}".
TIE_EVENTS = {
    "my_justifying": ("justification", "{p} began justifying a war goal against {o}."),
    "their_justifying": ("justification", "{o} began justifying a war goal against {p}."),
    "my_wargoal": ("wargoal", "{p} now holds a war goal against {o}."),
    "their_wargoal": ("wargoal", "{o} now holds a war goal against {p}."),
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
               "faction_joined", "independence", "subject", "capitulation", "war", "peace", "territory",
               "justification", "wargoal",
               # treaties.py: a treaty broken, renounced, called on or lapsing changes what the two are to each other
               "treaty:broken", "treaty:renounced", "treaty:clause", "treaty:lapsed",
               # ultimatums.py: a threat of war, and how it ended
               "ultimatum:issued", "ultimatum:met", "ultimatum:war", "ultimatum:ready", "ultimatum:dropped",
               # guarantees.py: a letter guarantee that ran its term
               "guarantee:expired"} | {f"agreement:{k}{e}" for k in AGREEMENTS for e in ("", ":end")
                                         if k != "market"}   # market access comes and goes in bulk: no news
MARKET_MASS = 3             # more market access changes than this in one snapshot are told as one line


def snapshot(player: str) -> Dict:
    """{campaign, date, date_text, countries: {tag: {...}}, wars: [A|B], states: {id: [name, owner]},
    front: {tag: ...} (the player and their enemies, see _read_front)}"""
    snap: Dict = {"campaign": 0, "date": "", "date_text": "", "countries": {}, "wars": [], "states": {}}
    wars: Set[str] = set()
    lines = query(f"{player} = {{ {_EFFECT} {_FRONT} every_enemy_country = {{ {_FRONT} }} {_ties(player)} }}")
    snap["ties"] = _read_ties(lines, player)
    snap["tie_kinds"] = sorted(TIES)                  # what was looked for: a kind added later isn't news at first
    snap["player"] = player
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
    # Completed focuses of the player, the major powers and WATCH (those it writes to): what a focus did is news
    # too. One small read each (0.03 s), so not every country in the game.
    focuses: Dict[str, List[List[str]]] = {}
    for tag in {player} | WATCH | {t for t, x in snap["countries"].items() if x["major"]}:
        if tag in snap["countries"]:
            try:
                focuses[tag] = [[f.id, f.name] for f in completed_focuses(tag)]
            except (StateError, RuntimeError):
                pass
    snap["focuses"] = focuses
    return snap


WATCH: Set[str] = set()      # countries beyond the major powers whose focuses are read (set by the letter loop)


def recent_focuses(events: List[Dict], tag: str, now: str, n: int = 3) -> List[str]:
    """The names of the last `n` national focuses `tag` completed, newest last."""
    return [e["text"].split('focus "', 1)[1].rstrip('."') for e in events
            if e.get("type") == "focus" and tag in e["tags"] and e["date"] <= now][-n:]


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

    # The player's war goals and justifications, both ways (older snapshots have no ties: nothing to compare)
    player = new.get("player")
    if player and "ties" in old:
        markets: Dict[str, List[str]] = {"": [], ":end": []}     # market access opened / closed with each
        for tag, kinds in new.get("ties", {}).items():
            for kind in set(kinds) - set(old["ties"].get(tag, [])):
                if kind in TIE_EVENTS and tag in nc:
                    kind_name, text = TIE_EVENTS[kind]
                    # its overlord hears of it too (a threat to a colony is a threat to its master)
                    add([player, tag, nc[tag].get("overlord", "")], text.format(p=name(player), o=name(tag)), kind_name)
                elif kind == "market" and tag in nc and kind in old.get("tie_kinds", ()):
                    markets[""].append(tag)
                elif kind in AGREEMENTS and tag in nc and kind in old.get("tie_kinds", ()):
                    add([player, tag], AGREEMENTS[kind][0].format(p=name(player), o=name(tag)), f"agreement:{kind}")
        for tag, kinds in old["ties"].items():
            for kind in set(kinds) - set(new.get("ties", {}).get(tag, [])):
                if kind == "market" and tag in nc and kind in new.get("tie_kinds", ()):
                    markets[":end"].append(tag)
                elif kind in AGREEMENTS and tag in nc and kind in new.get("tie_kinds", ()):
                    add([player, tag], AGREEMENTS[kind][1].format(p=name(player), o=name(tag)), f"agreement:{kind}:end")
        # The game opens and closes market access with dozens of countries at once (a trade law, a new government):
        # then one line for the player alone, not news between it and Oman
        for end, tags in markets.items():
            if len(tags) > MARKET_MASS:
                names = ", ".join(name(t) for t in sorted(tags, key=lambda t: not nc[t].get("major"))[:6])
                add([player], f"{name(player)} {'closed' if end else 'opened'} market access with {len(tags)} "
                              f"countries at once ({names} and others).", f"agreement:market{end}:mass")
            for t in tags if len(tags) <= MARKET_MASS else ():
                add([player, t], AGREEMENTS["market"][1 if end else 0].format(p=name(player), o=name(t)),
                    f"agreement:market{end}")

    moved: Dict[Tuple[str, str], List[str]] = defaultdict(list)
    for sid, (sname, owner) in new["states"].items():
        before = old["states"].get(sid)
        if before and before[1] != owner:
            moved[(before[1], owner)].append(sname)
    for (frm, to), names in moved.items():
        add([to, frm], f"{name(to)} took {', '.join(names)} from {name(frm)}.", "territory")

    # The player's national focuses (an older snapshot without them: nothing to compare). A focus can bring a
    # country under the player, into its faction or across its border; those events say so, or they read as force.
    # Older snapshots held the player's alone, as a list.
    as_dict = lambda f: f if isinstance(f, dict) else ({player: f} if player else {})
    of, nf = as_dict(old.get("focuses")), as_dict(new.get("focuses"))
    for tag in nf.keys() & of.keys():
        done = {f[0] for f in of[tag]}
        fresh = [f[1] for f in nf[tag] if f[0] not in done]
        for focus in fresh:
            add([tag], f"{name(tag)} completed the national focus \"{focus}\".", "focus")
            events[-1]["major"] = tag == player          # a major power's focus is not world news in itself
        if fresh:
            because = f" This followed {name(tag)}'s national focus \"{fresh[-1]}\"."
            for e in events:
                if (e["type"] in ("subject", "independence", "faction_joined", "faction_founded", "territory")
                        and tag in e["tags"][:1 if e["type"] == "territory" else 2]
                        and "national focus" not in e["text"]):
                    e["text"] += because
    return events


# ---------------------------------------------------------------- compact memory for prompts
# Wars and peaces are recorded pair by pair: a world war joined by a faction of forty brings hundreds of lines
# ("War broke out between X and Y."). For a prompt, one moment's pairs read as one or a few lines.

MAX_SIDE = 8                   # names listed per side of a grouped line; the rest are counted


def _names_from_events(events: List[Dict]) -> Dict[str, str]:
    """Names of countries no longer in the snapshot, from one-country events ("X ceased to exist.")."""
    out = {}
    for e in events:
        if len(e.get("tags", [])) == 1 and e.get("type") in ("country_gone", "country_new", "capitulation"):
            m = re.match(r"(?:In )?(.+?) (?:ceased to exist|appeared as a country|capitulated)\.", e["text"])
            if m:
                out[e["tags"][0]] = m.group(1)
    return out


def _pair_kind(e: Dict) -> str:
    if len(e.get("tags", [])) != 2:
        return ""
    if e["type"] == "war" and e["text"].startswith("War broke out between "):
        return "war"
    if e["type"] == "peace" and e["text"].endswith(" are no longer at war."):
        return "peace"
    if e["type"] == "peace" and e["text"].endswith(" was defeated and ceased to exist."):
        return "gone"
    return ""


MAX_LIST = 8                   # place names kept from a long list in an event text ("and 23 more")
_PLACE = r"[A-ZÀ-Þ][\w'’.\-]*(?: (?:[A-ZÀ-Þ][\w'’.\-]*|de|du|des|of|and|al|el|la|le|del|am|an|der))*"
_LONG_LIST = re.compile(rf"(?:{_PLACE}, ){{{MAX_LIST},}}{_PLACE}")


def _shorten_lists(text: str) -> str:
    """'took A, B, C, ... Z from X' -> 'took A, ..., H and 18 more from X'."""
    def cut(m: re.Match) -> str:
        items = m.group(0).split(", ")
        return ", ".join(items[:MAX_LIST]) + f" and {len(items) - MAX_LIST} more"
    return _LONG_LIST.sub(cut, text)


# Lines told of several countries at once ("A was established..." x 40): (one, many)
_PLURALS = [(" was established as a country, a subject of ", " were established as countries, subjects of "),
            (" became a subject of ", " became subjects of "),
            (" is no longer a subject of ", " are no longer subjects of "),
            (" appeared as a country.", " appeared as countries."),
            (" joined the ", " joined the "), (" left the ", " left the "),
            (" ceased to exist.", " ceased to exist."), (" capitulated.", " capitulated.")]


def _merge_alike(lines: List[Tuple[str, str]], events: List[Dict], names: Dict[str, str], listed) -> List[Tuple[str, str]]:
    """Same-moment lines that differ only in the country they open with become one line."""
    groups: Dict[Tuple[str, str], List[str]] = defaultdict(list)
    order: List[Tuple[str, str]] = []
    first_tag = {e["text"]: e["tags"][0] for e in events if e.get("tags")}
    for when, text in lines:
        tag = first_tag.get(text)
        rest = text[len(names.get(tag, "")):] if tag and text.startswith(names.get(tag, "\0")) else ""
        if rest and any(rest.startswith(one) for one, _ in _PLURALS):
            key = (when, rest)
            if key not in groups:
                order.append(key)
            groups[key].append(tag)
        else:
            order.append((when, text))
    out = []
    for key in order:
        if key in groups:
            when, rest = key
            tags = groups[key]
            if len(tags) == 1:
                out.append((when, names[tags[0]] + rest))
            else:
                many = next(m for one, m in _PLURALS if rest.startswith(one))
                one = next(o for o, _ in _PLURALS if rest.startswith(o))
                out.append((when, listed(tags) + many + rest[len(one):]))
        else:
            out.append(key)
    return out


def compact(events: List[Dict], snapshot: Optional[Dict] = None) -> List[Tuple[str, str]]:
    """[(when, text)] for a prompt, oldest first: each moment's war and peace pairs grouped into a few lines."""
    countries = (snapshot or {}).get("countries", {})
    names = {**_names_from_events(events), **{t: c["name"] for t, c in countries.items()}}
    major = lambda t: bool(countries.get(t, {}).get("major"))

    def listed(tags: List[str]) -> str:
        tags = sorted(tags, key=lambda t: (not major(t), names[t]))
        shown = [names[t] for t in tags[:MAX_SIDE]]
        if len(tags) > MAX_SIDE:
            shown.append(f"{len(tags) - MAX_SIDE} others")
        return shown[0] if len(shown) == 1 else ", ".join(shown[:-1]) + " and " + shown[-1]

    out: List[Tuple[str, str]] = []
    i = 0
    while i < len(events):
        date = events[i]["date"]
        j = i
        while j < len(events) and events[j]["date"] == date:
            j += 1
        batch = events[i:j]
        i = j
        # a peace summary already tells where the loser's land went: its "X took ... from L" lines go
        settled = {t for e in batch if e["type"] == "peace" and e["text"].startswith("Peace between")
                   for t in e["tags"]}
        batch = [e for e in batch if not (e["type"] == "territory" and len(e["tags"]) == 2
                                          and e["tags"][1] in settled)]
        if not batch:
            continue
        groups: Dict[str, List[Dict]] = defaultdict(list)
        for e in batch:
            k = _pair_kind(e)
            if k and all(t in names for t in e["tags"]):
                groups[k].append(e)
            else:
                out.append((e["when"], e["text"]))
        when = batch[0]["when"]
        for kind, evs in groups.items():
            if len(evs) == 1:
                out.append((when, evs[0]["text"]))
                continue
            if kind == "gone":
                # "The war between X and L ended: L was defeated and ceased to exist." -> per loser
                losers: Dict[str, List[str]] = defaultdict(list)
                for e in evs:
                    lost = next((t for t in e["tags"] if e["text"].endswith(f"{names[t]} was defeated and ceased "
                                                                            f"to exist.")), e["tags"][1])
                    losers[lost] += [t for t in e["tags"] if t != lost]
                for lost, winners in losers.items():
                    out.append((when, f"{names[lost]} was defeated and ceased to exist, ending its wars with "
                                      f"{listed(winners)}."))
                continue
            partners: Dict[str, set] = defaultdict(set)
            for e in evs:
                a, b = e["tags"]
                partners[a].add(b)
                partners[b].add(a)
            done = set()
            # Two sides where everyone on one side is at war (or made peace) with everyone on the other
            by_set: Dict[frozenset, List[str]] = defaultdict(list)
            for t, ps in partners.items():
                by_set[frozenset(ps)].append(t)
            for ps, side in by_set.items():
                other = sorted(ps)
                if len(side) < 2 or len(other) < 2 or side[0] in done or \
                        any(partners[t] != set(side) for t in other):
                    continue
                done.update(side + other)
                out.append((when, f"War broke out between {listed(side)} on one side and {listed(other)} on the "
                                  f"other." if kind == "war" else
                                  f"Peace came between {listed(side)} on one side and {listed(other)} on the "
                                  f"other."))
            # The rest: around whoever has the most of them
            left = {t: {p for p in ps if p not in done} for t, ps in partners.items() if t not in done}
            while any(left.values()):
                hub = max(left, key=lambda t: (len(left[t]), major(t)))
                others = sorted(left[hub])
                side = [t for t in left if left[t] == left[hub]]     # others with just the same partners
                for t in side:
                    left[t] = set()
                for p in others:
                    left[p].difference_update(side)
                if len(side) > 1:
                    out.append((when, f"War broke out between {listed(side)} on one side and {listed(others)} on the "
                                      f"other." if kind == "war" else
                                      f"Peace came between {listed(side)} on one side and {listed(others)} on the "
                                      f"other."))
                else:
                    out.append((when, f"War broke out between {names[hub]} and {listed(others)}." if kind == "war"
                                      else f"{names[hub]} is no longer at war with {listed(others)}."))
    out = _merge_alike(out, events, names, listed)
    return [(when, _shorten_lists(text)) for when, text in out]


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
