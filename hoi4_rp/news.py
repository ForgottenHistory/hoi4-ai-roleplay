"""News events: what the storyteller's incidents came to, as a newspaper popup in the player's game.

When an incident between two other countries is settled (diplomacy.record, mode = on) the chronicle gets an event of
type "news". This module turns the latest one into a news event: a headline, a short report and one button, pure
flavour with no effect, shown with the game's own newspaper frame (mod_files.NEWS_PICTURES). Its ingredients are
rolled in code, not left to the model, so that reports differ in who is writing and how:

    outlet   who reports it (wire service, a paper at home, a tabloid, the foreign ministry's organ...)
    tone     how it is written
    length   short / medium / long

    due(camp)               the news worth a popup now, or None (off, too soon, nothing new)
    messages(camp, ev)      the LLM prompt for one report
    parse(text)             -> Draft (title, picture, body, button)

Plain lines from the LLM (no JSON):
    title: ...   picture: <key>   body: ...   reaction: the button's text

Stored in the campaign: data["news"] = {"told": date of the news last reported, "shown": [{"date", "title"}]}.
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass
from typing import Dict, List, Optional

from . import campaign, game, world
from .config import settings
from .mod_files import NEWS_PICTURES, NEWS_PICTURE_KEYS

FRESH_DAYS = 45            # news older than this is not worth a popup
MAX_TITLE = 34             # characters: longer headlines overflow the popup's banner

_OUTLETS = [
    ("an international wire service, neutral and clipped", 4),
    ("the player's own national newspaper, reporting what its embassy in the capital cabled home", 4),
    ("a sensationalist tabloid", 2),
    ("the government's own official newspaper, careful what it implies", 2),
    ("an opposition paper that distrusts its government's foreign policy", 2),
    ("a dry financial and trade journal, interested in what it means for markets and arms", 1),
]
_TONES = [
    ("a straight, sober report", 4),
    ("dry and factual", 3),
    ("wry and understated, the reporter clearly unimpressed", 3),
    ("grave and solemn", 2),
    ("breathless and excitable", 1),
]
# (instruction, weight, body cap in characters)
_LENGTHS = [("2 sentences, 180-260 characters", 4, 330), ("2-3 sentences, 220-320 characters", 4, 380),
            ("3 sentences, 300-400 characters, room for a short quote", 2, 450)]


def _roll(options):
    return random.choices([o[0] for o in options], weights=[o[1] for o in options], k=1)[0]


def _news(camp: campaign.Campaign) -> Dict:
    n = camp.data.setdefault("news", {})
    n.setdefault("told", "")
    n.setdefault("shown", [])
    return n


def _days(key: str) -> int:
    import datetime
    try:
        y, m, d = (int(x) for x in key.split(".")[:3])
        return datetime.date(y, m, d).toordinal()
    except ValueError:
        return 0


def due(camp: campaign.Campaign) -> Optional[Dict]:
    """The chronicle event the next news event is about, or None: news off, the last was too recent, nothing new."""
    n = _news(camp)
    today = camp.data.get("last_date") or ""
    cfg = settings().story
    if not cfg.news or not today:
        return None
    if n["shown"] and _days(today) - _days(n["shown"][-1]["date"]) < cfg.news_gap_days:
        return None
    fresh = campaign.shift(today, -FRESH_DAYS)
    found = [e for e in camp.data["events"] if e.get("type") in ("news", "crisis") and e["date"] > n["told"]
             and e["date"] >= fresh and camp.data["player"] not in e["tags"]]
    return found[-1] if found else None


def messages(camp: campaign.Campaign, ev: Dict, me_name: str, ideologies: List[str]) -> List[Dict[str, str]]:
    from . import storyteller
    c = (camp.snapshot or {}).get("countries", {})
    name = lambda t: c.get(t, {}).get("name", t)
    length, _, _ = random.choices(_LENGTHS, weights=[w for _, w, _ in _LENGTHS], k=1)[0]
    pictures = "\n".join(f"  {k}: {d}" for k, _, d in NEWS_PICTURES)
    earlier = [f"- {x['when']}: {x['title']}" for x in _news(camp)["shown"][-5:]]
    blocs = storyteller.blocs_text(camp, name)
    system = (
        f"{world.text(ideologies)}\n\n"
        f"You are the editor writing ONE news event for a Hearts of Iron IV game: the newspaper popup that tells "
        f"the player of {me_name} that something happened in the world. It is flavour only; it has no effect on "
        f"the game.\n\n"
        f"WHAT HAPPENED ({ev['when']}), the facts, which you must not contradict or inflate:\n{ev['text']}\n\n"
        f"HOW THE WORLD LINES UP (aims lining up, for background only):\n{blocs}\n\n"
        + ("EARLIER HEADLINES (do not repeat them):\n" + "\n".join(earlier) + "\n\n" if earlier else "")
        + f"THE REPORT\n- Reported by: {_roll(_OUTLETS)}\n- Tone: {_roll(_TONES)}\n- Body length: {length}\n\n"
        f"WHAT MAKES IT GOOD\n- Grounded and specific beats vague: a named minister, an ambassador, a city, a "
        f"quoted line. Invent small details but keep to the facts above. One human or odd detail is the hook; play it "
        f"completely straight.\n- It is news from abroad as {me_name}'s readers get it: what happened, and why it "
        f"matters to a country watching from the side.\n- Broadcast-safe: no slurs, no sexual content.\n\n"
        f"FORMAT RULES\n- title: a SHORT headline, 2-5 words, at most {MAX_TITLE} characters, Title Case, no full "
        f"stop.\n- body: one paragraph within the length above, past tense, starting with the place in capitals, "
        f"for example \"PARIS - \".\n- reaction: the button the player clicks, 2-7 words, a dry in-world reaction "
        f"(\"Noted in the ministry.\").\n- picture: the ONE best key:\n{pictures}\n- Plain text only: no $ [ ] § £ or "
        f"markup.\n\nOutput EXACTLY these four labelled parts, nothing before or after:\n"
        f"title: <headline>\npicture: <picture key>\nbody: <the report>\nreaction: <button text>"
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": "Write the news event."}]


@dataclass
class Draft:
    title: str
    body: str
    button: str
    picture: str


_SENTENCE = re.compile(r'[^.!?]+(?:[.!?]+["\')]*|$)')


def _trim(body: str, cap: int) -> str:
    """Keep whole sentences up to `cap` characters (the model runs long)."""
    if len(body) <= cap:
        return body
    out = ""
    for s in (x for x in _SENTENCE.findall(body) if x.strip()):
        if len(out) + len(s) > cap and out:
            break
        out += s
    return out.strip()


def parse(raw: str, clean) -> Optional[Draft]:
    """The reply as a Draft, or None when it has no title or body. `clean` makes text safe for the game."""
    parts: Dict[str, List[str]] = {}
    key = ""
    for line in (raw or "").splitlines():
        s = line.strip().lstrip("*-# ").strip()
        m = re.match(r"(title|picture|body|reaction)\s*\**\s*:\s*(.*)$", s, re.I)
        if m:
            key = m.group(1).lower()
            parts.setdefault(key, []).append(m.group(2).strip())
        elif s and key in ("", "title", "picture", "body"):       # an unlabelled paragraph is the body
            key = "body"
            parts.setdefault("body", []).append(s)
    one = lambda k: " ".join(parts.get(k, [])).strip().strip('"*').strip()
    title = clean(one("title")).rstrip(".")
    body = _trim(clean(" ".join(parts.get("body", []))), 450)
    if not title or not body:
        return None
    if len(title) > MAX_TITLE:
        title = title[:MAX_TITLE + 1].rsplit(" ", 1)[0].rstrip(",;:-")
    pic = one("picture").lower().strip(" .'`")
    if pic not in NEWS_PICTURE_KEYS:
        pic = next((k for k in NEWS_PICTURE_KEYS if k in pic), "press")
    return Draft(title, body, (clean(one("reaction")) or "Noted.")[:60], pic)


def show(camp: campaign.Campaign, draft: Draft, told: str) -> None:
    """Open the news event and remember it. `told`: the date of the chronicle event it reports."""
    game.show_news(draft.title, draft.body, draft.button, draft.picture)
    n = _news(camp)
    key = camp.data.get("last_date") or told
    n["told"] = told
    n["shown"].append({"date": key, "when": campaign.date_words(key), "title": draft.title})
    del n["shown"][:-20]
    camp.save()


def drop_after(camp: campaign.Campaign, key: str) -> None:
    """An earlier save was loaded: news from that moment on never happened."""
    n = camp.data.get("news")
    if not n:
        return
    n["shown"] = [x for x in n["shown"] if x["date"] < key]
    if n["told"] >= key:
        n["told"] = key                              # what happened before the loaded save is not reported again
