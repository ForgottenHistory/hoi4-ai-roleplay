"""Crises: things that happen between two countries or inside one, as flavour the AI countries then act upon.

A crisis has no mechanical effect: nothing in the game changes, least of all a government, a leader or a focus
tree. It is a fact in the chronicle (a clash on a border, a protest put down with force, a strike wave) that the
agendas, the storyteller's moves, the AI-to-AI letters and the news all see, so that what the countries decide
follows from what has happened to them. The player's country is never part of one.

    between   two neighbouring countries: a border clash, an insulted envoy, smuggled arms, a minority quarrel
    inside    one country: a protest put down, a strike wave, a famine, a mutiny, a scandal in the cabinet

Once a month or so (due) one LLM call may open a crisis, from the neighbouring pairs and the countries the
storyteller already looks at; most months it opens none. A crisis stays open for 2-6 months, then fades.

    due(camp)                    is it time to consider a crisis?
    messages(...) / parse(text)  the prompt, and its reading ("CRISIS: between | inside | none" and the lines below)
    check(...)                   is the pick allowed?
    open_crisis(camp, ...)       keep it, and put it in the chronicle (type "crisis", which the news reports)
    context(camp, tags, name)    prompt lines: the open crises of these countries, and those lately faded

Stored in the campaign: data["crises"] = {"tick": date of the last try, "list": [{"id", "date", "when", "scope",
"tags", "title", "text", "severity", "until", "status": "open" | "faded"}]}.
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

from . import agendas, campaign, world

GAP_DAYS = 28            # a try about this often (most come to nothing)
MAX_OPEN = 3
MIN_MONTHS, MAX_MONTHS = 2, 6
PAIRS = 10               # neighbouring pairs offered
RECENT_DAYS = 120        # a country with a crisis this lately is not offered for another
HISTORY_DAYS = 270       # a faded crisis is still remembered this long
SEVERITIES = ("minor", "serious", "grave")


def _store(camp: campaign.Campaign) -> Dict:
    st = camp.data.setdefault("crises", {})
    st.setdefault("tick", "")
    st.setdefault("list", [])
    return st


def drop_after(camp: campaign.Campaign, key: str) -> None:
    """An earlier save was loaded: crises after it never happened."""
    st = _store(camp)
    st["list"] = [x for x in st["list"] if x["date"] < key]
    for x in st["list"]:
        if x["status"] == "faded" and x["until"] >= key:
            x["status"] = "open"
    st["tick"] = st["tick"] if st["tick"] < key else ""


def fade(camp: campaign.Campaign, now: str) -> None:
    """Close the crises whose time has run out, or whose country is no longer the one they were about."""
    for x in _store(camp)["list"]:
        if x["status"] == "open" and (x["until"] <= now or any(agendas._reborn(camp, t, x["date"]) for t in x["tags"])):
            x["status"] = "faded"


def _open(camp: campaign.Campaign) -> List[Dict]:
    return [x for x in _store(camp)["list"] if x["status"] == "open"]


def due(camp: campaign.Campaign) -> bool:
    """Time for a try: none yet, or GAP_DAYS since the last, and fewer than MAX_OPEN open."""
    st, today = _store(camp), (camp.snapshot or {}).get("date", "")
    if not today:
        return False
    fade(camp, today)
    return len(_open(camp)) < MAX_OPEN and (not st["tick"] or campaign.shift(st["tick"], GAP_DAYS) <= today)


def context(camp: campaign.Campaign, tags: List[str], name, now: str = "") -> str:
    """Prompt text: the crises that concern any of these countries, open ones and those faded lately, or ''."""
    now = now or (camp.snapshot or {}).get("date", "") or camp.data.get("last_date") or ""
    since = campaign.shift(now, -HISTORY_DAYS) if now else ""
    found = [x for x in _store(camp)["list"] if set(x["tags"]) & set(tags)
             and (x["status"] == "open" or x["until"] >= since)]
    if not found:
        return ""
    lines = []
    for x in found[-6:]:
        who = " and ".join(name(t) for t in x["tags"])
        lines.append(f"- {x['when']}, {x['severity']}, {'ongoing' if x['status'] == 'open' else 'past'} ({who}): "
                     f"{x['text']}")
    return ("RECENT CRISES (events that happened to these countries; a government takes them into account, "
            "but nothing forces its hand):\n" + "\n".join(lines))


# ---------------------------------------------------------------- the pick

def candidates(camp: campaign.Campaign, snap: Dict, w: Dict[str, Dict], scored: List[Tuple]
               ) -> Tuple[List[Tuple[str, str]], List[str]]:
    """([neighbouring pairs], [countries]) a crisis may be about: from the scored pairs, never the player, no
    subject or beaten country."""
    c = snap.get("countries", {})
    player = camp.data["player"]
    ok = lambda t: t in c and t != player and not c[t].get("capitulated") and not (w.get(t) or {}).get("subject")
    now = snap.get("date", "")
    # in a crisis now, or lately (the same few countries do not take every turn)
    busy = {t for x in _store(camp)["list"] if x["status"] == "open" or x["date"] >= campaign.shift(now, -RECENT_DAYS)
            for t in x["tags"]}
    wars = set(snap.get("wars", []))
    pairs, tags = [], []
    for x in scored:
        a, b = x[1], x[2]
        if not (ok(a) and ok(b)):
            continue
        for t in (a, b):
            if t not in tags and t not in busy:
                tags.append(t)
        if (b in (w.get(a) or {}).get("neighbours", ()) and not {a, b} & busy and len(pairs) < PAIRS
                and campaign.pair(a, b) not in wars):
            pairs.append((a, b))
    return pairs, tags[:14]


def messages(camp: campaign.Campaign, snap: Dict, w: Dict[str, Dict], pairs: List[Tuple[str, str]],
             tags: List[str], now: str) -> Optional[List[Dict]]:
    """The LLM messages for a try, or None when there is nobody a crisis could be about."""
    if not tags:
        return None
    c = snap.get("countries", {})
    name = lambda t: c.get(t, {}).get("name", t)
    wars = snap.get("wars", [])
    lines = []
    for t in tags:
        x = c[t]
        bits = [f"{name(t)} ({t}): {'major' if x.get('major') else 'minor'} power, {x.get('ideology') or '?'} "
                f"government"]
        if x.get("faction"):
            bits.append(f"faction {x['faction']}")
        at_war = [name(o) for p in wars if t in p.split("|") for o in p.split("|") if o != t]
        if at_war:
            bits.append("at war with " + ", ".join(at_war))
        aims = agendas.describe(camp, t, name)
        lines.append("; ".join(bits) + ("\n" + "\n".join("    " + a for a in aims.splitlines()) if aims else ""))
    parts = [
        world.text({c[t].get("ideology") for t in tags if c[t].get("ideology")}),
        "You are the storyteller of this Hearts of Iron IV world. Besides the diplomatic moves the countries make, "
        "things HAPPEN to them: a clash on a border, an insulted envoy, a protest put down with force, a strike wave, "
        "a mutiny, a famine. A crisis is flavour: nothing changes in the game, and no government, leader or "
        "ideology is ever changed by it. It is written into the record, and the countries' governments take it into "
        "account in what they decide next. You may open ONE crisis now, or none.",
        f"THE DATE: {campaign.date_words(now)}.",
        "COUNTRIES (a crisis may be about any of these, never anyone else):\n" + "\n".join(lines),
    ]
    if pairs:
        parts.append("NEIGHBOURING PAIRS (the only pairs a 'between' crisis may be about):\n" + "\n".join(
            f"- {name(a)} ({a}) and {name(b)} ({b})" for a, b in pairs))
    ctx = context(camp, tags, name, now)
    if ctx:
        parts.append(ctx)
    world_events = [e for e in camp.data["events"] if e.get("major") and e["date"] <= now][-6:]
    if world_events:
        parts.append("RECENT WORLD EVENTS:\n" + "\n".join(f"- {e['when']}: {e['text'][:300]}" for e in world_events))
    parts.append(
        "RULES: a crisis needs a cause in the country's situation (its ideology, a border, an aim above), not a "
        "random shock. This world is its own: the RECENT WORLD EVENTS and the countries above are the truth, so do not "
        "re-enact what happened in real history on its real date, and never contradict them. Leave to the countries "
        "(and the game) everything they decide or fight: no coups, no change of government, no leader's death, no war "
        "begun, no annexation or occupation or remilitarisation, no battle or bombing of a war already under way. A "
        "crisis is the trouble that leads to such things or follows them: a clash, an incident, unrest, a quarrel, "
        "a shortage. 'inside' is about one country alone; anything done by another country is 'between'. Choose none unless a crisis would truly make the world richer: most months none.\n"
        "Answer with exactly these lines and nothing else:\n"
        "CRISIS: between, inside, or none\nTAGS: one tag (inside) or two tags separated by a comma (between)\n"
        "TITLE: at most 6 words\nTEXT: one or two sentences of fact, past tense, naming places and what was done\n"
        f"SEVERITY: one of {', '.join(SEVERITIES)}\nMONTHS: how long it stays in the news and in mind, "
        f"{MIN_MONTHS} to {MAX_MONTHS}")
    return [{"role": "system", "content": "\n\n".join(parts)}, {"role": "user", "content": "Decide now."}]


_KEYLINE = r"^[\W_]*{key}[*_\s]*[:\-–]+\s*(.+)$"


def _line(text: str, key: str) -> str:
    m = re.search(_KEYLINE.format(key=key), text, re.I | re.M)
    return m.group(1).strip(" *_") if m else ""


def parse(raw: str) -> Dict:
    """{"scope": "between" | "inside" | "none" | "", "tags", "title", "text", "severity", "months"}, leniently."""
    said = _line(raw, "crisis").lower()
    scope = "none" if "none" in said else next((s for s in ("between", "inside") if s in said), "")
    tags = [t for t in (re.sub(r"[^A-Za-z0-9]", "", x).upper() for x in re.split(r"[,/&\s]+", _line(raw, "tags"))) if t]
    sev = next((s for s in SEVERITIES if s in _line(raw, "severity").lower()), "minor")
    m = re.search(r"\d+", _line(raw, "months"))
    months = min(MAX_MONTHS, max(MIN_MONTHS, int(m.group()))) if m else MIN_MONTHS
    return {"scope": scope, "tags": tags, "tags_line": _line(raw, "tags"), "title": _line(raw, "title").strip(". "), "text": _line(raw, "text"),
            "severity": sev, "months": months}


def _resolve(line: str, scope: str, tags: List[str], names: Dict[str, str]) -> List[str]:
    """The tags a TAGS line means when it gave names instead ("Czechoslovak Republic"): the known countries in it,
    in the order they appear."""
    low = line.lower()
    found = []
    for t in tags:
        pats = (re.search(r"" + re.escape(x.lower()) + r"", low) for x in (t, names.get(t, "")) if x)
        spots = [m.start() for m in pats if m]
        if spots:
            found.append((min(spots), t))
    return [t for _, t in sorted(found)][:1 if scope == "inside" else 2]


def check(camp: campaign.Campaign, pick: Dict, pairs: List[Tuple[str, str]], tags: List[str],
          names: Optional[Dict[str, str]] = None) -> Tuple[bool, str]:
    """(allowed, why not). `names` {tag: name}: a TAGS line of names instead of tags is read too."""
    scope, t = pick["scope"], pick["tags"]
    if scope in ("inside", "between") and names and not (
            len(t) == (1 if scope == "inside" else 2) and all(x in tags for x in t)):
        t = pick["tags"] = _resolve(pick.get("tags_line", ""), scope, tags, names) or t
    if scope in ("none", ""):
        return False, "none" if scope else "no readable pick"
    if not pick["text"] or not pick["title"]:
        return False, "no title or text"
    if len(_open(camp)) >= MAX_OPEN:
        return False, "enough crises are open"
    if scope == "inside":
        if len(t) != 1 or t[0] not in tags:
            return False, f"{', '.join(t) or '?'} is not a country a crisis may be about"
    else:
        if len(t) != 2 or not any({t[0], t[1]} == {a, b} for a, b in pairs):
            return False, f"{', '.join(t) or '?'} is not one of the neighbouring pairs offered"
    if any(set(x["tags"]) & set(t) for x in _open(camp)):
        return False, "one of them is in an open crisis already"
    return True, ""


def open_crisis(camp: campaign.Campaign, pick: Dict, now: str) -> Dict:
    """Keep the crisis and put it in the chronicle, as an event the news reports (major when a major power is in it)."""
    st = _store(camp)
    c = (camp.snapshot or {}).get("countries", {})
    crisis = {"id": len(st["list"]) + 1, "date": now, "when": campaign.date_words(now), "scope": pick["scope"],
              "tags": pick["tags"], "title": pick["title"], "text": pick["text"], "severity": pick["severity"],
              "until": campaign.shift(now, 30 * pick["months"]), "status": "open"}
    st["list"].append(crisis)
    camp.add_events([{"date": now, "when": crisis["when"], "type": "crisis", "tags": list(pick["tags"]),
                      "text": pick["text"], "major": any(c.get(t, {}).get("major") for t in pick["tags"])}])
    return crisis
