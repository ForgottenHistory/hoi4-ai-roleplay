"""The storyteller: once a game month it picks at most one incident between AI countries, like a tabletop game master
steering the world. Countries keep their own direction (agendas.py); the storyteller only brings forward what a
country already wants, and paces it for the player.

One call a month. Its prompt: the world tension, the blocs (computed from the agendas), the best pairs from
diplomacy.score() with the agendas of both sides, the latest incidents and world events, and pacing facts computed in
Python (days since anything touched the player, incidents in the last 90 days, ultimatums open). The answer is
plain lines:
    INCIDENT: guarantee | commit | ultimatum | quiet
    FROM: GBR
    TO: POL
    AIM: contain Germany           copied from one of FROM's aims
    WHY: one sentence

check() then drops a pick that is not allowed, with no retry (the month passes quietly):
    both countries exist, are independent, not the player, not at war with each other, not on cooldown as a pair
    AIM matches an aim of FROM
    the kind is possible between them now (the same menu a dispatch uses, so the game's rules count):
        guarantee   FROM and TO can exchange a guarantee
        commit      FROM's aim is oppose / befriend / back toward a third country TO is aimed at the same way, and
                    that commitment can be made
        ultimatum   FROM's aim is land and its target is TO; FROM may threaten TO; world tension from
                    ULTIMATUM_TENSION; none open; one every ULTIMATUM_GAP_DAYS
    at most INCIDENTS_PER_MONTH a month

blocs(): countries whose aims (and commitments by letter) share a target: "against Germany: Britain, France, Poland".
Nobody schedules them; they appear because the agendas line up.

Stored in the campaign: data["storyteller"] = {"tick": date, "incidents": [{"date", "kind", "from", "to", "aim",
"why"}]}. Nothing here changes the game yet: `python -m hoi4_rp storyteller` runs a month (or --months N) dry.

    python -m hoi4_rp storyteller [--months N] [--prompt]    simulate N monthly picks, each remembering the earlier ones
"""

from __future__ import annotations

import datetime
import difflib
import re
import time
from typing import Dict, List, Optional, Tuple

from . import agendas, campaign, gamestate, world

KINDS = {"guarantee": "FROM and TO exchange a guarantee: one of them will defend the other",
         "commit": "FROM asks TO to join it in its AIM: to work against, befriend or stand behind the AIM's target "
                   "(a third country, not TO)",
         "ultimatum": "FROM sets TO a deadline to give up land, or face war: only for an AIM of the land kind whose "
                      "target is TO, and the gravest step there is"}
INCIDENTS_PER_MONTH = 1
ULTIMATUM_GAP_DAYS = 180
ULTIMATUM_TENSION = 0.10      # world tension below which nobody sets an ultimatum
PAIR_GAP_DAYS = 90           # the same two countries are not picked again within this
SHORTLIST = 12
RECENT = 6
MATCH = 0.6                  # how like one of FROM's aims the AIM line must be


# ---------------------------------------------------------------- the campaign's store

def _state(camp: campaign.Campaign) -> Dict:
    st = camp.data.setdefault("storyteller", {})
    for k, v in (("tick", ""), ("incidents", [])):
        st.setdefault(k, v)
    return st


def drop_after(camp: campaign.Campaign, key: str) -> None:
    """An earlier save was loaded: incidents after it never happened."""
    st = _state(camp)
    st["incidents"] = [x for x in st["incidents"] if x["date"] < key]
    st["tick"] = st["tick"] if st["tick"] < key else ""


def due(camp: campaign.Campaign) -> bool:
    """A game month since the last pick."""
    st, today = _state(camp), (camp.snapshot or {}).get("date", "")
    return bool(today) and (not st["tick"] or campaign.shift(st["tick"], 30) <= today)


def _days(a: str, b: str) -> int:
    """Whole days from date key a to date key b (0 if either is missing)."""
    try:
        x, y = (datetime.date(*(int(p) for p in k.split(".")[:3])) for k in (a, b))
    except (ValueError, TypeError):
        return 0
    return (y - x).days


# ---------------------------------------------------------------- blocs

def blocs(camp: campaign.Campaign) -> List[Dict]:
    """Groups of two or more countries aiming the same way at a target: [{"stance": "against" | "for", "target",
    "members": [tags]}], the biggest first. From the agendas, and from commitments made by letter."""
    from . import treaties
    groups: Dict[Tuple[str, str], List[str]] = {}

    def add(stance: str, target: str, *members: str) -> None:
        g = groups.setdefault((stance, target), [])
        g.extend(m for m in members if m and m != target and m not in g)

    for tag, a in agendas._store(camp).items():
        for aim in a["aims"]:
            if aim["kind"] in ("oppose", "land"):
                add("against", aim["target"], tag)
            elif aim["kind"] in ("befriend", "back"):
                add("for", aim["target"], tag)
    for r in treaties.commitments(camp):
        add("against" if r["kind"] == "oppose" else "for", r["target"], r["actor"], r["for"])
    out = [{"stance": s, "target": t, "members": m} for (s, t), m in groups.items() if len(m) >= 2]
    return sorted(out, key=lambda b: (-len(b["members"]), b["target"]))


def blocs_text(camp: campaign.Campaign, name) -> str:
    rows = blocs(camp)
    if not rows:
        return "No blocs have formed yet."
    return "\n".join(f"- {b['stance'].capitalize()} {name(b['target'])}: " + ", ".join(name(m) for m in b["members"])
                     for b in rows)


# ---------------------------------------------------------------- the prompt

def _aims_lines(camp: campaign.Campaign, tag: str) -> str:
    aims = agendas.aims_of(camp, tag)
    return "\n".join(f"  - {a['aim']} [{a['kind']}{' ' + a['target'] if a['target'] else ''}]" for a in aims) \
        or "  (no agenda)"


def pacing(camp: campaign.Campaign, now: str, sim: List[Dict]) -> Dict:
    """What the storyteller needs to pace by, from the chronicle and its own log."""
    from . import ultimatums
    player = camp.data["player"]
    touched = max((e["date"] for e in camp.data["events"] if player in e["tags"] and e["date"] <= now), default="")
    mine = [x for x in _state(camp)["incidents"] + sim if x["date"] <= now]
    recent = [x for x in mine if _days(x["date"], now) <= 90]
    return {"quiet_days": _days(touched, now) if touched else None,
            "incidents_90": len(recent), "ultimatums_90": sum(1 for x in recent if x["kind"] == "ultimatum"),
            "open_ultimatums": [u for u in ultimatums._store(camp) if u["status"] in ("open", "ready")],
            "mine": mine}


def shortlist_options(camp: campaign.Campaign, snap: Dict, scored: List[Tuple],
                      cache: Optional[Dict] = None) -> Tuple[List[Tuple], Dict]:
    """The best SHORTLIST pairs of diplomacy.score() that have something possible between them, and the incidents
    possible in each direction: ([score rows], {(from, to): [options]}). `cache` keeps options() across calls (each
    ordered pair costs about a second of game queries)."""
    player = camp.data["player"]
    cache = {} if cache is None else cache
    shortlist, opts = [], {}
    for row in [x for x in scored if player not in (x[1], x[2])][:SHORTLIST]:
        a, b = row[1], row[2]
        for x, y in ((a, b), (b, a)):
            if (x, y) not in cache:
                cache[(x, y)] = options(camp, x, y, snap)
            opts[(x, y)] = cache[(x, y)]
        if opts[(a, b)] or opts[(b, a)]:
            shortlist.append(row)
    return shortlist, opts


def messages(camp: campaign.Campaign, snap: Dict, w: Dict[str, Dict], shortlist: List[Tuple], opts: Dict, now: str,
             sim: List[Dict]) -> Optional[List[Dict]]:
    """The LLM messages for this month's pick, or None when nothing is possible between any of the pairs."""
    from . import diplomacy
    if not shortlist:
        return None
    c = snap.get("countries", {})
    name = lambda t: c.get(t, {}).get("name", t)
    player = camp.data["player"]
    me = gamestate.country(player)
    tags = list(dict.fromkeys(t for x in shortlist for t in (x[1], x[2])))
    pc = pacing(camp, now, sim)
    try:
        level = world.tension(tags[:1] or [player])[0]
    except Exception as e:
        print(f"  world tension could not be read: {e}")
        level = None
    parts = [
        world.text({p.ideology for p in me.parties}),
        "You are the storyteller of this Hearts of Iron IV world, in the way a game master steers a tabletop game or "
        "RimWorld's storyteller steers a colony: once a month you may bring forward ONE diplomatic incident between two "
        "AI countries that makes the next stretch of play interesting, or none. You do not invent what countries "
        "want: every incident serves an aim a country already has (its agenda below). Groups of countries form where "
        "their aims line up, as in a multiplayer game where everyone plays their own plan and alliances follow.",
        f"THE DATE: {campaign.date_words(now)}.",
        "THE PLAYER (a human; never an incident's FROM or TO, but what happens near them matters):\n"
        + diplomacy.card(me, w.get(player, {}), ""),
    ]
    if level is not None:
        parts.append(f"WORLD TENSION: {round(level * 100)}%. A quiet world makes alarm and war unlikely; Python rejects "
                     f"anything the game's rules forbid at this level.")
    parts.append("BLOCS (aims lining up, computed):\n" + blocs_text(camp, name))
    parts.append("AGENDAS (what each country wants):\n" + "\n".join(
        f"{name(t)} ({t}):\n{_aims_lines(camp, t)}" for t in tags))

    def possible(x: str, y: str) -> str:
        found = opts.get((x, y), [])
        return f"\n  {x} -> {y}: " + "; ".join(f"{o['kind']} [{o['aim']['aim']}]" for o in found) if found else ""

    parts.append("PAIRS, AND THE INCIDENTS POSSIBLE BETWEEN THEM NOW (the game's rules already checked; pick only "
                 "from these):\n" + "\n".join(
                     f"- {name(a)} ({a}) and {name(b)} ({b}): {', '.join(why)}{possible(a, b)}{possible(b, a)}"
                     for _, a, b, why, _ in shortlist))
    world_events = [e for e in camp.data["events"] if e.get("major") and e["date"] <= now][-8:]
    if world_events:
        parts.append("RECENT WORLD EVENTS:\n" + "\n".join(f"- {e['when']}: {e['text']}" for e in world_events))
    if pc["mine"]:
        parts.append("YOUR LATEST INCIDENTS (vary them: not the same countries or kind again):\n" + "\n".join(
            f"- {campaign.date_words(x['date'])}: {x['kind']} {name(x['from'])} -> {name(x['to'])} "
            f"({x['aim']})" for x in pc["mine"][-RECENT:]))
    quiet = ("nothing in the chronicle has touched the player's country yet" if pc["quiet_days"] is None
             else f"{pc['quiet_days']} days since anything in the chronicle involved the player's country")
    parts.append(f"PACING: {quiet}; {pc['incidents_90']} incidents of yours in the last 90 days "
                 f"({pc['ultimatums_90']} ultimatums); {len(pc['open_ultimatums'])} ultimatums standing in the world. "
                 f"After a quiet stretch for the player, bring something near their region (they see it as news and "
                 f"may be written to); after a busy one, choose quiet or something distant. Favour the player's "
                 f"corner of the world moderately, not exclusively. Aim for an incident roughly every other month: "
                 f"if you picked one in the last 45 days, choose quiet unless something truly calls for another. "
                 f"Choosing QUIET is right whenever nothing earns an incident.")
    kinds = "\n".join(f"  {k}: {v}" for k, v in KINDS.items())
    parts.append("Choose one of the possible incidents above, or quiet. Answer with exactly these lines and nothing "
                 "else:\n"
                 f"INCIDENT: one of {', '.join(KINDS)}, quiet\nFROM: tag of the country that acts\n"
                 "TO: tag of the other country (for quiet, none)\nAIM: the aim in the brackets of that incident, copied\n"
                 f"WHY: one sentence: why now, and why it fits both countries\nKinds:\n{kinds}")
    return [{"role": "system", "content": "\n\n".join(parts)}, {"role": "user", "content": "Choose now."}]


# ---------------------------------------------------------------- reading and checking a pick

_KEYLINE = r"^[\W_]*{key}[*_\s]*[:\-–]+\s*(.+)$"


def _line(text: str, key: str) -> str:
    m = re.search(_KEYLINE.format(key=key), text, re.I | re.M)
    return m.group(1).strip(" *_.") if m else ""


def parse(raw: str) -> Dict:
    """{"kind": one of KINDS or "quiet" or "", "from", "to", "aim", "why"} from the lines, leniently."""
    said = _line(raw, "incident").lower()
    tag = lambda key: re.sub(r"[^A-Za-z0-9].*$", "", _line(raw, key)).upper()
    kind = "quiet" if "quiet" in said else next((k for k in KINDS if k in said), "")
    return {"kind": kind, "from": tag("from"), "to": tag("to"), "aim": _line(raw, "aim"), "why": _line(raw, "why")}


def match_aim(camp: campaign.Campaign, tag: str, text: str) -> Optional[Dict]:
    """The aim of `tag` that `text` is (nearly) copied from, or None."""
    norm = lambda s: re.sub(r"[^a-z0-9 ]", "", s.lower()).strip()
    want = norm(text)
    best, top = None, 0.0
    for a in agendas.aims_of(camp, tag):
        have = norm(a["aim"])
        r = 1.0 if want and (want in have or have in want) else difflib.SequenceMatcher(None, have, want).ratio()
        if r > top:
            best, top = a, r
    return best if top >= MATCH else None


def _side(kind: str) -> str:
    return "against" if kind in ("oppose", "land") else "for"


def _tense(tag: str) -> bool:
    """Is the world tense enough for an ultimatum (ULTIMATUM_TENSION)? Not knowing counts as no."""
    try:
        return world.tension([tag])[0] >= ULTIMATUM_TENSION
    except Exception:
        return False


def options(camp: campaign.Campaign, a: str, b: str, snap: Dict) -> List[Dict]:
    """Every incident FROM `a` TO `b` that serves one of a's aims and that the game allows now: [{"kind", "aim"}].
        guarantee   the aim befriends or backs b, or b has an aim of the same side toward the same target
        commit      the aim opposes, befriends or backs a third country that b has an aim of the same side toward
                    too, and that commitment is possible
        ultimatum   a land aim whose target is b, a may threaten b, and the world is tense enough"""
    from . import actions, diplomacy
    c = snap.get("countries", {})
    aims = agendas.aims_of(camp, a)
    if not aims or a not in c or b not in c:
        return []
    rel = actions.relation(a, b)
    offers, asks, _ = diplomacy.menu(camp, a, b, c[a]["name"], c[b]["name"])
    shared = {(x["target"], _side(x["kind"])) for x in agendas.aims_of(camp, b) if x["target"]}
    can_guarantee = "give:guarantee" in offers or "ask:guarantee" in asks
    out = []
    for aim in aims:
        t, k = aim["target"], aim["kind"]
        if not t:
            continue
        if can_guarantee and ((t == b and k in ("befriend", "back")) or (t != b and (t, _side(k)) in shared)):
            out.append({"kind": "guarantee", "aim": aim})
        if (k in ("oppose", "befriend", "back") and t not in (a, b) and (t, _side(k)) in shared
                and (f"give:{k}:{t}" in offers or f"ask:{k}:{t}" in asks)):
            out.append({"kind": "commit", "aim": aim})
        if k == "land" and t == b and diplomacy.may_threaten(camp, a, b, rel) and _tense(a):
            out.append({"kind": "ultimatum", "aim": aim})
    return out


def check(camp: campaign.Campaign, pick: Dict, w: Dict[str, Dict], snap: Dict, now: str, sim: List[Dict],
          opts: Optional[Dict] = None) -> Tuple[bool, str, Optional[Dict]]:
    """(allowed, why not, the aim it serves). A quiet month is allowed and serves no aim. `opts`: options() already
    worked out, {(from, to): [...]}."""
    from . import diplomacy
    kind, a, b = pick["kind"], pick["from"], pick["to"]
    if kind == "quiet":
        return True, "", None
    if kind not in KINDS:
        return False, "no known incident kind", None
    c = snap.get("countries", {})
    player = camp.data["player"]
    for t in (a, b):
        if t not in c or t not in w:
            return False, f"{t or '?'} is not a country in the game", None
        if t == player:
            return False, "the player is not part of an incident", None
        if w[t]["subject"] or c[t].get("capitulated"):
            return False, f"{t} is a subject or beaten", None
    if a == b:
        return False, "a country cannot act on itself", None
    if campaign.pair(a, b) in snap.get("wars", []):
        return False, "they are at war", None
    pc = pacing(camp, now, sim)
    if sum(1 for x in pc["mine"] if _days(x["date"], now) < 30) >= INCIDENTS_PER_MONTH:
        return False, "an incident was already picked this month", None
    if any(campaign.pair(x["from"], x["to"]) == campaign.pair(a, b) and _days(x["date"], now) < PAIR_GAP_DAYS
           for x in pc["mine"]):
        return False, "these two had an incident lately", None
    aim = match_aim(camp, a, pick["aim"])
    if not aim:
        return False, f"AIM is not one of {a}'s aims", None
    have = (opts or {}).get((a, b))
    if have is None:
        have = options(camp, a, b, snap)
    if not any(o["kind"] == kind and o["aim"] == aim for o in have):
        return False, (f"{kind} from {a} to {b} for that aim is not possible now (the game's rules, or the aim "
                       f"does not concern them)"), aim
    if kind == "ultimatum" and (pc["open_ultimatums"] or any(x["kind"] == "ultimatum" and _days(x["date"], now)
                                                             < ULTIMATUM_GAP_DAYS for x in pc["mine"])):
        return False, "an ultimatum is standing or was set lately", aim
    return True, "", aim


# ---------------------------------------------------------------- python -m hoi4_rp storyteller

def main(args: List[str]) -> int:
    from . import chronicle, diplomacy, llm
    show = "--prompt" in args
    months = 1
    if "--months" in args and args.index("--months") + 1 < len(args):
        months = max(1, int(args[args.index("--months") + 1]))
    player = gamestate.player_tag()
    camp = campaign.Campaign(campaign.read_id(player), player)
    camp.snapshot = snap = chronicle.snapshot(player)
    w = diplomacy.world()
    scored = diplomacy.score(camp, w)
    missing = [t for t in agendas.covered(camp, w, scored) if not agendas.aims_of(camp, t)]
    if missing:
        print(f"writing the agendas of {len(missing)} countries without one: {', '.join(missing)}")
        agendas.generate(camp, w, missing)          # kept in memory for this run only
    name = lambda t: snap["countries"].get(t, {}).get("name", t)
    print("\nBLOCS\n" + blocs_text(camp, name))
    sim: List[Dict] = []
    started = time.monotonic()
    cache: Dict = {}
    shortlist, opts = shortlist_options(camp, snap, scored, cache)
    print(f"\n{len(shortlist)} pairs with something possible between them "
          f"({sum(len(v) for v in opts.values())} incidents; {time.monotonic() - started:.0f} s to work out)")
    for i in range(months):
        now = campaign.shift(snap["date"], 30 * i)
        head = f"\nmonth {i + 1} ({campaign.date_words(now)}): "
        msgs = messages(camp, snap, w, shortlist, opts, now, sim)
        if not msgs:
            print(head + "quiet (nothing is possible between the pairs; no call made)")
            continue
        if show and i == 0:
            print(f"\n----- prompt\n{msgs[0]['content']}\n-----")
        raw = llm.chat(msgs, None, 0.8, 400, "ai:storyteller", "")
        pick = parse(raw)
        ok, why, aim = check(camp, pick, w, snap, now, sim, opts)
        if not pick["kind"]:
            print(head + f"no readable pick; the reply was:\n{raw}")
        elif pick["kind"] == "quiet":
            print(head + "quiet" + (f" - {pick['why']}" if pick["why"] else ""))
        elif ok:
            print(head + f"{pick['kind']} {name(pick['from'])} -> {name(pick['to'])}   [{aim['aim']}]\n   {pick['why']}")
            sim.append({"date": now, "kind": pick["kind"], "from": pick["from"], "to": pick["to"], "aim": aim["aim"],
                        "why": pick["why"]})
            # as a real run would have it: a guarantee or commitment made is in force and no longer possible
            for key in ((pick["from"], pick["to"]), (pick["to"], pick["from"])):
                opts[key] = [o for o in opts.get(key, []) if o["kind"] != pick["kind"]
                             or (pick["kind"] == "commit" and o["aim"] != aim)]
            shortlist = [x for x in shortlist if opts.get((x[1], x[2])) or opts.get((x[2], x[1]))]
        else:
            print(head + f"DROPPED {pick['kind']} {pick['from']} -> {pick['to']}: {why}\n   {pick['why']}")
    return 0
