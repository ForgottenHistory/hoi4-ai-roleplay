"""Show LLM-written events in the running game and act on what the player picks.

    ev = RPEvent("A Quiet Word", "The minister lingers after the meeting...", "conference", [
        Option("Hear him out.", "He will owe us.", "add_political_power = 25"),
        Option("Dismiss him.", "Stability will suffer.", "add_stability = -0.02"),
    ])
    slot = show(ev)                 # popup is open now
    n = wait_for_pick(slot)         # blocks until the player clicks; 1-based option number
    run_effect(ev.options[n - 1].effect)

Effects run through the console's `eval_effect`, which compiles normal effect script, so anything a
mod event could do is possible. Callers that take effects from the LLM must vet them first.
"""

from __future__ import annotations

import itertools
import re
import threading
import time
from dataclasses import dataclass, field
from typing import List, Optional

from . import bridge
from .mod_files import (MAX_OPTIONS, NEWS_PICTURE_KEYS, NEWS_SLOTS, PICTURE_KEYS, SLOTS, event_id, loc_key,
                        news_event_id, news_loc_key, option_flag, pick_var)


@dataclass
class Option:
    text: str
    tooltip: str = ""
    effect: str = ""   # effect script run in the player's country scope after the pick, e.g. "add_stability = 0.05"


@dataclass
class RPEvent:
    title: str
    body: str
    picture: str
    options: List[Option] = field(default_factory=list)


class BridgeError(RuntimeError):
    pass


def _one_line(text: str) -> str:
    return text.replace("\r", "").replace("\n", "\\n")


def _check(replies: List[str], what: str) -> None:
    bad = [r for r in replies if not r.startswith("OK")]
    if bad:
        raise BridgeError(f"{what}: " + " | ".join(bad))


def run_effect(effect: str, tag: Optional[str] = None) -> None:
    """Run effect script in a country's scope (the player's by default)."""
    if not effect.strip():
        return
    tag = tag or bridge.player_tag()
    # Wrapping in TAG = { } pins the scope: bare eval_effect runs on whatever is selected in the UI.
    script = " ".join(effect.split())
    _check(bridge.send(f"eval_effect {tag} = {{ {script} }}"), "eval_effect")


_slot_cycle = itertools.cycle(range(1, SLOTS + 1))
_lock = threading.Lock()


def show(event: RPEvent) -> int:
    """Fill the next slot with the event's text and open it. Returns the slot number."""
    if not 1 <= len(event.options) <= MAX_OPTIONS:
        raise ValueError(f"events need 1..{MAX_OPTIONS} options")
    if event.picture not in PICTURE_KEYS:
        raise ValueError(f"unknown picture {event.picture!r}; pick one of {PICTURE_KEYS}")
    tag = bridge.player_tag()
    with _lock:
        slot = next(_slot_cycle)
        cmds = [f"bridge.setloc {loc_key(slot, 't')} {_one_line(event.title)}",
                f"bridge.setloc {loc_key(slot, 'd')} {_one_line(event.body)}"]
        flags = [f"set_variable = {{ {pick_var(slot)} = 0 }}"]
        for n in range(1, MAX_OPTIONS + 1):
            opt = event.options[n - 1] if n <= len(event.options) else None
            if opt:
                cmds.append(f"bridge.setloc {loc_key(slot, f'o{n}')} {_one_line(opt.text)}")
                # bridge.setloc refuses empty text: a non-breaking space shows nothing
                cmds.append(f"bridge.setloc {loc_key(slot, f'o{n}.tt')} {_one_line(opt.tooltip) or chr(160)}")
            flags.append(f"{'set' if opt else 'clr'}_country_flag = {option_flag(slot, n)}")
        cmds.append(f"eval_effect {tag} = {{ {' '.join(flags)} }}")
        # Open the popup only once its text and options are in place, so a failure never shows a half-made event.
        _check(bridge.send(*cmds), "show")
        _check(bridge.send(f"event {event_id(slot, event.picture)}"), "show")
    return slot


_news_cycle = itertools.cycle(range(1, NEWS_SLOTS + 1))


def show_news(title: str, body: str, button: str, picture: str) -> None:
    """Open a news event: a newspaper popup with one button and no effect."""
    if picture not in NEWS_PICTURE_KEYS:
        raise ValueError(f"unknown news picture {picture!r}; pick one of {NEWS_PICTURE_KEYS}")
    with _lock:
        slot = next(_news_cycle)
        _check(bridge.send(f"bridge.setloc {news_loc_key(slot, 't')} {_one_line(title)}",
                           f"bridge.setloc {news_loc_key(slot, 'd')} {_one_line(body)}",
                           f"bridge.setloc {news_loc_key(slot, 'a')} {_one_line(button)}"), "news")
        _check(bridge.send(f"event {news_event_id(slot, picture)}"), "news")


def read_pick(slot: int, tag: Optional[str] = None) -> int:
    """0 while the popup is unanswered, else the 1-based option the player clicked."""
    block = bridge.country_block(tag or bridge.player_tag())
    m = re.search(rf"\b{pick_var(slot)}=(-?[\d.]+)", block)
    return int(float(m.group(1))) if m else 0


def wait_for_pick(slot: int, timeout: Optional[float] = None, poll: float = 0.5) -> Optional[int]:
    """Block until the player answers the slot's popup. None on timeout."""
    tag = bridge.player_tag()
    deadline = None if timeout is None else time.monotonic() + timeout
    while deadline is None or time.monotonic() < deadline:
        n = read_pick(slot, tag)
        if n:
            return n
        time.sleep(poll)
    return None
