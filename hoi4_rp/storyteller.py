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


def pair_rows(camp: campaign.Campaign, scored: List[Tuple], now: str) -> List[Tuple]:
    """The scored pairs without the player, those whose countries had an incident lately last, so that the same
    few countries do not crowd the month's shortlist (a stable sort: the score order is kept otherwise)."""
    player = camp.data["player"]
    seen: Dict[str, int] = {}
    for x in _state(camp)["incidents"]:
        if 0 <= _days(x["date"], now) <= 90:
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
    for row in pair_rows(camp, scored, snap.get("date", ""))[:SHORTLIST]:
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
        parts.append("AGENDAS (what each country wants):\n" + "\n".join(
            f"{name(t)} ({t}):\n{_aims_lines(camp, t)}" for t in tags))

    def possible(x: str, y: str) -> str:
        found = opts.get((x, y), [])
        return f"\n  {x} -> {y}: " + "; ".join(f"{o['kind']} [{o['aim']['aim']}]" for o in found) if found else ""

    if shortlist:
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
                 f"After a quiet stretch for the player, a reach_out (if one is listed) or something near their region "
                 f"brings them news; after a busy one, choose quiet or something distant. Most incidents should be "
                 f"between other countries: favour the player's corner only moderately, and name the player's country "
                 f"in WHY only when the incident really concerns it. Aim for an incident most months: "
                 f"if you picked one in the last 25 days, choose quiet unless something truly calls for another. "
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
    if sum(1 for x in pc["mine"] if _days(x["date"], now) < 20) >= INCIDENTS_PER_MONTH:
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

    def _write_agenda(self, tag: str) -> None:
        from . import llm
        try:
            me = gamestate.country(tag)
        except gamestate.StateError as e:
            print(f"  [storyteller] no agenda for {tag}: {e}")
            return
        cands = agendas.candidates(me, self.snap, self.w)
        self.jobs.append((me, cands, self.pool.submit(llm.chat, agendas.prompt(me, cands, self.snap), None, 0.7, 600,
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
        from . import diplomacy, llm
        started = time.monotonic()
        player = self.camp.data["player"]
        while True:
            if self.phase == "world":
                self.w = diplomacy.world()
                self.scored = diplomacy.score(self.camp, self.w)
                self.todo = [t for t in agendas.covered(self.camp, self.w, self.scored) if agendas.due(self.camp, t)]
                self.phase = "agendas"
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
                else:
                    self.shortlist, self.opts = shortlist_options(self.camp, self.snap, self.scored, self.cache)
                    self.opts.update(reach_options(self.camp, self.snap, self.w))
                    msgs = messages(self.camp, self.snap, self.w, self.shortlist, self.opts, self.now, [])
                    if not msgs:
                        return {"pick": None, "ok": False, "why": "", "aim": None, "raw": "", "error": "", "pairs": 0}
                    self.fut = self.pool.submit(llm.chat, msgs, None, 0.8, 400, "ai:storyteller", "")
                    self.phase = "wait"
            else:
                if not self.fut.done():
                    return None
                res = {"pick": None, "ok": False, "why": "", "aim": None, "raw": "", "error": "",
                       "pairs": len(self.shortlist)}
                try:
                    res["raw"] = self.fut.result()
                except llm.LLMError as e:
                    res["error"] = str(e)
                    return res
                res["pick"] = parse(res["raw"])
                # pacing is measured to the day the pick lands: the game ran on while the month was worked out
                res["ok"], res["why"], res["aim"] = check(self.camp, res["pick"], self.w, self.snap,
                                                          max(self.now, self.camp.data.get("last_date") or ""), [],
                                                          self.opts)
                return res
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
