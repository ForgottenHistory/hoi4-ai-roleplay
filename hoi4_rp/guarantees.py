"""Guarantees made by letter have a price: the guarantor ties up civilian factories while they last, and they end.

The game's own guarantee costs political power, 15 for each country already guaranteed, and lasts until revoked; a
guarantee given in a letter cost nothing and lasted forever, so Austria could gather Britain's and France's for the
asking. Now (letter guarantees only; the game's Guarantee button is unchanged):

    a guarantee lasts TERM_MONTHS (or the deal's own [[for N months]]), then ends on its own; a new letter renews it
    the guarantor pays upkeep in civilian factories, more for each letter guarantee it holds: its 1st costs 1, its 2nd
    2 more, its 3rd 3 more (1, 3, 6, 10 in all), as the game's PP cost grows with each guarantee
    the upkeep is a dynamic modifier on the guarantor: civilian_factory_use = airp_guarantee_civs (a country
    variable), defined by the mod (files()); `python -m hoi4_rp write-mod` and a restart of HOI4 bring it in

A guarantee that ran its term is no broken promise (reputation.py asks expired()); one that ended otherwise (war
between the two, the ward becoming a subject) simply stops costing.

Stored in the campaign: data["guarantees"] = [{"by", "of", "date", "until", "status": active | expired | ended,
"closed"}].

    record(camp, tag, types, terms)   after a letter deal: the guarantees in it (event types give:/ask:guarantee)
    tick(camp)                        end those whose term ran out; stop charging for those that ended otherwise
    describe(camp, tag, ...)          for prompts: the guarantees between the two, and what a new one would cost
    proposal_line(camp, items, ...)   under a proposal with a guarantee: its term and cost
"""

from __future__ import annotations

import os
from typing import Dict, List, Optional, Tuple

from . import actions, bridge, campaign

TERM_MONTHS = 24
MODIFIER = "airp_guarantee_upkeep"
VARIABLE = "airp_guarantee_civs"


def upkeep(n: int) -> int:
    """Civilian factories tied up by n letter guarantees: 1, 3, 6, 10..."""
    return n * (n + 1) // 2


def _all(camp: campaign.Campaign) -> List[Dict]:
    return camp.data.setdefault("guarantees", [])


def _active(camp: campaign.Campaign, by: str) -> List[Dict]:
    return [g for g in _all(camp) if g["by"] == by and g["status"] == "active"]


def next_cost(camp: campaign.Campaign, by: str) -> int:
    """What one more letter guarantee would add to `by`'s upkeep."""
    return len(_active(camp, by)) + 1


def _charge(camp: campaign.Campaign, by: str) -> None:
    """Set `by`'s upkeep to what its active letter guarantees cost."""
    civs = upkeep(len(_active(camp, by)))
    has = f"has_dynamic_modifier = {{ modifier = {MODIFIER} }}"
    script = (f"{by} = {{ set_variable = {{ {VARIABLE} = {civs} }} "
              + (f"if = {{ limit = {{ NOT = {{ {has} }} }} add_dynamic_modifier = {{ modifier = {MODIFIER} }} }}"
                 if civs else f"if = {{ limit = {{ {has} }} remove_dynamic_modifier = {{ modifier = {MODIFIER} }} }}")
              + " }")
    actions._effect(script)


def record(camp: campaign.Campaign, tag: str, types: List[str], terms: Optional[Dict]) -> None:
    """The guarantees a letter deal with `tag` just made (give:guarantee: `tag` guarantees the player; ask:guarantee:
    the player guarantees `tag`). Renewing a standing one extends it, at no new cost."""
    from . import treaties
    player = camp.data["player"]
    today = camp.data.get("last_date") or ""
    months = (terms or {}).get("months") or TERM_MONTHS
    payers = set()
    for kind in types:
        if kind not in ("give:guarantee", "ask:guarantee"):
            continue
        by, of = (tag, player) if kind == "give:guarantee" else (player, tag)
        until = treaties._add_months(today, months)
        g = next((g for g in _active(camp, by) if g["of"] == of), None)
        if g:
            g["until"] = max(g["until"], until)
        else:
            _all(camp).append({"by": by, "of": of, "date": today, "until": until, "status": "active", "closed": ""})
        payers.add(by)
    for by in payers:
        _charge(camp, by)
    if payers:
        camp.save()


def tick(camp: campaign.Campaign) -> List[Tuple[str, str]]:
    """End the letter guarantees whose term ran out; stop charging for those that ended some other way. Returns
    [(the other country, what happened)] worth a letter from it."""
    today = camp.data.get("last_date") or ""
    player = camp.data["player"]
    countries = (camp.snapshot or {}).get("countries", {})
    name = lambda t: countries.get(t, {}).get("name", t)
    letters, payers = [], set()
    for g in _all(camp):
        if g["status"] != "active" or not today:
            continue
        if g["by"] not in countries or g["of"] not in countries:
            g["status"], g["closed"] = "ended", today
            continue
        if not bridge.trigger(f"{g['by']} = {{ has_guaranteed = {g['of']} }}"):
            g["status"], g["closed"] = "ended", today            # war, a subject, revoked: no longer paid for
            payers.add(g["by"])
            continue
        if today >= g["until"]:
            actions._effect(f"{g['by']} = {{ diplomatic_relation = {{ country = {g['of']} relation = guarantee "
                            f"active = no }} }}")
            g["status"], g["closed"] = "expired", today
            payers.add(g["by"])
            text = (f"{name(g['by'])}'s guarantee of {name(g['of'])}'s independence, given by letter on "
                    f"{campaign.date_words(g['date'])}, ran its term and ended.")
            camp.add_events([{"date": today, "when": campaign.date_words(today), "type": "guarantee:expired",
                              "tags": [g["by"], g["of"]], "text": text, "major": False}])
            print(f"  [guarantee] {text}")
            other = g["of"] if g["by"] == player else g["by"]
            letters.append((other, text))
    for by in payers:
        if by in countries:
            _charge(camp, by)
    if payers:
        camp.save()
    return letters


def expired(camp: campaign.Campaign, by: str, of: str) -> bool:
    """`by`'s letter guarantee of `of` ran its term (lately): it ended as agreed, nothing was broken."""
    today = camp.data.get("last_date") or ""
    return any(g["by"] == by and g["of"] == of and g["status"] == "expired"
               and campaign.shift(today, -30) <= g["closed"] for g in _all(camp))


def describe(camp: campaign.Campaign, tag: str, them_name: str, me_name: str) -> str:
    """For `tag`'s prompts: the letter guarantees between it and the player, and the price of a new one."""
    player = camp.data["player"]
    lines = [f"- {them_name if g['by'] == tag else me_name} guarantees {me_name if g['by'] == tag else them_name} "
             f"until {campaign.date_words(g['until'])}"
             for g in _all(camp) if g["status"] == "active" and {g["by"], g["of"]} == {tag, player}]
    mine, theirs = len(_active(camp, tag)), len(_active(camp, player))
    return ("GUARANTEES BY LETTER: a guarantee given in a letter lasts " + f"{TERM_MONTHS} months (or the deal's own "
            f"term) and ties up the guarantor's civilian factories while it lasts, more for each it holds. You hold "
            f"{mine} (upkeep {upkeep(mine)} civilian factories); one more would cost you {mine + 1} more. "
            f"{me_name} holds {theirs} (upkeep {upkeep(theirs)}); one more would cost it {theirs + 1} more. A "
            f"guarantee is worth giving only to a country whose survival matters to you."
            + ("\n" + "\n".join(lines) if lines else ""))


def proposal_line(camp: campaign.Campaign, items: List[str], tag: str, them_name: str,
                  terms: Optional[Dict] = None) -> str:
    """Under a proposal holding a guarantee: how long it lasts and what it costs whom."""
    player = camp.data["player"]
    months = (terms or {}).get("months") or TERM_MONTHS
    parts = []
    if "give:guarantee" in items:
        parts.append(f"{them_name}'s guarantee lasts {months} months and ties up {next_cost(camp, tag)} of its "
                     f"civilian factories")
    if "ask:guarantee" in items:
        parts.append(f"our guarantee lasts {months} months and ties up {next_cost(camp, player)} of our civilian "
                     f"factories")
    return f"\n§Y{'; '.join(parts)}.§!" if parts else ""


def drop_after(camp: campaign.Campaign, key: str) -> None:
    """An earlier save was loaded: guarantees made from then on never were, and those ended since stand again (the
    save holds the upkeep as it was)."""
    keep = []
    for g in _all(camp):
        if g["date"] >= key:
            continue
        if g["closed"] and g["closed"] >= key:
            g["status"], g["closed"] = "active", ""
        keep.append(g)
    camp.data["guarantees"] = keep


def files(mod_dir: str) -> Dict[str, Tuple[str, str]]:
    modifier = (f"# GENERATED by hoi4_rp/guarantees.py. Don't edit by hand.\n\n{MODIFIER} = {{\n"
                f"\tenable = {{ always = yes }}\n\ticon = GFX_idea_generic_war_preparation\n\n\tcivilian_factory_use = {VARIABLE}\n}}\n")
    loc = (f'l_english:\n {MODIFIER}:0 "Guarantees by Treaty"\n'
           f' {MODIFIER}_desc:0 "Civilian factories kept busy upholding the guarantees we gave by letter."\n')
    return {
        os.path.join(mod_dir, "common", "dynamic_modifiers", "airp_guarantees.txt"): (modifier, "utf-8"),
        os.path.join(mod_dir, "localisation", "english", "airp_guarantees_l_english.yml"): (loc, "utf-8-sig"),
    }
