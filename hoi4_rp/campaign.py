"""Per-campaign memory: the world chronicle and every letter, stored on disk and dated in game time.

A campaign is one playthrough. Its id lives in the game itself as the global variable airp_campaign,
so it's saved with the save file: loading any save of a playthrough finds the same memory.
Memory is dated in game time. If the game date goes backwards (an earlier save was loaded), anything
dated after it never happened in this timeline and is dropped.

Files: data/campaigns/<id>.json (git-ignored).

Relationships are stored as pairs, so that any two countries can have them, not only a country and the player
(SCHEMA 2; older campaigns are converted on load):
    letters["AUS|FRA"]        the correspondence of two countries (pair(): tags sorted), each letter with "from"
    policies["FRA>AUS"]       France's policy toward Austria (toward())
    treaties                  "with" the side that wrote the deal, "me" the other ("give" = with gives me)
    ultimatums                "from" and "to";  refusals: "from" (who proposed) and "to" (who refused)
    reputation deeds          "by": the country whose deed it is
    guarantees ("by", "of"), steering (per country, each push with the country it is about), events ("tags")
                              were pairs already
The accessors (letters_with, policy_of, ...) take the other side, the player by default. The reply window's state
(pending_offer, treaty_due, sent) is the player's alone.
"""

from __future__ import annotations

import json
import os
import random
import re
from typing import Dict, List, Optional

from . import bridge

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "campaigns")
ID_VARIABLE = "global.airp_campaign"

SCHEMA = 2


def pair(a: str, b: str) -> str:
    """The key of what two countries share, in either order: 'AUS|FRA'."""
    return "|".join(sorted([a, b]))


def toward(tag: str, other: str) -> str:
    """The key of what one country holds toward another: 'FRA>AUS' (France toward Austria)."""
    return f"{tag}>{other}"


_MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September",
           "October", "November", "December"]


def date_key(date_text: str) -> str:
    """'12:00, 1 January, 1936' -> '1936.01.01.12', which sorts in time order."""
    m = re.search(r"(\d+):\d+,\s*(\d+)\s+(\w+),?\s+(\d+)", date_text)
    if not m:
        return ""
    hour, day, month, year = m.groups()
    mon = _MONTHS.index(month) + 1 if month in _MONTHS else 0
    return f"{int(year):04d}.{mon:02d}.{int(day):02d}.{int(hour):02d}"


def save_key(raw: str) -> str:
    """Save-format date '1939.9.1.12' -> '1939.09.01.12'."""
    try:
        y, m, d, h = (int(x) for x in (raw.split(".") + ["0"] * 4)[:4])
        return f"{y:04d}.{m:02d}.{d:02d}.{h:02d}"
    except ValueError:
        return ""


def shift(key: str, days: int) -> str:
    """The date key `days` later (negative: earlier)."""
    import datetime
    try:
        y, m, d, h = (int(x) for x in key.split(".")[:4])
        t = datetime.date(y, m, d) + datetime.timedelta(days=days)
        return f"{t.year:04d}.{t.month:02d}.{t.day:02d}.{h:02d}"
    except ValueError:
        return ""


def date_words(key: str) -> str:
    """'1936.01.01.12' -> '1 January 1936'."""
    try:
        y, m, d = key.split(".")[:3]
        return f"{int(d)} {_MONTHS[int(m) - 1]} {int(y)}"
    except (ValueError, IndexError):
        return key


class Campaign:
    def __init__(self, cid: int, player: str):
        self.id = cid
        self.path = os.path.join(DATA_DIR, f"{cid}.json")
        self.data: Dict = {"id": cid, "player": player, "last_date": "", "snapshot": None,
                           "events": [], "letters": {}}
        if os.path.exists(self.path):
            with open(self.path, encoding="utf-8") as f:
                self.data.update(json.load(f))
        self.data["player"] = player
        self._migrate()
        if "refusals" not in self.data:
            self.data["refusals"] = self._past_refusals()

    def _migrate(self) -> None:
        """Campaigns from before relationships were pairs: everything was between a country and the player."""
        d, p = self.data, self.data["player"]
        if d.get("schema", 1) >= SCHEMA:
            return
        d["letters"] = {(k if "|" in k else pair(k, p)): v for k, v in d.get("letters", {}).items()}
        if "policies" in d:
            d["policies"] = {(k if ">" in k else toward(k, p)): v for k, v in d["policies"].items()}
        for t in d.get("treaties", []):
            t.setdefault("me", p)
        for u in d.get("ultimatums", []):
            u.setdefault("to", p)
        for r in d.get("refusals", []):
            r.setdefault("to", p)
        for x in (d.get("reputation") or {}).get("deeds", []):
            x.setdefault("by", p)
        d["schema"] = SCHEMA

    def _past_refusals(self) -> List[Dict]:
        """Campaigns from before refusals were stored: rebuild them from 'declined' answers and the
        proposal on that country's latest letter before each one."""
        out = []
        for e in self.data["events"]:
            if e.get("type") != "offer_answer" or " declined " not in e["text"] or not e["tags"]:
                continue
            tag = e["tags"][0]
            letters = [x for x in self.letters_with(tag) if x.get("proposal") or x.get("offer")]
            letters = [x for x in letters if x["date"] <= e["date"]]
            if not letters:
                continue
            p = letters[-1].get("proposal") or f"give:{letters[-1]['offer']}"
            out.append({"from": tag, "to": self.data["player"], "keys": p if isinstance(p, list) else [p],
                        "date": e["date"]})
        return out

    # ------------------------------------------------------------ persistence
    def save(self) -> None:
        os.makedirs(DATA_DIR, exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)

    # ------------------------------------------------------------ timeline
    def at(self, key: str) -> bool:
        """Move to game date `key`. Returns True if time went backwards (an earlier save was loaded):
        memories from that moment on are dropped and the next snapshot starts a fresh baseline. Dated in hours,
        so what happened in the save's own hour is dropped too: it is mostly what a replay of that hour redoes."""
        last = self.data.get("last_date") or ""
        rewound = bool(last and key and key < last)
        if rewound:
            self.data["events"] = [e for e in self.data["events"] if e["date"] < key]
            for tag, letters in self.data["letters"].items():
                self.data["letters"][tag] = [x for x in letters if x["date"] < key]
            self.data["policies"] = {t: p for t, p in self.data.get("policies", {}).items() if p["date"] < key}
            self.data["refusals"] = [r for r in self.data.get("refusals", []) if r["date"] < key]
            rep = self.data.get("reputation")
            if rep:                                    # deeds and promises after the loaded save never happened
                rep["deeds"] = [d for d in rep.get("deeds", []) if d["date"] < key]
                rep["promises"] = [x for x in rep.get("promises", []) if x["date"] < key]
                rep["threats"] = [x for x in rep.get("threats", []) if x["date"] < key]
            self.data["surrender"] = {t: [x for x in h if x[0] < key]
                                      for t, h in self.data.get("surrender", {}).items()}
            from . import agendas, crises, plots, diplomacy, guarantees, news, steering, story, storyteller, treaties, ultimatums
            agendas.drop_after(self, key)
            crises.drop_after(self, key)
            plots.drop_after(self, key)
            storyteller.drop_after(self, key)
            news.drop_after(self, key)
            story.drop_after(self, key)
            treaties.drop_after(self, key)
            ultimatums.drop_after(self, key)
            steering.drop_after(self, key)
            guarantees.drop_after(self, key)
            diplomacy.drop_after(self, key)
            offer = self.data.get("pending_offer")
            if offer and offer["date"] >= key:
                self.data["pending_offer"] = None
            self.data["snapshot"] = None
        if key:
            self.data["last_date"] = key
        return rewound

    # ------------------------------------------------------------ chronicle
    @property
    def snapshot(self) -> Optional[Dict]:
        return self.data.get("snapshot")

    @snapshot.setter
    def snapshot(self, snap: Dict) -> None:
        self.data["snapshot"] = snap

    def record_surrender(self, snap: Dict) -> None:
        """Keep each warring country's surrender progress over time (only changes are stored)."""
        hist = self.data.setdefault("surrender", {})
        for tag, c in snap["countries"].items():
            v = c.get("surrender")
            if v is None:
                continue
            h = hist.setdefault(tag, [])
            if not h or abs(h[-1][1] - v) >= 0.01:
                h.append([snap["date"], round(v, 3)])
                del h[:-100]

    def surrender_at(self, tag: str, key: str) -> Optional[float]:
        """Surrender progress as last recorded on or before `key`."""
        h = [x for x in self.data.get("surrender", {}).get(tag, []) if x[0] <= key]
        return h[-1][1] if h else None

    def add_events(self, events: List[Dict]) -> None:
        self.data["events"].extend(events)

    def memory_of(self, tag: str) -> List[Dict]:
        """Events a country remembers: those it took part in, and world events involving major powers."""
        return [e for e in self.data["events"] if tag in e["tags"] or e.get("major")]

    # ------------------------------------------------------------ letters
    def letters_with(self, tag: str, other: str = "") -> List[Dict]:
        """The correspondence between `tag` and `other` (the player by default)."""
        return self.data["letters"].setdefault(pair(tag, other or self.data["player"]), [])

    def add_letter(self, tag: str, sender: str, text: str, key: str, other: str = "") -> None:
        self.letters_with(tag, other).append({"date": key, "when": date_words(key), "from": sender, "text": text})

    def threads(self, of: str = "") -> Dict[str, List[Dict]]:
        """Every correspondence of `of` (the player by default): the other country -> its letters."""
        of = of or self.data["player"]
        out = {}
        for k, letters in self.data["letters"].items():
            a, _, b = k.partition("|")
            if of in (a, b):
                out[b if a == of else a] = letters
        return out

    # ------------------------------------------------------------ policies
    def policy_of(self, tag: str, other: str = "") -> Optional[Dict]:
        """`tag`'s stored policy toward `other` (the player by default), as stored (see policy.current)."""
        return self.data.setdefault("policies", {}).get(toward(tag, other or self.data["player"]))

    def set_policy(self, tag: str, p: Dict, other: str = "") -> None:
        self.data.setdefault("policies", {})[toward(tag, other or self.data["player"])] = p

    def drop_policy(self, tag: str, other: str = "") -> None:
        self.data.setdefault("policies", {}).pop(toward(tag, other or self.data["player"]), None)

    def policies_toward(self, other: str = "") -> Dict[str, Dict]:
        """Every stored policy toward `other` (the player by default): country -> policy."""
        end = ">" + (other or self.data["player"])
        return {k[:-len(end)]: p for k, p in self.data.get("policies", {}).items() if k.endswith(end)}


def read_id(player: str) -> int:
    from .gamestate import query
    lines = query(f'{player} = {{ log = "AIRPID|[?{ID_VARIABLE}]" }}')
    for line in lines:
        if line.startswith("AIRPID|"):
            try:
                return int(float(line[7:].strip()))
            except ValueError:
                return 0
    return 0


def new_id(player: str) -> int:
    """Give a game without one a campaign id (stored in the game, so it's saved with it)."""
    cid = random.randint(100000, 999999)
    bridge.send(f"eval_effect {player} = {{ set_variable = {{ {ID_VARIABLE} = {cid} }} }}")
    return cid
