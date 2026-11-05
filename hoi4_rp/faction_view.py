"""The faction window: an in-game view of the world's factions and of what this mod does with them. letter_mod.py
builds the window (a button in the top-bar reputation box opens it); letters.py sets its text (show_factions) while
it's open.

    1 Factions    every faction: leader, members, wars, its share of the world's industry against the size limit
                  (factions.cap), major powers, the faction DLC's joining rules
    2 Yours       the player's faction; which AI factions could take the player in by letter (factions.room); what
                  each country's policy would offer or ask about factions
    3 Prospects   what the storyteller finds possible (factions.candidates: join, found, break) and the blocs whose
                  aims line up (storyteller.blocs)
    4 History     factions founded, joined, left and dissolved, deals by letter about factions, the storyteller's moves

Tabs 1 and 2 ask the game (industry, world tension, joining rules) once per game day; tab 3 works out the possible
moves once per game week (agendas, threats, the focus trees), so the window is cheap while it's open.
"""

from __future__ import annotations

from typing import Callable, Dict, List, Optional, Tuple

from . import campaign, chronicle, factions, gamestate

TABS = {1: "Factions", 2: "Yours", 3: "Prospects", 4: "History"}
HEADINGS = {
    1: "Every faction, its members and how large it may grow",
    2: "Your faction, and where you could stand",
    3: "Who might join, found or break a faction, and the blocs taking shape",
    4: "How the factions changed: founded, joined, left, broken",
}
MAX_ENTRIES = 80
MAX_CHARS = 20000
PROSPECT_DAYS = 7               # the possible moves are worked out again after this many game days

# What a policy toward the player would do about factions (policy.py: offers, asks, breaks)
_WISHES = {
    "give:faction": "would take you into its faction",
    "give:join": "would join your faction",
    "give:found": "would found a faction with you",
    "ask:faction": "wants a place in your faction",
    "ask:join": "wants you to join its faction",
    "ask:found": "wants you to found a faction with it",
    "give:leave": "would leave its faction for your sake",
    "ask:leave": "wants you to leave your faction",
    "end:faction": "means to break with you",
}
_HISTORY = {"faction_founded", "faction_joined", "faction_left", "faction_dissolved", "faction:break"} | set(_WISHES)
_STANCE_COLOUR = {"friendly": "G", "cordial": "G", "indifferent": "Y", "wary": "R", "hostile": "R"}

_cache: Dict[str, Tuple[str, object]] = {}     # what -> (game date it was worked out, value)


def _cached(what: str, date: str, make: Callable[[], object], days: int = 1) -> object:
    got = _cache.get(what)
    if got and got[0] and date and (got[0] == date or (days > 1 and got[0] <= date < campaign.shift(got[0], days))):
        return got[1]
    value = make()
    _cache[what] = (date, value)
    return value


def forget() -> None:
    """A game loaded or the country changed: work everything out again."""
    _cache.clear()


def _industry() -> Dict[str, float]:
    """{tag: civilian + military factories}, one query."""
    out: Dict[str, float] = {}
    num = lambda x: float(x) if x.strip().replace(".", "", 1).isdigit() else 0.0
    for line in gamestate.query('every_country = { limit = { exists = yes } log = "AIRPFV|[THIS.GetTag]|'
                                '[?THIS.num_of_civilian_factories]|[?THIS.num_of_military_factories]" }'):
        f = [x.strip() for x in line.split("|")]
        if f[0] == "AIRPFV" and len(f) >= 4:
            out[f[1]] = num(f[2]) + num(f[3])
    return out


def _tension(player: str) -> float:
    from . import world
    try:
        return world.tension([player])[0]
    except Exception:
        return 0.0


def _rule_names(rules: List[str]) -> List[str]:
    try:
        texts = gamestate.loc(*rules) if rules else []
    except gamestate.StateError:
        texts = list(rules)
    return [t if t and t != r else r.replace("_", " ") for r, t in zip(rules, texts)]


def _live(camp: campaign.Campaign, player: str) -> Dict:
    """What tabs 1 and 2 ask the game: industry, world tension, each AI faction's joining rules."""
    facs = factions.world_factions(camp)
    leaders = [f["leader"] for f in facs.values() if f["leader"] and f["leader"] != player]
    try:
        industry = _industry()
    except gamestate.StateError:
        industry = {}
    rules = factions.active_rules(leaders)
    return {"industry": industry, "tension": _tension(player),
            "rules": {L: _rule_names(r) for L, r in rules.items()}}


def _list(names: List[str]) -> str:
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1] if names else ""


def _enemies(snap: Dict, members: List[str]) -> List[str]:
    return sorted(set().union(*(factions._enemies(snap, m) for m in members)) - set(members)) if members else []


# ---------------------------------------------------------------- 1 Factions

def _factions_tab(camp: campaign.Campaign, player: str, name: Callable[[str], str]) -> List[str]:
    snap = camp.snapshot or {}
    c = snap.get("countries", {})
    live = _cached("live", snap.get("date", ""), lambda: _live(camp, player))
    power, level = live["industry"], live["tension"]
    total = sum(power.values()) or 1
    cap = factions.cap(level)
    facs = factions.world_factions(camp)
    share = lambda tags: sum(power.get(t, 0) for t in tags) / total
    majors = lambda tags: sum(1 for t in tags if c.get(t, {}).get("major"))
    from . import gates
    who = ("the storyteller moves the factions between AI countries" if gates.ours("factions")
           else "HOI4's AI moves the factions between AI countries, beside the deals made by letter")
    out = [f"World tension §Y{round(level * 100)}%§!: a faction led by an AI country holds at most §Y{cap:.0%}§! of the "
           f"world's industry and {factions.MAJORS} major powers (a country already at war beside it may join "
           f"regardless). The limit is {factions.SHARE_CALM:.0%} in a calm world and {factions.SHARE_TENSE:.0%} at "
           f"full tension. In this game {who}."]
    rows = sorted(facs.items(), key=lambda kv: -share(kv[1]["members"]))
    for fname, f in rows:
        L, members = f["leader"], f["members"]
        s, m = share(members), majors(members)
        mine = player in members
        if L == player:
            size = f"§Y{s:.0%}§! of the world's industry, {m} major power{'s' if m != 1 else ''} (no limit: you lead it)"
        else:
            bar = "R" if s >= cap else "Y" if s >= cap * 0.75 else "G"
            size = (f"§{bar}{s:.0%}§! of the world's industry of {cap:.0%}, {m} of {factions.MAJORS} major powers"
                    + (" §R(full)§!" if s >= cap else " §Y(no more major powers)§!" if m >= factions.MAJORS else ""))
        ideology = c.get(L, {}).get("ideology", "")
        lines = [f"§Y{fname}§!" + (" §G(yours)§!" if mine else ""),
                 f"Led by {name(L)}" + (f" ({ideology})" if ideology else "") + f"; {size}"]
        others = [name(t) for t in members if t != L]
        lines.append(f"Members: {_list(others)}" if others else "§gNo other members yet.§!")
        foes = _enemies(snap, members)
        if foes:
            lines.append(f"§RAt war with§! {_list([name(t) for t in foes])}")
        rules = live["rules"].get(L)
        if rules:
            lines.append(f"Joining rules: {', '.join(rules)}")
        out.append("\n".join(lines))
    if not rows:
        out.append("§gThere are no factions in the world.§!")
    loose = [name(t) for t, x in sorted(c.items()) if x.get("major") and not x.get("faction")]
    if loose:
        out.append(f"§gMajor powers in no faction:§! {_list(loose)}")
    return out


# ---------------------------------------------------------------- 2 Yours

def _policy(camp: campaign.Campaign, p: Dict) -> bool:
    """A full policy toward the player in force: not one only a stance (left by an AI exchange), nor from after the
    game date (an earlier save loaded). Read, never dropped: this runs beside the main loop."""
    from . import policy
    return bool(p) and "policy" in p and not p.get("light") and not policy._future(camp, p)


def _rooms(camp: campaign.Campaign, player: str) -> Dict[str, str]:
    """{leader: why the player can't join its faction by letter ('' if they can)}, for every AI-led faction."""
    level = _tension(player)
    return {f["leader"]: factions.room(f["leader"], player, level)
            for f in factions.world_factions(camp).values() if f["leader"] and f["leader"] != player}


def _yours_tab(camp: campaign.Campaign, player: str, name: Callable[[str], str]) -> List[str]:
    snap = camp.snapshot or {}
    c = snap.get("countries", {})
    me = c.get(player, {})
    facs = factions.world_factions(camp)
    out = []
    policies = {t: p for t, p in camp.policies_toward(player).items() if _policy(camp, p)}
    stance = lambda t: policies.get(t, {}).get("stance", "")
    coloured = lambda s: f"§{_STANCE_COLOUR.get(s, 'Y')}{s}§!" if s else "§gno policy yet§!"
    if me.get("faction"):
        f = facs.get(me["faction"], {"leader": me.get("faction_leader", ""), "members": [player]})
        role = "lead" if f["leader"] == player else "are a member of"
        lines = [f"You {role} §Y{me['faction']}§!." + ("" if f["leader"] == player else f" Its leader: {name(f['leader'])}.")]
        for t in f["members"]:
            if t != player:
                lines.append(f"  {name(t)}: {coloured(stance(t))} toward you")
        foes = _enemies(snap, f["members"])
        if foes:
            lines.append(f"§RAt war with§! {_list([name(t) for t in foes])}")
        lines.append("§gYou lead it, so it may grow without limit.§!" if f["leader"] == player else
                     "§gIts leader is an AI country, so the faction size limit binds it (see Factions): a newcomer "
                     "by letter must fit.§!")
        out.append("\n".join(lines))
    else:
        out.append("You are in no faction.")

    if not me.get("faction"):
        rooms = _cached("rooms", snap.get("date", ""), lambda: _rooms(camp, player))
        lines = ["§YFactions that could take you in by letter§!"]
        for fname, f in facs.items():
            L = f["leader"]
            if not L or L == player or player in f["members"]:
                continue
            why = rooms.get(L, "")
            lines.append(f"  {fname} ({name(L)}): " + (f"§Rno§!, {why}" if why else "§Gthere is room§!"))
        if len(lines) > 1:
            lines.append("§gWhether its leader wants you is another matter: write to it. The game's own Join button "
                         "keeps the game's rules.§!")
            out.append("\n".join(lines))

    wishes = []
    for t, p in sorted(policies.items(), key=lambda kv: name(kv[0])):
        said = [_WISHES[k] for k in p.get("offers", []) + p.get("asks", []) + p.get("breaks", []) if k in _WISHES]
        if said:
            wishes.append(f"  {name(t)} ({coloured(p.get('stance', ''))}): {'; '.join(said)}")
    out.append("§YWhat their policies toward you say about factions§!\n" + ("\n".join(wishes) if wishes else
               "  §gNo country's policy offers or asks anything about factions now.§!"))
    return out


# ---------------------------------------------------------------- 3 Prospects

def _prospects(camp: campaign.Campaign) -> List[Dict]:
    from . import diplomacy
    return factions.candidates(camp, diplomacy.world())


def _prospects_tab(camp: campaign.Campaign, player: str, name: Callable[[str], str],
                   clean: Callable[[str], str]) -> List[str]:
    from . import gates, storyteller
    from .config import settings
    snap = camp.snapshot or {}
    if gates.ours("factions"):
        ai = settings().ai
        note = ("The storyteller picks at most one of these a month (factions = ours)." if ai.mode == "on"
                and ai.driver == "storyteller" else "factions = ours needs [ai_diplomacy] mode = on and driver = "
                "storyteller for the storyteller to make these moves.")
    else:
        note = ("HOI4's AI moves the factions in this game (factions = game): these are the moves the storyteller "
                "would weigh with factions = ours.")
    out = [f"§g{note} World tension and the deal rules are checked when a move is made. Worked out once a game "
           f"week.§!"]
    try:
        cands = _cached("prospects", snap.get("date", ""), lambda: _prospects(camp), PROSPECT_DAYS)
    except Exception as e:                       # the agendas or the game not ready: say so, try again next week
        cands = []
        out.append(f"§gThe possible moves could not be worked out now ({type(e).__name__}).§!")
    reach = {"near": "neighbours", "present": "holds land nearby", "far": "from across the world"}
    for x in cands:
        a, b = name(x["a"]), name(x["b"])
        if x["move"] == "join":
            head = f"§GJoin§!  {a} into {x['faction']}, led by {b}"
        elif x["move"] == "found":
            head = f"§GFound§!  {a} and {b}, a new faction led by {a}"
        else:
            head = f"§RBreak§!  {a} and {x['faction']}, led by {b}"
        out.append(f"{head} §g({reach.get(x['reach'], x['reach'])})§!\n  " + clean("; ".join(x["stake"])))
    if not cands:
        out.append("§gNo faction move is possible now.§!")
    rows = storyteller.blocs(camp)
    if rows:
        out.append("§YBlocs§!  countries whose aims or commitments point the same way: the seeds of factions\n" +
                   "\n".join(f"  {'§RAgainst§!' if b['stance'] == 'against' else '§GFor§!'} {name(b['target'])}: "
                             f"{_list([name(m) for m in b['members']])}" for b in rows[:20]))
    return out


# ---------------------------------------------------------------- 4 History

def _history_tab(camp: campaign.Campaign, player: str, name: Callable[[str], str],
                 clean: Callable[[str], str]) -> List[str]:
    from . import storyteller
    rows: List[Tuple[str, str]] = []
    for e in chronicle.retell(camp.data.get("events", [])):
        kind = e.get("type", "")
        if kind not in _HISTORY or e.get("part"):
            continue
        colour = "R" if kind in ("faction_left", "faction_dissolved", "faction:break", "end:faction") else "G"
        by_letter = "§gby letter§! " if kind in _WISHES else ""
        rows.append((e["date"], f"§{colour}{e['when']}§!  {by_letter}{clean(e['text'])}"))
    for x in storyteller._state(camp)["incidents"]:
        if x.get("slot") != "faction" or x["kind"] == "break":     # a break is told as it happened
            continue
        what = "joining a faction" if x["kind"] == "join" else "founding a faction together"
        why = f": {clean(x['why'])}" if x.get("why") else ""
        rows.append((x["date"], f"§Y{campaign.date_words(x['date'])}§!  §gThe storyteller brought {name(x['from'])} "
                                f"and {name(x['to'])} to talks about {what}§!{why}"))
    rows.sort(key=lambda r: r[0], reverse=True)
    return [r[1] for r in rows]


def text(camp: campaign.Campaign, player: str, tab: int, name: Callable[[str], str],
         clean: Callable[[str], str]) -> Tuple[str, str]:
    """(heading, body) of the faction window for one tab."""
    tab = tab if tab in TABS else 1
    if tab == 1:
        found = _factions_tab(camp, player, name)
    elif tab == 2:
        found = _yours_tab(camp, player, name)
    elif tab == 3:
        found = _prospects_tab(camp, player, name, clean)
    else:
        found = _history_tab(camp, player, name, clean)
    lines, size = [], 0
    for line in found[:MAX_ENTRIES]:
        if size + len(line) > MAX_CHARS:
            break
        lines.append(line)
        size += len(line) + 2
    left = len(found) - len(lines)
    head = f"§Y{TABS[tab]}§!  {HEADINGS[tab]}"
    if not lines:
        return head, "§gNothing recorded here yet.§!"
    body = "\n\n".join(lines)
    if left:
        body += f"\n\n§g{left} older entries not shown.§!"
    return head, body


def main(args: List[str]) -> int:
    """python -m hoi4_rp factions [TAB]: the faction window's text for one tab (1-4), colour codes kept."""
    player = gamestate.player_tag()
    camp = campaign.Campaign(campaign.read_id(player), player)
    camp.snapshot = chronicle.snapshot(player)
    tab = int(args[0]) if args and args[0].isdigit() else 1
    names = (camp.snapshot or {}).get("countries", {})
    head, body = text(camp, player, tab, lambda t: names.get(t, {}).get("name", t), lambda s: s)
    print(head + "\n\n" + body)
    return 0
