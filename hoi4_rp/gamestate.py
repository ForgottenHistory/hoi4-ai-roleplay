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
    ideology: str
    name: str
    leader: str
    popularity: float


@dataclass
class Named:
    id: str
    name: str


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
    names: Dict[str, str] = field(default_factory=dict)       # display name of every tag above

    def name_of(self, tag: Optional[str]) -> str:
        return self.names.get(tag, tag) if tag else ""

    def names_of(self, tags: List[str]) -> str:
        return ", ".join(self.name_of(t) for t in tags)


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
    ]
    return f"{tag} = {{ {' '.join(lines)} }}"


def _named(ids: List[str], hide_missing: bool) -> List[Named]:
    """Pair ids with their display names. Like the save parser, an idea whose name doesn't resolve
    (or whose id ends in _hidden) is treated as hidden; a level suffix (_2) falls back to the base key."""
    if not ids:
        return []
    bases = [re.sub(r"_\d+$", "", i) for i in ids]
    names = loc(*ids, *bases)
    out = []
    for i, (fid, base) in enumerate(zip(ids, bases)):
        name = names[i] if names[i] and names[i] != fid else ""
        if not name and base != fid and names[len(ids) + i] != base:
            name = names[len(ids) + i]
        if hide_missing and (not name or fid.endswith("_hidden")):
            continue
        out.append(Named(fid, name or fid))
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
    party_block = _sub_block(politics, "parties", 3) or ""
    for ideo in list(parties) + [i for i in IDEOLOGIES if i not in parties]:
        pb = _sub_block(party_block, ideo, 4)
        if pb is None:
            continue
        pm = re.search(r"\bpopularity=([\d.]+)", pb)
        party = parties.setdefault(ideo, Party(ideo, ideo, "", 0.0))
        party.popularity = float(pm.group(1)) if pm else 0.0
    s.parties = sorted(parties.values(), key=lambda p: -p.popularity)
    s.ideas = _named((_sub_block(politics, "ideas", 3) or "").split(), hide_missing=True)
    focus_block = _sub_block(block, "focus", 2) or ""
    s.focuses = _named(re.findall(r'completed="([^"]+)"', focus_block), hide_missing=False)
    return s


def player_tag() -> str:
    return bridge.player_tag()


def describe(s: CountryState, max_states: int = 12) -> str:
    """Readable summary, e.g. for an LLM prompt."""
    pct = lambda v: "?" if v is None else f"{round(v * 100)}%"
    lines = [
        f"{s.name} ({s.tag}){', a major power' if s.is_major else ''}. Capital: {s.capital}.",
        f"Led by {s.leader} of the {s.ruling_party_long or s.ruling_party} ({s.ideology}).",
        f"Stability {pct(s.stability)}, war support {pct(s.war_support)}.",
        "Party support: " + ", ".join(f"{p.name or p.ideology} ({p.ideology}) {p.popularity:g}%"
                                      for p in s.parties if p.popularity > 0) + ".",
    ]
    if s.faction:
        lines.append(f"Member of the faction {s.faction} (leader {s.name_of(s.faction_leader)}; "
                     f"members {s.names_of(s.faction_members)}).")
    else:
        lines.append("Not in any faction.")
    if s.overlord:
        lines.append(f"A subject of {s.name_of(s.overlord)}.")
    if s.subjects:
        lines.append(f"Subjects: {s.names_of(s.subjects)}.")
    lines.append(f"At war with: {s.names_of(s.enemies)}." if s.enemies else "At peace.")
    if s.neighbors:
        lines.append(f"Neighbours: {s.names_of(s.neighbors)}.")
    if s.states:
        more = f" and {len(s.states) - max_states} more" if len(s.states) > max_states else ""
        lines.append(f"Owns {len(s.states)} states: {', '.join(x.name for x in s.states[:max_states])}{more}.")
    if s.ideas:
        lines.append("National spirits and laws: " + ", ".join(i.name for i in s.ideas) + ".")
    if s.focuses:
        lines.append("Completed focuses: " + ", ".join(f.name for f in s.focuses) + ".")
    return "\n".join(lines)
