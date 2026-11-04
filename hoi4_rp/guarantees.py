"""Guarantees made by letter have a price: the guarantor ties up civilian factories while they last, and they end.

The game's own guarantee costs political power, 15 for each country already guaranteed, and lasts until revoked; a
guarantee given in a letter cost nothing and lasted forever, so Austria could gather Britain's and France's for the
asking. Now (letter guarantees only; the game's Guarantee button is unchanged):

    a guarantee lasts TERM_MONTHS (or the deal's own [[for N months]]), then ends on its own; a new letter renews it
    the guarantor pays upkeep in civilian factories for each letter guarantee it holds: 1 each for the first two,
    2 each after that (1, 2, 4, 6 in all), as the game's PP cost grows with each guarantee
    the upkeep is a dynamic modifier on the guarantor: civilian_factory_use = airp_guarantee_civs (a country
    variable), defined by the mod (files()); `python -m hoi4_rp write-mod` and a restart of HOI4 bring it in

A guarantee that ran its term is no broken promise (reputation.py asks expired()); one that ended otherwise (war
between the two, the ward becoming a subject) simply stops costing.

Stored in the campaign: data["guarantees"] = [{"by", "of", "date", "until", "status": active | expired | ended,
"closed", "renewed": [[date, the end before]]}].

With [ai_diplomacy] guarantees = ours the game's AI gives none (gates.py) and the storyteller gives the world
its guarantees from threats() and candidates(), renewing those by agreement as they run out (renewals(), renew()).

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
    """Civilian factories tied up by n letter guarantees: the first two cost 1 each, every one after that 2
    (1, 2, 4, 6, 8...)."""
    return n if n <= 2 else 2 + 2 * (n - 2)


def _all(camp: campaign.Campaign) -> List[Dict]:
    return camp.data.setdefault("guarantees", [])


def _active(camp: campaign.Campaign, by: str) -> List[Dict]:
    return [g for g in _all(camp) if g["by"] == by and g["status"] == "active"]


def next_cost(camp: campaign.Campaign, by: str) -> int:
    """What one more letter guarantee would add to `by`'s upkeep."""
    n = len(_active(camp, by))
    return upkeep(n + 1) - upkeep(n)


def _charge(camp: campaign.Campaign, by: str) -> None:
    """Set `by`'s upkeep to what its active letter guarantees cost."""
    civs = upkeep(len(_active(camp, by)))
    has = f"has_dynamic_modifier = {{ modifier = {MODIFIER} }}"
    script = (f"{by} = {{ set_variable = {{ {VARIABLE} = {civs} }} "
              + (f"if = {{ limit = {{ NOT = {{ {has} }} }} add_dynamic_modifier = {{ modifier = {MODIFIER} }} }}"
                 if civs else f"if = {{ limit = {{ {has} }} remove_dynamic_modifier = {{ modifier = {MODIFIER} }} }}")
              + " }")
    actions._effect(script)


def record(camp: campaign.Campaign, tag: str, types: List[str], terms: Optional[Dict], other: str = "") -> List[str]:
    """The guarantees a letter deal between `tag` and `other` (the player by default) just made (give:guarantee:
    `tag` guarantees `other`; ask:guarantee: `other` guarantees `tag`). Renewing a standing one extends it, at no new
    cost. Returns a line for the player on each (its term and upkeep), for a guarantee granted outright, which no
    proposal line announced."""
    from . import treaties
    player = other or camp.data["player"]
    today = camp.data.get("last_date") or ""
    months = (terms or {}).get("months") or TERM_MONTHS
    payers, lines = set(), []
    names = (camp.snapshot or {}).get("countries", {})
    name = lambda t: "we" if t == player else names.get(t, {}).get("name", t)
    for kind in types:
        if kind not in ("give:guarantee", "ask:guarantee"):
            continue
        by, of = (tag, player) if kind == "give:guarantee" else (player, tag)
        until = treaties._add_months(today, months)
        g = next((g for g in _active(camp, by) if g["of"] == of), None)
        if g:
            g["until"] = max(g["until"], until)
            lines.append(f"The guarantee is renewed until {campaign.date_words(g['until'])}, at no new cost.")
        else:
            cost = next_cost(camp, by)
            _all(camp).append({"by": by, "of": of, "date": today, "until": until, "status": "active", "closed": ""})
            lines.append(f"The guarantee lasts until {campaign.date_words(until)}; {name(by)} "
                         f"{'tie' if by == player else 'ties'} up {cost} civilian "
                         f"{'factory' if cost == 1 else 'factories'} for it.")
        payers.add(by)
    for by in payers:
        _charge(camp, by)
    if payers:
        camp.save()
    return lines


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
            if player in (g["by"], g["of"]):         # one between two other countries is news, not a letter
                letters.append((g["of"] if g["by"] == player else g["by"], text))
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
            f"{mine} (upkeep {upkeep(mine)} civilian factories); one more would cost you {next_cost(camp, tag)} more. "
            f"{me_name} holds {theirs} (upkeep {upkeep(theirs)}); one more would cost it {next_cost(camp, player)} more. A "
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
        while g.get("renewed") and g["renewed"][-1][0] >= key:
            g["until"] = g["renewed"].pop()[1]
        keep.append(g)
    camp.data["guarantees"] = keep


# ---------------------------------------------------------------- who needs a guarantee, and who might give it
#
# With guarantees = ours the storyteller gives the world its guarantees (storyteller.GuaranteeMonth). Python lists
# what is possible, from the running game only (no tags or continents written in, so mods work alike):
#
#   threats      a country another one holds or justifies a war goal on, sets an ultimatum, or aims to take land
#                from (agendas; an aim to oppose it adds weight, but rivalry alone is no threat); only from a country
#                about as strong or stronger, and not already at war
#   candidates   for each threat, countries with a stake: an aim against the aggressor, an aim to back the
#                threatened, the same faction, threatened by the same country (a border or being a major adds
#                weight); a minor power only for a neighbour, a faction member or a country it aims to back; never
#                for a country more than twice its strength (factories)
#   reach        near: capitals on one continent. present: owns land on the ward's continent (a colony, a border
#                through one). far: neither. A guarantee that isn't near needs an aim at stake, and the world sees
#                one present guarantee every DISTANT_GAP_DAYS["present"] days at most, a far one (a major only,
#                with an aim to back that very country: an aim against the aggressor elsewhere isn't enough)
#                every DISTANT_GAP_DAYS["far"]: a European guarantee in Asia can happen, but rarely

DISTANT_GAP_DAYS = {"present": 180, "far": 365}
REACH_WEIGHT = {"near": 1.0, "present": 0.4, "far": 0.15}
RENEW_DAYS = 60             # a guarantee by agreement this close to its end comes up for renewal
CANDIDATES = 8
PER_WARD = 2
THREAT_WEIGHT = {"ultimatum": 4, "wargoal": 3, "justifying": 3, "troops": 2, "land": 2, "oppose": 1}
THREAT_WORDS = {"ultimatum": "{t} has set it an ultimatum", "wargoal": "{t} holds a war goal on it",
                "troops": "{t} has massed troops on its border",
                "justifying": "{t} is justifying a war goal on it", "land": "{t} aims to take its land",
                "oppose": "{t} works against it"}


def reach(by: str, of: str, w: Dict[str, Dict]) -> str:
    """near | present | far: how close `by` is to `of`'s home (see above)."""
    a, b = w.get(by), w.get(of)
    if not a or not b:
        return "far"
    if a["continent"] and a["continent"] == b["continent"]:
        return "near"
    if of in a["neighbours"] or (b.get("capital") and bridge.trigger(
            f"{by} = {{ any_owned_state = {{ is_on_same_continent_as = {b['capital']} }} }}")):
        return "present"
    return "far"


def distant_open(camp: campaign.Campaign, tier: str, now: str) -> bool:
    """May the world see one more guarantee this far from home now?"""
    from . import storyteller
    gap = DISTANT_GAP_DAYS.get(tier)
    return not gap or not any(x.get("reach") == tier and x["kind"] == "guarantee"
                              and 0 <= storyteller._days(x["date"], now) < gap
                              for x in storyteller._state(camp)["incidents"])


def reach_ok(camp: campaign.Campaign, by: str, of: str, w: Dict[str, Dict], stake: bool) -> str:
    """The reach tier if `by` may guarantee `of` from where it is ('' if not): near always; present or far only with
    an aim at stake and while the world's allowance for them is open; far only for a major power."""
    tier = reach(by, of, w)
    if tier == "near":
        return tier
    major = (camp.snapshot or {}).get("countries", {}).get(by, {}).get("major")
    now = (camp.snapshot or {}).get("date", "")
    if not stake or (tier == "far" and not major) or not distant_open(camp, tier, now):
        return ""
    return tier


def _ok(camp: campaign.Campaign, tag: str, w: Dict[str, Dict]) -> bool:
    c = (camp.snapshot or {}).get("countries", {})
    return (tag in w and tag in c and not w[tag]["subject"] and not c[tag].get("capitulated")
            and not c[tag].get("voiceless") and tag != camp.data["player"])


def threats(camp: campaign.Campaign, w: Dict[str, Dict]) -> List[Dict]:
    """Countries under threat: [{"of", "by", "how": [kinds], "weight"}], the gravest first. The aggressor may be the
    player; the threatened is never the player (a guarantee to the player comes by letter)."""
    from . import agendas, gamestate, ultimatums
    snap = camp.snapshot or {}
    c, wars = snap.get("countries", {}), set(snap.get("wars", []))
    found: Dict[Tuple[str, str], List[str]] = {}
    add = lambda of, by, how: found.setdefault((of, by), []).append(how)
    lines = gamestate.query(
        'every_country = { limit = { exists = yes is_subject = no } '
        'every_country = { limit = { has_wargoal_against = PREV } log = "AIRPGT|[PREV.GetTag]|[THIS.GetTag]|wargoal" } '
        'every_country = { limit = { is_justifying_wargoal_against = PREV } '
        'log = "AIRPGT|[PREV.GetTag]|[THIS.GetTag]|justifying" } }')
    for line in lines:
        f = line.split("|")
        if f[0] == "AIRPGT" and len(f) >= 4:
            add(f[1], f[2], f[3])
    for u in ultimatums._store(camp):
        if u["status"] in ("open", "ready"):
            add(u["to"], u["from"], "ultimatum")
    from . import borders
    for tag in borders.massed(camp):                   # the player's troops on its border (borders.py)
        add(tag, camp.data["player"], "troops")
    for tag in list(agendas._store(camp)):
        for aim in agendas.aims_of(camp, tag):
            if aim["kind"] in ("land", "oppose") and aim["target"]:
                add(aim["target"], tag, aim["kind"])
    power = lambda t: w.get(t, {}).get("civ", 0) + w.get(t, {}).get("mil", 0)
    fighting = {t for p in wars for t in p.split("|")}
    out = []
    for (of, by), how in found.items():
        if of == by or not _ok(camp, of, w) or by not in w or of in fighting:
            continue                                   # a guarantee protects a country at peace, not one at war
        if not c.get(by, {}).get("major") and power(by) < 0.75 * power(of):
            continue                                   # no threat it couldn't face alone
        how = list(dict.fromkeys(how))
        if how == ["oppose"]:
            continue                                   # rivalry alone doesn't put a country's independence at stake
        out.append({"of": of, "by": by, "how": how, "weight": sum(THREAT_WEIGHT[h] for h in how)})
    return sorted(out, key=lambda x: -x["weight"])


def candidates(camp: campaign.Campaign, w: Dict[str, Dict], found: List[Dict]) -> List[Dict]:
    """Who might guarantee whom against what: [{"by", "of", "against", "stake": [words], "aim": the guarantor's
    aim at stake or None, "reach", "score"}], best first, at most PER_WARD per country and CANDIDATES in all.
    The game's rules (world tension) are checked later, by the storyteller (diplomacy.menu)."""
    from . import agendas
    snap = camp.snapshot or {}
    c, wars = snap.get("countries", {}), set(snap.get("wars", []))
    name = lambda t: c.get(t, {}).get("name", t)
    threatened = {(x["of"], x["by"]) for x in found}
    power = lambda t: w.get(t, {}).get("civ", 0) + w.get(t, {}).get("mil", 0)
    out = []
    for t in found:
        of, by = t["of"], t["by"]
        for g in w:
            if g in (of, by) or not _ok(camp, g, w) or of in w[g]["guarantees"] or campaign.pair(g, of) in wars:
                continue
            if c[g].get("faction") and c[g].get("faction") == c.get(by, {}).get("faction"):
                continue                               # on the aggressor's side
            s, stake, aim = 0, [], None
            backs = any(a["target"] == of and a["kind"] in ("back", "befriend") for a in agendas.aims_of(camp, g))
            ally = bool(c[g].get("faction")) and c[g].get("faction") == c[of].get("faction")
            if not c[g].get("major") and not (backs or ally or of in w[g]["neighbours"]):
                continue                               # a minor power guarantees only those it is bound to
            for a in agendas.aims_of(camp, g):
                if a["target"] == by and a["kind"] in ("oppose", "land"):
                    s, aim = s + 3, aim or a
                    stake.append(f"its aim: {a['aim']}")
                elif a["target"] == of and a["kind"] in ("back", "befriend"):
                    s, aim = s + 3, aim or a
                    stake.append(f"its aim: {a['aim']}")
            if ally:
                s, stake = s + 2, stake + ["the same faction"]
            if (g, by) in threatened:
                s, stake = s + 2, stake + [f"threatened by {name(by)} too"]
            if of in w[g]["neighbours"]:
                s, stake = s + 1, stake + ["neighbours"]
            if c[g].get("major"):
                s += 1
            if s < 3 or power(g) < 0.5 * power(of):
                continue                               # a guarantee from one much weaker protects nothing
            tier = reach_ok(camp, g, of, w, aim is not None)
            if not tier or (tier == "far" and not backs):
                continue                               # across the world only for the country itself, not a foe
            out.append({"by": g, "of": of, "against": by, "stake": stake, "aim": aim, "reach": tier,
                        "score": s * t["weight"] * REACH_WEIGHT[tier]})
    out.sort(key=lambda x: -x["score"])
    kept, per, seen = [], {}, set()
    for x in out:
        if (x["by"], x["of"]) in seen or per.get(x["of"], 0) >= PER_WARD:
            continue
        seen.add((x["by"], x["of"]))
        per[x["of"]] = per.get(x["of"], 0) + 1
        kept.append(x)
        if len(kept) >= CANDIDATES:
            break
    return kept


def renewals(camp: campaign.Campaign) -> List[Dict]:
    """Guarantees by agreement between two AI countries that end within RENEW_DAYS."""
    today = (camp.snapshot or {}).get("date", "") or camp.data.get("last_date") or ""
    player = camp.data["player"]
    return [g for g in _all(camp) if g["status"] == "active" and player not in (g["by"], g["of"])
            and today and g["until"] <= campaign.shift(today, RENEW_DAYS)]


def renew(camp: campaign.Campaign, by: str, of: str) -> str:
    """Extend `by`'s guarantee of `of` by a term, at no new cost; returns the new end date ('' if none stands)."""
    from . import treaties
    g = next((g for g in _active(camp, by) if g["of"] == of), None)
    if not g:
        return ""
    today = camp.data.get("last_date") or ""
    g.setdefault("renewed", []).append([today, g["until"]])
    g["until"] = treaties._add_months(max(g["until"], today), TERM_MONTHS)
    camp.save()
    return g["until"]


def files(mod_dir: str) -> Dict[str, Tuple[str, str]]:
    modifier =(f"# GENERATED by hoi4_rp/guarantees.py. Don't edit by hand.\n\n{MODIFIER} = {{\n"
                f"\tenable = {{ always = yes }}\n\ticon = GFX_idea_generic_war_preparation\n\n\tcivilian_factory_use = {VARIABLE}\n}}\n")
    loc = (f'l_english:\n {MODIFIER}:0 "Guarantees by Treaty"\n'
           f' {MODIFIER}_desc:0 "Civilian factories kept busy upholding the guarantees we gave by letter."\n')
    return {
        os.path.join(mod_dir, "common", "dynamic_modifiers", "airp_guarantees.txt"): (modifier, "utf-8"),
        os.path.join(mod_dir, "localisation", "english", "airp_guarantees_l_english.yml"): (loc, "utf-8-sig"),
    }
