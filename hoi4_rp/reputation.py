"""Reputation: an impartial game master's record of how the player keeps faith, known to every country.

Deeds are found by rules (detect(), from the chronicle each time the world moves):
    the player declared war on a country, with what bound the two just before (ties in the snapshot: a
        non-aggression pact, a guarantee either way, access, faction, subject) and recent deals made by letter
    the player went to war in defence of a country it had guaranteed
    a guarantee or non-aggression pact of the player's ended in peacetime
    a peace the player made (the chronicle's peace summary)
Promises are read from the player's own letters (most roleplay happens in letters): a background LLM call lists any
promise or assurance in each letter the player sends, kept per recipient.

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

from . import bridge, campaign

HALF_LIFE_YEARS = 3.0
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

    for e in events:
        if player not in e["tags"]:
            continue
        other = next((t for t in e["tags"] if t != player), "")
        if e["type"] == "war" and other:
            if bridge.trigger(f"{player} = {{ has_offensive_war_with = {other} }}"):
                ties = [TIE_WORDS[k].format(p=p, o=name(other)) for k in old_ties.get(other, []) if k in TIE_WORDS]
                deals = [x["text"] for x in camp.data["events"]
                         if other in x["tags"] and player in x["tags"] and x.get("type", "").startswith(_DEAL_TYPES)
                         and campaign.shift(new["date"], -730) <= x["date"]]
                goal = bridge.trigger(f"{player} = {{ has_wargoal_against = {other} }}")
                facts = (f"{p} declared war on {name(other)}."
                         + (f" Just before, there was {'; '.join(ties)}." if ties else
                            " Nothing bound the two by treaty just before.")
                         + (f" Deals between them in the last two years: {' '.join(deals)}" if deals else "")
                         + (f" {p} held a war goal against {name(other)}." if goal else
                            f" {p} held no war goal against {name(other)}."))
                deeds.append(_deed(new, [player, other], facts))
            else:
                # Called to defend a country it guaranteed?
                for ward, kinds in old_ties.items():
                    if "my_guarantee" in kinds and "|".join(sorted([ward, other])) in new.get("wars", []):
                        deeds.append(_deed(new, [player, ward, other],
                                           f"{p} went to war against {name(other)} in defence of {name(ward)}, "
                                           f"whose independence it had guaranteed."))
                        break
        elif e["type"] == "peace" and e["text"].startswith("Peace between"):
            deeds.append(_deed(new, [player, other], e["text"]))

    at_war = lambda t: "|".join(sorted([player, t])) in new.get("wars", [])
    for other, kinds in old_ties.items():
        if other not in nc or at_war(other) or nc[other].get("overlord") == player:
            continue
        for kind in ("my_guarantee", "pact"):
            if kind in kinds and kind not in new_ties.get(other, []):
                # A pact the other side ended by letter isn't the player's doing; a guarantee only the player ends.
                ended_by_them = kind == "pact" and any(x["date"] > old["date"] and x.get("type") == "end:pact"
                                                       and other in x["tags"] for x in camp.data["events"])
                if not ended_by_them:
                    what = TIE_WORDS[kind].format(p=p, o=name(other))
                    deeds.append(_deed(new, [player, other], f"In peacetime, {what} came to an end."))
    rep["deeds"].extend(deeds)
    return deeds


# ---------------------------------------------------------------- the game master (background LLM work)

def next_job(camp: campaign.Campaign, player: str) -> Optional[Tuple[str, Dict]]:
    """The next piece of judging to do: ("promises", letter) or ("deed", deed), oldest first; None if done."""
    rep = _rep(camp)
    for tag, letters in camp.data["letters"].items():
        for x in letters:
            if x["from"] == player and not x.get("promises_read") and x["date"] >= rep["since"]:
                return "promises", {**x, "to": tag}
    pending = [d for d in rep["deeds"] if d["weight"] is None]
    return ("deed", pending[0]) if pending else None


def promise_messages(player_name: str, to_name: str, letter: Dict) -> List[Dict[str, str]]:
    return [{"role": "system", "content":
             f"You keep an impartial record of what governments promise in Hearts of Iron IV. Below is a letter "
             f"{player_name}'s government sent to {to_name}. List every concrete promise, assurance or commitment "
             f"{player_name} makes in it (about war and peace, borders, aid, agreements, how it will treat "
             f"{to_name}). Answer with one line per promise, each starting PROMISE:, in your own short words. If it "
             f"promises nothing, answer NONE. Threats and demands are not promises."},
            {"role": "user", "content": letter["text"]}]


def read_promises(text: str) -> List[str]:
    return [m.strip(" .*") for m in re.findall(r"^[\W_]*promise[*_\s]*[:\-–]\s*(.+)$", text, re.I | re.M)]


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
    judged = [d for d in _rep(camp)["deeds"] if d["weight"] is not None and d["weight"] != 0]
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
    if not text:
        return ""
    return (f"{player_name.upper()}'S REPUTATION (the impartial record of how it keeps faith, known to every "
            f"government):\n{text}\nWeigh it as your government would: deeds against you count most, against your "
            f"friends and neighbours next; a country that breaks its word earns distrust, one that keeps it earns "
            f"credit.")


def drop_after(camp: campaign.Campaign, key: str) -> None:
    """An earlier save was loaded: forget what happened after it."""
    rep = camp.data.get("reputation")
    if rep:
        rep["deeds"] = [d for d in rep["deeds"] if d["date"] <= key]
        rep["promises"] = [x for x in rep["promises"] if x["date"] <= key]


def _days(key: str) -> int:
    import datetime
    try:
        y, m, d = (int(x) for x in key.split(".")[:3])
        return datetime.date(y, m, d).toordinal()
    except ValueError:
        return 0
