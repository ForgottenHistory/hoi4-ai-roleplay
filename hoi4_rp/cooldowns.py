"""A tie with a third country that a deal ended (actions DROP_KINDS: a guarantee, a non-aggression pact, military
access) stays ended for DAYS: neither side makes it again, by a deal or by the game's own diplomacy, the player's
buttons included.

In the game: the country that held the tie keeps the other in an array (ARRAYS), and write-mod puts a condition in
front of each diplomatic action that would make the tie again (TRIGGERS; ROOT acts, FROM is the target), in the
gates' files (gates.files). Our deals check the same arrays (blocked). The campaign's events are the record: each
drop event starts a cooldown from its date, so loading an earlier save rebuilds the arrays from what it had (sync).
"""

from __future__ import annotations

from typing import List, Set, Tuple

DAYS = 180
ARRAYS = {"drop_guarantee": "airp_cd_guarantee", "drop_pact": "airp_cd_pact", "drop_access": "airp_cd_access"}
TOOLTIP = "airp_cooldown_tt"
TOOLTIP_TEXT = "Ended by agreement not long ago"

# Pacts and access count either way: the pair can't make them again whoever asks.
_EITHER = lambda array: f"OR = {{ is_in_array = {{ {array} = FROM }} FROM = {{ is_in_array = {{ {array} = ROOT }} }} }}"
TRIGGERS = {
    "DIPLOMACY_GUARANTEE_ENABLE_TRIGGER": f"is_in_array = {{ {ARRAYS['drop_guarantee']} = FROM }}",
    # the game has no trigger of its own for the pact, but looks one up by the action's name
    "DIPLOMACY_NONAGGRESSIONPACT_ENABLE_TRIGGER": _EITHER(ARRAYS["drop_pact"]),
    "DIPLOMACY_MILACC_ENABLE_TRIGGER": _EITHER(ARRAYS["drop_access"]),
    "DIPLOMACY_OFFER_MILACC_ENABLE_TRIGGER": _EITHER(ARRAYS["drop_access"]),
}

# the deals' relations (actions._CHECKS) each array blocks, "them"/"me" the side holding it
_BLOCKS = {"drop_guarantee": {"them": ("they_guarantee",), "me": ("my_guarantee",)},
           "drop_pact": {"them": ("pact",), "me": ("pact",)},
           "drop_access": {"them": ("they_access", "my_access"), "me": ("they_access", "my_access")}}


def start(kind: str, holder: str, target: str) -> None:
    """A tie was dropped: `holder` may not make it again with `target` for DAYS."""
    from . import actions
    actions._effect(f"{holder} = {{ add_to_array = {{ {ARRAYS[kind]} = {target} }} }}")


def blocked(them: str, me: str) -> Set[str]:
    """The relations between the two (actions._CHECKS names) a cooldown keeps from being made again."""
    from . import gamestate
    checks = []
    for kind, array in ARRAYS.items():
        for side, a, b in (("them", them, me), ("me", me, them)):
            checks.append(f'if = {{ limit = {{ {a} = {{ is_in_array = {{ {array} = {b} }} }} }} '
                          f'log = "AIRPCD|{kind}|{side}" }} ')
    out: Set[str] = set()
    for line in gamestate.query("".join(checks)):
        f = line.strip().split("|")
        if len(f) == 3 and f[0] == "AIRPCD" and f[1] in _BLOCKS:
            out.update(_BLOCKS[f[1]].get(f[2], ()))
    return out


def active(camp, today: str) -> List[Tuple[str, str, str]]:
    """Cooldowns standing on `today`: (kind, holder, target), from the campaign's drop events."""
    from . import actions, storyteller
    out = []
    for e in camp.data.get("events", []):
        key, date = e.get("type", ""), e.get("date", "")
        if not actions.is_drop(key) or len(e.get("tags", [])) < 3 or not date:
            continue
        age = storyteller._days(date, today)
        if 0 <= age < DAYS:
            verb, kind, target = key.split(":")
            holder = e["tags"][0] if verb == "give" else e["tags"][1]
            out.append((kind, holder, target))
    return out


def sync(camp, force: bool = False) -> None:
    """Make the game's arrays hold the cooldowns standing today: after loading (force), and when one ran out."""
    from . import actions
    today = (camp.snapshot or {}).get("date", "") or camp.data.get("last_date") or ""
    now = sorted(set(active(camp, today)))
    st = camp.data.setdefault("cooldowns", {"holders": [], "synced": []})
    if not force and [list(x) for x in now] == st["synced"]:
        return
    holders = set(st["holders"]) | {h for _, h, _ in now}
    script = "".join(f"{h} = {{ " + " ".join(f"clear_array = {a}" for a in ARRAYS.values()) + " } "
                     for h in sorted(holders))
    script += "".join(f"{h} = {{ add_to_array = {{ {ARRAYS[k]} = {t} }} }} " for k, h, t in now)
    if script and actions._effect(script):
        st["holders"] = sorted({h for _, h, _ in now})
        st["synced"] = [list(x) for x in now]
        for k, h, t in now:
            print(f"  cooldown: {h} may not make {k[5:]} with {t} again yet")
