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

With [ai_diplomacy] guarantees = ours a second call follows each month: the month's guarantee (GuaranteeMonth), kept
apart from the incident above (its incidents carry "slot": "guarantee" and don't count toward INCIDENTS_PER_MONTH).
With factions = ours a last call may make one faction move (FactionMonth: join, found or break; "slot": "faction"),
and the incident above offers no alliances then. With wars = ours the last call weighs the would-be wars
(WarMonth: war, ultimatum, prepare or wait for each; wars.py carries them out).

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

from . import agendas, campaign, course, gamestate, world

KINDS = {"guarantee": "FROM and TO exchange a guarantee: one of them will defend the other",
         "commit": "FROM asks TO to join it in its AIM: to work against, befriend or stand behind the AIM's target "
                   "(a third country, not TO)",
         "access": "FROM and TO open their lands to each other's armies (military access): FROM backs or befriends TO",
         "alliance": "FROM and TO ally in a faction (one founds it or joins the other's) against the country FROM "
                     "opposes, which TO opposes too",
         "ultimatum": "FROM sets TO a deadline to give up land, or face war: only for an AIM of the land kind whose "
                      "target is TO, and the gravest step there is",
         "reach_out": "FROM writes to the player (TO is the player's tag) about its AIM, which concerns the player's "
                      "country: for when the player's corner has been quiet"}
INCIDENTS_PER_MONTH = 1
REACH_QUIET_DAYS = 45        # a country that wrote to the player within this many days does not reach out
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
    """Three weeks since the last try began (a try itself takes a while, so tries come about a month apart)."""
    st, today = _state(camp), (camp.snapshot or {}).get("date", "")
    return bool(today) and (not st["tick"] or campaign.shift(st["tick"], 21) <= today)


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

    for tag in list(agendas._store(camp)):
        for aim in agendas.aims_of(camp, tag):
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


def pair_rows(camp: campaign.Campaign, scored: List[Tuple], now: str) -> List[Tuple]:
    """The scored pairs without the player, those whose countries had an incident lately last, so that the same
    few countries do not crowd the month's shortlist (a stable sort: the score order is kept otherwise)."""
    player = camp.data["player"]
    seen: Dict[str, int] = {}
    for x in _state(camp)["incidents"]:
        if 0 <= _days(x["date"], now) <= 180:
            for t in (x["from"], x["to"]):
                seen[t] = seen.get(t, 0) + 1
    rows = [x for x in scored if player not in (x[1], x[2])]
    return sorted(rows, key=lambda x: seen.get(x[1], 0) + seen.get(x[2], 0))


def reach_options(camp: campaign.Campaign, snap: Dict, w: Dict[str, Dict]) -> Dict:
    """{(country, player): [reach_out incident]}: countries with an aim that concerns the player and a standing
    policy toward them, that have not written to them lately and are not already writing urgently."""
    player = camp.data["player"]
    today = snap.get("date", "")
    c = snap.get("countries", {})
    out = {}
    for tag, p in camp.policies_toward().items():
        if tag == player or tag not in c or tag not in w or w[tag]["subject"] or c[tag].get("capitulated"):
            continue
        if p.get("urgency", 0) >= 3 or p.get("stance") == "indifferent":
            continue
        last = max((x["date"] for x in camp.letters_with(tag) if x["from"] == tag), default="")
        if last and _days(last, today) < REACH_QUIET_DAYS:
            continue
        aim = next((a for a in agendas.aims_of(camp, tag) if a["target"] == player), None)
        if aim:
            out[(tag, player)] = [{"kind": "reach_out", "aim": aim}]
    return out


def shortlist_options(camp: campaign.Campaign, snap: Dict, scored: List[Tuple],
                      cache: Optional[Dict] = None) -> Tuple[List[Tuple], Dict]:
    """The best SHORTLIST pairs of diplomacy.score() that have something possible between them, and the incidents
    possible in each direction: ([score rows], {(from, to): [options]}). `cache` keeps options() across calls (each
    ordered pair costs about a second of game queries)."""
    player = camp.data["player"]
    cache = {} if cache is None else cache
    shortlist, opts = [], {}
    now = snap.get("date", "")
    for row in pair_rows(camp, scored, now)[:SHORTLIST]:
        a, b = row[1], row[2]
        if any(campaign.pair(x["from"], x["to"]) == campaign.pair(a, b) and _days(x["date"], now) < PAIR_GAP_DAYS
               for x in _state(camp)["incidents"]):
            continue                                  # check() would turn it down: don't offer it
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
    player = camp.data["player"]
    reach = {k: v for k, v in opts.items() if k[1] == player and v}
    if not shortlist and not reach:
        return None
    c = snap.get("countries", {})
    name = lambda t: c.get(t, {}).get("name", t)
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
    if reach:
        parts.append(f"COUNTRIES THAT MAY WRITE TO THE PLAYER (INCIDENT reach_out; TO is {player}):\n" + "\n".join(
            f"- {name(t)} ({t}) [{o[0]['aim']['aim']}]" for (t, _), o in reach.items()))
    if tags:
        course_line = lambda t: f"\n  course: {way}" if (way := course.short(t)) else ""
        parts.append("AGENDAS (what each country wants, and the course its national focuses take):\n" + "\n".join(
            f"{name(t)} ({t}):\n{_aims_lines(camp, t)}{course_line(t)}" for t in tags))

    def possible(x: str, y: str) -> str:
        found = opts.get((x, y), [])
        return f"\n  {x} -> {y}: " + "; ".join(f"{o['kind']} [{o['aim']['aim']}]" for o in found) if found else ""

    if shortlist:
        parts.append("PAIRS, AND THE INCIDENTS POSSIBLE BETWEEN THEM NOW (the game's rules already checked; pick only "
                     "from these):\n" + "\n".join(
                         f"- {name(a)} ({a}) and {name(b)} ({b}): {', '.join(why)}{possible(a, b)}{possible(b, a)}"
                         for _, a, b, why, _ in shortlist))
    from . import crises, plots
    ctx = crises.context(camp, tags, name, now)
    if ctx:
        parts.append(ctx)
    story = plots.context(camp, name)
    if story:
        parts.append(story)
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
                 f"After a quiet stretch for the player, a reach_out (if one is listed) or something near their region "
                 f"brings them news; after a busy one, choose quiet or something distant. Most incidents should be "
                 f"between other countries: favour the player's corner only moderately. Aim for an incident most months: "
                 f"if you picked one in the last 25 days, choose quiet unless something truly calls for another. "
                 f"Choosing QUIET is right whenever nothing earns an incident.")
    kinds = "\n".join(f"  {k}: {v}" for k, v in KINDS.items())
    parts.append("Choose one of the possible incidents above, or quiet. Answer with exactly these lines and nothing "
                 "else:\n"
                 f"INCIDENT: one of {', '.join(KINDS)}, quiet\nFROM: tag of the country that acts\n"
                 "TO: tag of the other country (for quiet, none)\nAIM: the aim in the brackets of that incident, copied\n"
                 f"WHY: one sentence, as a historian would put it: what moves FROM to act toward TO now, from the two "
                 f"countries' own interests. It is shown in the game and given to FROM's diplomats, so it is never "
                 f"about the player or their country (unless TO is the player), nor about pacing, news reaching "
                 f"anyone or your reasons as storyteller\nKinds:\n{kinds}")
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


_WORLD: Dict = {"date": None, "w": {}}


def _guarantee_reach(camp: campaign.Campaign, a: str, b: str, snap: Dict, friend: bool) -> bool:
    """Is a guarantee between a and b, either way, within reach (guarantees.reach_ok; an aim is at stake here)?
    From across the world only when the aim is to back or befriend the other country itself (`friend`), not to
    oppose a common foe. The world is read once a game day."""
    from . import diplomacy, guarantees
    if _WORLD["date"] != snap.get("date"):
        _WORLD.update(date=snap.get("date"), w=diplomacy.world())
    w = _WORLD["w"]
    return any(t and (friend or t != "far")
               for t in (guarantees.reach_ok(camp, a, b, w, True), guarantees.reach_ok(camp, b, a, w, True)))


def _guarantee_slot(x: Dict) -> bool:
    """An incident of the month's own guarantee or faction pass, apart from the month's incident."""
    return bool(x.get("slot"))


def options(camp: campaign.Campaign, a: str, b: str, snap: Dict) -> List[Dict]:
    """Every incident FROM `a` TO `b` that serves one of a's aims and that the game allows now: [{"kind", "aim"}].
        guarantee   the aim befriends or backs b, or opposes a third country that b opposes too
        access      the aim befriends or backs b, and military access is possible
        alliance    the aim opposes a third country that b opposes too, and a faction is possible
        commit      the aim opposes, befriends or backs a third country that b has an aim of the same side toward
                    too, and that commitment is possible
        ultimatum   a land aim whose target is b, a may threaten b, and the world is tense enough"""
    from . import actions, diplomacy, gates
    c = snap.get("countries", {})
    aims = agendas.aims_of(camp, a)
    own_factions = gates.ours("factions")              # then the month's faction pass handles them (FactionMonth)
    if not aims or a not in c or b not in c:
        return []
    rel = actions.relation(a, b)
    offers, asks, _ = diplomacy.menu(camp, a, b, c[a]["name"], c[b]["name"])
    shared = {(x["target"], _side(x["kind"])) for x in agendas.aims_of(camp, b) if x["target"]}
    can_guarantee = "give:guarantee" in offers or "ask:guarantee" in asks
    can_access = "give:access" in offers or "ask:access" in asks
    can_ally = any(k in offers + asks for k in actions.FACTION_KEYS)
    out = []
    for aim in aims:
        t, k = aim["target"], aim["kind"]
        if not t:
            continue
        friend = t == b and k in ("befriend", "back")
        common_foe = t != b and k == "oppose" and (t, "against") in shared
        if can_guarantee and (friend or common_foe) and _guarantee_reach(camp, a, b, snap, friend):
            out.append({"kind": "guarantee", "aim": aim})
        if can_access and friend:
            out.append({"kind": "access", "aim": aim})
        if can_ally and common_foe and not own_factions:
            out.append({"kind": "alliance", "aim": aim})
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
    if kind == "reach_out" and b != player:
        return False, "a reach_out is to the player", None
    for t in (a, b):
        if t not in c or t not in w:
            return False, f"{t or '?'} is not a country in the game", None
        if t == player and not (kind == "reach_out" and t == b):
            return False, "the player is not part of an incident", None
        if w[t]["subject"] or c[t].get("capitulated"):
            return False, f"{t} is a subject or beaten", None
    if a == b:
        return False, "a country cannot act on itself", None
    if campaign.pair(a, b) in snap.get("wars", []):
        return False, "they are at war", None
    pc = pacing(camp, now, sim)
    # due() already spaces the jobs a month apart; a slow job's incident is dated when it finished, so allow for that
    if sum(1 for x in pc["mine"] if _days(x["date"], now) < 20 and not _guarantee_slot(x)) >= INCIDENTS_PER_MONTH:
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


# ---------------------------------------------------------------- a month in the running game

class Month:
    """This month's pick, worked out in slices so that the main loop stays responsive. The game queries it needs
    (about a second each: the agendas' countries, then every ordered pair's options) are spread over many calls of
    step(); the LLM calls (agendas, the pick) run in `pool` and are only looked at. step() returns the result when done:
    {"pick", "ok", "why", "aim", "raw", "error", "pairs"} (pick None when nothing was possible, so no call was made)."""

    def __init__(self, camp: campaign.Campaign, snap: Dict, pool) -> None:
        self.camp, self.snap, self.pool = camp, snap, pool
        self.now = snap.get("date", "")
        self.phase = "world"
        self.w: Dict[str, Dict] = {}
        self.scored: List[Tuple] = []
        self.todo: List = []            # country tags to write agendas for, then ordered pairs to work out
        self.jobs: List[tuple] = []     # agenda calls running: (country, candidates, future)
        self.cache: Dict = {}
        self.opts: Dict = {}
        self.shortlist: List[Tuple] = []
        self.fut = None
        self.wfut = None                # the war decisions' call: it runs beside the incident, guarantee and faction
        self.war: Dict = {}
        self.main: Dict = {}
        self.gm: Optional[GuaranteeMonth] = None
        self.fm: Optional[FactionMonth] = None
        self.wm: Optional[WarMonth] = None

    def _guarantees(self) -> bool:
        """After the month's incident: with guarantees = ours, go on to the month's guarantee (True); else to the
        factions."""
        from . import gates
        if not gates.ours("guarantees"):
            return self._factions()
        self.gm = GuaranteeMonth(self.camp, self.snap, self.w)
        self.phase = "g_rules"
        return True

    def _factions(self) -> bool:
        """With factions = ours, go on to the month's faction move (True); else to the end."""
        from . import gates
        if not gates.ours("factions"):
            return self._end()
        self.fm = FactionMonth(self.camp, self.snap, self.w)
        self.phase = "f_rules"
        return True

    def _end(self) -> bool:
        """Last: wait for the war decisions (True), whose call ran beside the others; False when there are none."""
        if not self.wm:
            return False
        self.phase = "w_wait"
        return True

    def _write_agenda(self, tag: str) -> None:
        from . import llm
        try:
            me = gamestate.country(tag)
        except gamestate.StateError as e:
            print(f"  [storyteller] no agenda for {tag}: {e}")
            return
        cands = agendas.candidates(me, self.snap, self.w)
        self.jobs.append((me, cands, self.pool.submit(llm.chat, agendas.prompt(me, cands, self.snap, self.camp), None, 0.7, 1500,
                                                      "ai:agenda", tag)))

    def _agendas_done(self) -> bool:
        from . import llm
        for job in list(self.jobs):
            me, cands, fut = job
            if not fut.done():
                continue
            self.jobs.remove(job)
            try:
                aims, _ = agendas.parse(fut.result(), cands)
            except llm.LLMError as e:
                print(f"  [storyteller] agenda of {me.tag} failed: {e}")
                continue
            if aims:
                agendas.keep(self.camp, me.tag, aims)
        return not self.jobs

    def step(self, budget: float = 2.0) -> Optional[Dict]:
        """Work for about `budget` seconds. The result when the month's pick is made, else None."""
        from . import diplomacy, gates, llm
        started = time.monotonic()
        player = self.camp.data["player"]
        while True:
            if self.phase == "world":
                self.w = diplomacy.world()
                self.scored = diplomacy.score(self.camp, self.w)
                self.todo = [t for t in agendas.covered(self.camp, self.w, self.scored) if agendas.due(self.camp, t)]
                self.phase = "agendas"
            elif self.phase == "w_rules":
                if self.wm.todo:
                    self.wm.rule_step()
                else:
                    msgs = self.wm.messages()
                    if msgs:
                        self.wfut = self.pool.submit(llm.chat, msgs, None, 0.7, 900, "ai:storyteller", "wars")
                    else:
                        self.war = {"error": "", "raw": "", "picks": [], "note": "no war to weigh; no call made"}
                    self.phase = "options"              # on to the incident's call

            elif self.phase == "agendas":
                if self.todo:
                    self._write_agenda(self.todo.pop(0))
                elif not self._agendas_done():
                    return None                     # agenda calls still running
                else:
                    rows = pair_rows(self.camp, self.scored, self.now)[:SHORTLIST]
                    self.todo = [(x[1], x[2]) for x in rows] + [(x[2], x[1]) for x in rows]
                    self.phase = "options"
            elif self.phase == "options":
                if self.todo:
                    x, y = self.todo.pop(0)
                    self.cache[(x, y)] = options(self.camp, x, y, self.snap)
                elif gates.ours("wars") and not self.wm:
                    # the war decisions are worked out now, from the latest snapshot (the agendas can take a while
                    # of game time), and their call runs beside the incident's, guarantee's and faction's
                    self.wm = WarMonth(self.camp, self.camp.snapshot or self.snap, self.w)
                    self.phase = "w_rules"
                else:
                    self.shortlist, self.opts = shortlist_options(self.camp, self.snap, self.scored, self.cache)
                    self.opts.update(reach_options(self.camp, self.snap, self.w))
                    today = max(self.now, self.camp.data.get("last_date") or "")
                    picked = any(_days(x["date"], today) < 20 and not _guarantee_slot(x)
                                 for x in _state(self.camp)["incidents"])
                    msgs = None if picked else messages(self.camp, self.snap, self.w, self.shortlist, self.opts,
                                                        self.now, [])
                    if picked:
                        self.main = {"pick": None, "ok": False, "why": "", "aim": None, "raw": "", "error": "",
                                     "pairs": 0, "note": "quiet (an incident was already picked this month; no call made)"}
                    elif not msgs:
                        self.main = {"pick": None, "ok": False, "why": "", "aim": None, "raw": "", "error": "",
                                     "pairs": 0}
                    else:
                        self.fut = self.pool.submit(llm.chat, msgs, None, 0.8, 1200, "ai:storyteller", "")
                        self.phase = "wait"
                        continue
                    if not self._guarantees():
                        return self.main
            elif self.phase == "wait":
                if not self.fut.done():
                    return None
                res = {"pick": None, "ok": False, "why": "", "aim": None, "raw": "", "error": "",
                       "pairs": len(self.shortlist)}
                try:
                    res["raw"] = self.fut.result()
                except llm.LLMError as e:
                    res["error"] = str(e)
                    self.main = res
                    if not self._end():
                        return res
                    continue
                res["pick"] = parse(res["raw"])
                # pacing is measured to the day the pick lands: the game ran on while the month was worked out
                res["ok"], res["why"], res["aim"] = check(self.camp, res["pick"], self.w, self.snap,
                                                          max(self.now, self.camp.data.get("last_date") or ""), [],
                                                          self.opts)
                self.main = res
                if not self._guarantees():
                    return res
            elif self.phase == "g_rules":
                if self.gm.todo:
                    self.gm.rule_step()
                else:
                    msgs = self.gm.messages()
                    if not msgs:
                        self.main["guarantee"] = {"error": "", "raw": "", "pick": None,
                                                  "note": "nothing to guarantee or renew; no call made"}
                        if not self._factions():
                            return self.main
                        continue
                    self.fut = self.pool.submit(llm.chat, msgs, None, 0.8, 800, "ai:storyteller", "guarantees")
                    self.phase = "g_wait"
            elif self.phase == "g_wait":
                if not self.fut.done():
                    return None
                try:
                    self.main["guarantee"] = self.gm.read(self.fut.result())
                except llm.LLMError as e:
                    self.main["guarantee"] = {"error": str(e), "raw": "", "pick": None}
                if not self._factions():
                    return self.main
            elif self.phase == "f_rules":
                if self.fm.todo:
                    self.fm.rule_step()
                else:
                    msgs = self.fm.messages()
                    if not msgs:
                        self.main["faction"] = {"error": "", "raw": "", "pick": None,
                                                "note": "no faction move possible; no call made"}
                        if not self._end():
                            return self.main
                        continue
                    self.fut = self.pool.submit(llm.chat, msgs, None, 0.8, 800, "ai:storyteller", "factions")
                    self.phase = "f_wait"
            elif self.phase == "f_wait":
                if not self.fut.done():
                    return None
                try:
                    self.main["faction"] = self.fm.read(self.fut.result())
                except llm.LLMError as e:
                    self.main["faction"] = {"error": str(e), "raw": "", "pick": None}
                if not self._end():
                    return self.main
            elif self.phase == "w_wait":
                if self.wfut and not self.wfut.done():
                    return None
                if self.wfut:
                    try:
                        self.war = self.wm.read(self.wfut.result())
                    except llm.LLMError as e:
                        self.war = {"error": str(e), "raw": "", "picks": []}
                self.main["war"] = self.war
                return self.main
            if time.monotonic() - started >= budget:
                return None


def finish(camp: campaign.Campaign, res: Dict) -> Optional[List]:
    """Log a month's result. An allowed incident is kept and its exchange queued for diplomacy.py (the queue item:
    [from, to, kind, [aim], brief]); returned, else None."""
    from . import diplomacy
    c = (camp.snapshot or {}).get("countries", {})
    name = lambda t: c.get(t, {}).get("name", t)
    key = camp.data.get("last_date") or ""
    head = f"=== {campaign.date_words(key)}: STORYTELLER: "
    pick = res["pick"]
    if res["error"]:
        diplomacy.log([head + f"the call failed: {res['error']}"])
        return None
    if not pick and res.get("note"):
        diplomacy.log([head + res["note"]])
        return None
    if not pick:
        diplomacy.log([head + "quiet (nothing is possible between the pairs; no call made)"])
        return None
    if not pick["kind"]:
        diplomacy.log([head + f"no readable pick; the reply was: {res['raw']}"])
        return None
    if pick["kind"] == "quiet":
        diplomacy.log([head + "quiet" + (f" - {pick['why']}" if pick["why"] else "")])
        return None
    line = f"{pick['kind']} {name(pick['from'])} -> {name(pick['to'])}"
    if not res["ok"]:
        diplomacy.log([head + f"dropped {line}: {res['why']}", f"  {pick['why']}"])
        return None
    aim = res["aim"]
    _state(camp)["incidents"].append({"date": key, "kind": pick["kind"], "from": pick["from"], "to": pick["to"],
                                      "aim": aim["aim"], "why": pick["why"]})
    if pick["kind"] == "guarantee" and _WORLD["w"]:          # a distant one counts against the world's allowance
        from . import guarantees
        _state(camp)["incidents"][-1]["reach"] = guarantees.reach(pick["from"], pick["to"], _WORLD["w"])
    if pick["kind"] == "reach_out":                   # no exchange: the country's letter to the player comes sooner
        pol = camp.policy_of(pick["from"])
        if pol:
            pol["urgency"] = 3
            camp.set_policy(pick["from"], pol)
        diplomacy.log([head + f"{line}   [{aim['aim']}]", f"  {pick['why']}"])
        return [pick["from"], pick["to"], "reach_out", [aim["aim"]]]
    brief = {"kind": pick["kind"], "aim": aim["aim"], "aim_kind": aim["kind"], "target": aim["target"],
             "why": pick["why"]}
    item = [pick["from"], pick["to"], pick["kind"], [aim["aim"]], brief]
    diplomacy._state(camp)["queue"].append(item)
    diplomacy.log([head + f"{line}   [{aim['aim']}]", f"  {pick['why']}"])
    return item


# ---------------------------------------------------------------- the month's guarantee ([ai_diplomacy] guarantees = ours)
#
# The game's AI gives no guarantees then (guarantees.py's gate), so the storyteller gives the world its own: after
# the month's incident, a second short call sees the countries under threat and who might guarantee them
# (guarantees.threats(), candidates(): stakes, distance, the game's rules), picks one guarantee (offered by the
# guarantor or asked by the threatened) or none, and decides which guarantees by agreement running out are renewed.
# The pick becomes an exchange like any incident, so the other side may refuse or name a price.

class GuaranteeMonth:
    """One month's guarantee: built with the world read, rule_step() checks one candidate against the game's rules
    (about a second each; called while `todo` is left), then messages() and read()."""

    def __init__(self, camp: campaign.Campaign, snap: Dict, w: Dict[str, Dict]) -> None:
        from . import guarantees
        self.camp, self.snap, self.w = camp, snap, w
        self.now = snap.get("date", "")
        self.threats = guarantees.threats(camp, w)
        recent = {campaign.pair(x["from"], x["to"]) for x in _state(camp)["incidents"]
                  if 0 <= _days(x["date"], self.now) < PAIR_GAP_DAYS}
        self.todo = [x for x in guarantees.candidates(camp, w, self.threats)
                     if campaign.pair(x["by"], x["of"]) not in recent]
        self.cands: List[Dict] = []
        self.renew = guarantees.renewals(camp)

    def name(self, t: str) -> str:
        return self.snap.get("countries", {}).get(t, {}).get("name", t)

    def rule_step(self) -> None:
        from . import diplomacy
        x = self.todo.pop(0)
        offers, _, _ = diplomacy.menu(self.camp, x["by"], x["of"], self.name(x["by"]), self.name(x["of"]),
                                      only=lambda k: k == "give:guarantee")
        if offers:
            self.cands.append(x)

    def messages(self) -> Optional[List[Dict]]:
        from . import guarantees
        if not self.cands and not self.renew:
            return None
        player = self.camp.data["player"]
        me = gamestate.country(player)
        name = self.name
        try:
            level = f"{round(world.tension([player])[0] * 100)}%"
        except Exception:
            level = "unknown"
        wards = {x["of"] for x in self.threats}
        held = [f"- {name(g)} guarantees {name(o)}" for g in sorted(self.w) for o in sorted(self.w[g]["guarantees"])
                if o in wards]
        home = self.w.get(player, {}).get("continent", "")
        parts = [
            world.text({p.ideology for p in me.parties}),
            "You are the storyteller of this Hearts of Iron IV world, in the way a game master steers a tabletop game. "
            "This call is about one thing: guarantees of independence between AI countries. The game's own AI gives "
            "none in this world, so every new guarantee comes from you: a promise to go to war for a country if it is "
            "attacked, which ties up the guarantor's civilian factories while it lasts "
            f"({guarantees.TERM_MONTHS} months, then renewed or ended). Guarantees shape the war to come: they deter, "
            "they draw powers into wars, and they make blocs. Give one where a real threat meets a power with a stake "
            "in stopping it, so that the coming crises have sides; none when nothing calls for one.",
            f"THE DATE: {campaign.date_words(self.now)}. WORLD TENSION: {level}.",
            f"THE PLAYER: {me.name} ({player}), on {home or 'an unknown continent'}; a human, never part of a "
            f"guarantee here (it may be the threat). What happens near them matters to their game, but most "
            f"guarantees should be elsewhere.",
            "COUNTRIES UNDER THREAT (computed):\n" + ("\n".join(
                f"- {name(x['of'])} ({x['of']}): " + "; ".join(guarantees.THREAT_WORDS[h].format(t=name(x["by"]))
                                                              for h in x["how"]) for x in self.threats[:12])
                or "none"),
        ]
        if held:
            parts.append("GUARANTEES ALREADY IN FORCE FOR THEM:\n" + "\n".join(held))
        if self.cands:
            where = {"near": "close by", "present": "a distant power with land on that continent: unusual",
                     "far": "from across the world: rare, only for a great stake"}
            parts.append("POSSIBLE GUARANTEES (the game's rules checked; pick only from these). Either side may make "
                         "the move: the guarantor offers (FROM guarantor, TO threatened) or the threatened asks "
                         "(FROM threatened, TO guarantor):\n" + "\n".join(
                             f"- {name(x['by'])} ({x['by']}) for {name(x['of'])} ({x['of']}) against "
                             f"{name(x['against'])}: {', '.join(x['stake'])}; {where[x['reach']]}" for x in self.cands))
        mine = [x for x in _state(self.camp)["incidents"] if x["kind"] == "guarantee"][-RECENT:]
        if mine:
            parts.append("YOUR LATEST GUARANTEES (vary them):\n" + "\n".join(
                f"- {campaign.date_words(x['date'])}: {name(x['from'])} -> {name(x['to'])} ({x['aim']})" for x in mine))
        if self.renew:
            parts.append("GUARANTEES BY AGREEMENT RUNNING OUT (renew those whose reason still stands; the rest end):\n"
                         + "\n".join(f"- {name(g['by'])} ({g['by']}) guarantees {name(g['of'])} ({g['of']}) until "
                                     f"{campaign.date_words(g['until'])}" for g in self.renew))
        parts.append("Answer with exactly these lines and nothing else:\n"
                     "GUARANTEE: FROM TO (two tags from the possible guarantees, the one who makes the move first), "
                     "or none\n"
                     "WHY: one sentence, as a historian would put it, from the two countries' own interests; never "
                     "about the player (unless they are the threat), pacing or your reasons as storyteller\n"
                     "RENEW: the guarantees to renew as BY OF pairs separated by semicolons, or none")
        return [{"role": "system", "content": "\n\n".join(parts)}, {"role": "user", "content": "Choose now."}]

    def read(self, raw: str) -> Dict:
        """{"raw", "error", "pick": {"from", "to", "why"} or None, "ok", "why_not", "cand", "renew": [(by, of)]}."""
        tags = lambda s: re.findall(r"\b[A-Z][A-Z0-9]{2}\b", s)
        said = _line(raw, "guarantee")
        out = {"raw": raw, "error": "", "pick": None, "ok": False, "why_not": "", "cand": None, "renew": []}
        t = tags(said)
        if len(t) >= 2 and "none" not in said.lower():
            out["pick"] = {"from": t[0], "to": t[1], "why": _line(raw, "why")}
            cand = next((x for x in self.cands if {x["by"], x["of"]} == {t[0], t[1]}), None)
            if not cand:
                out["why_not"] = "not one of the possible guarantees"
            else:
                out["ok"], out["cand"] = True, cand
        wanted = {(g["by"], g["of"]) for g in self.renew}
        for part in _line(raw, "renew").split(";"):
            p = tags(part)
            if len(p) >= 2 and (p[0], p[1]) in wanted:
                out["renew"].append((p[0], p[1]))
        return out


def finish_guarantee(camp: campaign.Campaign, g: Optional[Dict]) -> Optional[List]:
    """Log the month's guarantee; queue an allowed pick as an exchange (returned) and renew what was renewed."""
    from . import diplomacy, guarantees
    if not g:
        return None
    c = (camp.snapshot or {}).get("countries", {})
    name = lambda t: c.get(t, {}).get("name", t)
    key = camp.data.get("last_date") or ""
    head = f"=== {campaign.date_words(key)}: STORYTELLER (guarantees): "
    if g.get("error"):
        diplomacy.log([head + f"the call failed: {g['error']}"])
        return None
    if g.get("note"):
        diplomacy.log([head + g["note"]])
        return None
    for by, of in g["renew"]:
        until = guarantees.renew(camp, by, of)
        if until:
            text = f"{name(by)} renewed its guarantee of {name(of)}'s independence until {campaign.date_words(until)}."
            camp.add_events([{"date": key, "when": campaign.date_words(key), "type": "guarantee:renewed",
                              "tags": [by, of], "text": text, "major": False}])
            diplomacy.log([head + text])
    pick = g["pick"]
    if not pick:
        diplomacy.log([head + "none" + (" renewed" if g["renew"] else "")])
        return None
    line = f"guarantee {name(pick['from'])} -> {name(pick['to'])}"
    if not g["ok"]:
        diplomacy.log([head + f"dropped {line}: {g['why_not']}", f"  {pick['why']}"])
        return None
    x = g["cand"]
    offers = pick["from"] == x["by"]
    aim = (x["aim"]["aim"] if offers and x["aim"] else
           f"stand by {name(x['of'])} against {name(x['against'])}" if offers else
           f"find a protector against {name(x['against'])}")
    _state(camp)["incidents"].append({"date": key, "kind": "guarantee", "from": pick["from"], "to": pick["to"],
                                      "aim": aim, "why": pick["why"], "slot": "guarantee", "reach": x["reach"]})
    brief = {"kind": "guarantee", "aim": aim, "aim_kind": "back", "target": x["of"] if offers else x["by"],
             "why": pick["why"]}
    item = [pick["from"], pick["to"], "guarantee", [aim], brief]
    diplomacy._state(camp)["queue"].append(item)
    diplomacy.log([head + f"{line}   [{aim}; {x['reach']}]", f"  {pick['why']}"])
    camp.save()
    return item


def guarantees_main(args: List[str]) -> int:
    """python -m hoi4_rp storyteller --guarantees [--prompt]: one guarantee month, dry (nothing is queued)."""
    from . import chronicle, diplomacy, llm
    player = gamestate.player_tag()
    camp = campaign.Campaign(campaign.read_id(player), player)
    camp.snapshot = chronicle.snapshot(player)
    started = time.monotonic()
    gm = GuaranteeMonth(camp, camp.snapshot, diplomacy.world())
    print("THREATS\n" + ("\n".join(f"- {gm.name(x['of'])} by {gm.name(x['by'])}: {', '.join(x['how'])} "
                                   f"(weight {x['weight']})" for x in gm.threats[:20]) or "none"))
    while gm.todo:
        gm.rule_step()
    print(f"\nPOSSIBLE GUARANTEES ({time.monotonic() - started:.0f} s to work out)\n" + ("\n".join(
        f"- {gm.name(x['by'])} for {gm.name(x['of'])} against {gm.name(x['against'])}: {', '.join(x['stake'])}; "
        f"{x['reach']}, score {x['score']:.1f}" for x in gm.cands) or "none"))
    if gm.renew:
        print("\nRUNNING OUT\n" + "\n".join(f"- {g['by']} -> {g['of']} until {g['until']}" for g in gm.renew))
    msgs = gm.messages()
    if not msgs:
        print("\nnothing to guarantee or renew; no call made")
        return 0
    if "--prompt" in args:
        print(f"\n----- prompt\n{msgs[0]['content']}\n-----")
    res = gm.read(llm.chat(msgs, None, 0.8, 800, "ai:storyteller", "guarantees"))
    p = res["pick"]
    print("\nPICK: " + (f"{p['from']} -> {p['to']} " + ("(allowed)" if res["ok"] else f"(DROPPED: {res['why_not']})")
                        + f"\n  {p['why']}" if p else "none")
          + (f"\nRENEW: {res['renew']}" if res["renew"] else ""))
    return 0


# ---------------------------------------------------------------- the month's faction move ([ai_diplomacy] factions = ours)
#
# The game's AI then neither invites, asks to join, expels nor leaves (gates.py), so the storyteller moves the world's
# factions: after the month's incident (and guarantee) a third short call sees the factions, the countries outside
# them and what factions.candidates() finds possible (joining, founding together, a break), and picks one move or
# none. Joining and founding become an exchange like any incident (the other side may refuse or name a price); a
# break is done at once (the member leaves, or the leader expels it).

MOVE_WORDS = {"join": "joins", "found": "founds a faction with", "break": "breaks with"}


class FactionMonth:
    """One month's faction move: rule_step() checks one joining or founding against the game's rules (the deal
    menu: world tension, subjects, wars; about a second each), then messages() and read()."""

    def __init__(self, camp: campaign.Campaign, snap: Dict, w: Dict[str, Dict]) -> None:
        from . import factions
        self.camp, self.snap, self.w = camp, snap, w
        self.now = snap.get("date", "")
        recent = {campaign.pair(x["from"], x["to"]) for x in _state(camp)["incidents"]
                  if 0 <= _days(x["date"], self.now) < PAIR_GAP_DAYS}
        self.todo = [x for x in factions.candidates(camp, w) if campaign.pair(x["a"], x["b"]) not in recent]
        self.cands: List[Dict] = []

    def name(self, t: str) -> str:
        return self.snap.get("countries", {}).get(t, {}).get("name", t)

    def rule_step(self) -> None:
        from . import diplomacy
        x = self.todo.pop(0)
        if x["move"] == "break":
            self.cands.append(x)
            return
        keys = (("give:found", "ask:found") if x["move"] == "found"
                else ("give:faction", "give:join", "ask:faction", "ask:join"))
        offers, asks, _ = diplomacy.menu(self.camp, x["a"], x["b"], self.name(x["a"]), self.name(x["b"]),
                                         only=lambda k: k in keys)
        if offers or asks:
            self.cands.append(x)

    def line(self, x: Dict) -> str:
        n = self.name
        where = {"near": "", "present": "; from another continent: unusual", "far": "; across the world: rare"}
        if x["move"] == "join":
            head = f"JOIN: {n(x['a'])} ({x['a']}) and the {x['faction']}, led by {n(x['b'])} ({x['b']})"
        elif x["move"] == "found":
            head = f"FOUND: {n(x['a'])} ({x['a']}) founds a new faction with {n(x['b'])} ({x['b']})"
        else:
            head = (f"BREAK: {n(x['a'])} ({x['a']}) and the {x['faction']}, led by {n(x['b'])} ({x['b']}): the "
                    f"member leaves (FROM {x['a']}) or the leader expels it (FROM {x['b']})")
        return f"- {head}: {'; '.join(x['stake'])}{where.get(x['reach'], '')}"

    def messages(self) -> Optional[List[Dict]]:
        from . import factions
        if not self.cands:
            return None
        player = self.camp.data["player"]
        me = gamestate.country(player)
        n = self.name
        try:
            level = f"{round(world.tension([player])[0] * 100)}%"
        except Exception:
            level = "unknown"
        c = self.snap.get("countries", {})
        facs = factions.world_factions(self.camp)
        ideo = factions.ideologies(list({f["leader"] for f in facs.values() if f["leader"]}))
        enemies = lambda t: sorted(factions._enemies(self.snap, t))
        rows = [f"- the {fname}: led by {n(f['leader'])} ({ideo.get(f['leader'], '?')})"
                f"{'; at war with ' + ', '.join(n(e) for e in enemies(f['leader'])) if enemies(f['leader']) else ''}; "
                f"members: {', '.join(n(m) for m in f['members'][1:]) or 'none yet'}" for fname, f in facs.items()]
        loose = [n(t) for t, x in sorted(c.items()) if x.get("major") and not x.get("faction")]
        parts = [
            world.text({p.ideology for p in me.parties}),
            "You are the storyteller of this Hearts of Iron IV world, in the way a game master steers a tabletop game. "
            "This call is about one thing: factions, the standing alliances whose members fight each other's wars. "
            "The game's own AI forms none in this world: national focuses and decisions still found and fill some "
            "(the great historical ones mostly), and every other change comes from you. Factions decide the shape of "
            "the war to come: who stands with whom, which wars spread and which stay small. Bring a country into a "
            "faction where a common enemy, a war or a threat binds them; found a new one where neighbours face the "
            "same danger and no faction serves them; break a member away where it no longer belongs. Most months "
            "in peacetime need no move: alliances are made for life and death. Once wars are fought, countries at "
            "war with the same enemy draw together, and the moves come oftener.",
            f"THE DATE: {campaign.date_words(self.now)}. WORLD TENSION: {level}.",
            f"THE PLAYER: {me.name} ({player}); a human, never part of a move here (their own letters do that), "
            f"though what happens near them matters to their game.",
            "FACTIONS NOW:\n" + ("\n".join(rows) or "none"),
        ]
        if loose:
            parts.append("MAJOR POWERS IN NO FACTION: " + ", ".join(loose))
        parts.append("POSSIBLE MOVES (the game's rules checked; pick only from these). For JOIN and FOUND either side "
                     "may make the move (FROM is the one who asks or offers first); for BREAK see each line:\n"
                     + "\n".join(self.line(x) for x in self.cands))
        mine = [x for x in _state(self.camp)["incidents"] if x.get("slot") == "faction"][-RECENT:]
        if mine:
            parts.append("YOUR LATEST FACTION MOVES (vary them):\n" + "\n".join(
                f"- {campaign.date_words(x['date'])}: {n(x['from'])} {MOVE_WORDS.get(x['kind'], x['kind'])} "
                f"{n(x['to'])}" for x in mine))
        parts.append("Answer with exactly these lines and nothing else:\n"
                     "MOVE: FROM TO (two tags from one possible move, the one who acts first), or none\n"
                     "NAME: for FOUND only, the new faction's name as the world would come to know it (a few words, "
                     "in the style of its era and region, without 'The'); otherwise none\n"
                     "WHY: one sentence, as a historian would put it, from the countries' own interests; never about "
                     "the player (unless they are the enemy), pacing or your reasons as storyteller")
        return [{"role": "system", "content": "\n\n".join(parts)}, {"role": "user", "content": "Choose now."}]

    def read(self, raw: str) -> Dict:
        """{"raw", "error", "pick": {"from", "to", "why", "name"} or None, "ok", "why_not", "cand"}."""
        said = _line(raw, "move")
        t = re.findall(r"\b[A-Z][A-Z0-9]{2}\b", said)
        out = {"raw": raw, "error": "", "pick": None, "ok": False, "why_not": "", "cand": None}
        if len(t) < 2 or "none" in said.lower():
            return out
        name = _line(raw, "name")
        out["pick"] = {"from": t[0], "to": t[1], "why": _line(raw, "why"),
                       "name": "" if name.lower().startswith("none") else name}
        cand = next((x for x in self.cands if {x["a"], x["b"]} == {t[0], t[1]}), None)
        if not cand:
            out["why_not"] = "not one of the possible moves"
        else:
            out["ok"], out["cand"] = True, cand
        return out


def finish_faction(camp: campaign.Campaign, f: Optional[Dict]) -> str:
    """Log the month's faction move; queue a joining or founding as an exchange, carry out a break. Returns a line
    for the console ('' if nothing happened)."""
    from . import actions, diplomacy, factions
    if not f:
        return ""
    c = (camp.snapshot or {}).get("countries", {})
    name = lambda t: c.get(t, {}).get("name", t)
    key = camp.data.get("last_date") or ""
    head = f"=== {campaign.date_words(key)}: STORYTELLER (factions): "
    if f.get("error"):
        diplomacy.log([head + f"the call failed: {f['error']}"])
        return ""
    if f.get("note"):
        diplomacy.log([head + f["note"]])
        return ""
    pick = f["pick"]
    if not pick:
        diplomacy.log([head + "none"])
        return ""
    if not f["ok"]:
        diplomacy.log([head + f"dropped {pick['from']} -> {pick['to']}: {f['why_not']}", f"  {pick['why']}"])
        return ""
    x, a, b = f["cand"], pick["from"], pick["to"]
    move = x["move"]
    if move == "break":
        by_leader = a == x["b"]
        if not actions._effect(factions.break_effect(x["a"], x["b"], by_leader)):
            diplomacy.log([head + f"break {name(a)} -> {name(b)} failed in the game"])
            return ""
        text = (f"{name(x['b'])} expelled {name(x['a'])} from the {x['faction']}." if by_leader
                else f"{name(x['a'])} left the {x['faction']}, led by {name(x['b'])}.")
        if pick["why"]:
            text += f" {pick['why']}"
        camp.add_events([{"date": key, "when": campaign.date_words(key), "type": "faction:break",
                          "tags": [x["a"], x["b"]], "text": text, "major": False}])
        _state(camp)["incidents"].append({"date": key, "kind": "break", "from": a, "to": b, "aim": "",
                                          "why": pick["why"], "slot": "faction"})
        diplomacy.log([head + text])
        camp.save()
        return text
    if move == "found":
        factions.propose_name(a, b, pick["name"])
    joiner = x["a"]
    if move == "join":
        aim = (x["aim"]["aim"] if a == joiner and x["aim"] else
               f"join the {x['faction']}" if a == joiner else f"bring {name(joiner)} into the {x['faction']}")
    else:
        aim = (x["aim"]["aim"] if x["aim"] and a == x["a"] else
               f"found a faction with {name(b)}")
    brief = {"kind": move, "aim": aim, "aim_kind": "back", "target": b, "why": pick["why"],
             "name": factions._clean(pick["name"]) if move == "found" else ""}
    _state(camp)["incidents"].append({"date": key, "kind": move, "from": a, "to": b, "aim": aim,
                                      "why": pick["why"], "slot": "faction", "reach": x["reach"]})
    diplomacy._state(camp)["queue"].append([a, b, move, [aim], brief])
    line = (f"{move} {name(a)} -> {name(b)}" + (f" ({brief['name']})" if brief["name"] else "")
            + (f" [{x['faction']}]" if x["faction"] else ""))
    diplomacy.log([head + f"{line}   [{aim}; {x['reach']}]", f"  {pick['why']}"])
    camp.save()
    return line


def factions_main(args: List[str]) -> int:
    """python -m hoi4_rp storyteller --factions [--prompt]: one faction month, dry (nothing is queued or done)."""
    from . import chronicle, diplomacy, llm
    player = gamestate.player_tag()
    camp = campaign.Campaign(campaign.read_id(player), player)
    camp.snapshot = chronicle.snapshot(player)
    w = diplomacy.world()
    missing = [t for t in agendas.covered(camp, w, diplomacy.score(camp, w)) if not agendas.aims_of(camp, t)]
    if missing:
        print(f"writing the agendas of {len(missing)} countries without one: {', '.join(missing)}")
        agendas.generate(camp, w, missing)          # kept in memory for this run only
    started = time.monotonic()
    fm = FactionMonth(camp, camp.snapshot, w)
    while fm.todo:
        fm.rule_step()
    print(f"POSSIBLE MOVES ({time.monotonic() - started:.0f} s to work out)\n"
          + ("\n".join(f"{fm.line(x)} (score {x['score']:.1f})" for x in fm.cands) or "none"))
    msgs = fm.messages()
    if not msgs:
        print("\nno faction move possible; no call made")
        return 0
    if "--prompt" in args:
        print(f"\n----- prompt\n{msgs[0]['content']}\n-----")
    res = fm.read(llm.chat(msgs, None, 0.8, 800, "ai:storyteller", "factions"))
    p = res["pick"]
    print("\nPICK: " + (f"{p['from']} -> {p['to']}" + (f" ({p['name']})" if p["name"] else "")
                        + (" (allowed)" if res["ok"] else f" (DROPPED: {res['why_not']})") + f"\n  {p['why']}"
                        if p else "none"))
    return 0


# ---------------------------------------------------------------- the month's wars ([ai_diplomacy] wars = ours)
#
# The game's AI then declares no war of its own (the bridge's veto) and justifies only where allowed (gates.py), so
# the storyteller weighs the would-be wars each month (wars.candidates(): war goals held, declarations the bridge
# stopped, land aims without a war goal) and decides for each as its government's shrewd player would, seeing the
# war it would really get (ultimatums.balance()). wars.decide() carries it out.

WAR_STEPS = ("war", "ultimatum", "prepare", "wait")


_NAME_FILLER = {"republic", "kingdom", "empire", "state", "states", "union", "people's", "peoples", "socialist",
                "federal", "democratic", "united", "free", "national", "provisional", "government", "dominion",
                "commonwealth", "reich", "soviet", "chinese", "the", "of", "and"}


def _named_at(text: str, tag: str, name: str) -> Optional[int]:
    """Where a country is named in a line written by the model: its tag, its full name, or a telling word of the
    name by its first letters (Czechoslovakia for Czechoslovak Republic, Ethiopia for Governorate of Ethiopia).
    None if it isn't."""
    hits = [m.start() for m in [re.search(rf"\b{re.escape(tag)}\b", text),
                                re.search(re.escape(name), text, re.I)] if m]
    for word in re.findall(r"[\w']+", name):
        if len(word) >= 5 and word.lower() not in _NAME_FILLER:
            m = re.search(rf"\b{re.escape(word[:6])}", text, re.I)
            if m:
                hits.append(m.start())
    return min(hits) if hits else None


class WarMonth:
    """One month's war decisions: rule_step() weighs one would-be war (the balance of both sides, whether an
    ultimatum is possible; a few seconds of game queries), then messages() and read()."""

    def __init__(self, camp: campaign.Campaign, snap: Dict, w: Dict[str, Dict]) -> None:
        from . import wars
        self.camp, self.snap, self.w = camp, snap, w
        self.now = snap.get("date", "")
        wars.poll(camp)
        self.todo = wars.candidates(camp, w)
        self.cands: List[Dict] = []

    def name(self, t: str) -> str:
        return self.snap.get("countries", {}).get(t, {}).get("name", t)

    def rule_step(self) -> None:
        from . import actions, diplomacy, ultimatums
        x = self.todo.pop(0)
        a, b = x["from"], x["to"]
        x["balance"], _ = ultimatums.balance(self.camp, a, b, self.name(a), self.name(b))
        x["enemies"] = _enemies_in(self.snap, a)      # its wars as it was weighed (finish_war: still the same?)
        x["ultimatum_ok"] = bool(x["kind"] == "war" and x["aim"] and b != self.camp.data["player"] and _tense(a)
                                 and diplomacy.may_threaten(self.camp, a, b, actions.relation(a, b)))
        self.cands.append(x)

    def block(self, x: Dict) -> str:
        n = self.name
        a, b = x["from"], x["to"]
        player = self.camp.data["player"]
        why = []
        if x["wargoal"]:
            why.append(f"{n(a)} holds a war goal on {n(b)}")
        if x.get("ultimatum"):
            why.append(f"its ultimatum to {n(b)} ran out unmet")
        if x["attempts"]:
            from . import wars
            since = wars._store(self.camp)["attempts"].get(f"{a}:{b}", {}).get("first", "")
            why.append("its generals press to declare war" + (f" (since {campaign.date_words(since)})" if since else ""))
        if x["aim"]:
            why.append(f"its aim: {x['aim']['aim']}")
        if b == player:
            why.append(f"{n(b)} is the player's country (a human)")
        steps = (["war", "ultimatum", "wait"] if x["ultimatum_ok"] else ["war", "wait"]) if x["kind"] == "war" \
            else ["prepare", "wait"]
        way = f"\n  course: {c}" if (c := course.short(a)) else ""
        return (f"=== {n(a)} ({a}) and {n(b)} ({b}): {'; '.join(why)}\n"
                f"{n(a)}'s aims:\n{_aims_lines(self.camp, a)}{way}\n{x['balance']}\n"
                f"Possible: {', '.join(steps)}")

    def messages(self) -> Optional[List[Dict]]:
        if not self.cands:
            return None
        player = self.camp.data["player"]
        me = gamestate.country(player)
        try:
            level = f"{round(world.tension([player])[0] * 100)}%"
        except Exception:
            level = "unknown"
        parts = [
            world.text({p.ideology for p in me.parties}),
            "You are the storyteller of this Hearts of Iron IV world, in the way a game master steers a tabletop game. "
            "This call is about one thing: war. The game's own AI may not start wars in this world; every war a "
            "government starts by its own choice is decided here (wars from national focuses and events, and "
            "allies and guarantors joining a war, come on their own). For each government below, decide as a "
            "shrewd player of that country would, wanting it to survive and prosper:\n"
            "  war: it goes to war now; its generals strike when the armies are ready\n"
            "  ultimatum: a deadline to give up the land it claims first, so the other side may yield without war\n"
            "  prepare: it may justify a war goal, to be ready when the moment comes (a step short of war)\n"
            "  wait: not now\n"
            "War is the gravest step. Read the balance: a guarantor usually honours its guarantee, a faction is called "
            "in. A government that starts a war it can't win loses everything; one that is strong, has a cause and "
            "faces no deterrent strikes, often while the others hesitate. One already at war rarely opens a second "
            "front. The world's tension matters: in a calm world an attack shocks everyone. The world should see "
            "wars when they make sense for the country, not because a war goal exists.",
            f"THE DATE: {campaign.date_words(self.now)}. WORLD TENSION: {level}.",
            "THE WOULD-BE WARS:\n\n" + "\n\n".join(self.block(x) for x in self.cands),
        ]
        from . import wars
        past = wars._store(self.camp)["decisions"][-6:]
        if past:
            parts.append("YOUR LATEST DECISIONS:\n" + "\n".join(
                f"- {campaign.date_words(d['date'])}: {self.name(d['from'])} -> {self.name(d['to'])}: {d['step']}"
                for d in past))
        world_events = [e for e in self.camp.data["events"] if e.get("major") and e["date"] <= self.now][-6:]
        if world_events:
            parts.append("RECENT WORLD EVENTS:\n" + "\n".join(f"- {e['when']}: {e['text']}" for e in world_events))
        parts.append("Answer with one line for each pair above and nothing else, in this form:\n"
                     "FROM TO: step - one sentence, as a historian would put it, from the country's own interests "
                     "and its situation (never about the player's game or your reasons as storyteller)\n"
                     "where FROM and TO are the two tags in brackets (e.g. GER CZE: wait - ...) and step is one of "
                     "the possible steps listed for that pair.")
        return [{"role": "system", "content": "\n\n".join(parts)}, {"role": "user", "content": "Decide now."}]

    def read(self, raw: str) -> Dict:
        """{"raw", "error", "picks": [(candidate, step, why)]}: one for each pair answered with a known step."""
        out = {"raw": raw, "error": "", "picks": []}
        done = set()
        for line in raw.splitlines():
            m = re.match(rf"^(.*?)[:\-–]+\s*\**\s*({'|'.join(WAR_STEPS)})\b[\s*\-–:.,]*(.*)$", line, re.I)
            if not m:
                continue
            left = m.group(1)
            for i, x in enumerate(self.cands):     # the pair named on the left, by tag or name, in that order
                a, b = _named_at(left, x["from"], self.name(x["from"])), _named_at(left, x["to"], self.name(x["to"]))
                if i not in done and a is not None and b is not None and a < b:
                    done.add(i)
                    out["picks"].append((x, m.group(2).lower(), m.group(3).strip(" *")))
                    break
        return out


def _enemies_in(snap: Optional[Dict], tag: str) -> List[str]:
    """Who `tag` is at war with in a chronicle snapshot."""
    return sorted({t for p in (snap or {}).get("wars", []) for t in p.split("|") if tag in p.split("|") and t != tag})


def finish_war(camp: campaign.Campaign, res: Optional[Dict]) -> List[str]:
    """Log the month's war decisions and carry them out (wars.decide). Returns a line for each. A decision whose
    country went to war since it was weighed (the game runs on while the month is worked out) is dropped: the pair
    is weighed again next month, as it stands then."""
    from . import diplomacy, wars
    if not res:
        return []
    key = camp.data.get("last_date") or ""
    head = f"=== {campaign.date_words(key)}: STORYTELLER (wars): "
    if res.get("error"):
        diplomacy.log([head + f"the call failed: {res['error']}"])
        return []
    if res.get("note"):
        diplomacy.log([head + res["note"]])
        return []
    if not res["picks"]:
        diplomacy.log([head + f"no readable decision; the reply was: {res['raw']}"])
        return []
    lines = []
    c = (camp.snapshot or {}).get("countries", {})
    for x, step, why in res["picks"]:
        new = set(_enemies_in(camp.snapshot, x["from"])) - set(x.get("enemies", []))
        if new:
            diplomacy.log([head + f"{c.get(x['from'], {}).get('name', x['from'])} -> "
                                  f"{c.get(x['to'], {}).get('name', x['to'])}: {step} dropped (at war with "
                                  f"{', '.join(c.get(t, {}).get('name', t) for t in sorted(new))} since it was weighed; "
                                  f"weighed again next month)"])
            continue
        _, line = wars.decide(camp, x, step, why)
        lines.append(line)
        diplomacy.log([head + line, f"  {why}"])
    camp.save()
    return lines


def wars_main(args: List[str]) -> int:
    """python -m hoi4_rp storyteller --wars [--prompt]: one war month, dry (nothing is decided)."""
    from . import chronicle, diplomacy, llm
    player = gamestate.player_tag()
    camp = campaign.Campaign(campaign.read_id(player), player)
    camp.snapshot = chronicle.snapshot(player)
    w = diplomacy.world()
    started = time.monotonic()
    wm = WarMonth(camp, camp.snapshot, w)
    while wm.todo:
        wm.rule_step()
    print(f"WOULD-BE WARS ({time.monotonic() - started:.0f} s to work out)\n"
          + ("\n\n".join(wm.block(x) for x in wm.cands) or "none"))
    msgs = wm.messages()
    if not msgs:
        print("\nno war to weigh; no call made")
        return 0
    if "--prompt" in args:
        print(f"\n----- prompt\n{msgs[0]['content']}\n-----")
    res = wm.read(llm.chat(msgs, None, 0.7, 900, "ai:storyteller", "wars"))
    print("\nDECISIONS:\n" + ("\n".join(f"- {x['from']} -> {x['to']}: {s} - {why}" for x, s, why in res["picks"])
                              or f"none readable:\n{res['raw']}"))
    return 0


# ---------------------------------------------------------------- python -m hoi4_rp storyteller

def main(args: List[str]) -> int:
    from . import chronicle, diplomacy, llm
    if "--guarantees" in args:
        return guarantees_main(args)
    if "--wars" in args:
        return wars_main(args)
    if "--factions" in args:
        return factions_main(args)
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
    opts.update(reach_options(camp, snap, w))
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
        raw = llm.chat(msgs, None, 0.8, 1200, "ai:storyteller", "")
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
