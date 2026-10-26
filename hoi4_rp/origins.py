"""How the wars began: who declared war on whom, and when, read from the game itself; and why, where the game or the
chronicle can tell (a guarantee honoured, a faction's war, a letter deal).

Every pair of countries at war has a war_relation in the save format, kept in the declarer's own block: first is the
country that declared war on second, start_date is to the hour. Checked in a 1939 save: German Reich -> Poland on
1939.8.31.2, then United Kingdom -> German Reich and France -> German Reich on 1939.8.31.3, the guarantors declaring an
hour later. That is not the game's attacker/defender split (has_offensive_war_with), which follows the war as a whole:
France, declaring war on Germany for Poland, is on the defending side. The chronicle's own war lines are dated when a
snapshot first saw the war, which can be days late; these dates are the game's.

    text(camp, them, me)   the section for a prompt: the wars either country is in, declaration by declaration
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from typing import Dict, List, Set, Tuple

from . import bridge, campaign, gamestate

MAX_LINES = 12           # declarations told per prompt; the rest are counted
MAX_NAMES = 4            # declarers named in one line; the rest are counted


@dataclass
class Declaration:
    first: str           # declared war
    second: str          # on this country
    date: str            # date key, to the hour


def declarations(tags: Set[str]) -> List[Declaration]:
    """The war declarations made by these countries that still stand (their wars now)."""
    out = []
    for tag in sorted(tags):
        try:
            block = bridge.country_block(tag)
        except Exception:
            continue
        for other, rel in gamestate._relations(block).items():
            m = re.search(r"\bwar_relation=\{", rel)
            if not m:
                continue
            wr = rel[m.end():gamestate._walk(rel, m.end()) - 1]
            if gamestate._field(wr, "first") == tag and gamestate._field(wr, "second") == other:
                out.append(Declaration(tag, other, campaign.save_key(gamestate._field(wr, "start_date") or "")))
    return out


def _component(decls: List[Declaration], roots: Set[str]) -> List[Declaration]:
    """The declarations connected to `roots`: the wars they are in, not unrelated wars elsewhere."""
    reach, grew = set(roots), True
    while grew:
        grew = False
        for d in decls:
            if (d.first in reach) != (d.second in reach):
                reach |= {d.first, d.second}
                grew = True
    return [d for d in decls if d.first in reach and d.second in reach]


def _why(camp: campaign.Campaign, d: Declaration, decls: List[Declaration], factions: Dict[str, str],
         name) -> str:
    """Why `d.first` declared, where it can be told."""
    near = lambda e: abs(_days(e["date"]) - _days(d.date)) <= 3
    for e in camp.data["events"]:
        kind, tags = e.get("type", ""), e.get("tags", [])
        if len(tags) < 3 or tags[2] != d.second or not near(e):
            continue
        if kind.startswith("ask:war:") and tags[1] == d.first:        # the player joined them's war
            return f"joining {name(tags[0])}'s war, by an agreement made in letters"
        if kind.startswith("give:war:") and tags[0] == d.first:        # them joined the player's war
            return f"joining {name(tags[1])}'s war, by an agreement made in letters"
        if kind == "treaty:clause" and e.get("joiner") == d.first:
            ally = tags[0] if tags[1] == d.first else tags[1]
            return f"honouring a war pledge to {name(ally)}"
    # The same hour counts as before: a guarantor's or faction's declaration can come in the same tick
    earlier = [x for x in decls if x is not d and x.date <= d.date]
    victims = [x.second for x in earlier if x.first == d.second]          # whom the target had attacked
    for v in victims:
        if v != d.first and bridge.trigger(f"{d.first} = {{ has_guaranteed = {v} }}"):
            return f"honouring its guarantee of {name(v)}"
    fighting = ({x.first for x in earlier if x.second == d.second}
                | {x.second for x in earlier if x.first == d.second}) - {d.first}
    fac = factions.get(d.first)
    if fac and any(factions.get(t) == fac for t in fighting):
        return f"called into the war by its faction, the {fac}"
    return ""


def text(camp: campaign.Campaign, them: "gamestate.CountryState", me: "gamestate.CountryState") -> str:
    """How the wars `them` and `me` are in began, for a prompt ('' if neither is at war)."""
    if not them.enemies and not me.enemies:
        return ""
    countries = (camp.snapshot or {}).get("countries", {})
    name = lambda t: them.names.get(t) or me.names.get(t) or countries.get(t, {}).get("name") or t
    factions = {t: c.get("faction", "") for t, c in countries.items() if c.get("faction")}
    majors_at_war = {line[2:].strip() for line in gamestate.query(
        'every_country = { limit = { is_major = yes has_war = yes } log = "M|[THIS.GetTag]" }') if line.startswith("M|")}
    # Who may have declared in these wars: the two, everyone at war with either, their allies at war, the majors
    tags = ({them.tag, me.tag} | set(them.enemies) | set(me.enemies) | majors_at_war
            | {t for t in them.faction_members + me.faction_members if t})
    decls = sorted(_component(declarations(tags), {them.tag, me.tag} if me.enemies and them.enemies
                              else {them.tag} if them.enemies else {me.tag}), key=lambda d: d.date)
    if not decls:
        return ""
    # One line per (day, target, reason): "United Kingdom, France and 9 others declared war on German Reich, ..."
    groups: Dict[Tuple[str, str, str], List[str]] = defaultdict(list)
    first_at: Dict[Tuple[str, str, str], str] = {}
    for d in decls:                                               # in time order, so a group's first is its earliest
        key = (d.date[:10], d.second, _why(camp, d, decls, factions, name))
        groups[key].append(d.first)
        first_at.setdefault(key, d.date)
    lines = []
    for (day, target, why), firsts in groups.items():
        firsts.sort(key=lambda t: (t not in (them.tag, me.tag), t not in majors_at_war, name(t)))
        shown = [name(t) for t in firsts[:MAX_NAMES]]
        who = (", ".join(shown[:-1]) + " and " + shown[-1] if len(shown) > 1 else shown[0]) \
            if len(firsts) <= MAX_NAMES else f"{', '.join(shown)} and {len(firsts) - MAX_NAMES} others"
        weight = (any(t in (them.tag, me.tag) for t in firsts + [target]) * 2
                  + any(t in majors_at_war for t in firsts + [target]))
        lines.append((first_at[(day, target, why)], weight,
                      f"- {campaign.date_words(day + '.00')}: {who} declared war on {name(target)}"
                      + (f", {why}" if why else "") + "."))
    kept = sorted(lines, key=lambda x: (-x[1], x[0]))[:MAX_LINES]
    more = len(lines) - len(kept)
    body = "\n".join(text for _, _, text in sorted(kept, key=lambda x: x[0]))
    return (f"HOW THE WARS BEGAN (the game's own record of who declared war on whom, and when; trust these dates over "
            f"anything else, including the chronicle below, which notes a war only when it was first seen):\n{body}"
            + (f"\n(and {more} more declarations)" if more > 0 else "")
            + "\nJoining a war, for a guarantee or a faction, is declaring war too: the country that joins is the one "
              "that declared.")


def _days(key: str) -> int:
    import datetime
    try:
        y, m, d = (int(x) for x in key.split(".")[:3])
        return datetime.date(y, m, d).toordinal()
    except ValueError:
        return 0
