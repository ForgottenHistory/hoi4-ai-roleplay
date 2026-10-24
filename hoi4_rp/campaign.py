"""Per-campaign memory: the world chronicle and every letter, stored on disk and dated in game time.

A campaign is one playthrough. Its id lives in the game itself as the global variable airp_campaign,
so it's saved with the save file: loading any save of a playthrough finds the same memory.
Memory is dated in game time. If the game date goes backwards (an earlier save was loaded), anything
dated after it never happened in this timeline and is dropped.

Files: data/campaigns/<id>.json (git-ignored).
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
        later memories are dropped and the next snapshot starts a fresh baseline."""
        last = self.data.get("last_date") or ""
        rewound = bool(last and key and key < last)
        if rewound:
            self.data["events"] = [e for e in self.data["events"] if e["date"] <= key]
            for tag, letters in self.data["letters"].items():
                self.data["letters"][tag] = [x for x in letters if x["date"] <= key]
            self.data["policies"] = {t: p for t, p in self.data.get("policies", {}).items() if p["date"] <= key}
            offer = self.data.get("pending_offer")
            if offer and offer["date"] > key:
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

    def add_events(self, events: List[Dict]) -> None:
        self.data["events"].extend(events)

    def memory_of(self, tag: str) -> List[Dict]:
        """Events a country remembers: those it took part in, and world events involving major powers."""
        return [e for e in self.data["events"] if tag in e["tags"] or e.get("major")]

    # ------------------------------------------------------------ letters
    def letters_with(self, tag: str) -> List[Dict]:
        return self.data["letters"].setdefault(tag, [])

    def add_letter(self, tag: str, sender: str, text: str, key: str) -> None:
        self.letters_with(tag).append({"date": key, "when": date_words(key), "from": sender, "text": text})


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
