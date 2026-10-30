"""Reputation: an impartial game master's record of how the player keeps faith, known to every country.

Deeds are found by rules (detect(), from the chronicle each time the world moves):
    the player declared war on a country, with what bound the two just before (ties in the snapshot: a
        non-aggression pact, a guarantee either way, access, faction, subject) and recent deals made by letter
    the player went to war in defence of a country it had guaranteed
    a guarantee or non-aggression pact of the player's ended in peacetime
    a peace the player made (the chronicle's peace summary)
Promises are read from the player's own letters (most roleplay happens in letters): a background LLM call lists any
promise or assurance in each letter the player sends, kept per recipient. The same call lists threats: each is
"open" until the player carries it out (attacks the country, or takes its land by war, focus or deal) or it turns
"empty" (its deadline passes, else after THREAT_DAYS). credibility() tells every country how often the player's
threats came true, so a threat from a power that follows through carries weight, and empty ones lose it.

Each deed is then judged in the background by the LLM acting as an impartial game master (not any country): the
facts, the ties, the letters and promises between the two, and the player's record so far ->
    WEIGHT: -3..+3      how it reflects on the player's honour and reliability
    VERDICT: one sentence
Plain lines, parsed leniently (no JSON). The standing every country sees (describe()) sums the weights, older
deeds fading (half-life HALF_LIFE_YEARS), with the most telling deeds listed and the ones against that country
marked. Countries react to it in their policies and letters; nothing in the game is blocked.

Stored in the campaign: data["reputation"] = {"since": date, "deeds": [...], "promises": [...]}.
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

from . import bridge, campaign, guarantees

HALF_LIFE_YEARS = 3.0
THREAT_DAYS = 365             # a threat without a deadline is empty if nothing came of it within this
TIE_WORDS = {
    "pact": "a non-aggression pact between them",
    "my_guarantee": "{p}'s guarantee of {o}'s independence",
    "their_guarantee": "{o}'s guarantee of {p}'s independence",
    "their_access": "military access {o} had granted {p}",
    "my_access": "military access {p} had granted {o}",
    "faction": "membership of the same faction",
    "subject": "{o} being {p}'s subject",
}
_DEAL_TYPES = ("give:", "ask:")


def _rep(camp: campaign.Campaign) -> Dict:
    rep = camp.data.setdefault("reputation", {})
    rep.setdefault("since", camp.data.get("last_date") or "")
    rep.setdefault("deeds", [])
    rep.setdefault("promises", [])
    rep.setdefault("threats", [])
    rep["threats"] = [t for t in rep["threats"] if not _none(t["text"])]   # "none — no threats made", read as one
    return rep


def _deed(snap: Dict, tags: List[str], facts: str) -> Dict:
    return {"date": snap["date"], "when": campaign.date_words(snap["date"]), "tags": tags, "facts": facts,
            "weight": None, "verdict": ""}


# ---------------------------------------------------------------- finding deeds

def detect(camp: campaign.Campaign, player: str, old: Dict, new: Dict, events: List[Dict]) -> List[Dict]:
    """New deeds of the player between two snapshots (also added to the campaign)."""
    rep = _rep(camp)
    nc = new["countries"]
    name = lambda t: (nc.get(t) or old["countries"].get(t) or {}).get("name") or t
    p = name(player)
    old_ties, new_ties = old.get("ties") or {}, new.get("ties") or {}
    deeds: List[Dict] = []
    oc = old["countries"]
    lord_of = lambda t: (oc.get(t) or nc.get(t) or {}).get("overlord", "")
    batch = {(e["type"], t) for e in events if player in e["tags"] for t in e["tags"] if t != player}
    # A subject at war or peace together with its overlord in the same moment: one deed, the overlord's.
    with_lord = lambda kind, t: bool(lord_of(t)) and (kind, lord_of(t)) in batch

    attacked: List[str] = []                      # countries the player went to war with as the attacker
    defended: Dict[str, List[str]] = {}           # a ward it guaranteed -> the countries it fights for it
    # Wars the player chose to enter beside someone else, by a letter deal ([[ask war X]]) or by honouring a war
    # pledge: not offensive in the game's eyes (it joins the other's side), but its own choice all the same.
    since = campaign.shift(new["date"], -30)
    joined_with: Dict[str, str] = {}              # enemy -> the country whose war it joined
    for x in camp.data["events"]:
        kind = x.get("type", "")
        if x["date"] >= since and len(x["tags"]) >= 3 and (
                kind.startswith("ask:war:") or (kind == "treaty:clause" and x.get("joiner") == player)):
            joined_with[x["tags"][2]] = x["tags"][0]
    joined: Dict[str, List[str]] = {}             # ally -> enemies the player joined its war against
    for e in events:
        if player not in e["tags"]:
            continue
        other = next((t for t in e["tags"] if t != player), "")
        if e["type"] in ("war", "peace") and with_lord(e["type"], other):
            continue
        if e["type"] == "war" and other:
            if bridge.trigger(f"{player} = {{ has_offensive_war_with = {other} }}"):
                attacked.append(other)
            elif other in joined_with:
                joined.setdefault(joined_with[other], []).append(other)
            else:
                # Called to defend a country it guaranteed?
                for ward, kinds in old_ties.items():
                    if "my_guarantee" in kinds and "|".join(sorted([ward, other])) in new.get("wars", []):
                        defended.setdefault(ward, []).append(other)
                        break
        elif e["type"] == "peace" and e["text"].startswith("Peace between"):
            subs = [x["text"] for x in events if x["type"] == "peace" and x["text"].startswith("Peace between")
                    and player in x["tags"] and any(lord_of(t) == other for t in x["tags"] if t != player)]
            deeds.append(_deed(new, [player, other], " ".join([e["text"]] + subs)))

    if attacked:
        deeds.append(_war_deed(camp, player, p, new, attacked, batch, lord_of, old_ties, name))
    for ally, enemies in joined.items():
        deeds.append(_war_deed(camp, player, p, new, enemies, batch, lord_of, old_ties, name,
                               how=f"entered the war, by its own choice, joining {name(ally)}'s war against"))
    for ward, enemies in defended.items():
        deeds.append(_deed(new, [player, ward] + enemies,
                           f"{p} went to war against {', '.join(name(t) for t in enemies)} in defence of "
                           f"{name(ward)}, whose independence it had guaranteed."))

    at_war = lambda t: "|".join(sorted([player, t])) in new.get("wars", [])
    for other, kinds in old_ties.items():
        if other not in nc or at_war(other) or nc[other].get("overlord") == player:
            continue
        if {"faction", "subject"} & set(new_ties.get(other, [])):
            continue                     # joined the player's faction or became its subject: nothing was broken
        if nc[other].get("overlord") and nc[other]["overlord"] != (oc.get(other) or {}).get("overlord", ""):
            continue                     # became another's subject: the game ends guarantees of it (Yugoslavia
                                         # under Italy, 1938), the player didn't
        # A guarantee only the player ends. (A non-aggression pact either side, or the game, can end, and which
        # can't be told: not held against the player.)
        if "my_guarantee" in kinds and "my_guarantee" not in new_ties.get(other, []) \
                and not guarantees.expired(camp, player, other):         # one that ran its term broke nothing
            what = TIE_WORDS["my_guarantee"].format(p=p, o=name(other))
            deeds.append(_deed(new, [player, other], f"In peacetime, {what} came to an end."))
    for d in deeds:
        d["by"] = player
    rep["deeds"].extend(deeds)
    _settle_threats(camp, player, new, events, name)
    return deeds


def _war_deed(camp: campaign.Campaign, player: str, p: str, new: Dict, attacked: List[str], batch, lord_of,
              old_ties: Dict[str, List[str]], name, how: str = "went to war, as the attacker, against") -> Dict:
    """One deed for every war the player started (or chose to join: `how`) at one moment: a declaration on one
    country brings in its subjects, faction and guarantors, all of whom are listed, and the war is judged once, not
    once per country."""
    drawn = [t for (kind, t) in batch if kind == "war" and lord_of(t) in attacked and t not in attacked]
    party = attacked + drawn
    countries = (camp.snapshot or {}).get("countries", {})
    ties = [TIE_WORDS[k].format(p=p, o=name(t)) for t in party for k in old_ties.get(t, []) if k in TIE_WORDS]
    deals = [x["text"] for x in camp.data["events"]
             if any(t in x["tags"] for t in party) and player in x["tags"]
             and x.get("type", "").startswith(_DEAL_TYPES) and campaign.shift(new["date"], -730) <= x["date"]]
    if len(attacked) == 1:
        who = name(attacked[0]) + (f" and its subjects {', '.join(name(t) for t in drawn)}, in one war (an attack "
                                   f"on either brings in the other)" if drawn else "")
    else:
        factions = {t: (new["countries"].get(t) or countries.get(t) or {}).get("faction", "") for t in attacked}
        who = (", ".join(name(t) + (f" ({factions[t]})" if factions[t] else "") for t in attacked)
               + (f", with their subjects {', '.join(name(t) for t in drawn)}" if drawn else "")
               + ", all at the same moment: one declaration brings in the target's faction, guarantors and "
                 "subjects, so most of these were drawn in rather than chosen. Judge it as one war, by whom "
                 f"{p} meant to attack and why")
    facts = (f"{p} {how} {who}."
             + (f" Just before, there was {'; '.join(ties)}." if ties else " Nothing bound them by treaty just before.")
             + (f" Deals between them in the last two years: {' '.join(deals)}" if deals else "")
             + " " + _war_goals(player, p, party, old_ties, name)
             + _focuses(player))
    return _deed(new, [player] + party, facts)


# ---------------------------------------------------------------- the game master (background LLM work)

def _war_goals(player: str, p: str, party: List[str], old_ties: Dict[str, List[str]], name) -> str:
    """What pretext the player had against the countries it attacked: war goals held, or still being justified,
    before the war (declaring may use them up), else as they stand now."""
    held, justifying = [], []
    for t in party:
        before = old_ties.get(t, [])
        if "my_wargoal" in before or bridge.trigger(f"{player} = {{ has_wargoal_against = {t} }}"):
            held.append(name(t))
        elif "my_justifying" in before:
            justifying.append(name(t))
    out = []
    if held:
        out.append(f"{p} held a war goal against {', '.join(held)}.")
    if justifying:
        out.append(f"{p} was still justifying a war goal against {', '.join(justifying)}, not yet complete.")
    return " ".join(out) or f"{p} held no war goal against {', '.join(name(t) for t in party)}."


def _focuses(player: str) -> str:
    """The player's latest national focuses: a war its focus tree declared may have no war goal at all."""
    from .gamestate import StateError, country
    try:
        latest = [f.name for f in country(player).focuses][-4:]
    except (StateError, RuntimeError):
        return ""
    return (f" Its latest national focuses (a focus can lead to or declare a war by itself): {', '.join(latest)}."
            if latest else "")


def next_job(camp: campaign.Campaign, player: str) -> Optional[Tuple[str, Dict]]:
    """The next piece of judging to do: ("promises", letter) or ("deed", deed), oldest first; None if done."""
    rep = _rep(camp)
    for tag, letters in camp.threads().items():
        for x in letters:
            if x["from"] == player and not x.get("promises_read") and x["date"] >= rep["since"]:
                return "promises", {**x, "to": tag}
    pending = [d for d in rep["deeds"] if d["weight"] is None and d.get("by", player) == player]
    return ("deed", pending[0]) if pending else None


def promise_messages(player_name: str, to_name: str, letter: Dict) -> List[Dict[str, str]]:
    return [{"role": "system", "content":
             f"You keep an impartial record of what governments promise in Hearts of Iron IV. Below is a letter "
             f"{player_name}'s government sent to {to_name}. List every concrete promise, assurance or commitment "
             f"{player_name} makes in it (about war and peace, borders, aid, agreements, how it will treat "
             f"{to_name}), one line each starting PROMISE:, in your own short words. Then list every threat it makes "
             f"against {to_name} (war, invasion, destruction, taking land, or worse if a demand is refused), one line "
             f"each starting THREAT:, and if the threat names a deadline, put it first in brackets in months, like "
             f"THREAT: [24 months] war unless Danzig is returned. If there is nothing of either, answer NONE. Demands "
             f"alone are neither."},
            {"role": "user", "content": letter["text"]}]


def _none(s: str) -> bool:
    s = s.strip(" .*()[]").lower()
    # "none", but also "none — no threats made", "nothing of the kind"
    return s in ("", "no promises", "no threats") or bool(re.match(r"(none|nothing|n/a)\b", s))


def read_promises(text: str) -> List[str]:
    found = [m.strip(" .*") for m in re.findall(r"^[\W_]*promise[*_\s]*[:\-–]\s*(.+)$", text, re.I | re.M)]
    return [m for m in found if not _none(m)]


def read_threats(text: str) -> List[Tuple[Optional[int], str]]:
    """[(deadline in months or None, what)]"""
    out = []
    for m in re.findall(r"^[\W_]*threat[*_\s]*[:\-–]\s*(.+)$", text, re.I | re.M):
        d = re.match(r"\[\s*(\d+)\s*(month|year)s?\s*\]\s*(.*)", m.strip(), re.I)
        if d:
            months = int(d.group(1)) * (12 if d.group(2).lower() == "year" else 1)
            out.append((months, d.group(3).strip(" .*")))
        else:
            out.append((None, m.strip(" .*")))
    return [(months, what) for months, what in out if not _none(what)]


def add_threats(camp: campaign.Campaign, letter: Dict, threats: List[Tuple[Optional[int], str]],
                to_name: str = "") -> None:
    for months, what in threats:
        _rep(camp)["threats"].append({
            "date": letter["date"], "when": letter["when"], "to": letter["to"], "to_name": to_name, "text": what,
            "deadline": campaign.shift(letter["date"], months * 30) if months else "", "status": "open", "how": ""})


_YIELDS = ("give:submit", "give:annex", "give:state", "give:subject", "give:govt", "give:rights", "give:dmz",
           "give:dismantle", "cede:")


def _settle_threats(camp: campaign.Campaign, player: str, new: Dict, events: List[Dict], name) -> None:
    """A threat comes true when the player attacks the country or takes its land ("kept"), or the country gives
    in by a deal: cedes land, submits, hands over a subject, changes government ("yielded"). It turns "empty" when
    its deadline (else THREAT_DAYS) passes with nothing done; carried out later, within THREAT_DAYS, it is kept
    late. Every event since the threat counts (the threat is read in the background, after the letter, and the
    country may have given in at once)."""
    p = name(player)
    for t in _rep(camp)["threats"]:
        last = campaign.shift(t["date"], THREAT_DAYS)
        if t["status"] not in ("open", "empty") or (t["status"] == "empty" and new["date"] > last):
            continue
        for e in camp.data["events"]:
            if e["date"] < t["date"] or player not in e["tags"] or t["to"] not in e["tags"]:
                continue
            kind = e["type"]
            if kind.startswith(_YIELDS) and e["tags"][0] == t["to"]:
                status = "yielded"
            elif kind == "territory" and e["text"].startswith(p) or                     kind == "subject" and f"subject of {p}." in e["text"] or                     kind == "war" and e in events and                     bridge.trigger(f"{player} = {{ has_offensive_war_with = {t['to']} }}"):
                status = "kept"
            else:
                continue
            late = " (after the deadline)" if t["status"] == "empty" else ""
            t["status"], t["how"] = status, f"{e['text']} ({e['when']}){late}"
            break
        if t["status"] == "open" and new["date"] > (t["deadline"] or last):
            t["status"] = "empty"


def credibility(camp: campaign.Campaign, player_name: str, viewer: str) -> str:
    """How often the player's threats came true, for a prompt."""
    threats = _rep(camp)["threats"]
    if not threats:
        return ""
    known = lambda t: (camp.snapshot or {}).get("countries", {}).get(t, {}).get("name", t)
    name = lambda t: next((x.get("to_name") for x in threats if x["to"] == t and x.get("to_name")), None) or known(t)
    kept = [t for t in threats if t["status"] == "kept"]
    yielded = [t for t in threats if t["status"] == "yielded"]
    empty = [t for t in threats if t["status"] == "empty"]
    open_ = [t for t in threats if t["status"] == "open"]
    lines = [f"{player_name} has made {len(threats)} threats: {len(kept)} carried out, {len(yielded)} got their way "
             f"(the country gave in), {len(empty)} came to nothing, {len(open_)} still open."]
    lines += [f"- carried out: to {name(t['to'])}, {t['when']}: \"{t['text']}\" -> {t['how']}" for t in kept[-4:]]
    lines += [f"- the country gave in: {name(t['to'])}, {t['when']}: \"{t['text']}\" -> {t['how']}"
              for t in yielded[-3:]]
    lines += [f"- came to nothing: to {name(t['to'])}, {t['when']}: \"{t['text']}\"" for t in empty[-2:]]
    mine = [t for t in open_ if t["to"] == viewer]
    lines += [f"- STILL OPEN, AGAINST YOU: {t['when']}: \"{t['text']}\""
              + (f" (deadline {campaign.date_words(t['deadline'])})" if t["deadline"] else "") for t in mine]
    return (f"{player_name.upper()}'S THREATS (do they come true?):\n" + "\n".join(lines)
            + f"\nWhen {player_name} threatens you, weigh it by this record and by your security: a power that "
              f"carries out its threats must be taken seriously; one whose threats come to nothing need not be.")


def judge_messages(camp: campaign.Campaign, player: str, deed: Dict, world_text: str) -> List[Dict[str, str]]:
    names = (camp.snapshot or {}).get("countries", {})
    name = lambda t: names.get(t, {}).get("name", t)
    others = [t for t in deed["tags"] if t != player]
    letters = []
    for t in others:
        for x in camp.letters_with(t)[-6:]:
            who = name(player) if x["from"] == player else name(t)
            letters.append(f"--- {x['when']}, {who} to {name(t) if x['from'] == player else name(player)}:\n"
                           f"{x['text'][:1200]}")
    promises = [f"- {x['when']}, to {name(x['to'])}: {x['text']}" for x in _rep(camp)["promises"]
                if x["to"] in others and x["date"] <= deed["date"]]
    context = (f"{world_text}\n\nTHE DEED ({deed['when']}):\n{deed['facts']}\n\n"
               f"PROMISES {name(player).upper()} MADE TO THEM IN LETTERS:\n" + ("\n".join(promises) or "None recorded.")
               + "\n\nTHEIR RECENT LETTERS:\n" + ("\n\n".join(letters) or "None.")
               + f"\n\n{name(player).upper()}'S RECORD SO FAR:\n{record(camp, player, name(player)) or 'Nothing yet.'}")
    return [{"role": "system", "content":
             f"You are the impartial game master of a Hearts of Iron IV game. You judge how a deed of "
             f"{name(player)} reflects on its honour and reliability: whether it keeps its word, treaties and "
             f"promises, as the governments of the world would weigh it. You are not any country and take no side. "
             f"Breaking a guarantee, pact or promise is grave; attacking without cause or war goal is dishonourable; "
             f"keeping faith at a cost, defending those it guaranteed, and lenient peace terms are honourable. "
             f"Context matters: a war against a country that wronged it first is not a betrayal.\n\n{context}"},
            {"role": "user", "content":
             "Answer with exactly two lines:\nWEIGHT: a whole number from -3 (a grave betrayal) to +3 (keeping "
             "faith at real cost); 0 if it says nothing about honour\nVERDICT: one sentence, plain and neutral, "
             "saying what was done and why it counts"}]


def read_verdict(text: str) -> Tuple[int, str]:
    w = re.search(r"weight[*_\s]*[:\-–]\s*([+-]?\d)", text, re.I)
    v = re.search(r"^[\W_]*verdict[*_\s]*[:\-–]\s*(.+)$", text, re.I | re.M)
    weight = max(-3, min(3, int(w.group(1)))) if w else 0
    return weight, (v.group(1).strip(" *") if v else text.strip().splitlines()[-1][:300])


# ---------------------------------------------------------------- what the world knows

def _faded(deed: Dict, today: str) -> float:
    years = max(0, (_days(today) - _days(deed["date"])) / 365.0)
    return deed["weight"] * 0.5 ** (years / HALF_LIFE_YEARS)


def standing(score: float) -> str:
    return ("a notorious breaker of its word" if score <= -6 else
            "untrustworthy" if score <= -3 else
            "of doubtful faith" if score <= -1 else
            "known to keep its word" if score >= 3 else
            "reliable so far" if score >= 1 else
            "untested")


def record(camp: campaign.Campaign, player: str, player_name: str, viewer: str = "", limit: int = 8) -> str:
    """The player's reputation for a prompt, as `viewer` sees it (its own dealings marked)."""
    today = camp.data.get("last_date") or ""
    judged = [d for d in _rep(camp)["deeds"] if d["weight"] is not None and d["weight"] != 0
              and d.get("by", player) == player]
    if not judged:
        return ""
    score = sum(_faded(d, today) for d in judged)
    telling = sorted(judged, key=lambda d: -abs(_faded(d, today)))[:limit]
    lines = [f"- {d['when']}: {d['verdict']}" + (" (this concerned you)" if viewer and viewer in d["tags"] else "")
             for d in sorted(telling, key=lambda d: d["date"])]
    return f"{player_name} is {standing(score)} (overall {score:+.1f}). Its most telling deeds:\n" + "\n".join(lines)


def describe(camp: campaign.Campaign, player: str, player_name: str, viewer: str) -> str:
    """The reputation section of a country's prompt (empty while there is nothing on record)."""
    text = record(camp, player, player_name, viewer)
    threats = credibility(camp, player_name, viewer)
    out = ""
    if text:
        out = (f"{player_name.upper()}'S REPUTATION (the impartial record of how it keeps faith, known to every "
               f"government):\n{text}\nWeigh it as your government would: deeds against you count most, against "
               f"your friends and neighbours next; a country that breaks its word earns distrust, one that keeps it "
               f"earns credit. Let it shape how far you trust them and what terms you want, not your tone: "
               f"governments rarely lecture a power about its record.")
    return "\n\n".join(x for x in (out, threats) if x)


def window(camp: campaign.Campaign, player: str, name, clean) -> str:
    """The reputation window's text (HOI4 colour codes): the standing the world sees, the deeds judged, the threats
    and the latest promises. `name(tag)` gives a country's name; `clean` makes LLM text safe for the game."""
    rep = _rep(camp)
    today = camp.data.get("last_date") or ""
    judged = [d for d in rep["deeds"] if d["weight"] is not None and d.get("by", player) == player]
    counted = [d for d in judged if d["weight"] != 0]
    waiting = [d for d in rep["deeds"] if d["weight"] is None and d.get("by", player) == player]
    score = sum(_faded(d, today) for d in counted)
    colour = "R" if score <= -1 else "G" if score >= 1 else "Y"
    out = [f"The world sees {name(player)} as §{colour}{standing(score)}§!  (standing {score:+.1f})",
           "Every government knows these deeds. Older ones fade, by half every three years.", ""]
    if not counted:
        out.append("§gNothing is on record yet. Keeping or breaking your word will be.§!")
    for d in sorted(counted, key=lambda d: d["date"], reverse=True)[:16]:
        w = d["weight"]
        others = ", ".join(name(t) for t in d["tags"] if t != player)
        out.append(f"§{'G' if w > 0 else 'R'}{w:+d}§!  §Y{d['when']}§!" + (f"  ({others})" if others else ""))
        out.append(clean(d["verdict"]))
        out.append("")
    if waiting:
        out.append(f"§g{len(waiting)} more deed{'s' if len(waiting) > 1 else ''} not judged yet.§!")
        out.append("")
    threats = rep["threats"]
    if threats:
        count = lambda st: sum(1 for t in threats if t["status"] == st)
        out.append(f"§YThreats§!  {len(threats)} made: {count('kept')} carried out, {count('yielded')} got their way, "
                   f"{count('empty')} came to nothing, {count('open')} still open.")
        for t in [t for t in threats if t["status"] == "open"]:
            out.append(f"Open, against {name(t['to'])}: {clean(t['text'])}")
        out.append("")
    promises = rep["promises"]
    if promises:
        out.append(f"§YLatest promises§!  ({len(promises)} on record)")
        out += [f"{x['when']}, to {name(x['to'])}: {clean(x['text'])}" for x in promises[-8:][::-1]]
    return "\n".join(out).strip()


def drop_after(camp: campaign.Campaign, key: str) -> None:
    """An earlier save was loaded: forget what happened after it."""
    rep = camp.data.get("reputation")
    if rep:
        rep["deeds"] = [d for d in rep["deeds"] if d["date"] <= key]
        rep["promises"] = [x for x in rep["promises"] if x["date"] <= key]
        rep["threats"] = [x for x in rep.get("threats", []) if x["date"] <= key]


def _days(key: str) -> int:
    import datetime
    try:
        y, m, d = (int(x) for x in key.split(".")[:3])
        return datetime.date(y, m, d).toordinal()
    except ValueError:
        return 0
