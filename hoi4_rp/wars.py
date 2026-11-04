"""Wars between countries by our decision ([ai_diplomacy] wars = ours; default game).

The game's AI then may not declare war on its own: the bridge drops every declaration an AI country sends unless
the pair was allowed (bridge.wars, hooked in hoi4-bridge), and it may justify a war goal only on a country in its
airp_justify array (gates.py). What isn't touched: war from focuses, events and decisions (declare_war_on), civil
wars, a country joining a faction at war, guarantors and allies following a declaration, the player's own wars and
our ultimatums (declared by script).

Each month the storyteller's war pass (storyteller.WarMonth) weighs the would-be wars:
    a country holding a war goal on another (from a focus, an event, its own justification)
    a country the bridge stopped declaring war (its AI tried: bridge.wars attempts, polled here)
    a country with an aim to take land from another and no war goal yet (it may be allowed to prepare)
and for each decides as that country's shrewd player would, seeing the war it would really get (ultimatums.balance:
both sides with allies, subjects, the defender's guarantors and their factions):
    war         we declare it now, by script (declare): the game's AI, merely allowed, may never strike
    ultimatum   a deadline first (an AI-to-AI ultimatum, as the storyteller's own; only with an aim on the target's
                land, never to the player: the player's come by letter)
    prepare     it may justify a war goal (added to its airp_justify array, its AI told to prepare for that war)
    wait        nothing now; the pair comes up again after WAIT_DAYS
A war at less than MIN_RATIO of the other side's strength is turned into a wait.

Stored in the campaign: data["wars"] = {"attempts": {"A:B": {"count", "first", "last"}}, "decisions": [{"date",
"from", "to", "step", "why"}], "allowed": [{"from", "to", "date", "until"}]}. Nothing is allowed any more (a war
decision is declared at once); "allowed" holds what campaigns from before granted, still let through until it ends.
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

from . import actions, agendas, bridge, campaign, gamestate, world

ALLOW_DAYS = 90         # an allowed war stays allowed this long; unused, the pair comes up again
WAIT_DAYS = 60          # a pair decided otherwise comes up again after this
ATTEMPT_DAYS = 120      # stopped declarations this recent count
MIN_RATIO = 0.7         # no war at less than this share of the other side's strength
WAR_CANDIDATES = 8
PREPARE_CANDIDATES = 2
ARRAY = "airp_justify"


def _store(camp: campaign.Campaign) -> Dict:
    st = camp.data.setdefault("wars", {})
    for k, v in (("attempts", {}), ("decisions", []), ("allowed", [])):
        st.setdefault(k, v)
    return st


def ours() -> bool:
    from . import gates
    return gates.ours("wars")


def _today(camp: campaign.Campaign) -> str:
    return (camp.snapshot or {}).get("date", "") or camp.data.get("last_date") or ""


def _days(a: str, b: str) -> int:
    from . import storyteller
    return storyteller._days(a, b)


# ---------------------------------------------------------------- the bridge

def allowed_now(camp: campaign.Campaign) -> List[Dict]:
    today = _today(camp)
    return [x for x in _store(camp)["allowed"] if x["until"] > today]


def sync(camp: Optional[campaign.Campaign]) -> None:
    """The bridge's veto on (wars = ours) or off, and the pairs allowed now (after loading, and when they change)."""
    try:
        if not ours():
            bridge.send("bridge.wars off")
            return
        pairs = [f"{x['from']}:{x['to']}" for x in allowed_now(camp)] if camp else []
        replies = bridge.send("bridge.wars on", "bridge.wars allow " + " ".join(pairs))
    except bridge.BridgeError as e:
        print(f"  [wars] the bridge has no war veto ({e}): the game's AI declares war on its own")
        return
    if any(not r.startswith("OK") for r in replies):
        print(f"  [wars] the bridge has no war veto ({'; '.join(replies)}): the game's AI declares war on its own")
    else:
        print(f"  [wars] the game's AI may not declare war on its own ({len(pairs)} wars allowed)")


def poll(camp: campaign.Campaign) -> None:
    """Take the declarations the bridge stopped since last asked into the campaign."""
    try:
        reply = bridge.send("bridge.wars attempts")[0]
    except (bridge.BridgeError, IndexError):
        return
    if not reply.startswith("OK"):
        return
    today = _today(camp)
    att = _store(camp)["attempts"]
    for pair, n in re.findall(r"([A-Z0-9]+:[A-Z0-9]+)=(\d+)", reply):
        x = att.setdefault(pair, {"count": 0, "first": today, "last": today})
        if _days(x["last"], today) > ATTEMPT_DAYS:
            x.update(count=0, first=today)
        x["count"] += int(n)
        x["last"] = today


def drop_after(camp: campaign.Campaign, key: str) -> None:
    """An earlier save was loaded: decisions, permissions and attempts from then on never happened."""
    st = _store(camp)
    st["decisions"] = [x for x in st["decisions"] if x["date"] < key]
    st["allowed"] = [x for x in st["allowed"] if x["date"] < key]
    st["attempts"] = {p: x for p, x in st["attempts"].items() if x["first"] < key}


# ---------------------------------------------------------------- who would go to war

def _wargoals() -> List[Tuple[str, str]]:
    """(holder, target) for every war goal held now."""
    out = []
    for line in gamestate.query('every_country = { limit = { exists = yes } every_country = { limit = { '
                                'has_wargoal_against = PREV } log = "AIRPWW|[THIS.GetTag]|[PREV.GetTag]" } }'):
        f = line.split("|")
        if f[0] == "AIRPWW" and len(f) >= 3:
            out.append((f[1].strip(), f[2].strip()))
    return out


def candidates(camp: campaign.Campaign, w: Dict[str, Dict]) -> List[Dict]:
    """The would-be wars to weigh this month: [{"from", "to", "kind": war | prepare, "wargoal", "attempts",
    "aim"}], war first (most tried, then with a war goal), then up to PREPARE_CANDIDATES to prepare."""
    from . import ultimatums
    snap = camp.snapshot or {}
    c = snap.get("countries", {})
    player = camp.data["player"]
    wars = set(snap.get("wars", []))
    today = _today(camp)
    st = _store(camp)
    recent = {(x["from"], x["to"]) for x in st["decisions"] if 0 <= _days(x["date"], today) < WAIT_DAYS}
    allowed = {(x["from"], x["to"]) for x in allowed_now(camp)}
    # an ultimatum still running holds the pair; one that ran out unmet (ready) is ours to weigh like any war goal
    threatening = {(u["from"], u["to"]) for u in ultimatums._store(camp) if u["status"] == "open"}
    readied = {(u["from"], u["to"]) for u in ultimatums._store(camp) if u["status"] == "ready"}
    ok = lambda t: (t in c and t in w and t != player and not w[t]["subject"] and not c[t].get("capitulated")
                    and not c[t].get("voiceless"))

    def free(a: str, b: str) -> bool:
        return (ok(a) and b in c and not c[b].get("capitulated") and campaign.pair(a, b) not in wars
                and c[b].get("overlord") != a and (a, b) not in recent | allowed | threatening
                and not actions.pact_between(a, b))           # no war, nor preparing one, while a pact stands

    land = lambda a, b: next((x for x in agendas.aims_of(camp, a) if x["kind"] == "land" and x["target"] == b), None)
    goals = set(_wargoals())
    tried = {tuple(p.split(":")): x["count"] for p, x in st["attempts"].items()
             if 0 <= _days(x["last"], today) <= ATTEMPT_DAYS}
    out = []
    for a, b in goals | set(tried):
        if free(a, b):
            out.append({"from": a, "to": b, "kind": "war", "wargoal": (a, b) in goals, "attempts": tried.get((a, b), 0),
                        "aim": land(a, b), "ultimatum": (a, b) in readied})
    out.sort(key=lambda x: (-x["attempts"], not x["wargoal"], not c[x["from"]].get("major")))
    out = out[:WAR_CANDIDATES]
    prep = []
    for a in agendas._store(camp):
        for aim in agendas.aims_of(camp, a):
            b = aim["target"]
            if (aim["kind"] == "land" and b and (a, b) not in goals and free(a, b)
                    and not bridge.trigger(f"{a} = {{ is_in_array = {{ {ARRAY} = {b} }} }}")):
                prep.append({"from": a, "to": b, "kind": "prepare", "wargoal": False, "attempts": 0, "aim": aim})
    if prep:     # only where world tension already lets the country justify (the game's threshold, its own)
        level, rules = world.tension({x["from"] for x in prep})
        prep = [x for x in prep if rules.get(x["from"], {}).get("generate_wargoal_tension", 0) <= level]
    prep.sort(key=lambda x: not c[x["from"]].get("major"))
    return out + prep[:PREPARE_CANDIDATES]


def ratio(balance_text: str) -> float:
    """The strength ratio ultimatums.balance() wrote ('about 1.4 to 1'); 1.0 if it can't be read."""
    m = re.search(r"about ([\d.]+) to 1", balance_text)
    return float(m.group(1)) if m else 1.0


# ---------------------------------------------------------------- carrying out a decision

def declare(camp: campaign.Campaign, a: str, b: str) -> bool:
    """`a` declares war on `b` by script, now (the game's AI, merely allowed, may never strike: Germany sat out 90
    days allowed on Czechoslovakia). The war goal: the states of b's that a claims or holds as cores, else
    everything. True once the two are at war. An ultimatum of a's to b that ran out unmet ends in this war."""
    from . import tension, ultimatums
    states = [f[1] for f in (line.split("|") for line in gamestate.query(
        f'{b} = {{ every_owned_state = {{ limit = {{ OR = {{ is_claimed_by = {a} is_core_of = {a} }} }} '
        f'log = "AIRPWS|[THIS.GetID]" }} }}')) if f[0] == "AIRPWS" and len(f) > 1]
    goal = f"type = take_state_focus generator = {{ {' '.join(states)} }}" if states else "type = annex_everything"
    if not (actions._effect(f"{a} = {{ declare_war_on = {{ target = {b} {goal} }} }}")
            and bridge.trigger(f"{a} = {{ has_war_with = {b} }}")):
        return False
    tension.war(a, b)                              # declare_war_on skips the game's own tension for a war
    for u in ultimatums._store(camp):
        if u["from"] == a and u["to"] == b and u["status"] == "ready":
            ultimatums._close(camp, u, "war", "it declared war, the deadline long past")
            ultimatums._stand_down(camp, u, wargoal=False)
    return True


def decide(camp: campaign.Campaign, x: Dict, step: str, why: str) -> Tuple[str, str]:
    """Carry out one decision (Python's rules first). Returns (the step taken, a line for the log)."""
    from . import actions, diplomacy
    a, b = x["from"], x["to"]
    c = (camp.snapshot or {}).get("countries", {})
    name = lambda t: c.get(t, {}).get("name", t)
    today = camp.data.get("last_date") or _today(camp)
    st = _store(camp)
    asked = step
    if step == "war" and (x["kind"] != "war" or ratio(x.get("balance", "")) < MIN_RATIO):
        step = "wait"
    if step == "ultimatum" and not x.get("ultimatum_ok"):
        step = "wait"
    if step == "prepare" and x["kind"] != "prepare":
        step = "wait"
    st["decisions"].append({"date": today, "from": a, "to": b, "step": step, "why": why})
    if step == "war":
        if declare(camp, a, b):
            return step, f"{name(a)} declared war on {name(b)}"
        st["decisions"][-1]["step"] = "wait"
        return "wait", f"{name(a)} waits with {name(b)} (it wanted war; the game refused the declaration)"
    if step == "ultimatum":
        from . import storyteller
        aim = x["aim"]
        brief = {"kind": "ultimatum", "aim": aim["aim"], "aim_kind": "land", "target": b, "why": why}
        storyteller._state(camp)["incidents"].append({"date": today, "kind": "ultimatum", "from": a, "to": b,
                                                      "aim": aim["aim"], "why": why, "slot": "war"})
        diplomacy._state(camp)["queue"].append([a, b, "ultimatum", [aim["aim"]], brief])
        return step, f"{name(a)} sets {name(b)} an ultimatum before any war"
    if step == "prepare":
        # conquer is what makes the AI justify (vanilla ends such plans once it has_wargoal_against the target);
        # prepare_for_war readies its armies
        actions._effect(f"{a} = {{ add_to_array = {{ {ARRAY} = {b} }} "
                        f"add_ai_strategy = {{ type = conquer id = {b} value = 200 }} "
                        f"add_ai_strategy = {{ type = prepare_for_war id = {b} value = 100 }} }}")
        return step, f"{name(a)} may prepare a war on {name(b)} (justify a war goal)"
    return step, f"{name(a)} waits with {name(b)}" + (f" (it wanted {asked}; not possible now)" if asked != step else "")
