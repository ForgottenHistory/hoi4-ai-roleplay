"""Story events: the player's own country reacts to what the player did through this mod.

Other mods and the base game already tell their own stories, so these events only come from what nothing else
knows about: deals made by letter (land, factories, subjects, governments, peace, agreements) and the player's
deeds as the reputation game master judged them (wars started, guarantees kept or dropped, peaces made).

    moments(camp, player)   recent moments worth an event, most telling first
    due(camp, player)       the moment for the next event, or None (setting off, too soon, nothing telling)
    messages(...)           the LLM prompt for one event
    parse(text)             -> Draft (title, picture, text, summary, options with their effects)
    show(draft) / apply()   open it in the game; apply the option the player picked

The LLM picks each option's effects from a short list of small effects every HOI4 mod has (political power,
stability, war support, a party's popularity, the other country's opinion). Python clamps them, writes the
tooltip from them (so it always says what happens) and runs them after the pick. Many options change nothing:
what was chosen is remembered in the chronicle, where the other country reads it before its next letter.

Plain lines from the LLM, parsed leniently (no JSON):
    TITLE: ...
    PICTURE: one of mod_files.PICTURE_KEYS
    TEXT: the event text (may run over several lines)
    SUMMARY: one sentence for the chronicle
    OPTION: what the government does [[stability -2]] [[pp +25]]

Stored in the campaign: data["story"] = {"last_moment": date, "events": [...], "pending": {...} or None}.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from . import actions, bridge, campaign, chronicle, game, reputation, world
from .config import settings
from .mod_files import PICTURES, PICTURE_KEYS, pick_var

FRESH_DAYS = 45            # a moment older than this is old news: no event about it
MAX_OPTIONS = 3
MAX_EFFECTS = 2            # per option
# Largest change one effect may make (political power, percent of stability / war support / popularity).
LIMITS = {"pp": 50, "stability": 5, "war_support": 5, "popularity": 5}
PP_STEPS = [10, 25, 50]
OPINION_STEPS = [-10, -5, 5, 10]

# How telling each kind of letter deal is (event types from actions.apply); unlisted kinds count 1.
_DEAL_WEIGHTS = [
    (3, ("give:state", "ask:state", "cede:", "give:annex", "give:submit", "ask:submit", "give:subject",
         "ask:subject", "subject:", "give:govt", "ask:govt", "give:peace")),
    (2, ("give:civ", "ask:civ", "give:mil", "ask:mil", "give:dismantle", "ask:dismantle", "give:dmz", "ask:dmz",
         "give:rights", "ask:rights", "embargo")),
]
# Plain agreements (pacts, guarantees, access, docking, market) count 1: no event of their own, they make dull news.
_DEALS = ("give:", "ask:", "end:", "cede:", "subject:", "embargo")


# ---------------------------------------------------------------- what an event can be about

@dataclass
class Moment:
    date: str
    when: str
    other: str          # the country it concerns ("" if none)
    weight: int         # 1..3, how telling
    text: str           # what happened, for the prompt
    kind: str           # "deal" or "deed"


def _story(camp: campaign.Campaign) -> Dict:
    s = camp.data.setdefault("story", {})
    s.setdefault("last_moment", "")
    s.setdefault("events", [])
    s.setdefault("pending", None)
    return s


def _deal_weight(kind: str) -> int:
    return next((w for w, prefixes in _DEAL_WEIGHTS if kind.startswith(prefixes)), 1)


def moments(camp: campaign.Campaign, player: str, since: str = "") -> List[Moment]:
    """Moments after `since`, most telling first (then newest). Deals with one country on one day are one moment."""
    deals: Dict[Tuple[str, str], List[Dict]] = {}
    for e in camp.data["events"]:
        if e["date"] <= since or player not in e["tags"] or not e.get("type", "").startswith(_DEALS):
            continue
        other = next((t for t in e["tags"] if t != player), "")
        deals.setdefault((e["date"], other), []).append(e)
    out = [Moment(date, group[0]["when"], other, max(_deal_weight(e["type"]) for e in group),
                  "A deal made by letter: " + " ".join(e["text"] for e in group), "deal")
           for (date, other), group in deals.items()]
    for d in reputation._rep(camp)["deeds"]:
        if d["date"] <= since or not d.get("weight"):
            continue                                   # not judged yet, or nothing to do with honour
        other = next((t for t in d["tags"] if t != player), "")
        out.append(Moment(d["date"], d["when"], other, 3 if abs(d["weight"]) >= 2 else 2,
                          f"{d['facts']} The impartial verdict on it: {d['verdict']}", "deed"))
    return sorted(out, key=lambda m: (m.weight, m.date), reverse=True)


def due(camp: campaign.Campaign, player: str) -> Optional[Moment]:
    """The moment the next event is about, or None: events off, one is still open, the last was too recent, or
    nothing telling happened since."""
    cfg = settings().story
    s = _story(camp)
    today = camp.data.get("last_date") or ""
    if not cfg.events or s["pending"] or not today:
        return None
    if s["events"] and _days(today) - _days(s["events"][-1]["date"]) < cfg.gap_days:
        return None
    fresh = campaign.shift(today, -FRESH_DAYS)
    found = [m for m in moments(camp, player, s["last_moment"]) if m.date >= fresh and m.weight >= 2]
    if not found:
        return None
    # What else happened on the same day belongs to the same story (one attack brings several deeds).
    best = found[0]
    same = [m for m in found[1:] if m.date == best.date][:4]
    if same:
        best = Moment(best.date, best.when, best.other, best.weight,
                      best.text + " At the same moment: " + " ".join(m.text for m in same), best.kind)
    return best


# ---------------------------------------------------------------- effects

@dataclass
class Effect:
    kind: str            # pp | stability | war_support | popularity | opinion
    value: int
    ideology: str = ""   # popularity only


@dataclass
class Option:
    text: str
    effects: List[Effect] = field(default_factory=list)


@dataclass
class Draft:
    title: str
    picture: str
    text: str
    summary: str
    options: List[Option]


_EFFECT = re.compile(r"\[\[\s*(pp|political[ _]power|stability|war[ _]support|opinion|popularity)\s*"
                     r"([a-z_]+)?\s*([+-]?\d+)\s*%?\s*\]\]", re.I)


def _clamp(kind: str, value: int) -> int:
    if kind == "pp":
        step = min(PP_STEPS, key=lambda s: abs(s - abs(value)))
        return step if value > 0 else -step if value < 0 else 0
    if kind == "opinion":
        return min(OPINION_STEPS, key=lambda s: (abs(s - value), -abs(s))) if value else 0
    return max(-LIMITS[kind], min(LIMITS[kind], value))


def _effects(text: str, ideologies: List[str]) -> List[Effect]:
    out: List[Effect] = []
    for m in _EFFECT.finditer(text):
        kind = m.group(1).lower().replace(" ", "_")
        kind = "pp" if kind == "political_power" else kind
        ideology = (m.group(2) or "").lower()
        if kind == "popularity" and ideology not in ideologies:
            continue
        value = _clamp(kind, int(m.group(3)))
        if value and kind not in {e.kind for e in out}:
            out.append(Effect(kind, value, ideology if kind == "popularity" else ""))
    return out[:MAX_EFFECTS]


def tooltip(option: Option, other_name: str, party_names: Dict[str, str]) -> str:
    """What the option does, in the game's usual words. Empty for an option that changes nothing."""
    parts = []
    for e in option.effects:
        if e.kind == "pp":
            parts.append(f"Political power: {e.value:+d}")
        elif e.kind == "stability":
            parts.append(f"Stability: {e.value:+d}%")
        elif e.kind == "war_support":
            parts.append(f"War support: {e.value:+d}%")
        elif e.kind == "popularity":
            parts.append(f"{party_names.get(e.ideology, e.ideology)} popularity: {e.value:+d}%")
        elif e.kind == "opinion":
            parts.append(f"{other_name}'s opinion of us: {e.value:+d}")
    return "\n".join(parts) or "No direct effect, but it will be remembered."


def apply(option: Option, player: str, other: str) -> List[str]:
    """Run the option's effects. Returns what was done, for the console."""
    script, done = [], []
    for e in option.effects:
        if e.kind == "pp":
            script.append(f"add_political_power = {e.value}")
        elif e.kind == "stability":
            script.append(f"add_stability = {e.value / 100:.2f}")
        elif e.kind == "war_support":
            script.append(f"add_war_support = {e.value / 100:.2f}")
        elif e.kind == "popularity":
            script.append(f"add_popularity = {{ ideology = {e.ideology} popularity = {e.value / 100:.2f} }}")
        elif e.kind == "opinion" and other:
            if actions._effect(f"{other} = {{ add_opinion_modifier = {{ target = {player} "
                               f"modifier = {actions.opinion_modifier(e.value)} }} }}"):
                done.append(f"opinion {e.value:+d}")
            continue
        done.append(f"{e.kind} {e.value:+d}")
    if script:
        try:
            game.run_effect(" ".join(script), player)
        except game.BridgeError as err:
            print(f"  story effect failed: {err}")
            return []
    return done


# ---------------------------------------------------------------- the prompt

_VOICE = {
    "serious": "Write like a Hearts of Iron IV event: grounded, period-appropriate prose, specific people and places.",
    "dramatic": "Write like a Hearts of Iron IV event, a notch more colourful: vivid characters, strong feelings, a "
                "little exaggeration, but never slapstick.",
    "players": "Write like a Hearts of Iron IV event, in plain modern English: clear, lively, easy to read.",
}


def _effects_help(other_name: str, parties: List[Tuple[str, str]]) -> str:
    party_list = ", ".join(f"{ideo} ({name})" for ideo, name in parties)
    return (
        "EFFECTS an option may carry, written after its text (at most two per option; small, since this is flavour, "
        "not a game changer):\n"
        "  [[pp N]]              political power, N from -50 to +50 (10, 25 or 50)\n"
        "  [[stability N]]       stability in percent, N from -5 to +5\n"
        "  [[war_support N]]     war support in percent, N from -5 to +5\n"
        f"  [[popularity IDEOLOGY N]]   a party's popularity in percent, -5 to +5; IDEOLOGY is one of: {party_list}\n"
        + (f"  [[opinion N]]         {other_name}'s opinion of us: -10, -5, +5 or +10\n" if other_name else "")
        + "Make the choice a real one: an option that gains something usually costs something else, and options "
          "pull different ways (order against freedom, prestige against money, friendship against pride). Give every "
          "option at least one effect. Nothing else changes in "
          "the game, so don't promise more in the option text than its effects do: an option is how the government "
          "handles it at home (a speech, a crackdown, a reward, a quiet word, letting it pass), never a deal, a "
          "treaty, a transfer of land or factories, or moving troops. Those happen only through letters."
    )


def messages(camp: campaign.Campaign, me, them, moment: Moment, relation: str) -> List[Dict[str, str]]:
    """`me`/`them` are gamestate.CountryState (them may be None); `relation` the letters' summary between them."""
    other_name = them.name if them else ""
    parties = [(p.ideology, p.long_name or p.name) for p in me.parties]
    letters = []
    if them:
        for x in camp.letters_with(them.tag)[-6:]:
            who = me.name if x["from"] == me.tag else them.name
            letters.append(f"--- {x['when']}, {who} wrote:\n{x['text'][:1000]}")
    rep = reputation._rep(camp)
    promises = [f"- {x['when']}: {x['text']}" for x in rep["promises"] if them and x["to"] == them.tag][-6:]
    threats = [f"- {x['when']}: {x['text']} ({x['status']})" for x in rep["threats"] if them and x["to"] == them.tag][-4:]
    recent = [e for e in camp.data["events"] if me.tag in e["tags"]
              and e["date"] >= campaign.shift(moment.date, -365)][-40:]
    memory = "\n".join(f"- {w}: {t}" for w, t in chronicle.compact(recent, camp.snapshot)) or "Nothing recorded."
    earlier = [f"- {x['when']}: {x['title']}: {x['summary']} Chosen: {x['picked']}" for x in _story(camp)["events"][-6:]]
    record = reputation.record(camp, me.tag, me.name)
    pictures = "\n".join(f"  {key}: {what}" for key, _, what in PICTURES)
    context = (
        f"{world.text([p.ideology for p in me.parties + (them.parties if them else [])])}\n\n"
        f"THE PLAYER'S COUNTRY:\n{_describe(me)}\n\n"
        + (f"THE OTHER COUNTRY:\n{_describe(them, short=True)}\n\nBETWEEN THEM: {relation}\n\n" if them else "")
        + (f"LETTERS BETWEEN THEM (latest):\n" + "\n\n".join(letters) + "\n\n" if letters else "")
        + (f"PROMISES {me.name.upper()} MADE TO {other_name.upper()}:\n" + "\n".join(promises) + "\n\n" if promises else "")
        + (f"THREATS {me.name.upper()} MADE TO {other_name.upper()} (and whether they came true):\n"
           + "\n".join(threats) + "\n\n" if threats else "")
        + (f"{me.name.upper()}'S REPUTATION ABROAD:\n{record}\n\n" if record else "")
        + f"WHAT HAPPENED IN THE LAST YEAR:\n{memory}\n\n"
        + (f"EARLIER EVENTS LIKE THIS ONE (don't repeat them):\n" + "\n".join(earlier) + "\n\n" if earlier else "")
    )
    task = (
        f"You are the game master of a Hearts of Iron IV game. Write one national event for {me.name}, the player's "
        f"country, growing out of this moment ({moment.when}):\n{moment.text}\n\n"
        f"The event is a DECISION, not a news report. The player already knows what happened; don't retell it. "
        f"Find the conflict it causes at home in {me.name}: someone specific (a named minister, general, party, "
        f"union, the industrialists, the church, the officer corps, the people of a region or town) wants something "
        f"from the government because of it, and someone else wants the opposite or will pay the price. Two examples, "
        f"only to show the shape (don't reuse them; find the conflict this moment would really cause): officers "
        f"resent a treaty that may drag them into war and demand the fall of the minister who signed it; the old "
        f"inhabitants of newly gained land riot against the officials sent to run it. Keep to what is plausible for "
        f"{me.name} under its current government; invent the small human details (names, places, a quote).\n"
        f"The text sets the scene in a sentence or two, then puts the conflict and the demand plainly, and ends on "
        f"the question the government must answer. Each option is a concrete act that takes a side (\"Dismiss the "
        f"minister\", \"Arrest the ringleaders\", \"Buy them off from the treasury\"), not a mood or a statement, and "
        f"each has its price. \"Do nothing\" is only an option if doing nothing is itself a gamble. "
        f"{_VOICE.get(settings().letters.style, _VOICE['serious'])}\n\n"
        f"{_effects_help(other_name, parties)}\n\n"
        f"PICTURES (pick the one that fits best):\n{pictures}\n\n"
        "Answer in exactly this form, plain text, no markdown:\n"
        "TITLE: a short event title, at most 6 words\n"
        "PICTURE: one picture key from the list\n"
        "TEXT: the event text, 50 to 100 words\n"
        f"SUMMARY: one sentence for the history books: the conflict in {me.name} and who wanted what (without the choice)\n"
        "OPTION: the first response, at most 12 words, then its effects\n"
        "OPTION: the second response, then its effects\n"
        "OPTION: (optional) a third response"
    )
    return [{"role": "system", "content": context + task},
            {"role": "user", "content": f"Write the event for {me.name}."}]


def _describe(s, short: bool = False) -> str:
    from . import gamestate
    return gamestate.describe(s, details=not short, focus_details=None if not short else 3,
                              spirit_details=not short)


# ---------------------------------------------------------------- reading the answer

_KEY = re.compile(r"^[\W_]*(title|picture|text|summary|option)[*_\s]*\d*[*_\s]*[:\-–]\s*(.*)$", re.I)


def parse(text: str, ideologies: List[str]) -> Optional[Draft]:
    """The LLM's answer as a Draft, or None if it has no title, text or at least two options."""
    fields: Dict[str, List[str]] = {}
    options: List[str] = []
    key = ""
    for line in text.replace("\r", "").split("\n"):
        m = _KEY.match(line)
        if m:
            key = m.group(1).lower()
            if key == "option":
                options.append(m.group(2).strip())
            else:
                fields[key] = [m.group(2).strip()]
        elif key == "text":
            fields["text"].append(line.strip())
        elif key == "option" and line.strip() and options:
            options[-1] += " " + line.strip()        # effects on the line below the option
    body = "\n".join(fields.get("text", [])).strip()
    body = re.sub(r"\n{3,}", "\n\n", body)
    title = " ".join(fields.get("title", [])).strip(" *\"")
    picture = " ".join(fields.get("picture", [])).strip(" *\"'.").lower()
    if picture not in PICTURE_KEYS:
        picture = next((k for k in PICTURE_KEYS if k in picture), "conference")
    opts = []
    for raw in options[:MAX_OPTIONS]:
        words = re.sub(r"\s{2,}", " ", re.sub(r"\[\[[^\]]*\]\]", "", raw)).strip(" -*\"")
        if words:
            opts.append(Option(words, _effects(raw, ideologies)))
    if not title or not body or len(opts) < 2:
        return None
    summary = " ".join(fields.get("summary", [])).strip(" *")
    if not summary:                                   # the chronicle still needs a line about it
        summary = f"{title}: " + re.split(r"(?<=[.!?])\s", body.replace("\n", " "), maxsplit=1)[0]
    return Draft(title, picture, body, summary, opts)


# ---------------------------------------------------------------- in the game

def show(camp: campaign.Campaign, draft: Draft, player: str, other: str, other_name: str,
         party_names: Dict[str, str], clean) -> None:
    """Open the event and remember it as pending until the player picks. `clean` makes text safe for the game."""
    ev = game.RPEvent(clean(draft.title), clean(draft.text), draft.picture,
                      [game.Option(clean(o.text), tooltip(o, other_name, party_names)) for o in draft.options])
    slot = game.show(ev)
    s = _story(camp)
    key = camp.data.get("last_date") or ""
    s["pending"] = {"slot": slot, "date": key, "when": campaign.date_words(key), "other": other,
                    "title": ev.title, "text": ev.body, "summary": draft.summary, "picture": draft.picture,
                    "options": [{"text": o.text, "effects": [vars(e) for e in o.effects]} for o in draft.options]}
    camp.save()


def read_pick(block: str, slot: int) -> int:
    m = re.search(rf"\b{pick_var(slot)}=(-?[\d.]+)", block)
    return int(float(m.group(1))) if m else 0


def resolve(camp: campaign.Campaign, player: str, n: int, me_name: str) -> None:
    """The player picked option n (1-based) of the pending event: apply it and remember it."""
    s = _story(camp)
    p = s["pending"]
    s["pending"] = None
    if not p or not 1 <= n <= len(p["options"]):
        camp.save()
        return
    chosen = p["options"][n - 1]
    option = Option(chosen["text"], [Effect(**e) for e in chosen["effects"]])
    done = apply(option, player, p["other"])
    s["events"].append({**{k: p[k] for k in ("date", "when", "other", "title", "summary")}, "picked": chosen["text"]})
    key = camp.data.get("last_date") or p["date"]
    tags = [player] + ([p["other"]] if p["other"] else [])
    camp.add_events([{"date": key, "when": campaign.date_words(key), "type": "story", "tags": tags,
                      "text": f"{p['summary']} {me_name}'s government chose: {chosen['text']}".strip(),
                      "major": False}])
    camp.save()
    print(f"  story event \"{p['title']}\": picked \"{chosen['text']}\" -> {', '.join(done) or 'no effect'}")


def drop_after(camp: campaign.Campaign, key: str) -> None:
    """An earlier save was loaded: forget story events from that moment on."""
    s = camp.data.get("story")
    if not s:
        return
    s["events"] = [x for x in s.get("events", []) if x["date"] < key]
    if s.get("pending") and s["pending"]["date"] >= key:
        s["pending"] = None
    s["last_moment"] = min(s.get("last_moment") or "", key) if s.get("last_moment") else ""


def _days(key: str) -> int:
    import datetime
    try:
        y, m, d = (int(x) for x in key.split(".")[:3])
        return datetime.date(y, m, d).toordinal()
    except ValueError:
        return 0
