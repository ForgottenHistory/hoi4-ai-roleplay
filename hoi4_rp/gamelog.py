"""Read localised game facts (names, leaders, the date) by having script `log` them to game.log.

The bridge's country data only has ids (leaders are character ids), but the log effect formats
text with the game's own localisation, and the game writes each line to disk straight away.
"""

from __future__ import annotations

import itertools
import os
import time
from dataclasses import dataclass
from typing import Optional

from . import bridge

GAME_LOG = os.path.join(os.path.expanduser("~"), "Documents", "Paradox Interactive",
                        "Hearts of Iron IV", "logs", "game.log")

_FIELDS = ["[THIS.GetTag]", "[THIS.GetName]", "[THIS.GetLeader]", "[THIS.GetRulingParty]",
           "[THIS.GetRulingIdeology]", "[GetDateText]"]
_counter = itertools.count(int(time.time()) % 100000)


@dataclass
class CountryInfo:
    tag: str
    name: str
    leader: str
    party: str
    ideology: str
    date: str


def _read_tail(path: str, nbytes: int = 65536) -> str:
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        f.seek(max(0, f.tell() - nbytes))
        return f.read().decode("utf-8", "replace")


def describe(tag: str, timeout: float = 3.0) -> Optional[CountryInfo]:
    """Name, leader, party, ideology and the current date for one country. None if it can't be read."""
    marker = f"AIRPINFO{next(_counter)}"
    # Scope with TAG = { } so THIS is that country (ROOT would be whatever the UI has selected).
    effect = f'{tag} = {{ log = "{marker}|{"|".join(_FIELDS)}" }}'
    replies = bridge.send(f"eval_effect {effect}")
    if not replies or not replies[0].startswith("OK"):
        return None
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for line in reversed(_read_tail(GAME_LOG).splitlines()):
            i = line.find(marker + "|")
            if i >= 0:
                parts = line[i + len(marker) + 1:].split("|")
                if len(parts) >= len(_FIELDS):
                    return CountryInfo(*[p.strip() for p in parts[:len(_FIELDS)]])
        time.sleep(0.1)
    return None
