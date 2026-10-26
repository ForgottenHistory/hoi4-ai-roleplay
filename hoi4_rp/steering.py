"""Policies steer the game's own AI: what a country says it wants with the player, its AI does.

Each country's policy toward the player (policy.py: a stance, and what it would give) sets a few of HOI4's AI
strategies toward the player (add_ai_strategy, id = player). Checked live in a 1938 game:

    diplo_action_acceptance  how the AI answers the player's own diplomatic requests: the value is added straight to
                             the acceptance score ("Strategic reasons"; Italy's answer to Austria's request for military
                             access went from -23 to +1000). Targets: military_access, docking_rights,
                             market_access_rights, call_allies (a call to arms), join_allies (asking to join its faction)
    befriend + diplo_action_desire improve_relation
                             the AI spends its diplomats on the player (Turkey's opinion of Italy, -10 to +92 in a month)
    protect, support, contain, antagonize
                             act only within the game's own rules: at 22% world tension a democracy can't guarantee (25%),
                             lend-lease (50%) or invite to a faction (80%), a non-aligned country can't justify war goals
                             (50%). They wait for the rules to allow them.

Strategies stack and are kept in saves, so each country keeps a dated ledger of what was pushed (as ultimatums do):
the difference to what its policy calls for now is pushed, a loaded earlier save forgets later pushes, and a country
that vanished or appeared since (dynamic tags are reused) starts from nothing. War itself stays with ultimatums.

    sync(camp, tag)     bring one country's strategies in line with its policy (after a policy refresh)
    sync_all(camp)      every country with a policy or pushes (after loading, or an earlier save)
    take_notice(camp, tag)  a line for the player when a country's stance toward its requests changed (once)
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from . import actions, campaign, policy

# How each stance answers the player's requests, added to the game's acceptance score
ACCEPT = {"friendly": 40, "cordial": 15, "indifferent": 0, "wary": -20, "hostile": -50}
ACCEPT_TARGETS = ("military_access", "docking_rights", "market_access_rights", "call_allies", "join_allies")
OFFER_BONUS = 30            # more for a request its policy says it would grant anyway
OFFER_TARGETS = {"give:access": "military_access", "give:docking": "docking_rights",
                 "give:market": "market_access_rights", "give:faction": "join_allies"}
# What it wants to do about the player, by stance (strategy type -> value)
DESIRES = {"friendly": {"befriend": 100, "improve_relation": 100, "protect": 100, "support": 100},
           "cordial": {"befriend": 50, "improve_relation": 50},
           "indifferent": {},
           "wary": {"contain": 50},
           "hostile": {"contain": 100, "antagonize": 50}}

NOTICE = {"friendly": ("{them}'s government now favours your requests: access, docking, markets, calls to arms.", False),
          "cordial": ("{them} now looks a little more kindly on your requests.", False),
          "indifferent": ("{them} now weighs your requests on their merits alone.", False),
          "wary": ("{them} now leans against your requests.", True),
          "hostile": ("{them}'s government now opposes your requests and works against you.", True)}

Key = Tuple[str, str]       # (strategy type, extra script: "target = military_access" or "")


def _wanted(p: Optional[Dict], bound: bool = False) -> Dict[Key, int]:
    """The strategies a policy calls for (nothing for no policy, or at war: the war is the game's). `bound`: a subject,
    overlord or faction member of the player, whose ties do what befriending or protecting would: only how it
    answers the player's requests (a call to arms above all)."""
    if not p or p.get("war"):
        return {}
    stance = p["stance"]
    out: Dict[Key, int] = {}
    for target in ACCEPT_TARGETS:
        out[("diplo_action_acceptance", f"target = {target}")] = ACCEPT.get(stance, 0)
    for key in p.get("offers", []):
        target = OFFER_TARGETS.get(key) or ("call_allies" if actions.is_war(key) and key.startswith("give:") else "")
        if target and ACCEPT.get(stance, 0) >= 0:
            out[("diplo_action_acceptance", f"target = {target}")] += OFFER_BONUS
    for kind, value in ({} if bound else DESIRES.get(stance, {})).items():
        out[("diplo_action_desire", "target = improve_relation") if kind == "improve_relation" else (kind, "")] = value
    return {k: v for k, v in out.items() if v}


def _ledger(camp: campaign.Campaign, tag: str) -> Dict:
    return camp.data.setdefault("steering", {}).setdefault(tag, {"pushes": [], "stance": ""})


def _totals(entry: Dict) -> Dict[Key, int]:
    out: Dict[Key, int] = {}
    for _, kind, extra, value in entry["pushes"]:
        out[(kind, extra)] = out.get((kind, extra), 0) + value
    return out


def _reborn(camp: campaign.Campaign, tag: str, entry: Dict) -> bool:
    """The country vanished or appeared since the first push: another country now, without our strategies."""
    first = entry["pushes"][0][0] if entry["pushes"] else ""
    return bool(first) and any(e["date"] > first and e["tags"][:1] == [tag]
                               and e.get("type") in ("country_gone", "country_new") for e in camp.data["events"])


def sync(camp: campaign.Campaign, tag: str) -> List[str]:
    """Push the difference between what `tag`'s policy calls for and what it has; returns log lines."""
    player = camp.data["player"]
    if tag == player or tag not in (camp.snapshot or {}).get("countries", {}):
        return []                              # gone (its strategies with it), or not in the world yet
    entry = _ledger(camp, tag)
    if _reborn(camp, tag, entry):
        entry["pushes"] = []
    p = policy.current(camp, tag)
    c = camp.snapshot["countries"]
    bound = (c[tag].get("overlord") == player or c[player].get("overlord") == tag
             or bool(c[tag].get("faction")) and c[tag].get("faction") == c[player].get("faction"))
    want, have = _wanted(p, bound), _totals(entry)
    diff = {k: want.get(k, 0) - have.get(k, 0) for k in set(want) | set(have)}
    diff = {k: v for k, v in diff.items() if v}
    if not diff:
        return []
    script = " ".join(f"add_ai_strategy = {{ type = {kind} id = {player} {extra} value = {v} }}"
                      for (kind, extra), v in diff.items())
    if not actions._effect(f"{tag} = {{ {script} }}"):
        return []
    key = camp.data.get("last_date") or ""
    entry["pushes"].extend([key, kind, extra, v] for (kind, extra), v in diff.items())
    stance = p["stance"] if want else ""
    if stance != entry.get("stance"):
        if stance or entry.get("stance"):
            entry["notice"] = stance or "indifferent"
        entry["stance"] = stance
    camp.save()
    return [f"  steering {tag}: {stance or 'none'} ("
            + ", ".join(f"{kind}{' ' + extra.split('= ')[-1] if extra else ''} {v:+d}" for (kind, extra), v in
                        sorted(diff.items())) + ")"]


def sync_all(camp: campaign.Campaign) -> None:
    for tag in sorted(set(camp.data.get("policies", {})) | set(camp.data.get("steering", {}))):
        for line in sync(camp, tag):
            print(line)


def take_notice(camp: campaign.Campaign, tag: str, them_name: str) -> Optional[Tuple[str, bool]]:
    """Once, after its stance changed: a line for the player under that country's next letter."""
    entry = camp.data.get("steering", {}).get(tag)
    stance = entry.pop("notice", None) if entry else None
    if not stance:
        return None
    text, bad = NOTICE[stance]
    return text.format(them=them_name), bad


def drop_after(camp: campaign.Campaign, key: str) -> None:
    """An earlier save was loaded: the game holds the strategies as they were then."""
    for entry in camp.data.get("steering", {}).values():
        entry["pushes"] = [x for x in entry["pushes"] if x[0] < key]
        entry.pop("notice", None)
