"""What national spirits do: their effects, read from the game's and the enabled mods' idea files, in words.

    spirits.effects_text("Xen_Invasion", name_of)  # 'Division Attack -25%, Construction Speed -20%, ...'
    spirits.harm(["Xen_Invasion", ...])            # [(id, how badly it hurts)], worst first; laws left out

The game shows a spirit's name and description to the prompts already; without its numbers "Marauded Industry"
reads as flavour, not as a third of the country's factory output gone.
"""

from __future__ import annotations

import re
from functools import lru_cache
from typing import Callable, Dict, List, Tuple

from . import gamefiles

Effect = Tuple[str, float, str]      # (modifier key, value, target tag for a targeted modifier, else "")

# Shown as plain numbers, not percentages (daily gains and the like).
RAW = {"political_power_gain", "political_power_cost", "command_power_gain",
       "experience_gain_army", "experience_gain_navy", "experience_gain_air"}
RAW_SUFFIX = ("_acceptance", "_weekly", "_daily", "_monthly", "_drift")

# How much each effect weighs in a country's plight: positive when a higher value helps the country.
HARM = {"stability_factor": 2, "war_support_factor": 1, "industrial_capacity_factory": 1,
        "industrial_capacity_dockyard": 0.5, "army_attack_factor": 1, "army_defence_factor": 1, "army_org_factor": 1,
        "army_core_attack_factor": 0.5, "army_core_defence_factor": 0.5, "local_resources": 0.5,
        "local_resources_factor": 0.5, "monthly_population": 0.5, "production_speed_buildings_factor": 0.5,
        "production_factory_max_efficiency_factor": 1, "production_factory_efficiency_gain_factor": 0.5,
        "political_power_factor": 1, "political_power_gain": 1, "research_speed_factor": 0.5,
        "conscription_factor": 0.5, "consumer_goods_factor": -1, "max_dig_in_factor": 0.3,
        "supply_consumption_factor": -0.5, "attrition": -1}


@lru_cache(maxsize=None)
def _defs() -> Dict[str, Tuple[bool, gamefiles.Node]]:
    """Every idea the game loads: id -> (is a law, its definition)."""
    out: Dict[str, Tuple[bool, gamefiles.Node]] = {}
    for path in gamefiles.files("common/ideas"):
        try:
            with open(path, encoding="utf-8-sig", errors="replace") as f:
                tree = gamefiles.parse(f.read())
        except OSError:
            continue
        for key, _, cats in tree:
            if key != "ideas" or not isinstance(cats, list):
                continue
            for _, _, body in cats:
                if not isinstance(body, list):
                    continue
                law = gamefiles.get(body, "law") == "yes"
                for iid, _, idea in body:
                    if isinstance(idea, list):
                        out[iid] = (law, idea)
    return out


def _numbers(node: gamefiles.Node, target: str = "") -> List[Effect]:
    out = []
    for k, _, v in node:
        n = gamefiles.num(v)
        if n is not None and k != "tag":
            out.append((k, n, target))
    return out


def effects(idea: str) -> List[Effect]:
    """The idea's modifiers, and its targeted modifiers (against one country)."""
    d = _defs().get(idea) or _defs().get(re.sub(r"_\d+$", "", idea))
    if not d:
        return []
    out: List[Effect] = []
    for k, _, v in d[1]:
        if k == "modifier" and isinstance(v, list):
            out += _numbers(v)
        elif k == "targeted_modifier" and isinstance(v, list):
            out += _numbers(v, str(gamefiles.get(v, "tag") or ""))
    return out


def is_law(idea: str) -> bool:
    d = _defs().get(idea)
    return bool(d and d[0])


LABELS = {"attack_bonus_against": "Attack", "defense_bonus_against": "Defense",
          "monthly_population": "Monthly Population Growth"}


@lru_cache(maxsize=None)
def _label(key: str) -> str:
    if key in LABELS:
        return LABELS[key]
    from . import gamestate
    keys = [key, f"MODIFIER_{key.upper()}"]
    try:
        found = [v for k, v in zip(keys, gamestate.loc(*keys)) if v and v != k]
    except gamestate.StateError:
        found = []
    text = gamestate.clean_loc(found[0]) if found else ""
    text = re.sub(r"^\W*(?:[a-z0-9]+_[a-z0-9_]+\W+)+", "", text)     # icon names left before the label
    return text.strip().strip(":").strip() or key.replace("_", " ")


def _value(key: str, v: float) -> str:
    if key in RAW or key.endswith(RAW_SUFFIX) or ("factor" not in key and abs(v) >= 1):
        return f"{v:+g}"
    return f"{v * 100:+.0f}%"


def effects_text(idea: str, name_of: Callable[[str], str] = lambda t: t) -> str:
    """'Factory Output -30%, Recruitable Population Factor -35%' (empty if it has no numeric effects)."""
    parts = []
    for k, v, target in effects(idea):
        parts.append(f"{_label(k)} {_value(k, v)}" + (f" against {name_of(target)}" if target else ""))
    return ", ".join(parts)


def harm(ideas: List[str]) -> List[Tuple[str, float]]:
    """National spirits (not laws) by how badly they hurt the country, worst first; only those that hurt."""
    out = []
    for i in ideas:
        if is_law(i):
            continue
        h = sum(max(0.0, -HARM[k] * v) for k, v, t in effects(i) if k in HARM and not t)
        if h > 0:
            out.append((i, h))
    return sorted(out, key=lambda x: -x[1])
