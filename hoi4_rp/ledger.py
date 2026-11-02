"""The ledger: an in-game window listing what the campaign has recorded, newest first, so the player needn't read
the run log. letter_mod.py builds the window (a button in the top-bar reputation box opens it); letters.py sets its
text (show_ledger) while it's open.

Everything comes from what the campaign already keeps, so a loaded earlier save drops later entries by itself:
    1 Yours        events the player took part in, letters to and from the player, the player's judged deeds
    2 Diplomacy    deals by letter (also between AI countries), treaties, ultimatums, guarantees, crises, news, stories
    3 World        the headlines: wars, peace, capitulations, factions, governments, involving a major power
    4 Reputation   every judged deed, whoever's it is
"""

from __future__ import annotations

from typing import Callable, List, Tuple

from . import campaign

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
              "justification", "focus"}
_HEADLINES = _CHRONICLE - {"territory", "focus"} | {"fork"}  # territory changes come by the dozen in a big war;
                                                              # a fork: a country settling its course (course.py)

# Colour of an event kind's date, so the eye finds the bad news.
_RED = ("treaty:broken", "ultimatum:issued", "ultimatum:war", "war", "capitulation", "crisis", "country_gone")
_GREEN = ("treaty:paid", "treaty:clause", "ultimatum:met", "peace", "independence")


def _chronicle(kind: str) -> bool:
    return kind in _CHRONICLE or kind.startswith("agreement:")


def _colour(kind: str) -> str:
    if kind.startswith(_RED):
        return "R"
    if kind.startswith(_GREEN):
        return "G"
    return "Y"


def entries(camp: campaign.Campaign, player: str, tab: int, name: Callable[[str], str],
            clean: Callable[[str], str]) -> List[Tuple[str, str]]:
    """(date key, text with HOI4 colour codes) for one tab, newest first."""
    out: List[Tuple[str, str]] = []
    for e in camp.data.get("events", []):
        kind = e.get("type", "")
        if "market" in kind.split(":"):         # market access opens and closes by the dozen; nobody needs to know
            continue
        if tab == 1:
            keep = player in e.get("tags", []) and kind != "focus"
        elif tab == 2:
            keep = not _chronicle(kind) and kind != "fork"
        elif tab == 3:
            keep = kind in _HEADLINES and e.get("major")
        else:
            keep = False
        if keep:
            label = {"crisis": "Crisis: ", "story": "Story: "}.get(kind, "")
            out.append((e["date"], f"§{_colour(kind)}{e['when']}§!  {label}{clean(e['text'])}"))

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
