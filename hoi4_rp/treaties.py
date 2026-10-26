"""Treaties: letter deals that run over time.

A deal made by letter may carry terms the mod plays out as the game runs:
    [[for 12 months]]               the agreements in it (pact, access, docking rights, guarantee, faction
                                    membership, resource rights, demilitarized zones) end on their own after this
    [[offer civ 2 x3]]              an amount paid in instalments: 2 now, then 2 every 12 months, 3 payments in all
    [[ask rifles 500 x4 every 6]]   ... every 6 months
    [[offer pledge GER]]            a war pledge (actions.py): whenever the other side goes to war with Germany,
                                    this side joins it
parse_terms() takes the term and instalment parts out of a letter before actions.parse() reads the rest.

Kept in the campaign (data["treaties"]) and played out by tick() each time the world moves:
    the term ends            -> its agreements end in the game
    an instalment them owes  -> paid automatically (if it can't be paid, it is missed and remembered)
    an instalment I owe      -> the player is asked: Accept pays, Decline refuses (the reply window's buttons)
    a war pledge is called   -> them joins the war at once; for my pledge the player is asked to honour it
                                (already at war with that country: honoured as it stands)
    war between the two      -> the treaty lapses
The player is never bound: refusing is allowed, and it is remembered (the other country sees it, the reputation
game master judges it, and the country writes about it). The AI keeps its side unless it renounces the treaty in a
letter ([[end treaty]]), which is remembered too.

Treaty: {"id", "with", "date", "when", "items": [keys], "labels": {key: text}, "text", "until": date or "",
         "status": active | fulfilled | ended | lapsed | broken | renounced, "closed": date, "how": text,
         "payments": [{"key", "count", "every", "paid": [dates], "missed": [dates]}],
         "clauses": [{"key", "fired": date, "asked": date}]}
data["treaty_due"]: [{"treaty", "kind": payment | clause, "key", "date", "n"}] waiting for the player.
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

from . import actions, bridge, campaign

MAX_PAYMENTS = 5
EVERY = (3, 24)          # months between instalments
TERM = (3, 120)          # months a term may run

# Agreements a term can end: key -> (does it still hold?, script ending it); T = them, M = me
_ENDS = {
    "give:pact": ("pact", "{M} = {{ diplomatic_relation = {{ country = {T} relation = non_aggression_pact active = no }} }}"),
    "give:access": ("they_access", "{M} = {{ diplomatic_relation = {{ country = {T} relation = military_access active = no }} }}"),
    "ask:access": ("my_access", "{T} = {{ diplomatic_relation = {{ country = {M} relation = military_access active = no }} }}"),
    "give:docking": ("they_docking", "{M} = {{ diplomatic_relation = {{ country = {T} relation = docking_rights active = no }} }}"),
    "ask:docking": ("my_docking", "{T} = {{ diplomatic_relation = {{ country = {M} relation = docking_rights active = no }} }}"),
    "give:guarantee": ("they_guarantee", "{T} = {{ diplomatic_relation = {{ country = {M} relation = guarantee active = no }} }}"),
    "ask:guarantee": ("my_guarantee", "{M} = {{ diplomatic_relation = {{ country = {T} relation = guarantee active = no }} }}"),
    "give:faction": ("faction", "{M} = {{ leave_faction = yes }}"),       # I am in their faction
    "ask:join": ("faction", "{M} = {{ leave_faction = yes }}"),
    "give:join": ("faction", "{T} = {{ leave_faction = yes }}"),          # they are in mine
    "ask:faction": ("faction", "{T} = {{ leave_faction = yes }}"),
}

_TERM = re.compile(r"\[\[\s*for\s+(\d+)\s*(months?|years?)\s*\]\]", re.I)
_INSTALMENTS = re.compile(r"\[\[\s*((?:offer|ask)\s+)?(" + "|".join(actions.AMOUNT_KINDS) + r")\s+(\d+)\s*"
                          r"(?:(?:x|×|times)\s*(\d+))?(?:\s*,?\s*every\s+(\d+)\s*(?:months?)?)?"
                          r"(?:\s*,?\s*(?:in|after)\s+(\d+)\s*(months?|years?)?)?\s*\]\]", re.I)


# ---------------------------------------------------------------- reading terms from a letter

def parse_terms(text: str) -> Tuple[str, Dict]:
    """Take terms out of the letter: (text with plain commands left, {"months": N, "payments": {key: [count,
    every, start]}}). An instalment command becomes the plain one: [[offer civ 2 x3]] -> [[offer civ 2]], the first
    payment made now; with "in N months" ([[offer civ 1 in 12 months]]) the first payment waits that long and
    nothing is paid now (deferred())."""
    terms: Dict = {"months": 0, "payments": {}}
    m = _TERM.search(text)
    if m:
        n = int(m.group(1)) * (12 if m.group(2).lower().startswith("year") else 1)
        terms["months"] = max(TERM[0], min(TERM[1], n))
    text = _TERM.sub("", text)

    def plain(m: re.Match) -> str:
        verb = (m.group(1) or "").strip().lower()
        kind, n, count = m.group(2).lower(), int(m.group(3)), int(m.group(4) or 1)
        every = int(m.group(5) or 12)
        start = int(m.group(6) or 0) * (12 if (m.group(7) or "").lower().startswith("year") else 1)
        if count >= 2 or start:
            key = f"{'ask' if verb == 'ask' else 'give'}:{kind}:{n}"
            terms["payments"][key] = [min(count, MAX_PAYMENTS), max(EVERY[0], min(EVERY[1], every)),
                                      min(start, TERM[1])]
        return f"[[{verb + ' ' if verb else ''}{kind} {n}]]"
    return _INSTALMENTS.sub(plain, text), terms


def _start(spec: List[int]) -> int:
    return spec[2] if len(spec) > 2 else 0


def deferred(terms: Optional[Dict], key: str) -> bool:
    """This item is only paid later (its first payment is months away): not to be applied now."""
    spec = ((terms or {}).get("payments") or {}).get(key)
    return bool(spec) and _start(spec) > 0


def for_items(terms: Optional[Dict], items: List[str]) -> Dict:
    """The part of a letter's terms that concerns these deal items."""
    terms = terms or {}
    pay = {k: v for k, v in (terms.get("payments") or {}).items() if k in items}
    lasting = any(_endable(k) or actions.is_pledge(k) for k in items)
    return {"months": terms.get("months", 0) if lasting else 0, "payments": pay}


def _endable(key: str) -> bool:
    return key in _ENDS or actions.is_rights(key) or actions.is_dmz(key)


def _every_words(every: int) -> str:
    return "every year" if every == 12 else f"every {every} months"


def terms_line(terms: Optional[Dict], items: List[str], them_name: str) -> str:
    """Under a proposal: what its terms mean, in plain words ('' if none)."""
    terms = for_items(terms, items)
    parts = []
    if terms["months"]:
        parts.append(f"the agreements last {terms['months']} months, then end")
    for key, spec in terms["payments"].items():
        count, every, start = spec[0], spec[1], _start(spec)
        what = actions.get_deal(key).label
        when = f"in {start} months" if start else "now"
        how = f"paid {count} times, {_every_words(every)}, the first {when}" if count > 1 else f"paid {when}"
        parts.append(f"{what} {how} ({'by ' + them_name if key.startswith('give:') else 'by us'})")
    return f"\n§YTerms: {'; '.join(parts)}.§!" if parts else ""


def help_text() -> str:
    """For the LLM: how to put terms on a deal."""
    return (f"TERMS. A deal can run over time. Add these next to the deal's commands; they apply to the agreements and "
            f"proposal in this letter:\n"
            f"- [[for N months]]: its agreements (pact, access, docking rights, guarantee, faction membership, resource "
            f"rights, demilitarized zones) and war pledges last N months ({TERM[0]} to {TERM[1]}), then end on "
            f"their own. Without it they last until someone ends them.\n"
            f"- Instalments: add xK to an amount's command, e.g. [[offer civ 2 x3]] = 2 now and 2 every year, 3 "
            f"payments in all, or [[ask rifles 1000 x4 every 6]] for every 6 months (up to {MAX_PAYMENTS} payments, "
            f"every {EVERY[0]} to {EVERY[1]} months). Add 'in N months' for a payment that starts later, with "
            f"nothing now: 2 factories now and 1 more in a year is [[offer civ 2]] [[offer civ 1 in 12 months]] "
            f"(the same amount now and later is [[offer civ 1 x2]]). Later instalments fall due on their own; a side "
            f"that stops "
            f"paying breaks the treaty, and it is remembered.\n"
            f"Use terms where they make a deal fairer or more believable (access for a year, aid paid over years, a "
            f"price paid in parts), not in every deal.")


# ---------------------------------------------------------------- keeping treaties

def _all(camp: campaign.Campaign) -> List[Dict]:
    out = camp.data.setdefault("treaties", [])
    for t in out:                                   # war pledges were "attack" keys at first
        if any(":attack:" in k for k in t["items"]):
            fix = lambda k: k.replace(":attack:", ":pledge:")
            t["items"] = [fix(k) for k in t["items"]]
            t["labels"] = {fix(k): v for k, v in t["labels"].items()}
            for c in t["clauses"]:
                c["key"] = fix(c["key"])
    return out


def _due(camp: campaign.Campaign) -> List[Dict]:
    return camp.data.setdefault("treaty_due", [])


def _add_months(key: str, months: int) -> str:
    try:
        y, m, d, h = (int(x) for x in key.split(".")[:4])
    except ValueError:
        return key
    m0 = m - 1 + months
    y, m = y + m0 // 12, m0 % 12 + 1
    return f"{y:04d}.{m:02d}.{min(d, 28):02d}.{h:02d}"


def record(camp: campaign.Campaign, tag: str, applied: List[str], terms: Optional[Dict], key: str,
           them_name: str, later: List[str] = ()) -> Optional[Dict]:
    """A deal with `tag` was just put in place (`applied`: the deal keys that took effect now; `later`: payments
    agreed to start later, see deferred()). Keep it as a treaty if it runs over time: a term, instalments still to
    come, or a war pledge."""
    applied = list(applied) + [k for k in later if k not in applied]
    terms = for_items(terms, applied)
    payments = [{"key": k, "count": spec[0], "every": spec[1], "start": _start(spec),
                 "paid": [] if _start(spec) else [key], "missed": []}
                for k, spec in terms["payments"].items()]
    clauses = [{"key": k, "fired": "", "asked": ""} for k in applied if actions.is_pledge(k)]
    until = _add_months(key, terms["months"]) if terms["months"] else ""
    if not (payments or clauses or (until and any(_endable(k) for k in applied))):
        return None
    me_name = (camp.snapshot or {}).get("countries", {}).get(camp.data["player"], {}).get("name", camp.data["player"])
    fmt = {"them_name": them_name, "me_name": me_name}
    # In names, not "we/they": the text goes into both sides' prompts.
    text = " ".join(actions.get_deal(k).remembered.format(**fmt) for k in applied).rstrip(".")
    t = {"id": max([x["id"] for x in _all(camp)] or [0]) + 1, "with": tag, "date": key,
         "when": campaign.date_words(key), "items": applied,
         "labels": {k: actions.get_deal(k).label for k in applied},
         "text": text, "until": until, "status": "active", "closed": "",
         "how": "", "payments": payments, "clauses": clauses}
    _all(camp).append(t)
    print(f"  treaty {t['id']} with {tag}: {t['text']}"
          + (f"; until {campaign.date_words(until)}" if until else "")
          + "".join(f"; {p['key']} x{p['count']} every {p['every']} months"
                    + (f", the first in {p['start']} months" if p["start"] else "") for p in payments))
    return t


def _event(camp: campaign.Campaign, kind: str, tags: List[str], text: str, **extra) -> None:
    key = camp.data.get("last_date") or ""
    camp.add_events([{"date": key, "when": campaign.date_words(key), "type": f"treaty:{kind}", "tags": tags,
                      "text": text, "major": False, **extra}])
    print(f"  [treaty] {text}")


def _close(t: Dict, status: str, today: str, how: str) -> None:
    t["status"], t["closed"], t["how"] = status, today, how


def _left(p: Dict) -> int:
    return p["count"] - len(p["paid"]) - len(p["missed"])


def _next_due(t: Dict, p: Dict) -> str:
    return _add_months(t["date"], p.get("start", 0) + p["every"] * (len(p["paid"]) + len(p["missed"])))


def _done(t: Dict) -> bool:
    return (all(_left(p) <= 0 for p in t["payments"]) and all(c["fired"] for c in t["clauses"])
            and not t["until"])


def _names(camp: campaign.Campaign, player: str, tag: str) -> Tuple[str, str]:
    c = (camp.snapshot or {}).get("countries", {})
    return c.get(tag, {}).get("name", tag), c.get(player, {}).get("name", player)


def _still(rel: "actions.Relation", key: str) -> bool:
    """Does this agreement of the treaty still hold?"""
    if key in _ENDS:
        what = _ENDS[key][0]
        return rel.bound == "faction" if what == "faction" else bool(rel.exists.get(what))
    sid = key.rsplit(":", 1)[1]
    give = key.startswith("give:")
    if actions.is_rights(key):
        return sid in ((rel.them_rights if give else rel.me_rights) or set())
    if actions.is_dmz(key):
        state = ((rel.them_border if give else rel.me_border) or {}).get(sid)
        return bool(state and state.dmz)
    return False


def _end_script(key: str, them: str, me: str) -> str:
    if key in _ENDS:
        return _ENDS[key][1].format(T=them, M=me)
    sid = key.rsplit(":", 1)[1]
    if actions.is_rights(key):         # the receiver gives the rights back
        return f"{me if key.startswith('give:') else them} = {{ remove_resource_rights = {sid} }}"
    return f"{sid} = {{ set_demilitarized_zone = no }}"


def tick(camp: campaign.Campaign, player: str) -> List[Tuple[str, str]]:
    """Play out the active treaties at today's date. Returns [(country, what happened)] worth a letter from it."""
    today = camp.data.get("last_date") or ""
    countries = (camp.snapshot or {}).get("countries", {})
    letters: List[Tuple[str, str]] = []
    changed = False
    for t in _all(camp):
        if t["status"] != "active" or not today:
            continue
        tag = t["with"]
        them_name, me_name = _names(camp, player, tag)
        if tag not in countries:
            _close(t, "lapsed", today, f"{them_name} no longer exists")
            changed = True
            continue
        if bridge.trigger(f"{player} = {{ has_war_with = {tag} }}"):
            _close(t, "lapsed", today, "war broke out between them")
            _event(camp, "lapsed", [tag, player], f"The treaty of {t['when']} between {them_name} and {me_name} "
                                                  f"lapsed with the war between them.")
            changed = True
            continue
        if t["until"] and today >= t["until"]:
            rel = actions.relation(tag, player)
            ended = [t["labels"].get(k, k) for k in t["items"]
                     if _endable(k) and _still(rel, k) and actions._effect(_end_script(k, tag, player))]
            _close(t, "ended", today, "its term ran out")
            text = (f"The term of the treaty of {t['when']} between {them_name} and {me_name} ran out"
                    + (f": {', '.join(ended)} came to an end." if ended else "."))
            _event(camp, "ended", [tag, player], text)
            letters.append((tag, text))
            changed = True
            continue
        for p in t["payments"]:
            if _left(p) <= 0 or today < _next_due(t, p):
                continue
            n = len(p["paid"]) + len(p["missed"]) + 1
            if p["key"].startswith("give:"):           # them pays me
                _, events = actions.apply(tag, player, them_name, me_name, [(p["key"], 0)], allowed=[p["key"]])
                what = t["labels"].get(p["key"], p["key"])
                if events:
                    p["paid"].append(today)
                    _event(camp, "paid", [tag, player], f"{them_name} paid {me_name} instalment {n} of {p['count']} "
                                                        f"under the treaty of {t['when']}: {what}.")
                else:
                    p["missed"].append(today)
                    _event(camp, "missed", [tag, player], f"{them_name} could not pay {me_name} instalment {n} of "
                                                          f"{p['count']} under the treaty of {t['when']} ({what}).")
                changed = True
            elif not any(d["treaty"] == t["id"] and d["key"] == p["key"] for d in _due(camp)) \
                    and not _shown(camp, t["id"], p["key"]):
                _due(camp).append({"treaty": t["id"], "kind": "payment", "key": p["key"], "date": today, "n": n})
                changed = True
        for c in t["clauses"]:
            if c["fired"] or c["asked"]:
                continue
            verb, _, enemy = c["key"].split(":")
            side, joiner = (player, tag) if verb == "give" else (tag, player)
            if not bridge.trigger(f"{side} = {{ has_war_with = {enemy} }}"):
                continue                                # the pledge isn't called yet
            enemy_name = countries.get(enemy, {}).get("name", enemy)
            side_name, joiner_name = (me_name, them_name) if verb == "give" else (them_name, me_name)
            changed = True
            if bridge.trigger(f"{joiner} = {{ has_war_with = {enemy} }}"):
                c["fired"] = today                      # already in that war: the pledge is kept as it stands
                _event(camp, "clause", [tag, player, enemy], f"{joiner_name} stands at war with {enemy_name} beside "
                                                             f"{side_name}, as it pledged in the treaty of {t['when']}.", joiner=joiner)
            elif verb == "give":                        # them keeps its word
                if actions._effect(actions.war_script(tag, player, enemy)) and \
                        bridge.trigger(f"{tag} = {{ has_war_with = {enemy} }}"):
                    c["fired"] = today
                    text = (f"Honouring the treaty of {t['when']}, {them_name} joined {me_name}'s war with "
                            f"{enemy_name}, as it had pledged.")
                    _event(camp, "clause", [tag, player, enemy], text, joiner=tag)
                    letters.append((tag, text))
                else:
                    c["asked"] = today                  # the game wouldn't let it join: don't try every tick
                    print(f"  treaty {t['id']}: {tag} could not join the war with {enemy}")
            else:
                c["asked"] = today
                _due(camp).append({"treaty": t["id"], "kind": "clause", "key": c["key"], "date": today, "n": 0})
        if _done(t):
            _close(t, "fulfilled", today, "all of it was carried out")
            changed = True
    if changed:
        camp.save()
    return letters


def _shown(camp: campaign.Campaign, tid: int, key: str) -> bool:
    """This obligation is the one waiting in the reply window right now."""
    offer = camp.data.get("pending_offer") or {}
    due = offer.get("treaty_due") or {}
    return due.get("treaty") == tid and due.get("key") == key


def _get(camp: campaign.Campaign, tid: int) -> Optional[Dict]:
    return next((t for t in _all(camp) if t["id"] == tid), None)


def next_prompt(camp: campaign.Campaign, player: str) -> Optional[Tuple[Dict, str, str]]:
    """The next obligation of the player's to ask about: (due, country, text for the reply window), or None.
    Taken off the waiting list; the caller keeps it as the pending offer."""
    while _due(camp):
        d = _due(camp).pop(0)
        t = _get(camp, d["treaty"])
        if not t or t["status"] != "active":
            continue
        them_name, me_name = _names(camp, player, t["with"])
        if d["kind"] == "payment":
            p = next((p for p in t["payments"] if p["key"] == d["key"]), None)
            if not p or _left(p) <= 0:
                continue
            what = t["labels"].get(d["key"], d["key"])
            text = (f"§YUnder the treaty of {t['when']}, {me_name} owes {them_name} instalment {d['n']} of "
                    f"{p['count']}: {what}.§!\n\nAccept pays it. Decline refuses, breaking the treaty: {them_name} "
                    f"and the world will remember it.")
        else:
            enemy = d["key"].rsplit(":", 1)[1]
            enemy_name = (camp.snapshot or {}).get("countries", {}).get(enemy, {}).get("name", enemy)
            text = (f"§Y{them_name} is at war with {enemy_name}. Under the treaty of {t['when']}, {me_name} "
                    f"pledged to join {them_name} whenever it goes to war with {enemy_name}.§!\n\nAccept honours "
                    f"the treaty: we go to war with {enemy_name} at once. Decline breaks our word: {them_name} and the "
                    f"world will remember it.")
        return d, t["with"], text
    return None


def answer(camp: campaign.Campaign, player: str, due: Dict, accepted: bool) -> str:
    """The player answered an obligation. Returns what happened, worth a letter from the other country if the
    treaty was broken ('' otherwise)."""
    from . import reputation
    t = _get(camp, due["treaty"])
    today = camp.data.get("last_date") or due["date"]
    if not t or t["status"] != "active":
        return ""
    tag = t["with"]
    them_name, me_name = _names(camp, player, tag)
    if due["kind"] == "payment":
        p = next((p for p in t["payments"] if p["key"] == due["key"]), None)
        if not p or _left(p) <= 0:
            return ""
        what = t["labels"].get(p["key"], p["key"])
        n = len(p["paid"]) + len(p["missed"]) + 1
        if accepted:
            _, events = actions.apply(tag, player, them_name, me_name, [(p["key"], 0)], allowed=[p["key"]])
            if events:
                p["paid"].append(today)
                _event(camp, "paid", [tag, player], f"{me_name} paid {them_name} instalment {n} of {p['count']} "
                                                    f"under the treaty of {t['when']}: {what}.")
            else:
                p["missed"].append(today)
                _event(camp, "missed", [tag, player], f"{me_name} could not pay {them_name} instalment {n} of "
                                                      f"{p['count']} under the treaty of {t['when']} ({what}).")
            if _done(t):
                _close(t, "fulfilled", today, "all of it was carried out")
            camp.save()
            return ""
        deed = (f"{me_name} refused to pay {them_name} instalment {n} of {p['count']} ({what}) owed under the treaty "
                f"of {t['when']} ({t['text']}), breaking it.")
    else:
        enemy = due["key"].rsplit(":", 1)[1]
        enemy_name = (camp.snapshot or {}).get("countries", {}).get(enemy, {}).get("name", enemy)
        c = next((c for c in t["clauses"] if c["key"] == due["key"]), None)
        if accepted:
            if bridge.trigger(f"{player} = {{ has_war_with = {enemy} }}") or \
                    actions._effect(actions.war_script(player, tag, enemy)):
                if c:
                    c["fired"] = today
                _event(camp, "clause", [tag, player, enemy], f"Honouring the treaty of {t['when']}, {me_name} joined "
                                                             f"{them_name}'s war against {enemy_name}.", joiner=player)
            if _done(t):
                _close(t, "fulfilled", today, "all of it was carried out")
            camp.save()
            return ""
        deed = (f"{them_name} went to war with {enemy_name}, and {me_name} refused to join it as it had pledged in "
                f"the treaty of {t['when']} ({t['text']}), breaking its word.")
    _close(t, "broken", today, f"broken by {me_name}")
    _event(camp, "broken", [tag, player], deed)
    reputation._rep(camp)["deeds"].append({"date": today, "when": campaign.date_words(today), "tags": [player, tag],
                                           "facts": deed, "weight": None, "verdict": ""})
    camp.save()
    return deed


# ---------------------------------------------------------------- the AI's side

def standing(camp: campaign.Campaign, tag: str) -> set:
    """War pledges between `tag` and the player that stand now (not called yet): not to be made twice."""
    return {c["key"] for t in _all(camp) if t["with"] == tag and t["status"] == "active"
            for c in t["clauses"] if not c["fired"]}


def owes(camp: campaign.Campaign, tag: str) -> List[Dict]:
    """Active treaties in which `tag` still owes the player something (instalments or a war pledge)."""
    return [t for t in _all(camp) if t["with"] == tag and t["status"] == "active"
            and (any(p["key"].startswith("give:") and _left(p) > 0 for p in t["payments"])
                 or any(c["key"].startswith("give:") and not c["fired"] for c in t["clauses"]))]


def renounce(camp: campaign.Campaign, tag: str, player: str) -> List[Tuple[str, bool]]:
    """`tag` renounces its treaties with the player ([[end treaty]] in its letter). Lines to show the player."""
    today = camp.data.get("last_date") or ""
    them_name, me_name = _names(camp, player, tag)
    shown = []
    for t in owes(camp, tag):
        _close(t, "renounced", today, f"renounced by {them_name}")
        _event(camp, "renounced", [tag, player], f"{them_name} renounced the treaty of {t['when']} with {me_name} "
                                                 f"({t['text']}).")
        shown.append((f"{them_name} renounced the treaty of {t['when']}.", True))
    return shown


def take_renounce(commands: List[Tuple[str, int]]) -> Tuple[List[Tuple[str, int]], bool]:
    """Pull [[end treaty]] out of the letter's commands."""
    rest = [c for c in commands if c[0] != "end:treaty"]
    return rest, len(rest) != len(commands)


def describe(camp: campaign.Campaign, tag: str, them_name: str, me_name: str) -> str:
    """Treaties between `tag` and the player, for its prompts ('' if none)."""
    lines = []
    for t in _all(camp):
        if t["with"] != tag:
            continue
        bits = [f"- Treaty of {t['when']}: {t['text']}."]
        if t["status"] == "active":
            if t["until"]:
                bits.append(f"Its agreements end on {campaign.date_words(t['until'])}.")
            for p in t["payments"]:
                payer, payee = (them_name, me_name) if p["key"].startswith("give:") else (me_name, them_name)
                bits.append(f"{payer} pays {payee} {t['labels'].get(p['key'], p['key'])} {_every_words(p['every'])}: "
                            f"{len(p['paid'])} of {p['count']} paid"
                            + (f", {len(p['missed'])} missed" if p["missed"] else "")
                            + (f", next due {campaign.date_words(_next_due(t, p))}" if _left(p) > 0 else "") + ".")
            for c in t["clauses"]:
                enemy = (camp.snapshot or {}).get("countries", {}).get(c["key"].rsplit(":", 1)[1], {}).get(
                    "name", c["key"].rsplit(":", 1)[1])
                who, whom = (them_name, me_name) if c["key"].startswith("give:") else (me_name, them_name)
                bits.append(f"War pledge: {who} joins {whom} whenever {whom} goes to war with {enemy}"
                            + (f" (honoured {campaign.date_words(c['fired'])})." if c["fired"] else " (stands)."))
        else:
            bits.append(f"{t['status'].upper()} on {campaign.date_words(t['closed'])}: {t['how']}.")
        lines.append(" ".join(bits))
    if not lines:
        return ""
    out = (f"TREATIES WITH {me_name.upper()} (deals made by letter that run over time; the game carries out the "
           f"active ones):\n" + "\n".join(lines[-8:]))
    if owes(camp, tag):
        out += (f"\nYou keep your side of an active treaty: the game pays your instalments and holds you to your "
                f"pledges. Only for grave cause ({me_name} broke faith, or everything has changed) may you renounce "
                f"your obligations with [[end treaty]] in a letter, which the world will remember.")
    return out


def drop_after(camp: campaign.Campaign, key: str) -> None:
    """An earlier save was loaded: forget treaties made, and steps taken, from that moment on."""
    keep = []
    for t in _all(camp):
        if t["date"] >= key:
            continue
        for p in t["payments"]:
            p["paid"] = [d for d in p["paid"] if d < key]
            p["missed"] = [d for d in p["missed"] if d < key]
        for c in t["clauses"]:
            c["fired"] = c["fired"] if c["fired"] and c["fired"] < key else ""
            c["asked"] = c["asked"] if c["asked"] and c["asked"] < key else ""
        if t["closed"] and t["closed"] >= key:
            t["status"], t["closed"], t["how"] = "active", "", ""
        keep.append(t)
    camp.data["treaties"] = keep
    camp.data["treaty_due"] = [d for d in _due(camp) if d["date"] < key]
