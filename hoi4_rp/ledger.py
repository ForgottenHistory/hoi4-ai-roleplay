"""The ledger: an in-game window listing what the campaign has recorded, newest first, so the player needn't read
the run log. letter_mod.py builds the window (a button in the top-bar reputation box opens it); letters.py sets its
text (show_window) while it's open.

Everything comes from what the campaign already keeps, so a loaded earlier save drops later entries by itself:
    1 Yours        events the player took part in, letters to and from the player, the player's judged deeds
    2 Diplomacy    deals by letter (also between AI countries), treaties, ultimatums, guarantees, crises, news, stories
    3 World        the headlines: wars, peace, capitulations, factions, governments, involving a major power
    4 Reputation   every judged deed, whoever's it is
"""

from __future__ import annotations

import re
from typing import Callable, List, Tuple

from . import campaign, chronicle

TABS = {1: "Yours", 2: "Diplomacy", 3: "World", 4: "Reputation"}
HEADINGS = {
    1: "What concerns you: letters, deals, wars and judged deeds",
    2: "Deals by letter, treaties, ultimatums and crises, between any countries",
    3: "World headlines involving the major powers",
    4: "Every judged deed, whoever's word it was",
}
MAX_ENTRIES = 80        # newest first; older ones are left out
MAX_CHARS = 20000       # the game draws the whole text each frame the window is open: keep it moderate

# Event kinds the world chronicle records from snapshots (chronicle.diff); everything else is the mod's own doing.
_CHRONICLE = {"war", "peace", "territory", "subject", "capitulation", "country_gone", "country_new", "government",
              "leader", "faction_founded", "faction_joined", "faction_left", "independence", "wargoal",
              "justification", "focus", "civil_war", "faction_dissolved"}
_HEADLINES = _CHRONICLE - {"territory", "focus"} | {"fork"}  # territory changes come by the dozen in a big war;
                                                              # a fork: a country settling its course (course.py)

# Colour of an event kind's date, so the eye finds the bad news.
_RED = ("treaty:broken", "ultimatum:issued", "ultimatum:war", "war", "capitulation", "crisis", "country_gone",
        "civil_war")
_GREEN = ("deal", "treaty:paid", "treaty:clause", "ultimatum:met", "peace", "independence")


def _chronicle(kind: str) -> bool:
    return kind in _CHRONICLE or kind.startswith("agreement:")


def _kind_of(line: str) -> str:
    """The kind of a line compact() made of several events."""
    if line.startswith("Civil war broke out"):
        return "civil_war"
    if line.startswith("War broke out"):
        return "war"
    if line.startswith("Peace came") or " no longer at war" in line or "defeated and ceased" in line:
        return "peace"
    return ""


def _colour(kind: str) -> str:
    if kind.startswith(_RED):
        return "R"
    if kind.startswith(_GREEN):
        return "G"
    return "Y"


# Where a clause's subject ends ("Italy and Austria | signed a non-aggression pact")
_VERB = re.compile(r"^(.+?) (signed|opened|granted|guaranteed|committed|agreed|joined|took|ceded|left|gave|paid|"
                   r"promised|will|imposed|lifted|declared|ended|accepted|demilitarized|dismantled|recognized|"
                   r"released|became|sent|allowed|let)\b(.*)$")


def _joined(texts: List[str]) -> str:
    """Clauses with the same subject told once: "A and B signed X and opened Y; B granted A Z"."""
    by: dict = {}
    for t in texts:
        m = _VERB.match(t.rstrip("."))
        subject, rest = (m.group(1), m.group(2) + m.group(3)) if m else (t.rstrip("."), "")
        by.setdefault(subject, []).append(rest)
    parts = []
    for subject, rests in by.items():
        told = [r for r in rests if r]
        if not told:
            parts.append(subject)
            continue
        parts.append(subject + " " + (told[0] if len(told) == 1 else ", ".join(told[:-1]) + " and " + told[-1]))
        parts += [subject for r in rests if not r]
    return "; ".join(parts)


def _deals(events: List[dict], name: Callable[[str], str]) -> List[dict]:
    """A deal by letter is recorded clause by clause ("give:pact", "ask:access", ...), and, when the player was asked,
    once more as "accepted the proposal (...)". One line per deal instead: the clauses of one moment between the
    same two countries, the acceptance left out (a declined proposal stays)."""
    deal = lambda e: e.get("type", "").startswith(("give:", "ask:")) and len(e.get("tags", [])) >= 2
    groups: dict = {}
    for e in events:
        if deal(e):
            groups.setdefault((e["date"], frozenset(e["tags"][:2])), []).append(e)
    out: List[dict] = []
    for e in events:
        key = (e["date"], frozenset(e.get("tags", [])[:2]))
        if deal(e):
            clauses = groups.pop(key, None)
            if clauses is None:                 # told with the first clause
                continue
            a, b = clauses[0]["tags"][:2]
            if len(clauses) == 1:
                out.append(clauses[0])
                continue
            out.append({**clauses[0], "type": "deal", "tags": [a, b] + [t for c in clauses for t in c["tags"][2:]],
                        "text": "Deal: " + _joined([c["text"] for c in clauses]) + "."})
        elif e.get("type") == "offer_answer" and " accepted the proposal" in e["text"] and                 any(deal(x) and (x["date"], frozenset(x["tags"][:2])) == key for x in events):
            continue
        else:
            out.append(e)
    return out


def entries(camp: campaign.Campaign, player: str, tab: int, name: Callable[[str], str],
            clean: Callable[[str], str]) -> List[Tuple[str, str]]:
    """(date key, text with HOI4 colour codes) for one tab, newest first."""
    out: List[Tuple[str, str]] = []
    told: dict = {}                             # date -> the world chronicle's events kept, told together
    for e in _deals(chronicle.retell(camp.data.get("events", [])), name):
        kind = e.get("type", "")
        if "market" in kind.split(":"):         # market access opens and closes by the dozen; nobody needs to know
            continue
        if e.get("part"):                       # told by its civil war or dissolution line
            continue
        if tab == 1:
            keep = player in e.get("tags", []) and kind != "focus"
        elif tab == 2:
            keep = not _chronicle(kind) and kind != "fork"
        elif tab == 3:
            keep = kind in _HEADLINES and (e.get("major") or kind == "civil_war")
        else:
            keep = False
        if keep and (_chronicle(kind) or kind == "fork"):
            told.setdefault(e["date"], []).append(e)
        elif keep:
            label = {"crisis": "Crisis: ", "story": "Story: "}.get(kind, "")
            out.append((e["date"], f"§{_colour(kind)}{e['when']}§!  {label}{clean(e['text'])}"))

    # One moment's war and peace pairs, and countries doing the same, as a few lines (as in the prompts)
    for date, group in told.items():
        kinds = {e["text"]: e["type"] for e in group}
        for when, line in chronicle.compact(group, camp.snapshot):
            out.append((date, f"§{_colour(kinds.get(line) or _kind_of(line))}{when}§!  {clean(line)}"))

    if tab == 1:
        for other, letters in camp.threads(player).items():
            for x in letters:
                if x["from"] == player:
                    line = f"You wrote to {name(other)}."
                else:
                    line = f"{name(other)} wrote to you" + (", with a proposal." if x.get("proposal") else ".")
                out.append((x["date"], f"§Y{x['when']}§!  §g{line}§!"))

    if tab in (1, 4):
        for d in (camp.data.get("reputation") or {}).get("deeds", []):
            w = d.get("weight")
            if w is None or (tab == 1 and d.get("by", player) != player):
                continue
            mark = f"§{'G' if w > 0 else 'R' if w < 0 else 'Y'}{w:+d}§!"
            who = "" if tab == 1 else f"{name(d.get('by', player))}: "
            out.append((d["date"], f"§Y{d['when']}§!  {mark} {who}{clean(d.get('verdict') or d.get('facts', ''))}"))

    if tab == 3:
        for p in (camp.data.get("plots") or {}).get("list", []):
            when = campaign.date_words(p.get("updated") or p["date"])
            out.append((p.get("updated") or p["date"],
                        f"§Y{when}§!  Plotline §Y{clean(p['title'])}§! ({p.get('stage', '')}): {clean(p.get('so_far', ''))}"))

    out.sort(key=lambda x: x[0], reverse=True)
    return out


def text(camp: campaign.Campaign, player: str, tab: int, name: Callable[[str], str],
         clean: Callable[[str], str]) -> Tuple[str, str]:
    """(heading, body) of the ledger window for one tab."""
    tab = tab if tab in TABS else 1
    found = entries(camp, player, tab, name, clean)
    lines, size = [], 0
    for _, line in found[:MAX_ENTRIES]:
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
