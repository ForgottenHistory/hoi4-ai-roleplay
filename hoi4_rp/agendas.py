"""Agendas: what each country wants from the world, written by the LLM from the live game and stored in the campaign
(data["agendas"][tag]). The storyteller (later) surfaces incidents from them: an incident must serve an aim of the
country that acts, so countries keep their own direction and groups form where aims meet.

An agenda is one to four plain lines (no JSON), parsed leniently:
    AIM: contain Germany | TARGET: GER | KIND: oppose
    AIM: stay out of the great powers' quarrels | TARGET: none | KIND: neutral
Kinds: oppose, befriend, back (what commitments are, treaties.py), land (win territory from a neighbour), build
(strengthen itself), neutral (stay out). The last two have no target.

An aim is only kept if its target has a tie to the country in the running game (candidates(): a neighbour, an enemy,
a war goal either way, a faction or overlord tie, a strong opinion, a rival major power), so an aim can't name a country
the model remembers from history but the map has put far away. The prompt builds the aims from the live situation
and tells the model not to follow real history.

Covered: the major powers plus the countries in diplomacy.score()'s shortlist (MAX_COUNTRIES in all). Refreshed
every REFRESH_DAYS of game time, or after something that changes what the country is (war, peace, capitulation, a
new government, a faction or subject change, vanishing or appearing).

    python -m hoi4_rp agendas [TAG ...] [--prompt] [--save]   print agendas for these countries (default: the covered
                                                              ones); --save keeps them (stop `run` first)
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

from . import campaign, gamestate, world

REFRESH_DAYS = 180
MAX_AIMS = 4                 # at most one of them without a target (build, neutral)
NO_TARGET = ("build", "neutral")
MAX_TARGETS = 14             # candidate targets listed to the model
MAX_COUNTRIES = 20
AIM_WORDS = 12
PARALLEL = 4
# Events that change what a country is, so that its agenda written before them is stale
TRIGGERS = {"war", "peace", "capitulation", "government", "country_new", "country_gone", "faction_left",
            "faction_founded", "faction_joined", "independence", "subject", "crisis"}
KINDS = {"oppose": "work against TARGET: contain it, isolate it, prepare for a clash",
         "befriend": "seek TARGET's friendship",
         "back": "stand behind TARGET: protect and support it",
         "land": "win territory from TARGET, a neighbour of yours, by pressure or by force",
         "unite": "become one state with TARGET (annexed by it or merged by agreement): only if your government "
                  "truly wants it, for its own reasons",
         "build": "strengthen yourself: rearm, industrialise, steady the country (TARGET none)",
         "neutral": "keep out of other countries' quarrels (TARGET none)"}


# ---------------------------------------------------------------- who an aim may name

def candidates(me: gamestate.CountryState, snap: Dict, w: Optional[Dict[str, Dict]] = None
               ) -> Dict[str, List[str]]:
    """The countries `me` has a tie to in the running game, with the ties: {tag: [reasons]}, the best MAX_TARGETS:
    the most ties first, major powers and the same continent before a colonial border, then the greater industry."""
    w = w or {}
    c = snap.get("countries", {})
    out: Dict[str, List[str]] = {}

    def add(tag: str, why: str) -> None:
        if tag and tag != me.tag and tag in c and not c[tag].get("capitulated"):
            out.setdefault(tag, [])
            if why not in out[tag]:
                out[tag].append(why)

    for t in me.enemies:
        add(t, "at war with you")
    for g in me.wargoals:
        add(g.other, "you hold a war goal on it")
    for g in me.wargoals_against:
        add(g.other, "it holds a war goal on you")
    for t in me.neighbors:
        add(t, "borders you")
    for t in me.faction_members:
        add(t, "in your faction")
    add(me.overlord or "", "your overlord")
    for t in me.subjects:
        add(t, "your subject")
    for t, o in me.opinion.items():
        if abs(o) >= 40:
            add(t, f"opinion of it {o:+.0f}")
    if me.is_major:
        for t, x in c.items():
            if x.get("major"):
                add(t, "a major power like you")
    home = (w.get(me.tag) or {}).get("continent")
    near = lambda t: not home or (w.get(t) or {}).get("continent") == home
    ranked = sorted(out, key=lambda t: (-len(out[t]) - 2 * bool(c[t].get("major")) - near(t),
                                        -(w.get(t) or {}).get("civ", 0), t))
    return {t: out[t] for t in ranked[:MAX_TARGETS]}


def _target_line(t: str, why: List[str], c: Dict, name_of) -> str:
    x = c[t]
    bits = [f"{x['name']} ({t}): {'a major power' if x.get('major') else 'a minor power'}, "
            f"{x.get('ideology') or '?'} government"]
    if x.get("faction"):
        bits.append(f"in the faction {x['faction']}")
    if x.get("overlord"):
        bits.append(f"a subject of {name_of(x['overlord'])}")
    return "- " + "; ".join(bits) + " - " + ", ".join(why) + "."


# ---------------------------------------------------------------- the prompt and its reading

def prompt(me: gamestate.CountryState, cands: Dict[str, List[str]], snap: Dict, camp=None) -> List[Dict]:
    """The LLM messages for one country's agenda (`camp`: its recent crises are part of the situation)."""
    c = snap.get("countries", {})
    kinds = "\n".join(f"  {k}: {v}" for k, v in KINDS.items())
    listed = "\n".join(_target_line(t, why, c, me.name_of) for t, why in cands.items()) or "(none: no country ties you)"
    parts = [
        world.text({p.ideology for p in me.parties}),
        f"You are the government of {me.name} in Hearts of Iron IV. The date is {me.date.strip()}. Act as a shrewd "
        f"player of {me.name} would: its interests first, true to its leader and ideology.",
        "THE LIVE SITUATION OF " + me.name.upper() + ":\n"
        + gamestate.describe(me, max_states=10, details=False, spirit_details=False),
        "COUNTRIES THAT TIE YOU (the only ones an aim may name, by tag):\n" + listed,
    ]
    wt = world.tension_text({me.tag: me.name})
    if wt:
        parts.append(wt)
    if camp is not None:
        from . import crises, plots
        name_of = lambda t: c.get(t, {}).get("name", t)
        ctx = crises.context(camp, [me.tag] + list(cands), name_of)
        if ctx:
            parts.append(ctx)
        story = plots.context_for(camp, me.tag, name_of)
        if story:
            parts.append(story)
    parts.append(
        f"Decide {me.name}'s standing aims toward the world: between 1 and {MAX_AIMS}, what its government means to work "
        f"toward over the next year or two. Build each from the live situation above: borders, claims and war goals, "
        f"wars, ideology, factions, relative strength, opinions. At most one aim may be without a target (build, "
        f"neutral), and only if it is truly the country's main concern. This world may have diverged from real history, so "
        f"do not follow what history did: a country that ties you only through the list above is the only kind you "
        f"may name. Answer with exactly one line per aim and nothing else:\n"
        f"AIM: at most {AIM_WORDS} words | TARGET: a tag from the list, or none | KIND: one of "
        f"{', '.join(KINDS)}\nKinds:\n{kinds}")
    return [{"role": "system", "content": "\n\n".join(parts)}, {"role": "user", "content": "Write the aims now."}]


_AIM = re.compile(r"^[\W_]*AIM[*_\s]*[:\-–]+\s*(.+)$", re.I | re.M)


def parse(text: str, cands: Dict[str, List[str]]) -> Tuple[List[Dict], List[Tuple[str, str]]]:
    """(the aims kept, [(line, why dropped)]) from the model's lines. A line is dropped for an unknown kind, a target
    without a tie to the country, a land aim on a country that doesn't border it, or a repeat."""
    aims: List[Dict] = []
    dropped: List[Tuple[str, str]] = []
    for m in _AIM.finditer(text):
        line = m.group(1).strip()
        parts = [p.strip() for p in line.split("|")]
        aim = re.sub(r"\s+", " ", parts[0]).strip(" *_.")
        field = lambda key: next((re.sub(r"^[\W_]*" + key + r"[*_\s]*[:\-–]+\s*", "", p, flags=re.I).strip(" *_.()")
                                  for p in parts[1:] if re.match(r"^[\W_]*" + key + r"\b", p, re.I)), "")
        kind = next((k for k in KINDS if k in field("kind").lower()), "")
        tag = re.sub(r"[^A-Za-z0-9].*$", "", field("target")).upper()
        if not aim or not kind:
            dropped.append((line, "no aim or no known kind"))
            continue
        if kind in NO_TARGET:
            tag = ""
        elif tag not in cands:
            dropped.append((line, f"target {tag or '?'} has no tie to the country"))
            continue
        elif kind == "land" and "borders you" not in cands[tag]:
            dropped.append((line, f"{tag} does not border the country"))
            continue
        if kind in NO_TARGET and any(a["kind"] in NO_TARGET for a in aims):
            dropped.append((line, "only one aim without a target"))
            continue
        if any(a["target"] == tag and a["kind"] == kind for a in aims):
            dropped.append((line, "a repeat"))
            continue
        aims.append({"aim": " ".join(aim.split()[:AIM_WORDS * 2]), "target": tag, "kind": kind})
        if len(aims) == MAX_AIMS:
            break
    return aims, dropped


# ---------------------------------------------------------------- in the campaign

def _store(camp: campaign.Campaign) -> Dict[str, Dict]:
    return camp.data.setdefault("agendas", {})


def _name(camp: campaign.Campaign, tag: str) -> str:
    return ((camp.snapshot or {}).get("countries", {}).get(tag) or {}).get("name", "")


def _reborn(camp: campaign.Campaign, tag: str, since: str) -> bool:
    """Did `tag` vanish or appear as a country after `since`? The game reuses dynamic tags (a civil war's side,
    later another country), so what was written about the tag before is about someone else now."""
    return any(e["date"] > since and tag in e["tags"][:1] and e.get("type") in ("country_gone", "country_new")
               for e in camp.data["events"])


def of(camp: campaign.Campaign, tag: str) -> Optional[Dict]:
    """The stored agenda of a country: {"date", "when", "name", "aims": [{"aim", "target", "kind"}]}, or None
    (also when the tag has been another country since it was written)."""
    a = _store(camp).get(tag)
    now = _name(camp, tag)
    if a and (_reborn(camp, tag, a["date"]) or (a.get("name") and now and a["name"] != now)):
        return None
    return a


def aims_of(camp: campaign.Campaign, tag: str) -> List[Dict]:
    """The country's aims; one aimed at a tag that has been another country since it was written is dropped."""
    a = of(camp, tag)
    return [x for x in a["aims"] if not (x["target"] and _reborn(camp, x["target"], a["date"]))] if a else []


def keep(camp: campaign.Campaign, tag: str, aims: List[Dict]) -> None:
    key = (camp.snapshot or {}).get("date") or camp.data.get("last_date") or ""
    _store(camp)[tag] = {"date": key, "when": campaign.date_words(key), "name": _name(camp, tag),
                      "aims": aims}


def due(camp: campaign.Campaign, tag: str) -> bool:
    """No agenda yet, one older than REFRESH_DAYS, or something since that changes what the country is."""
    a = of(camp, tag)
    today = (camp.snapshot or {}).get("date", "")
    if not a:
        return True
    if today and campaign.shift(a["date"], REFRESH_DAYS) <= today:
        return True
    from . import plots
    if any(x["updated"] > a["date"] and tag in x["tags"] for x in plots._store(camp)["list"]):
        return True                                   # the player's story has moved with this country in it
    return any(e["date"] > a["date"] and tag in e["tags"] and e.get("type") in TRIGGERS
               for e in camp.data["events"])


def wants(camp: campaign.Campaign, tag: str, target: str, kind: str = "unite") -> bool:
    """Does `tag` have an aim of this kind toward `target`?"""
    return any(x["kind"] == kind and x["target"] == target for x in aims_of(camp, tag))


def covered(camp: campaign.Campaign, w: Dict[str, Dict], scored: List[Tuple]) -> List[str]:
    """The countries that have an agenda: the major powers, then those in the storyteller's shortlist."""
    c = (camp.snapshot or {}).get("countries", {})
    player = camp.data["player"]
    ok = lambda t: t in c and t != player and not c[t].get("capitulated") and not (w.get(t) or {}).get("subject")
    from . import plots
    story = [t for t in plots.tags(camp) if ok(t)]       # the countries of the player's story first
    majors = [t for t in w if c.get(t, {}).get("major") and ok(t)]
    shortlist = [t for x in scored[:12] for t in (x[1], x[2]) if ok(t)]
    return list(dict.fromkeys(story + majors + shortlist))[:MAX_COUNTRIES]


def describe(camp: campaign.Campaign, tag: str, name_of) -> str:
    """For prompts: the country's aims, one per line, or ''."""
    lines = []
    for a in aims_of(camp, tag):
        who = f" ({a['kind']} {name_of(a['target'])})" if a["target"] else f" ({a['kind']})"
        lines.append(f"- {a['aim']}{who}")
    return "\n".join(lines)


def drop_after(camp: campaign.Campaign, key: str) -> None:
    """An earlier save was loaded: agendas written after it never happened."""
    camp.data["agendas"] = {t: a for t, a in _store(camp).items() if a["date"] < key}


# ---------------------------------------------------------------- writing them

def generate(camp: campaign.Campaign, w: Dict[str, Dict], tags: List[str], show: bool = False,
             keep_them: bool = True) -> List[Dict]:
    """Write the agendas of these countries, PARALLEL calls at a time. Returns, per country, {"me", "cands", "aims",
    "dropped", "raw", "error"}; with keep_them the usable ones are stored in the campaign (not saved to disk)."""
    from concurrent.futures import ThreadPoolExecutor
    from . import llm
    snap = camp.snapshot or {}
    jobs, out = [], []
    with ThreadPoolExecutor(max_workers=PARALLEL) as pool:
        for tag in tags:
            try:
                me = gamestate.country(tag)
            except gamestate.StateError as e:
                out.append({"me": None, "tag": tag, "error": str(e)})
                continue
            cands = candidates(me, snap, w)
            msgs = prompt(me, cands, snap)
            if show:
                print(f"----- prompt for {me.name}\n{msgs[0]['content']}\n-----")
            jobs.append((me, cands, pool.submit(llm.chat, msgs, None, 0.7, 600, "ai:agenda", tag)))
        for me, cands, fut in jobs:
            r = {"me": me, "tag": me.tag, "cands": cands, "aims": [], "dropped": [], "raw": "", "error": ""}
            try:
                r["raw"] = fut.result()
            except llm.LLMError as e:
                r["error"] = str(e)
            else:
                r["aims"], r["dropped"] = parse(r["raw"], cands)
                if r["aims"] and keep_them:
                    keep(camp, me.tag, r["aims"])
            out.append(r)
    return out


def show_result(r: Dict, snap: Dict) -> None:
    """Print one country's result of generate()."""
    me = r["me"]
    print(f"\n== {me.name if me else r['tag']} ({r['tag']}) ==")
    if r["error"]:
        print(f"  failed: {r['error']}")
        return
    c = snap["countries"]
    for a in r["aims"]:
        to = (f" -> {c[a['target']]['name']} ({a['target']}: {', '.join(r['cands'][a['target']])})"
              if a["target"] else "")
        print(f"  {a['kind']:<8} {a['aim']}{to}")
    for line, why in r["dropped"]:
        print(f"  dropped: {line}  [{why}]")
    if not r["aims"]:
        print(f"  no usable aim; the reply was:\n{r['raw']}")


# ---------------------------------------------------------------- python -m hoi4_rp agendas

def main(args: List[str]) -> int:
    from . import chronicle, diplomacy
    save, show = "--save" in args, "--prompt" in args
    tags = [a.upper() for a in args if not a.startswith("--")]
    player = gamestate.player_tag()
    camp = campaign.Campaign(campaign.read_id(player), player)
    camp.snapshot = chronicle.snapshot(player)
    w = diplomacy.world()
    tags = tags or covered(camp, w, diplomacy.score(camp, w))
    print(f"{len(tags)} countries: {', '.join(tags)}")
    for r in generate(camp, w, tags, show, keep_them=save):
        show_result(r, camp.snapshot)
    if save:
        camp.save()
        print("\nsaved to the campaign")
    return 0
