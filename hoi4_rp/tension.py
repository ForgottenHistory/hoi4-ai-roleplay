"""World tension for what our deals and ultimatums do, as the game adds it for its own.

The game raises world tension when a country declares war, takes land in a peace or justifies a war goal, but the
script effects our deals use (declare_war_on, transfer_state, puppet, annex_country) skip that. So the one who gains
gets add_named_threat, the effect the game's own focuses and events use (Germany's Sudetenland demand +4, Memel +3,
Danzig +5, the end of Czechoslovakia +5), named so the game's tension tooltip says where it came from:

    an ultimatum's war                  the game's own tension for a war with a war goal (TENSION_CB_WAR, read from
                                        the game's and mods' defines: 10 in vanilla)
    land handed over under threat       LAND for the first state, +LAND_MORE for each further one, at most LAND_MAX
    submission                          SUBMIT under threat, SUBMIT_FREE given freely
    annexation of an independent state  ANNEX under threat, ANNEX_FREE given freely (a subject joining its overlord:
                                        none, as the game's integration)

Under threat: an ultimatum from the one who gains to the one who gives stands (ultimatums.py), or the player
threatened that country in a letter within reputation.THREAT_DAYS (the game master's reading). Land sold or given
freely adds nothing.

    deal(camp, events)                after a deal is put in place (its events, as actions.apply() returns them)
    war(by, against)                  after an ultimatum's declaration of war
"""

from __future__ import annotations

import os
import re
from typing import Dict, List, Optional, Tuple

from . import actions, campaign

LAND, LAND_MORE, LAND_MAX = 3, 1, 6
SUBMIT, SUBMIT_FREE = 5, 2
ANNEX, ANNEX_FREE = 6, 3
NAMES = {"war": ("airp_threat_ultimatum_war", "War after an ultimatum"),
         "land": ("airp_threat_land", "Land taken under threat"),
         "submit": ("airp_threat_submit", "A country made a subject"),
         "annex": ("airp_threat_annex", "A country annexed")}


def _define(name: str, default: float) -> float:
    """A value from the game's defines (the last one the game and the enabled mods set)."""
    from . import gamefiles
    found = default
    for path in gamefiles.files("common/defines", ".lua"):
        with open(path, encoding="utf-8-sig", errors="replace") as f:
            text = re.sub(r"--[^\n]*", "", f.read())
        for m in re.finditer(rf"\b{name}\s*=\s*(-?[\d.]+)", text):
            found = float(m.group(1))
    return found


_WAR: List[float] = []


def _add(tag: str, kind: str, amount: float) -> None:
    if amount > 0 and actions._effect(f"{tag} = {{ add_named_threat = {{ threat = {amount:g} name = {NAMES[kind][0]} }} }}"):
        print(f"  [tension] {tag} +{amount:g} ({NAMES[kind][1].lower()})")


def war(by: str, against: str) -> None:
    """An ultimatum's war: the tension the game adds for a war with a war goal."""
    if not _WAR:
        _WAR.append(_define("TENSION_CB_WAR", 10))
    _add(by, "war", _WAR[0])


def coerced(camp: campaign.Campaign, taker: str, giver: str) -> bool:
    """Is `giver` handing something to `taker` under threat?"""
    from . import reputation, ultimatums
    if any(u["from"] == taker and u["to"] == giver and u["status"] in ("open", "ready")
           for u in ultimatums._store(camp)):
        return True
    today = camp.data.get("last_date") or ""
    return taker == camp.data["player"] and any(
        t["to"] == giver and t["status"] in ("open", "empty") and campaign.shift(t["date"], reputation.THREAT_DAYS) >= today
        for t in reputation._rep(camp)["threats"])


def _sides(e: Dict) -> Tuple[str, str]:
    """(who gains, who gives) for a deal's event: give:X is the first tag giving to the second, ask:X the reverse; a
    cession to a third country (cede:third, [giver, receiver, asked by])."""
    if e["type"].startswith("cede:"):
        return e["tags"][1], e["tags"][0]
    a, b = e["tags"][0], e["tags"][1]
    return (b, a) if e["type"].startswith("give:") else (a, b)


def deal(camp: campaign.Campaign, events: List[Dict]) -> None:
    """Add the tension a deal's land, submission and annexation bring (see above)."""
    lands: Dict[Tuple[str, str], int] = {}
    c = (camp.snapshot or {}).get("countries", {})
    for e in events:
        kind = e.get("type", "")
        if not (actions.is_state(kind) or kind.startswith("cede:")
                or kind in ("give:submit", "ask:submit", "give:annex", "ask:annex")):
            continue
        taker, giver = _sides(e)
        if actions.is_state(kind) or kind.startswith("cede:"):
            if coerced(camp, taker, giver):
                lands[(taker, giver)] = lands.get((taker, giver), 0) + 1
        elif kind.endswith("submit"):
            _add(taker, "submit", SUBMIT if coerced(camp, taker, giver) else SUBMIT_FREE)
        elif c.get(giver, {}).get("overlord") != taker:            # integrating one's own subject: nothing
            _add(taker, "annex", ANNEX if coerced(camp, taker, giver) else ANNEX_FREE)
    for (taker, _), n in lands.items():
        _add(taker, "land", min(LAND_MAX, LAND + LAND_MORE * (n - 1)))


def files(mod_dir: str) -> Dict[str, Tuple[str, str]]:
    loc = "l_english:\n" + "".join(f' {key}:0 "{text}"\n' for key, text in NAMES.values())
    return {os.path.join(mod_dir, "localisation", "english", "airp_tension_l_english.yml"): (loc, "utf-8-sig")}
