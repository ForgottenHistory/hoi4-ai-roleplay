"""Plotlines: the story the player is playing, written down so that the storyteller can answer it.

The player's story is in their own letters, their promises, the deeds the reputation record weighs and the treaties
they keep. Every PLOT_DAYS or so one LLM call reads those and writes the plotlines the player is running (at most
MAX_PLOTS): what each is, its goal, the countries it involves, the stage it has reached and what has happened so far.
The storyteller (the monthly pick) and the crisis pick read them as the player's story, and the world answers it as a
game master would: it builds the conditions, the chances and the obstacles, and never the outcome, which belongs to
the countries and to the player. The plotlines are plain data in the campaign save, yours to read or edit.

    due(camp)              time to (re)write the plotlines?
    messages(camp, snap)   the LLM prompt
    parse(text)            -> [{"title", "tags", "stage", "goal", "so_far"}]
    keep(camp, found, now) merge them into the stored ones (a plot already there keeps its id and date)
    context(camp, name)    prompt text for the storyteller, '' while there is no plotline
    tags(camp)             the countries the open plotlines involve

Plain lines from the LLM (no JSON):
    PLOT: title | WITH: TAG, TAG | STAGE: setup / rising / ripe / done | GOAL: ... | SO FAR: ...

Stored in the campaign: data["plots"] = {"tick": date of the last try, "list": [{"id", "date", "when", "updated",
"title", "tags", "stage", "goal", "so_far"}]}.
"""

from __future__ import annotations

import difflib
import re
from typing import Dict, List

from . import campaign

PLOT_DAYS = 120
MAX_PLOTS = 3
MIN_LETTERS = 3               # the player's own letters it takes before there is a story to read
LETTERS_SHOWN = 40
STAGES = ("setup", "rising", "ripe", "done")
MATCH = 0.6                   # how like a stored title a plot's title must be to be the same plot


def _store(camp: campaign.Campaign) -> Dict:
    st = camp.data.setdefault("plots", {})
    st.setdefault("tick", "")
    st.setdefault("list", [])
    return st


def drop_after(camp: campaign.Campaign, key: str) -> None:
    """An earlier save was loaded: plotlines born after it never happened."""
    st = _store(camp)
    st["list"] = [x for x in st["list"] if x["date"] < key]
    st["tick"] = st["tick"] if st["tick"] < key else ""


def _letters(camp: campaign.Campaign) -> List[Dict]:
    player = camp.data["player"]
    out = [dict(x, to=k.replace(player, "").strip("|")) for k, ls in camp.data["letters"].items()
           if player in k.split("|") for x in ls if x["from"] == player]
    return sorted(out, key=lambda x: x["date"])


def due(camp: campaign.Campaign) -> bool:
    st, today = _store(camp), (camp.snapshot or {}).get("date", "")
    if not today or len(_letters(camp)) < MIN_LETTERS:
        return False
    return not st["tick"] or campaign.shift(st["tick"], PLOT_DAYS) <= today


def tags(camp: campaign.Campaign) -> List[str]:
    """The countries the open plotlines involve, the most recent plotline first."""
    out = []
    for x in reversed(_store(camp)["list"]):
        if x["stage"] != "done":
            out += [t for t in x["tags"] if t not in out]
    return out


def messages(camp: campaign.Campaign, snap: Dict) -> List[Dict]:
    """The LLM messages for writing the plotlines."""
    from . import reputation, treaties, world
    c = snap.get("countries", {})
    name = lambda t: c.get(t, {}).get("name", t)
    player = camp.data["player"]
    now = snap.get("date", "")
    letters = _letters(camp)[-LETTERS_SHOWN:]
    rep = camp.data.get("reputation") or {}
    deeds = [d for d in rep.get("deeds", []) if abs(d.get("weight", 0)) >= 2][-8:]
    promises = rep.get("promises", [])[-12:]
    active = [t for t in treaties._store(camp) if t["status"] == "active" and t["me"] == player][-10:]
    parts = [
        world.text({c[player].get("ideology")} if c.get(player, {}).get("ideology") else set()),
        f"You are the storyteller of this Hearts of Iron IV world. {name(player)} is played by a human, who roleplays "
        f"in letters to the other governments. Read what they have done and said, and write down the plotlines they "
        f"are playing: the arcs of their story (a redemption, a rivalry, a union, a revenge), so that the world can "
        f"answer them. The date is {campaign.date_words(now)}.",
        "THE PLAYER'S OWN LETTERS (oldest first):\n" + "\n".join(
            f"- {x['when']}, to {name(x['to'])}: {x['text'][:320]}" for x in letters),
    ]
    replies = []                                        # how the other side last answered, per country written to
    for t in dict.fromkeys(x["to"] for x in reversed(letters)):
        got = [x for x in camp.letters_with(t) if x["from"] == t][-2:]
        replies += [f"- {x['when']}, {name(t)}: {x['text'][:360]}" for x in got]
        if len(replies) >= 14:
            break
    if replies:
        parts.append("HOW THEY ANSWERED (the latest replies; the truth of where each government stands):\n"
                     + "\n".join(replies))
    if promises:
        parts.append("PROMISES THEY MADE:\n" + "\n".join(f"- {x['when']}, to {name(x['to'])}: {x['text']}" for x in promises))
    if deeds:
        parts.append("DEEDS THAT WEIGHED ON THEIR REPUTATION:\n" + "\n".join(
            f"- {x['when']} ({x['weight']:+d}): {x['verdict']}" for x in deeds))
    if active:
        parts.append("TREATIES THEY KEEP:\n" + "\n".join(f"- {x['when']}: {x['text']}" for x in active))
    world_events = [e for e in camp.data["events"] if e.get("major") and e["date"] <= now][-6:]
    if world_events:
        parts.append("RECENT WORLD EVENTS:\n" + "\n".join(f"- {e['when']}: {e['text'][:240]}" for e in world_events))
    old = [x for x in _store(camp)["list"] if x["stage"] != "done"]
    if old:
        parts.append("PLOTLINES YOU WROTE BEFORE (keep the title of one that goes on, change its stage and what has "
                     "happened; use stage done when it has ended):\n" + "\n".join(
                         f"- {x['title']} | WITH: {', '.join(x['tags'])} | STAGE: {x['stage']} | GOAL: {x['goal']} | "
                         f"SO FAR: {x['so_far']}" for x in old))
    named = list(dict.fromkeys([x["to"] for x in letters] + [x["to"] for x in promises]
                                + [t for x in active for t in (x["with"],)]))
    parts.append("COUNTRIES (tag = name; use these tags in WITH): " + ", ".join(f"{t} = {name(t)}" for t in named))
    parts.append(
        f"Write at most {MAX_PLOTS} plotlines, only those the player really plays: a goal they have stated and "
        f"worked toward more than once, not a passing remark. Put the countries it involves (never {player}) in WITH. "
        f"Stages: setup (the goal is stated, little done), rising (deeds and talks are building toward it), ripe "
        f"(the world is ready for the next step), done. Write none if there is no story yet.\n"
        f"Answer with one line per plotline and nothing else:\n"
        f"PLOT: title of at most 8 words | WITH: TAG, TAG | STAGE: one of {', '.join(STAGES)} | GOAL: at most 20 words | "
        f"SO FAR: at most 40 words, what has happened")
    return [{"role": "system", "content": "\n\n".join(parts)}, {"role": "user", "content": "Write the plotlines now."}]


_PLOT = re.compile(r"^[\W_]*PLOT[*_\s]*[:\-–]+\s*(.+)$", re.I | re.M)


def parse(raw: str) -> List[Dict]:
    """The plotlines in the model's lines, leniently: each needs a title and a goal."""
    out = []
    for m in _PLOT.finditer(raw):
        parts = [p.strip() for p in m.group(1).split("|")]
        field = lambda key: next((re.sub(r"^[\W_]*" + key + r"[*_\s]*[:\-–]+\s*", "", p, flags=re.I).strip(" *_")
                                  for p in parts[1:] if re.match(r"^[\W_]*" + key.replace(" ", r"[ _]") + r"\b", p, re.I)),
                                 "")
        title = re.sub(r"\s+", " ", parts[0]).strip(" *_.")
        stage = next((s for s in STAGES if s in field("stage").lower()), "setup")
        found = [t for t in (re.sub(r"[^A-Za-z0-9]", "", x).upper() for x in re.split(r"[,/&\s]+", field("with"))) if t]
        goal, so_far = field("goal"), field("so far")
        if title and goal:
            out.append({"title": title, "tags": found, "stage": stage, "goal": goal, "so_far": so_far})
    return out[:MAX_PLOTS]


def keep(camp: campaign.Campaign, found: List[Dict], now: str) -> List[Dict]:
    """Merge the plotlines into the stored ones; returns those that are new or changed. A tag that is not a country
    in the game, or the player, is dropped. A stored plot the model no longer lists stays as it was."""
    st = _store(camp)
    c = (camp.snapshot or {}).get("countries", {})
    player = camp.data["player"]
    changed = []
    for f in found:
        f["tags"] = [t for t in dict.fromkeys(f["tags"]) if t in c and t != player]
        if not f["tags"]:
            continue
        same = max(st["list"], key=lambda x: difflib.SequenceMatcher(None, x["title"].lower(), f["title"].lower()).ratio(),
                   default=None)
        if same and difflib.SequenceMatcher(None, same["title"].lower(), f["title"].lower()).ratio() >= MATCH:
            if any(same[k] != f[k] for k in ("tags", "stage", "goal", "so_far")):
                same.update(f, updated=now)
                changed.append(same)
        else:
            st["list"].append({"id": len(st["list"]) + 1, "date": now, "when": campaign.date_words(now),
                               "updated": now, **f})
            changed.append(st["list"][-1])
    return changed


def context(camp: campaign.Campaign, name) -> str:
    """For the storyteller's prompts: the open plotlines of the player, with how the world should answer."""
    open_ = [x for x in _store(camp)["list"] if x["stage"] != "done"]
    if not open_:
        return ""
    player = name(camp.data["player"])
    lines = [f"- {x['title']} ({x['stage']}) with {', '.join(name(t) for t in x['tags'])}. Goal: {x['goal']} "
             f"So far: {x['so_far']}" for x in open_]
    return (f"THE STORY {player.upper()}'S PLAYER IS PLAYING (their plotlines). The world answers it as a game master "
            f"would: build the conditions, the chances and the obstacles it would meet, in the lives of the other "
            f"countries, and never settle its outcome, which belongs to the countries and to the player. Never "
            f"mention the player's country in a crisis:\n" + "\n".join(lines))


def context_for(camp: campaign.Campaign, tag: str, name) -> str:
    """For a country's own prompts (its agenda): the plotlines it is part of, as news of what the player's country is
    after. The country owes the player nothing: it weighs the plot for its own sake."""
    mine = [x for x in _store(camp)["list"] if x["stage"] != "done" and tag in x["tags"]]
    if not mine:
        return ""
    player = name(camp.data["player"])
    lines = [f"- {x['title']} ({x['stage']}). Goal: {x['goal']} So far: {x['so_far']}" for x in mine]
    return (f"WHAT {player.upper()} IS AFTER, AND HOW IT INVOLVES YOU (what its government has been working toward, as "
            f"the world sees it):\n" + "\n".join(lines) + f"\nWeigh it as your government would, for your own sake, "
            f"and owe {player} nothing: it may serve your interests, or threaten them. Let your aims answer it "
            f"(befriend, back, oppose, or, only if your government truly wants it for its own reasons, unite with "
            f"{player}); an aim may also simply keep your distance.")


def main(args: List[str]) -> int:
    """python -m hoi4_rp plots [--write]: the stored plotlines of the running campaign; --write has the model rewrite
    them now (kept)."""
    from . import chronicle, gamestate, llm
    player = gamestate.player_tag()
    camp = campaign.Campaign(campaign.read_id(player), player)
    camp.snapshot = snap = chronicle.snapshot(player)
    if "--write" in args:
        raw = llm.chat(messages(camp, snap), None, 0.7, 700, "ai:plots", "")
        found = parse(raw)
        print(f"{len(found)} plotlines read; {len(keep(camp, found, snap['date']))} new or changed\n")
        camp.save()
    for x in _store(camp)["list"]:
        print(f"[{x['id']}] {x['title']} ({x['stage']}) with {', '.join(x['tags'])}\n    goal: {x['goal']}\n"
              f"    so far: {x['so_far']}\n")
    return 0
