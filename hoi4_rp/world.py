"""The world the game is set in, for the LLM: vanilla history, or a mod's alternate history (Kaiserreich and
Kaiserredux, or Half-Life in Unforeseen Consequences), plus optional setting notes from a file. Chosen in config.ini
[world]:

    setting = auto          auto (default) detects Kaiserreich or Half-Life from the player's ideology groups
    lore_file =             optional text file with extra setting notes, added to every prompt

The setting text opens every prompt (letters, replies, policies). The live game data that follows is
always authoritative; the setting only tells the LLM which history to reason from.
"""

from __future__ import annotations

import os
from typing import Optional

from .config import settings

KAISERREICH_IDEOLOGIES = {"totalist", "syndicalist", "radical_socialist", "social_democrat", "social_liberal",
                          "market_liberal", "social_conservative", "authoritarian_democrat", "paternal_autocrat",
                          "national_populist"}

_VANILLA = ("This game follows real history from 1936, but the live situation below may already have diverged "
            "from it: trust the situation over your own historical knowledge.")

_KAISERREICH = (
    "SETTING: this game is set in the Kaiserreich alternate history (the Kaiserredux version). Real history "
    "after 1917 did not happen here; reason from this world, and trust the live situation below over anything "
    "you know of either history.\n"
    "- Germany won the Great War (the Weltkrieg) in 1919; there was no Treaty of Versailles. The German Empire "
    "dominates Europe and leads Mitteleuropa, a bloc of client states carved from Russia's former western lands "
    "(among them Poland, the United Baltic Duchy, Lithuania, White Ruthenia, Ukraine and Finland).\n"
    "- The defeated Entente fell to revolution. France is the syndicalist Commune of France, while the old "
    "government holds on in North Africa as National France. Britain is the syndicalist Union of Britain, while "
    "the monarchy and what remains of the Entente continue from Canada. Syndicalism, organised in the Third "
    "Internationale, is Germany's great ideological rival.\n"
    "- Russia lost the war and its western lands and is weak and resentful. Austria-Hungary survived but strains "
    "under its nationalities. The Ottoman Empire endures.\n"
    "- A stock market crash in Berlin, Black Monday, shook the world economy and Germany's confidence; the United "
    "States is divided and unstable.\n"
    "- China is fragmented among cliques, restorationists and foreign spheres of influence; Japan is the leading "
    "power in Asia.\n"
    "- Politics run along ten ideologies, from left to right: totalist, syndicalist, radical socialist, social "
    "democrat, social liberal, market liberal, social conservative, authoritarian democrat, paternal autocrat "
    "and national populist."
)


# Unforeseen Consequences, a Half-Life total conversion (Steam workshop 3635283949): starts 2000 or 2015.
HALFLIFE_IDEOLOGIES = {"combine_ascensionism", "combine_homosapience", "vortigaunt_rebels", "xen_hordes"}

_HALFLIFE = (
    "SETTING: this game is set in the Half-Life universe (the Unforeseen Consequences mod). Real history holds up "
    "to the year 2000; after that the world below follows Half-Life, not what really happened. Trust the live "
    "situation below over anything you know.\n"
    "- In 2000 the Cold War is over and the United States is the sole superpower. Two rival American research "
    "giants, Black Mesa (teleportation, exotic matter from the border world Xen) and Aperture Science, compete "
    "for government money. Few know how dangerous their work is.\n"
    "- The Black Mesa incident, a resonance cascade, tore open portals to Xen: alien creatures (headcrabs, "
    "antlions, vortigaunts and worse) spill across the Earth through portal storms. The US military's HECU was "
    "sent to cover it up.\n"
    "- The Combine, a vast inter-dimensional empire (the Universal Union) that enslaves species across many "
    "worlds, then invaded and defeated Earth in the Seven Hour War. Dr Wallace Breen, Black Mesa's "
    "administrator, negotiated Earth's surrender and governs it for them from City 17's Citadel. Under Combine "
    "rule humans live in policed cities, families are split up, reproduction is suppressed, and Civil "
    "Protection and the transhuman Overwatch soldiers keep order; the Resistance and the vortigaunts fight back "
    "from hiding.\n"
    "- Which stage the world is at (before the incident, the invasion, or years of occupation) is shown by the "
    "date and the live situation: who exists, who rules where, who is at war.\n"
    "- Not every country is a human government. The Combine speaks with cold, bureaucratic menace and treats "
    "humans as a resource. Xen creature hordes (Xenian Fauna) have no politics and cannot negotiate. Warlords, "
    "bandits and militias are survivors, not statesmen.")


def detect(party_ideologies) -> str:
    """'halflife' or 'kaiserreich' if the parties use those mods' ideology groups, else 'vanilla'."""
    found = set(party_ideologies)
    if HALFLIFE_IDEOLOGIES & found:
        return "halflife"
    return "kaiserreich" if KAISERREICH_IDEOLOGIES & found else "vanilla"


def setting_for(party_ideologies) -> str:
    chosen = settings().world.setting
    return detect(party_ideologies) if chosen == "auto" else chosen


def text(party_ideologies) -> str:
    """The setting paragraph(s) for the top of a prompt."""
    body = {"kaiserreich": _KAISERREICH, "halflife": _HALFLIFE}.get(setting_for(party_ideologies), _VANILLA)
    notes = _lore_notes()
    return body + (f"\n\nSETTING NOTES FROM THE PLAYER:\n{notes}" if notes else "")


def _lore_notes() -> Optional[str]:
    path = settings().world.lore_file
    if not path:
        return None
    if not os.path.isabs(path):
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), path)
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip() or None
    except OSError as e:
        print(f"lore_file {path} could not be read: {e}")
        return None


# ---------------------------------------------------------------- world tension
# How tense the world is (0 to 1, shown as %), and what that lets each government do: HOI4 ties guarantees,
# factions, lend-lease, volunteers and war goals to it, with thresholds set per ideology and moved by national spirits
# (the USA's neutrality acts put a guarantee at 150%: never). Read live, so mods' values count.
#     [?global.threat]                 world tension
#     [?THIS.modifier@guarantee_tension] ...   a country's own thresholds (0 or less: always)

TENSION_RULES = [("guarantee_tension", "guarantee another country"), ("join_faction_tension", "join or form a faction"),
                 ("lend_lease_tension", "send lend-lease"), ("send_volunteers_tension", "send volunteers"),
                 ("generate_wargoal_tension", "justify war goals")]


def tension(tags) -> tuple:
    """(world tension 0..1, {tag: {rule: threshold}}) for these countries, from the running game."""
    from . import gamestate
    eff = 'log = "AIRPWT|[?global.threat]" ' + " ".join(
        f'{t} = {{ log = "AIRPWR|{t}|' + "|".join(f"[?THIS.modifier@{k}]" for k, _ in TENSION_RULES) + '" }'
        for t in dict.fromkeys(tags) if t)
    level, rules = 0.0, {}
    num = lambda x: float(x) if x.strip().replace(".", "", 1).replace("-", "", 1).isdigit() else 0.0
    for line in gamestate.query(eff):
        f = [x.strip() for x in line.split("|")]
        if f[0] == "AIRPWT" and len(f) > 1:
            level = num(f[1])
        elif f[0] == "AIRPWR" and len(f) == 2 + len(TENSION_RULES):
            rules[f[1]] = {k: num(v) for (k, _), v in zip(TENSION_RULES, f[2:])}
    return level, rules


def _pct(x: float) -> str:
    return f"{round(x * 100)}%"


def tension_line(name: str, level: float, rules: dict) -> str:
    """'France may now send lend-lease; needs more tension to guarantee another country (25%), ...'"""
    may = [label for k, label in TENSION_RULES if rules.get(k, 0) <= level]
    later = [f"{label} ({_pct(rules[k])})" for k, label in sorted(TENSION_RULES, key=lambda r: rules.get(r[0], 0))
             if level < rules.get(k, 0) <= 1]
    never = [label for k, label in TENSION_RULES if rules.get(k, 0) > 1]
    parts = [f"may now {', '.join(may)}" if may else "may do none of these yet"]
    if later:
        parts.append("needs more tension to " + ", ".join(later))
    if never:
        parts.append("may not " + ", ".join(never) + " at any tension (its national policy forbids it)")
    return f"- {name} " + "; ".join(parts) + "."


def tension_text(names: dict) -> str:
    """For prompts: world tension, and what it allows each of these countries ({tag: name})."""
    try:
        level, rules = tension(list(names))
    except Exception as e:                          # the game not answering: leave it out rather than fail a letter
        print(f"  world tension could not be read: {e}")
        return ""
    lines = [tension_line(n, level, rules[t]) for t, n in names.items() if t in rules]
    if settings().world.tension_rules:
        lines.append("Deals by letter keep to these rules too: a guarantee only once the guarantor may give one, a "
                     "faction only once both sides may join one. Offer, ask or promise nothing else of the kind.")
    return (f"WORLD TENSION: {_pct(level)}. It rises with every war goal justified, annexation and war, and falls "
            f"with guarantees and lasting peace; the game ties what governments may do to it (low tension: a quiet "
            f"world that would see guarantees, alliances and war preparations as alarmism; high: a world bracing for "
            f"war):\n" + "\n".join(lines))
