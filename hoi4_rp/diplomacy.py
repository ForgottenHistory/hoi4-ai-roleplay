"""AI-to-AI diplomacy: countries other than the player write to each other.

[ai_diplomacy] mode = dry: exchanges are written and logged, nothing reaches the game. mode = on: an agreed package of
LIVE kinds (pacts, market access, guarantees) takes effect, as chronicle news; anything else stays logged.
One exchange is two short LLM calls: a dispatch from one government (a few sentences, its stance, at most one
proposal from a short menu) and the other's answer: accept, decline, or counter with a package of its own (often
theirs without what it refuses), on which the proposer has the last word in a third call. One round only. The prompts are short by
design (about 1.5-2k tokens, a tenth of a letter to the player): a card per country, what is in force between
them, their last few events together.

Who talks to whom (score()): pairs within reach of each other, weighted by what binds or divides them:
    neighbours                       +4    same continent (capitals)        +2
    a border through a colony (capitals on different continents: France and Siam)  +1
    both major powers                +2    a major and a minor, same continent  +1
    same faction / a guarantee      +2    recent events between them (90 days)  +3 each, at most +6
    a commitment by letter touching both (treaties.commitments)                  +3
    on the player's continent        +1 each   (what happens near the player matters to its story)
A pair with nothing of these (Siam and Venezuela) never talks. Pairs at war, subjects (their overlord speaks for
them) and the player are left out; the player has its own letters. Each game month EXCHANGES pairs are drawn at
random weighted by score squared, so France and Germany talk often and Japan and the USA now and then; a pair
off the player's continent counts FAR_WEIGHT as much unless both are major powers (Bolivia and Chile matter less to
an Austrian story than Hungary and Romania, but the world isn't silent). A pair talks
again only after COOLDOWN days, a country starts at most one exchange a month.

Kept in the campaign: data["ai_diplomacy"] = {"tick": date, "queue": [[a, b]], "pairs": {"A|B": date},
"started": {tag: date}}; the letters themselves under their pair (campaign.letters_with(a, b)), marked "dry".
Logged readably to data/logs/ai_diplomacy.txt.
"""

from __future__ import annotations

import os
import random
from typing import Dict, List, Optional, Tuple

from . import campaign, gamestate

LOG_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "logs",
                        "ai_diplomacy.txt")
EVENT_DAYS = 90
MIN_SCORE = 4
FAR_WEIGHT = 0.3


# ---------------------------------------------------------------- the world, once a month

def world() -> Dict[str, Dict]:
    """Every country: continent, neighbours, guarantees given, strength, subject or not (one game query)."""
    lines = gamestate.query(
        'every_country = { limit = { exists = yes } '
        'log = "AIRPDM|[THIS.GetTag]|[THIS.Capital.GetContinent]|[?THIS.num_of_civilian_factories]|'
        '[?THIS.num_of_military_factories]|[?THIS.num_divisions]" '
        'if = { limit = { is_subject = yes } log = "AIRPDS|[THIS.GetTag]" } '
        'every_neighbor_country = { log = "AIRPDN|[PREV.GetTag]|[THIS.GetTag]" } '
        'every_country = { limit = { PREV = { has_guaranteed = PREV } } log = "AIRPDG|[PREV.GetTag]|[THIS.GetTag]" } }')
    num = lambda x: int(float(x)) if x.strip().replace(".", "", 1).isdigit() else 0
    out: Dict[str, Dict] = {}
    for line in lines:
        f = [x.strip() for x in line.split("|")]
        if f[0] == "AIRPDM" and len(f) >= 6:
            out[f[1]] = {"continent": f[2], "civ": num(f[3]), "mil": num(f[4]), "divisions": num(f[5]),
                         "subject": False, "neighbours": set(), "guarantees": set()}
        elif f[0] == "AIRPDS" and f[1] in out:
            out[f[1]]["subject"] = True
        elif f[0] == "AIRPDN" and f[1] in out:
            out[f[1]]["neighbours"].add(f[2])
        elif f[0] == "AIRPDG" and f[1] in out:
            out[f[1]]["guarantees"].add(f[2])
    return out


# ---------------------------------------------------------------- who talks to whom

def score(camp: campaign.Campaign, w: Dict[str, Dict]) -> List[Tuple]:
    """Every pair worth an exchange: (score, a, b, reasons, draw weight), best first."""
    snap = camp.snapshot or {}
    c = snap.get("countries", {})
    player = camp.data["player"]
    home = w.get(player, {}).get("continent", "")
    wars = set(snap.get("wars", []))
    since = campaign.shift(snap.get("date", ""), -EVENT_DAYS)
    events: Dict[str, int] = {}
    for e in camp.data["events"]:
        if e["date"] >= since and not e.get("type", "").startswith(("agreement:market", "offer_answer")):
            tags = sorted(set(e["tags"]) - {player})
            for i, a in enumerate(tags):
                for b in tags[i + 1:]:
                    events[f"{a}|{b}"] = events.get(f"{a}|{b}", 0) + 1
    commits: Dict[str, int] = {}
    from . import treaties
    for r in treaties.commitments(camp):
        for x, y in ((r["actor"], r["target"]), (r["for"], r["target"])):
            commits[campaign.pair(x, y)] = commits.get(campaign.pair(x, y), 0) + 1
    tags = sorted(t for t in w if t in c and t != player and not w[t]["subject"] and not c[t].get("capitulated"))
    out = []
    for i, a in enumerate(tags):
        for b in tags[i + 1:]:
            key = campaign.pair(a, b)
            if key in wars:
                continue
            wa, wb = w[a], w[b]
            same = wa["continent"] == wb["continent"]
            ma, mb = c[a].get("major"), c[b].get("major")
            s, why = 0, []
            if b in wa["neighbours"] and same:
                s, why = s + 4, why + ["neighbours"]
            elif b in wa["neighbours"]:
                s, why = s + 1, why + ["colonial border"]
            elif same:
                s, why = s + 2, why + ["same continent"]
            if ma and mb:
                s, why = s + 2, why + ["both majors"]
            elif (ma or mb) and same:
                s, why = s + 1, why + ["a major nearby"]
            if c[a].get("faction") and c[a].get("faction") == c[b].get("faction"):
                s, why = s + 2, why + ["same faction"]
            if b in wa["guarantees"] or a in wb["guarantees"]:
                s, why = s + 2, why + ["guarantee"]
            if events.get(key):
                s, why = s + min(6, 3 * events[key]), why + [f"{events[key]} recent events"]
            if commits.get(key):
                s, why = s + 3, why + ["a commitment"]
            if s < MIN_SCORE:
                continue
            near = (wa["continent"] == home) + (wb["continent"] == home)
            if near:
                s, why = s + near, why + ["near the player"]
            weight = s * s * (1 if near or (ma and mb) else FAR_WEIGHT)
            out.append((s, a, b, why, weight))
    return sorted(out, key=lambda x: -x[0])


def pick(camp: campaign.Campaign, scored: List[Tuple], n: int, cooldown_days: int,
         rng: random.Random = random) -> List[Tuple[str, str]]:
    """Draw n pairs by their draw weight (score squared, less far from the player), leaving out pairs on cooldown and countries that started one
    within a month. Who writes first: either side, at random."""
    st = _state(camp)
    today = (camp.snapshot or {}).get("date", "")
    ok = lambda d, days: not d or campaign.shift(d, days) <= today
    pool = [x for x in scored if ok(st["pairs"].get(campaign.pair(x[1], x[2]), ""), cooldown_days)]
    chosen: List[Tuple[str, str]] = []
    busy = {t for t, d in st["started"].items() if not ok(d, 30)}
    while pool and len(chosen) < n:
        s, a, b = rng.choices(pool, [x[4] for x in pool])[0][:3]
        pool = [x for x in pool if (x[1], x[2]) != (a, b)]
        first, second = (a, b) if rng.random() < 0.5 else (b, a)
        if first in busy:
            first, second = second, first
        if first in busy or second in {x for p in chosen for x in p}:
            continue
        chosen.append((first, second))
        busy.add(first)
    return chosen


def _state(camp: campaign.Campaign) -> Dict:
    st = camp.data.setdefault("ai_diplomacy", {})
    for k, v in (("tick", ""), ("queue", []), ("pairs", {}), ("started", {}), ("applied", [])):
        st.setdefault(k, v)
    return st


# ---------------------------------------------------------------- the monthly draw

def due(camp: campaign.Campaign) -> bool:
    """A game month since the last draw."""
    st, today = _state(camp), (camp.snapshot or {}).get("date", "")
    return bool(today) and (not st["tick"] or campaign.shift(st["tick"], 30) <= today)


def draw(camp: campaign.Campaign, n: int, cooldown_days: int) -> List[List]:
    """This month's pairs, queued: [[first, second, score, reasons]]. Pairs left from last month go: written
    late, a dispatch would answer a month that has passed."""
    st = _state(camp)
    scored = score(camp, world())
    why = {(x[1], x[2]): (x[0], x[3]) for x in scored}
    st["queue"] = []
    for a, b in pick(camp, scored, n, cooldown_days):
        s, reasons = why.get((a, b)) or why.get((b, a))
        st["queue"].append([a, b, s, reasons])
    st["tick"] = (camp.snapshot or {}).get("date", "")
    return st["queue"]


# ---------------------------------------------------------------- the short prompts

def card(s: gamestate.CountryState, w: Dict, other: str) -> str:
    """A country in a few lines: government, weight, alignment, wars, and its opinion of `other`."""
    pct = lambda v: f"{round(v * 100)}%" if v is not None else "?"
    bits = [f"{s.name} ({s.tag}): {s.ideology} government, {s.ruling_party or 'ruling party'} under {s.leader}; "
            f"{'a major power' if s.is_major else 'a minor power'}",
            f"{w.get('civ', 0)} civilian / {w.get('mil', 0)} military factories, {w.get('divisions', 0)} divisions; "
            f"stability {pct(s.stability)}, war support {pct(s.war_support)}"]
    if s.faction:
        bits.append(f"in the faction {s.faction}" + (" as its leader" if s.faction_leader == s.tag else
                                                     f" led by {s.name_of(s.faction_leader)}" if s.faction_leader
                                                     else ""))
    if s.overlord:
        bits.append(f"a subject of {s.name_of(s.overlord)}")
    if s.enemies:
        bits.append("at war with " + ", ".join(s.name_of(t) for t in s.enemies[:6]))
    if s.wargoals:
        bits.append("holds war goals on " + ", ".join(dict.fromkeys(s.name_of(g.other) for g in s.wargoals[:4])))
    if other in s.opinion:
        bits.append(f"its opinion of {s.name_of(other)}: {round(s.opinion[other]):+d}")
    return "- " + "; ".join(bits) + "."


_TIES = [  # (trigger, words), {A} {B} tags, {a} {b} names
    ("{A} = {{ has_non_aggression_pact_with = {B} }}", "a non-aggression pact"),
    ("{A} = {{ is_in_faction_with = {B} }}", "the same faction"),
    ("{A} = {{ has_guaranteed = {B} }}", "{a} guarantees {b}"),
    ("{B} = {{ has_guaranteed = {A} }}", "{b} guarantees {a}"),
    ("{A} = {{ gives_military_access_to = {B} }}", "{a} gives {b} military access"),
    ("{B} = {{ gives_military_access_to = {A} }}", "{b} gives {a} military access"),
    ("{A} = {{ has_market_access_with = {B} }}", "market access"),
    ("{A} = {{ is_embargoing = {B} }}", "{a} embargoes {b}"),
    ("{B} = {{ is_embargoing = {A} }}", "{b} embargoes {a}"),
    ("{A} = {{ has_wargoal_against = {B} }}", "{a} holds a war goal on {b}"),
    ("{B} = {{ has_wargoal_against = {A} }}", "{b} holds a war goal on {a}"),
]


def ties(a: str, b: str, an: str, bn: str) -> str:
    """What is in force between two countries, read live."""
    from . import bridge
    found = [words.format(a=an, b=bn) for trig, words in _TIES if bridge.trigger(trig.format(A=a, B=b))]
    return "; ".join(found) or "nothing in force"


def _pair_events(camp: campaign.Campaign, a: str, b: str, n: int = 5) -> List[str]:
    ev = [e for e in camp.data["events"] if a in e["tags"] and b in e["tags"]
          and not e.get("type", "").startswith(("agreement:market", "offer_answer"))]
    return [f"- {e['when']}: {e['text']}" for e in ev[-n:]]


def _commitments(camp: campaign.Campaign, a: str, b: str, name) -> List[str]:
    from . import treaties
    words = treaties._COMMIT_WORDS
    return [f"- {name(r['actor'])} has committed to " + words[r["kind"]].format(x=name(r["target"]), b=name(r["for"]))
            for r in treaties.commitments(camp) if {a, b} & {r["actor"], r["for"], r["target"]}]


def _last_exchange(camp: campaign.Campaign, a: str, b: str, name) -> str:
    ls = camp.letters_with(a, b)
    if not ls:
        return "You have not corresponded before."
    return "\n".join([f"Your last exchange ({ls[-1]['when']}):"] + [f"{name(x['from'])}: {x['text']}" for x in ls[-2:]])


SHORT_NOTE = ("A proposal is one package the other side accepts or declines whole: propose only what your government "
              "wants and could get, often nothing at all. A guarantee means defending them if attacked; joining a war "
              "or a faction is the gravest step; a commitment (oppose, befriend, back) is a standing diplomatic line "
              "toward a third country, not a war.")
WEIGH_NOTE = ("Weigh each item for your own country alone: what it gains and what it costs. A guarantee can drag you "
              "into the other's wars and ties up factories; military access lets foreign armies through your land; "
              "joining a faction or a war is for life and death; a commitment against a country makes it your enemy. "
              "Take what serves you, counter what only half serves you, decline what serves only them: courtesy is "
              "no reason to agree, and most governments say no more often than yes.")
MENU_GIVES = ("give:pact", "give:access", "give:guarantee", "give:market", "give:faction", "give:join")
MENU_ASKS = ("ask:access", "ask:guarantee", "ask:faction", "ask:join")
STANCES = ("friendly", "cordial", "indifferent", "wary", "hostile")


def menu(a: str, b: str, an: str, bn: str) -> Tuple[List[str], List[str], str]:
    """What `a` could propose to `b`: (offers, asks, help lines). No land, factories or governments yet."""
    from . import actions
    rel = actions.relation(a, b)
    offers = actions.possible_keys(rel, list(MENU_GIVES) + actions.war_gives(rel) + actions.commit_gives(rel))
    asks = actions.possible_keys(rel, list(MENU_ASKS) + actions.war_asks(rel) + actions.commit_asks(rel))
    f = actions._fmt(an, bn)
    lines = actions._item_lines("offer ", offers, f) + actions._item_lines("ask ", asks, f)
    return offers, asks, "\n".join(lines)


def _situation(camp: campaign.Campaign, me: gamestate.CountryState, other: gamestate.CountryState,
               w: Dict[str, Dict]) -> str:
    c = (camp.snapshot or {}).get("countries", {})
    name = lambda t: me.name if t == me.tag else other.name if t == other.tag else c.get(t, {}).get("name", t)
    parts = [f"You are the government of {me.name} in Hearts of Iron IV. The date is {me.date.strip()}. Act as a "
             f"shrewd player of {me.name} would: its interests first, true to its leader and ideology. Write in "
             f"English, whatever your country's language.",
             "THE TWO COUNTRIES:\n" + card(me, w.get(me.tag, {}), other.tag) + "\n"
             + card(other, w.get(other.tag, {}), me.tag),
             f"BETWEEN YOU: {ties(me.tag, other.tag, me.name, other.name)}."]
    ev = _pair_events(camp, me.tag, other.tag)
    if ev:
        parts.append("RECENT EVENTS BETWEEN YOU:\n" + "\n".join(ev))
    cm = _commitments(camp, me.tag, other.tag, name)
    if cm:
        parts.append("STANDING COMMITMENTS (agreed by letter):\n" + "\n".join(cm))
    from . import guarantees
    civ = lambda t: w.get(t, {}).get("civ", 0)
    parts.append(f"A guarantee given by letter is no gesture: it ties up the guarantor's civilian factories for "
                 f"{guarantees.TERM_MONTHS} months. One more would cost {me.name} "
                 f"{guarantees.next_cost(camp, me.tag)} of its {civ(me.tag)}, and {other.name} "
                 f"{guarantees.next_cost(camp, other.tag)} of its {civ(other.tag)}.")
    parts.append(_last_exchange(camp, me.tag, other.tag, name))
    return "\n\n".join(parts)


def dispatch_prompt(camp: campaign.Campaign, a: gamestate.CountryState, b: gamestate.CountryState,
                    w: Dict[str, Dict]) -> Tuple[List[Dict], List[str], List[str]]:
    offers, asks, lines = menu(a.tag, b.tag, a.name, b.name)
    task = (f"Write a short diplomatic dispatch to {b.name}: 2 to 4 sentences in {a.name}'s own voice, about what "
            f"matters most between you now (no heading, date or place line). Then, on a line of its own:\n"
            f"STANCE: one of {', '.join(STANCES)} (how {a.name} regards {b.name} now)\n"
            + (f"Only if {a.name} really wants a deal now, add a proposal, one command per item (at most three):\n"
               f"{lines}\n{SHORT_NOTE}" if lines else "No agreement is possible between you now: propose nothing."))
    return ([{"role": "system", "content": _situation(camp, a, b, w) + "\n\n" + task},
             {"role": "user", "content": "Write the dispatch now."}], offers, asks)


def answer_prompt(camp: campaign.Campaign, b: gamestate.CountryState, a: gamestate.CountryState,
                  w: Dict[str, Dict], text: str, items: List[str]) -> Tuple[List[Dict], List[str], List[str]]:
    """`b` answers `a`'s dispatch: accept, decline or (with a proposal) counter with a package of its own. Returns
    (messages, what b could offer a, what b could ask of a) for reading the counter."""
    from . import actions
    deal = (f"Its proposal, a package you accept or decline as a whole: {actions.package_text(items, a.name)}."
            if items else "It makes no proposal.")
    c_offers, c_asks, lines = menu(b.tag, a.tag, b.name, a.name) if items else ([], [], "")
    answer = ""
    if items:
        answer = (f"ANSWER: accept, decline or counter. accept: every item of the package. decline: none of it. "
                  f"counter: you refuse part of it but would agree to a different package; write ANSWER: counter, "
                  f"then your package as commands, one per item (at most three). Often it is their package without "
                  f"what you refuse; it may also ask something modest in return (not always a guarantee: market "
                  f"access, a pact, a commitment, or simply less). {a.name} then accepts or declines it.\n"
                  f"{WEIGH_NOTE}\n")
        if lines:
            answer += f"Commands for a counter (your offers to {a.name} and your requests of it):\n{lines}\n"
    task = (f"{a.name} sent you this dispatch:\n\"{text}\"\n{deal}\n\nAnswer it in 1 to 3 sentences in {b.name}'s "
            f"own voice (no heading, date or place line). Then, on lines of their own:\n{answer}"
            f"STANCE: one of {', '.join(STANCES)} (how {b.name} regards {a.name} now)")
    return ([{"role": "system", "content": _situation(camp, b, a, w) + "\n\n" + task},
             {"role": "user", "content": "Write the answer now."}], c_offers, c_asks)


def final_prompt(camp: campaign.Campaign, a: gamestate.CountryState, b: gamestate.CountryState, w: Dict[str, Dict],
                 text: str, reply: str, counter: List[str]) -> List[Dict]:
    """`a` decides on `b`'s counter-proposal (the last word: one round only)."""
    from . import actions
    task = (f"You wrote to {b.name}:\n\"{text}\"\n\n{b.name} answered:\n\"{reply}\"\nand made a counter-proposal "
            f"instead of yours, a package you accept or decline as a whole: {actions.package_text(counter, b.name)}."
            f"\n\nAnswer in 1 or 2 sentences in {a.name}'s own voice: is the counter-proposal still worth it to "
            f"{a.name}? {WEIGH_NOTE}\nThen, on lines of their own:\nANSWER: accept or decline\n"
            f"STANCE: one of {', '.join(STANCES)} (how {a.name} regards {b.name} now)")
    return [{"role": "system", "content": _situation(camp, a, b, w) + "\n\n" + task},
            {"role": "user", "content": "Write the answer now."}]


_KEYLINE = r"^[\W_]*{key}[*_\s]*[:\-–]+\s*(.+)$"


def _line(text: str, key: str) -> str:
    import re
    m = re.search(_KEYLINE.format(key=key), text, re.I | re.M)
    return m.group(1).strip(" *_.").lower() if m else ""


def _tidy(text: str) -> str:
    """Lines left empty but for a dash or bullet once their commands are taken out go, and runs of blank lines."""
    import re
    text = re.sub(r"^[ \t]*[-*•–]+[ \t]*$", "", text, flags=re.M)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _strip(text: str) -> str:
    """The text without its STANCE and ANSWER lines."""
    import re
    return re.sub(r"^[\W_]*(stance|answer)[*_\s]*[:\-–].*$", "", text, flags=re.I | re.M).strip()


def read_dispatch(raw: str, offers: List[str], asks: List[str]) -> Tuple[str, str, List[str]]:
    """(text, stance, proposal items) from a dispatch."""
    from . import actions
    stance = next((s for s in STANCES if s in _line(raw, "stance")), "indifferent")
    text, _, proposals = actions.parse(_strip(raw))
    items = actions.package([p for p in proposals if p in offers + asks], offers + asks)[:3]
    return _tidy(text), stance, items


def read_answer(raw: str, items: List[str], c_offers: List[str] = (), c_asks: List[str] = ()
                ) -> Tuple[str, str, str, List[str]]:
    """(text, answer: accept | decline | counter | '', stance, the counter's items) from an answer. A counter
    without a valid item is a decline."""
    import re
    from . import actions
    stance = next((s for s in STANCES if s in _line(raw, "stance")), "indifferent")
    said = _line(raw, "answer")
    text, _, proposals = actions.parse(_strip(raw))
    allowed = list(c_offers) + list(c_asks)
    counter = actions.package([p for p in proposals if p in allowed], allowed)[:3] if allowed else []
    if not items:
        answer = ""
    elif "counter" in said and counter:
        answer = "counter"
    # "decline, though we would accept the pact alone" is a refusal: a refusal word anywhere wins
    elif "accept" in said and not re.search(r"\b(declin|reject|refus)", said):
        answer = "accept"
    else:
        answer = "decline"
    return _tidy(text), answer, stance, counter if answer == "counter" else []


# ---------------------------------------------------------------- keeping it, and what takes effect

# The deal kinds that take effect between AI countries (mode = on); a package holding anything else stays logged
# only, whole (half a trade is no deal). Commitments, sanctions, factions and wars come later.
LIVE = ("give:pact", "give:market", "give:guarantee", "ask:guarantee")


def _settled(a: str, b: str, items: List[str], answer: str, counter: List[str], final_answer: str
             ) -> Tuple[List[str], str, str]:
    """The package agreed, and who wrote it ("give" = writer gives other): (items, writer, other), or ([], "", "")."""
    if items and answer == "accept":
        return list(items), a, b
    if counter and final_answer == "accept":
        return list(counter), b, a
    return [], "", ""


def apply_deal(camp: campaign.Campaign, writer: str, other: str, items: List[str]) -> List[str]:
    """Put an agreed package in place between two AI countries: the game's agreements, the chronicle's news (major
    when a major power is in it), a letter guarantee's term and upkeep, and the ledger undo() works from. Returns
    what took effect, as news."""
    from . import actions, guarantees
    c = (camp.snapshot or {}).get("countries", {})
    name = lambda t: c.get(t, {}).get("name", t)
    key = camp.data.get("last_date") or ""
    _, events = actions.apply(writer, other, name(writer), name(other), [(k, 0) for k in items], allowed=list(items))
    major = any(c.get(t, {}).get("major") for t in (writer, other))
    camp.add_events([{**e, "date": key, "when": campaign.date_words(key), "major": major, "ai": True}
                     for e in events])
    done = [e["type"] for e in events]
    guarantees.record(camp, writer, done, None, other=other)
    if done:
        _state(camp)["applied"].append({"date": key, "with": writer, "me": other, "items": done})
    return [e["text"] for e in events]


_UNDO = {   # what ends each live kind ("give" = W gives M)
    "give:pact": ("{W} = {{ has_non_aggression_pact_with = {M} }}",
                  "{W} = {{ diplomatic_relation = {{ country = {M} relation = non_aggression_pact active = no }} }}"),
    # the game has no effect that ends market access, but an embargo does, and it stays ended once lifted
    "give:market": ("{W} = {{ has_market_access_with = {M} }}",
                    "{W} = {{ send_embargo = {M} }} {W} = {{ diplomatic_relation = {{ country = {M} relation = embargo "
                    "active = no }} }}"),
    "give:guarantee": ("{W} = {{ has_guaranteed = {M} }}",
                       "{W} = {{ diplomatic_relation = {{ country = {M} relation = guarantee active = no }} }}"),
    "ask:guarantee": ("{M} = {{ has_guaranteed = {W} }}",
                      "{M} = {{ diplomatic_relation = {{ country = {W} relation = guarantee active = no }} }}"),
}


def undo(camp: campaign.Campaign) -> List[str]:
    """End every agreement AI-to-AI diplomacy put in place that still holds (`python -m hoi4_rp ai-undo`), and stop
    the upkeep of its guarantees. Returns what was ended."""
    from . import actions, bridge, guarantees
    c = (camp.snapshot or {}).get("countries", {})
    name = lambda t: c.get(t, {}).get("name", t)
    ended = []
    for x in _state(camp)["applied"]:
        for k in x["items"]:
            if k not in _UNDO:
                continue
            holds, script = (s.format(W=x["with"], M=x["me"]) for s in _UNDO[k])
            if bridge.trigger(holds) and actions._effect(script):
                ended.append(f"{name(x['with'])} / {name(x['me'])}: {k}")
    _state(camp)["applied"] = []
    guarantees.tick(camp)                      # guarantees no longer held stop costing
    camp.save()
    return ended


def record(camp: campaign.Campaign, a: str, b: str, text: str, stance: str, items: List[str], reply: str,
           answer: str, b_stance: str, why: List, usage: str = "", counter: List[str] = (), final: str = "",
           final_answer: str = "", final_stance: str = "") -> None:
    """Store a finished exchange and log it; with mode = on, put an agreed package of LIVE kinds in place. With a
    counter-proposal, `final` is `a`'s last word on it."""
    from . import actions
    from .config import settings
    key = camp.data.get("last_date") or ""
    agreed, writer, other = _settled(a, b, items, answer, list(counter), final_answer)
    live = settings().ai.mode == "on" and bool(agreed) and all(k in LIVE for k in agreed)
    news = apply_deal(camp, writer, other, agreed) if live else []
    dry = not news
    camp.add_letter(a, a, text, key, other=b)
    camp.letters_with(a, b)[-1].update({"dry": dry, "proposal": items, "stance": stance})
    camp.add_letter(b, b, reply, key, other=a)
    camp.letters_with(a, b)[-1].update({"dry": dry, "answer": answer, "stance": b_stance,
                                        **({"proposal": list(counter)} if counter else {})})
    if counter:
        camp.add_letter(a, a, final, key, other=b)
        camp.letters_with(a, b)[-1].update({"dry": dry, "answer": final_answer, "stance": final_stance})
    for x, y, s in ((a, b, final_stance or stance), (b, a, b_stance)):
        camp.set_policy(x, {"stance": s, "date": key, "when": campaign.date_words(key), "light": True}, other=y)
    c = (camp.snapshot or {}).get("countries", {})
    name = lambda t: c.get(t, {}).get("name", t)
    deal = actions.package_text(items, name(a)) if items else ""
    lines = [f"=== {campaign.date_words(key)}: {name(a)} -> {name(b)}   (score {why[0]}: {', '.join(why[1])})",
             f"{name(a).upper()} ({stance}): {text}"]
    if deal:
        lines.append(f"  proposes: {deal}")
    lines.append(f"{name(b).upper()} ({b_stance}{', ' + answer.upper() if answer else ''}): {reply}")
    if counter:
        lines += [f"  counters: {actions.package_text(list(counter), name(b))}",
                  f"{name(a).upper()} ({final_stance}, {final_answer.upper()}): {final}"]
    if news:
        lines.append(f"  [live] took effect: {' '.join(news)}")
    elif agreed:
        why_not = ("dry run" if settings().ai.mode != "on" else
                   "not live yet" if not all(k in LIVE for k in agreed) else "could not be put in place")
        lines.append(f"  [{why_not}] would take effect: {actions.package_text(agreed, name(writer))}")
    if usage:
        lines.append(f"  ({usage})")
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n\n")
    print("\n".join("  [ai] " + x for x in lines))


def drop_after(camp: campaign.Campaign, key: str) -> None:
    """An earlier save was loaded: this month's draw, the cooldowns and what took effect since are forgotten (the
    save holds the game as it was; the letters, stances and guarantees go with their own rewinds)."""
    st = _state(camp)
    st["queue"] = []
    st["tick"] = st["tick"] if st["tick"] < key else ""
    st["pairs"] = {k: d for k, d in st["pairs"].items() if d < key}
    st["started"] = {k: d for k, d in st["started"].items() if d < key}
    st["applied"] = [x for x in st["applied"] if x["date"] < key]
