"""Live game state through the bridge: the same data the HOI4-AI-Addon save parser extracts from a save,
read from the running game instead.

    from hoi4_rp import gamestate
    s = gamestate.country("FRA")
    s.name, s.leader, s.faction, s.enemies, s.ideas ...

Three sources, all live:
  bridge.query   runs one effect per country whose `log` lines print what script can see (names, leaders,
                 parties, faction, overlord/subjects, wars, neighbours, owned states), formatted by the game's
                 own localisation, so mods and cosmetic names are handled for free
  bridge.country the country's save-format block: numbers and ids (stability, party popularity, ideas, focuses)
  bridge.loc     display names for idea and focus ids

    python -m hoi4_rp state [TAG]   # print it
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from . import bridge

IDEOLOGIES = ["democratic", "communism", "fascism", "neutrality"]       # vanilla ideology groups
_PARTY_PROPS = {"democratic": "Democratic", "communism": "Communist", "fascism": "Fascist", "neutrality": "Neutral"}
_SEP = "\x1e"


class StateError(RuntimeError):
    pass


def _unescape(s: str) -> str:
    return re.sub(r"\\(.)", lambda m: "\n" if m.group(1) == "n" else m.group(1), s)


def query(effect: str) -> List[str]:
    """Run an effect in the game and return the lines its `log = "..."` statements printed."""
    reply = bridge.send(f"bridge.query {' '.join(effect.split())}")[0]
    if not reply.startswith("OK"):
        raise StateError(reply)
    body = reply[3:]
    # Older bridge builds keep the log stream's trailing line break on each line.
    return [_unescape(x).rstrip("\r\n") for x in body.split(_SEP)] if body else []


def loc(*keys: str) -> List[str]:
    """Localised text for each key, as the game shows it (a missing key comes back unchanged)."""
    if not keys:
        return []
    reply = bridge.send("bridge.loc " + " ".join(keys))[0]
    if not reply.startswith("OK"):
        raise StateError(reply)
    vals = [_unescape(x) for x in reply[3:].split(_SEP)]
    return vals + [""] * (len(keys) - len(vals))


# ---------------------------------------------------------------- save-format block helpers

def _walk(text: str, open_pos: int) -> int:
    """Index one past the } matching an already-consumed {."""
    depth, i, n = 1, open_pos, len(text)
    while depth and i < n:
        c = text[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
        i += 1
    return i


def _sub_block(text: str, key: str, indent: int) -> Optional[str]:
    m = re.search(r"\n" + "\t" * indent + re.escape(key) + r"=\{", text)
    if not m:
        return None
    return text[m.end():_walk(text, m.end()) - 1]


def _num(text: str, key: str) -> Optional[float]:
    m = re.search(r"\n\t\t" + re.escape(key) + r"=(-?[\d.]+)", text)
    return float(m.group(1)) if m else None


# ---------------------------------------------------------------- data

@dataclass
class Party:
    ideology: str                # party id = ideology group ("democratic", "totalist", ...)
    name: str
    leader: str
    popularity: float
    ideology_name: str = ""      # "Democratic", "Totalists"
    long_name: str = ""          # full name (with translation where the mod gives one)
    description: str = ""        # the mod's description of the party, if any
    ruling: bool = False


@dataclass
class Named:
    id: str
    name: str
    description: str = ""


@dataclass
class War:
    enemy: str                  # tag
    we_attacked: bool           # True if this country started the war
    start_date: str             # save format, e.g. "1939.9.1.12"
    our_casualties: int = 0
    their_casualties: int = 0


@dataclass
class Wargoal:
    other: str                  # tag: the target (ours) or the holder (against us)
    type: str = ""              # e.g. annex_everything; empty when held against us


@dataclass
class CountryState:
    tag: str
    date: str = ""
    name: str = ""
    adjective: str = ""
    leader: str = ""
    ruling_party: str = ""
    ruling_party_long: str = ""
    ideology: str = ""                  # ruling ideology group, e.g. "democratic"
    capital: str = ""
    is_major: bool = False
    capitulated: bool = False           # defeated in a war still going on
    stability: Optional[float] = None   # 0..1
    war_support: Optional[float] = None
    political_power: Optional[float] = None
    parties: List[Party] = field(default_factory=list)
    ideas: List[Named] = field(default_factory=list)          # visible national ideas, save order
    focuses: List[Named] = field(default_factory=list)        # completed national focuses
    faction: Optional[str] = None
    faction_leader: Optional[str] = None
    faction_members: List[str] = field(default_factory=list)
    overlord: Optional[str] = None
    subjects: List[str] = field(default_factory=list)
    enemies: List[str] = field(default_factory=list)          # currently at war with
    neighbors: List[str] = field(default_factory=list)
    states: List[Named] = field(default_factory=list)         # owned states (id, name)
    wars: List[War] = field(default_factory=list)             # one per enemy, with direction and start
    wargoals: List[Wargoal] = field(default_factory=list)     # justified against others
    wargoals_against: List[Wargoal] = field(default_factory=list)  # others' wargoals against us
    autonomy: Dict[str, str] = field(default_factory=dict)    # subject or overlord tag -> subject level id
    opinion: Dict[str, float] = field(default_factory=dict)   # this country's opinion of others (cached_sum)
    names: Dict[str, str] = field(default_factory=dict)       # display name of every tag above
    strength: Dict[str, float] = field(default_factory=dict)  # STRENGTH_FIELDS -> value (industry, army, manpower)

    def name_of(self, tag: Optional[str]) -> str:
        return self.names.get(tag, tag) if tag else ""

    def names_of(self, tags: List[str]) -> str:
        return ", ".join(self.name_of(t) for t in tags)


# Country variables the game exposes to script (documentation/dynamic_variables_documentation.md).
STRENGTH_FIELDS = {
    "civ": "num_of_civilian_factories",
    "mil": "num_of_military_factories",
    "dock": "num_of_naval_factories",
    "divisions": "num_divisions",
    "army_k": "deployed_army_manpower_k",
    "navy_k": "deployed_navy_manpower_k",
    "air_k": "deployed_airforce_manpower_k",
    "planes": "num_deployed_planes",
    "manpower_k": "manpower_k",           # recruitable pool
    "population_k": "max_manpower_k",     # total population
}


def _effect(tag: str) -> str:
    """One effect that logs everything script can tell us about a country, as `key|value` lines."""
    p = "AIRPQ|"
    lines = [
        f'log = "{p}date|[GetDateText]"',
        f'log = "{p}name|[THIS.GetName]"',
        f'log = "{p}adjective|[THIS.GetAdjective]"',
        f'log = "{p}leader|[THIS.GetLeader]"',
        f'log = "{p}ruling_party|[THIS.GetRulingParty]"',
        f'log = "{p}ruling_party_long|[THIS.GetRulingPartyLong]"',
        f'log = "{p}ideology|[THIS.GetRulingIdeology]"',
        f'log = "{p}capital|[THIS.Capital.GetName]"',
        f'if = {{ limit = {{ is_major = yes }} log = "{p}major|yes" }}',
        f'if = {{ limit = {{ has_capitulated = yes }} log = "{p}capitulated|yes" }}',
        f'log = "{p}strength|{"|".join(f"[?THIS.{v}]" for v in STRENGTH_FIELDS.values())}"',
    ]
    for ideo, prop in _PARTY_PROPS.items():
        lines.append(f'log = "{p}party|{ideo}|[THIS.Get{prop}Party]|[THIS.Get{prop}Leader]"')
    tn = "[THIS.GetTag]|[THIS.GetName]"
    lines += [
        f'if = {{ limit = {{ is_in_faction = yes }} '
        f'log = "{p}faction|[THIS.GetFactionName]|[THIS.FactionLeader.GetTag]|[THIS.FactionLeader.GetName]" '
        f'every_faction_member = {{ log = "{p}faction_member|{tn}" }} }}',
        f'if = {{ limit = {{ is_subject = yes }} log = "{p}overlord|[THIS.Overlord.GetTag]|[THIS.Overlord.GetName]" }}',
        f'every_subject_country = {{ log = "{p}subject|{tn}" }}',
        f'every_enemy_country = {{ log = "{p}enemy|{tn}" }}',
        f'every_neighbor_country = {{ log = "{p}neighbor|{tn}" }}',
        f'every_owned_state = {{ log = "{p}state|[THIS.GetID]|[THIS.GetName]" }}',
        # PREV inside the loop is the country being described.
        f'every_other_country = {{ limit = {{ has_wargoal_against = PREV }} log = "{p}wargoal_against|{tn}" }}',
    ]
    return f"{tag} = {{ {' '.join(lines)} }}"


def names_for(tags: List[str]) -> Dict[str, str]:
    """Display names for several tags in one query."""
    tags = [t for t in dict.fromkeys(tags) if t]
    if not tags:
        return {}
    eff = " ".join(f'{t} = {{ log = "AIRPN|[THIS.GetTag]|[THIS.GetName]" }}' for t in tags)
    out = {}
    for line in query(eff):
        if line.startswith("AIRPN|"):
            t, _, n = line[6:].partition("|")
            out[t.strip()] = n.strip()
    return out


# ---------------------------------------------------------------- diplomacy from save-format blocks

def _relations(block: str) -> Dict[str, str]:
    """diplomacy.active_relations: other tag -> its relation block."""
    dipl = _sub_block(block, "diplomacy", 2) or ""
    rel = _sub_block(dipl, "active_relations", 3) or ""
    out = {}
    for m in re.finditer(r"\n\t\t\t\t([A-Z0-9]{3})=\{", rel):
        out[m.group(1)] = rel[m.end():_walk(rel, m.end()) - 1]
    return out


def _field(text: str, key: str) -> Optional[str]:
    m = re.search(r"\b" + re.escape(key) + r'="?([^"\s]+)"?', text)
    return m.group(1) if m else None


def _war(relation: str, us: str) -> Optional[War]:
    """A war_relation block (stored in the attacker's relation to the defender), seen from `us`."""
    wr_start = re.search(r"\bwar_relation=\{", relation)
    if not wr_start:
        return None
    wr = relation[wr_start.end():_walk(relation, wr_start.end()) - 1]
    # Not filtered on hostility_reason_*: real wars can say "not_applicable" there. Callers keep only
    # enemies the game itself lists (every_enemy_country).
    first, second = _field(wr, "first"), _field(wr, "second")
    if us not in (first, second):
        return None
    cas = lambda k: int(float(_field(wr, k) or 0))
    we_first = us == first
    return War(enemy=second if we_first else first, we_attacked=we_first,
               start_date=_field(wr, "start_date") or "",
               our_casualties=cas("first_casualties" if we_first else "second_casualties"),
               their_casualties=cas("second_casualties" if we_first else "first_casualties"))


def _diplomacy(s: CountryState, block: str) -> None:
    for other, rel in _relations(block).items():
        cs = re.search(r"\bcached_sum=(-?[\d.]+)", rel)
        if cs:
            s.opinion[other] = float(cs.group(1))
        war = _war(rel, s.tag)
        if war and war.enemy in s.enemies:
            s.wars.append(war)
        pm = re.search(r"\bpuppet=\{", rel)
        if pm:
            pup = rel[pm.end():_walk(rel, pm.end()) - 1]
            first, second = _field(pup, "first"), _field(pup, "second")
            if s.tag in (first, second):
                s.autonomy[second if first == s.tag else first] = _field(pup, "autonomy_state") or ""
    dipl = _sub_block(block, "diplomacy", 2) or ""
    for wg in re.finditer(r'wargoaldata_actor="([A-Z0-9]{3})"\s*wargoaldata_recipient="([A-Z0-9]{3})"\s*type=(\w+)',
                          _sub_block(dipl, "wargoals", 3) or ""):
        if wg.group(1) == s.tag:
            s.wargoals.append(Wargoal(wg.group(2), wg.group(3)))
    # A war we're defending in is stored in the attacker's block.
    for enemy in s.enemies:
        if any(w.enemy == enemy for w in s.wars):
            continue
        war = _war(_relations(bridge.country_block(enemy)).get(s.tag, ""), s.tag)
        s.wars.append(war or War(enemy=enemy, we_attacked=False, start_date=""))


def _diplomacy_level(s: CountryState) -> None:
    rel = _relations(bridge.country_block(s.overlord)).get(s.tag, "")
    pup = re.search(r"\bpuppet=\{", rel)
    if pup:
        s.autonomy[s.overlord] = _field(rel[pup.end():_walk(rel, pup.end()) - 1], "autonomy_state") or ""


def _named(ids: List[str], hide_missing: bool) -> List[Named]:
    """Pair ids with their display names. Like the save parser, an idea whose name doesn't resolve
    (or whose id ends in _hidden) is treated as hidden; a level suffix (_2) falls back to the base key."""
    if not ids:
        return []
    n = len(ids)
    bases = [re.sub(r"_\d+$", "", i) for i in ids]
    keys = ids + bases + [f"{i}_desc" for i in ids] + [f"{b}_desc" for b in bases]
    vals = loc(*keys)
    found = lambda k: vals[k] if vals[k] and vals[k] != keys[k] else ""   # a missing key comes back unchanged
    out = []
    for i, fid in enumerate(ids):
        name = found(i) or (found(n + i) if bases[i] != fid else "")
        desc = found(2 * n + i) or (found(3 * n + i) if bases[i] != fid else "")
        if hide_missing and (not name or fid.endswith("_hidden")):
            continue
        out.append(Named(fid, name or fid, desc))
    return out


def country(tag: str) -> CountryState:
    tag = tag.upper()
    s = CountryState(tag)
    parties: Dict[str, Party] = {}
    for line in query(_effect(tag)):
        if not line.startswith("AIRPQ|"):
            continue
        key, _, rest = line[6:].partition("|")
        rest = rest.strip()
        if key == "party":
            ideo, name, leader = (rest.split("|") + ["", ""])[:3]
            parties[ideo] = Party(ideo, name, leader, 0.0)
        elif key in ("faction_member", "subject", "enemy", "neighbor"):
            ctag, _, cname = rest.partition("|")
            s.names[ctag] = cname or ctag
            getattr(s, {"faction_member": "faction_members", "subject": "subjects",
                        "enemy": "enemies", "neighbor": "neighbors"}[key]).append(ctag)
        elif key == "state":
            sid, _, sname = rest.partition("|")
            s.states.append(Named(sid, sname))
        elif key == "faction":
            s.faction, s.faction_leader, lname = (rest.split("|") + ["", ""])[:3]
            s.names[s.faction_leader] = lname or s.faction_leader
        elif key == "overlord":
            s.overlord, _, oname = rest.partition("|")
            s.names[s.overlord] = oname or s.overlord
        elif key == "major":
            s.is_major = True
        elif key == "capitulated":
            s.capitulated = True
        elif key == "strength":
            for name, value in zip(STRENGTH_FIELDS, rest.split("|")):
                try:
                    s.strength[name] = float(value.strip())
                except ValueError:
                    pass
        elif key == "wargoal_against":
            ctag, _, cname = rest.partition("|")
            s.names[ctag] = cname or ctag
            s.wargoals_against.append(Wargoal(ctag))
        elif hasattr(s, key):
            setattr(s, key, rest)
    if not s.name:
        raise StateError(f"no data for {tag} (does it exist?)")
    s.names[tag] = s.name

    block = bridge.country_block(tag)
    s.stability, s.war_support = _num(block, "stability"), _num(block, "war_support")
    politics = _sub_block(block, "politics", 2) or ""
    m = re.search(r"\n\t\t\tpolitical_power=(-?[\d.]+)", politics)
    s.political_power = float(m.group(1)) if m else None
    s.parties = _parties(tag, politics, parties, s.leader)
    s.ideas = _named((_sub_block(politics, "ideas", 3) or "").split(), hide_missing=True)
    focus_block = _sub_block(block, "focus", 2) or ""
    s.focuses = _named(re.findall(r'completed="([^"]+)"', focus_block), hide_missing=False)
    _diplomacy(s, block)
    if s.overlord and s.overlord not in s.autonomy:      # the relation is kept in the overlord's block
        _diplomacy_level(s)
    s.names.update(names_for([w.other for w in s.wargoals if w.other not in s.names]))
    return s


def _parties(tag: str, politics: str, queried: Dict[str, Party], ruling_leader: str) -> List[Party]:
    """Every party the country has (vanilla's 4 ideology groups or a mod's, e.g. Kaiserreich's 10), read from
    its politics block, with names from the localisation: a name override in the block, else the standard
    {TAG}_{ideology}_party / _party_long keys. `queried` holds what script could tell (vanilla's per-group
    party name and leader), used where the localisation has nothing."""
    party_block = _sub_block(politics, "parties", 3) or ""
    rm = re.search(r"\n\t\t\truling_party=\"?(\w+)", politics)
    ruling = rm.group(1) if rm else ""
    ids = re.findall(r"\n\t\t\t\t([a-z_]+)=\{", party_block)
    raw = {}
    for ideo in ids:
        pb = _sub_block(party_block, ideo, 4) or ""
        pm = re.search(r"\bpopularity=([\d.]+)", pb)
        nm, lm = re.search(r'\bname="([^"]+)"', pb), re.search(r'\blong_name="([^"]+)"', pb)
        raw[ideo] = (float(pm.group(1)) if pm else 0.0,
                     nm.group(1) if nm else f"{tag}_{ideo}_party", lm.group(1) if lm else f"{tag}_{ideo}_party_long")
    keys = [k for ideo in ids for k in (raw[ideo][1], raw[ideo][2], ideo)]
    vals = loc(*keys) if keys else []
    found = lambda i: vals[i] if i < len(vals) and vals[i] and vals[i] != keys[i] else ""
    out = []
    for n, ideo in enumerate(ids):
        pop = raw[ideo][0]
        q = queried.get(ideo)
        short, long_raw, ideo_name = found(3 * n), found(3 * n + 1), found(3 * n + 2) or ideo.replace("_", " ")
        # A mod's long name can hold the full name, its translation and a description, one per line.
        lines = [clean_loc(l) for l in long_raw.split("\n")]
        lines = [l for l in lines if l]
        names = [l for l in lines if len(l) <= 160]
        desc = " ".join(l for l in lines if len(l) > 160)
        out.append(Party(ideology=ideo, name=clean_loc(short) or (q.name if q else "") or ideo_name,
                         leader=(q.leader if q else "") or (ruling_leader if ideo == ruling else ""),
                         popularity=pop, ideology_name=clean_loc(ideo_name), long_name=" / ".join(names),
                         description=desc, ruling=ideo == ruling))
    return sorted(out, key=lambda p: -p.popularity)


_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def save_date(raw: str) -> str:
    """'1939.9.1.12' -> '1 Sep 1939'."""
    parts = raw.split(".")
    try:
        return f"{int(parts[2])} {_MONTHS[int(parts[1]) - 1]} {int(parts[0])}"
    except (IndexError, ValueError):
        return raw


def player_tag() -> str:
    return bridge.player_tag()


def clean_loc(text: str) -> str:
    """Strip HOI4 formatting from raw localisation: colour codes, icons, and references that need a
    scope to resolve ([Root.GetName], $KEY$)."""
    text = re.sub(r"[§\x11].", "", text)  # colour codes §Y ... §! (the game holds § as 0x11 in memory)
    text = re.sub(r"£\w+(\|\w+)?", "", text)    # £icon
    text = re.sub(r"\[[^\]]*\]|\$[^$\s]*\$", "", text)
    text = re.sub(r"\s*\n\s*", " ", text)
    return re.sub(r" {2,}", " ", text).strip()


def _ruling_name(s: CountryState) -> str:
    """The ruling party's clean full name (a mod's long-name text can hold a whole description)."""
    p = next((p for p in s.parties if p.ruling), None)
    if p:
        return p.long_name or p.name
    return clean_loc((s.ruling_party_long or s.ruling_party).split("\n")[0])


def _men(thousands: float) -> str:
    """12.5 -> '12,500', 1332.6 -> '1.33 million'."""
    n = thousands * 1000
    return f"{n / 1e6:.2f} million" if n >= 1e6 else f"{n:,.0f}"


def strength_text(s: CountryState) -> str:
    """Industry, armed forces and manpower in one paragraph (empty if unknown)."""
    v = s.strength
    if not v:
        return "Strength: unknown."
    g = lambda k: v.get(k, 0)
    return (f"Industry: {g('civ'):.0f} civilian factories, {g('mil'):.0f} military factories, {g('dock'):.0f} "
            f"dockyards. Armed forces: {g('divisions'):.0f} divisions ({_men(g('army_k'))} soldiers deployed), "
            f"navy {_men(g('navy_k'))} sailors, air force {g('planes'):.0f} planes ({_men(g('air_k'))} airmen). "
            f"Manpower available to recruit: {_men(g('manpower_k'))}; population {_men(g('population_k'))}.")


def strength_comparison(a: CountryState, b: CountryState) -> str:
    """One line comparing two countries' factories, divisions and deployed soldiers."""
    if not a.strength or not b.strength:
        return ""
    def ratio(k: str) -> str:
        x, y = a.strength.get(k, 0), b.strength.get(k, 0)
        if y == 0:
            return "no comparison" if x == 0 else "unmatched"
        return f"{x / y:.1f}x"
    fac = lambda s: s.strength.get("civ", 0) + s.strength.get("mil", 0) + s.strength.get("dock", 0)
    return (f"Strength: {a.name} has {fac(a):.0f} factories ({a.strength.get('mil', 0):.0f} military) to "
            f"{b.name}'s {fac(b):.0f} ({b.strength.get('mil', 0):.0f} military), {a.strength.get('divisions', 0):.0f} "
            f"divisions to {b.strength.get('divisions', 0):.0f}, {_men(a.strength.get('army_k', 0))} soldiers to "
            f"{_men(b.strength.get('army_k', 0))} (military factories {ratio('mil')}, soldiers {ratio('army_k')}).")


def describe(s: CountryState, max_states: Optional[int] = None, details: bool = True) -> str:
    """Readable summary, e.g. for an LLM prompt. Complete by default: every state, and the
    descriptions of national spirits and completed focuses. max_states/details=False shorten it."""
    pct = lambda v: "?" if v is None else f"{round(v * 100)}%"
    lines = [
        f"{s.name} ({s.tag}){', a major power' if s.is_major else ''}. Adjective: {s.adjective}. Capital: {s.capital}.",
        f"Led by {s.leader} of the {_ruling_name(s)} ({s.ideology}).",
        f"Stability {pct(s.stability)}, war support {pct(s.war_support)}"
        + (f", political power {s.political_power:.0f}." if s.political_power is not None else "."),
        strength_text(s),
        "Parties: " + "; ".join(
            f"{p.name}" + (f" [{p.long_name}]" if p.long_name and p.long_name != p.name else "")
            + f" ({p.ideology_name or p.ideology}, {round(p.popularity, 1):g}% support"
            + (f", led by {p.leader}" if p.leader else "") + (", in power" if p.ruling else "") + ")"
            for p in s.parties) + ".",
    ]
    ruling = next((p for p in s.parties if p.ruling and p.description), None)
    if ruling and details:
        lines.append(f"About the ruling party, {ruling.name}: {ruling.description}")
    if s.faction:
        lines.append(f"Member of the faction {s.faction} (leader {s.name_of(s.faction_leader)}; "
                     f"members {s.names_of(s.faction_members)}).")
    else:
        lines.append("Not in any faction.")
    from . import subjects
    auto = lambda t: f" ({subjects.name(s.autonomy[t])})" if s.autonomy.get(t) else ""
    if s.overlord:
        lvl = s.autonomy.get(s.overlord, "")
        rules = subjects.rules_text(lvl, s.name, s.name_of(s.overlord)) if lvl else ""
        lines.append(f"NOT INDEPENDENT: a subject of {s.name_of(s.overlord)}{auto(s.overlord)}. " + (rules or ""))
    if s.subjects:
        lines.append("Subjects it controls, with their level: "
                     + "; ".join(s.name_of(t) + auto(t) for t in s.subjects) + ".")
    if s.capitulated:
        lines.append("HAS CAPITULATED: its armies are beaten and its government has surrendered or fled; it can no "
                     "longer resist, and the winners will decide its fate at the peace.")
    if s.wars:
        wars = []
        for w in s.wars:
            how = "which it attacked" if w.we_attacked else "which attacked it"
            since = f", since {save_date(w.start_date)}" if w.start_date else ""
            cas = (f"; casualties {w.our_casualties:,} suffered, {w.their_casualties:,} inflicted"
                   if w.our_casualties or w.their_casualties else "")
            wars.append(f"{s.name_of(w.enemy)} ({how}{since}{cas})")
        lines.append("At war with: " + "; ".join(wars) + ".")
    else:
        lines.append("At peace.")
    if s.wargoals:
        lines.append("Holds war goals against: " + ", ".join(
            f"{s.name_of(w.other)} ({w.type.replace('_', ' ')})" for w in s.wargoals) + ".")
    if s.wargoals_against:
        lines.append("Countries with war goals against it: " + s.names_of([w.other for w in s.wargoals_against]) + ".")
    if s.neighbors:
        lines.append(f"Neighbours: {s.names_of(s.neighbors)}.")
    if s.states:
        shown = s.states if max_states is None else s.states[:max_states]
        more = f" and {len(s.states) - len(shown)} more" if len(shown) < len(s.states) else ""
        lines.append(f"Owns {len(s.states)} states: {', '.join(x.name for x in shown)}{more}.")

    def items(title: str, entries: List[Named]) -> None:
        if not entries:
            return
        if not details:
            lines.append(f"{title}: " + ", ".join(e.name for e in entries) + ".")
            return
        lines.append(f"{title}:")
        for e in entries:
            desc = clean_loc(e.description)
            lines.append(f"- {e.name}" + (f": {desc}" if desc else ""))

    items("National spirits and laws", s.ideas)
    items("Completed national focuses", s.focuses)
    return "\n".join(lines)
