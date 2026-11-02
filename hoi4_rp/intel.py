"""Intelligence: what another country knows of the player, as far as HOI4's intel system allows ([world] intel).

The game keeps every country's intel over every other, 0-100% in four kinds, readable as script variables:
OBSERVER.civilian_intel@TARGET, and army_intel, navy_intel, air_intel. At the 1936 start it is the target's openness,
the same for every observer (Poland 60% civilian, Britain 40%, the USSR 25%, Germany 0%); agencies, operatives and
networks raise it later. Negative values (counter-intelligence) count as nothing.

With intel = yes, a country dealing with the player sees the player's country through view(): a copy of its state
with what that country's intel doesn't reach left out or estimated. Which kind covers what:

    civilian   factories, dockyards, stability, war support, political power, national focuses
    army       divisions, soldiers, manpower
    navy       sailors
    air        planes, airmen

Numbers are never hidden outright (a government always has some idea of a neighbour's size), only less accurate:
off by up to MAX_ERROR at no intel, exact from EXACT, and a whole kind together (an air force overestimated in planes
is overestimated in airmen too), JITTER more per number. An estimate is the same all game quarter, so it doesn't change
from one letter to the next. Focuses are all or nothing: completed ones known from FOCUS_DONE, the one under way
from FOCUS_NOW.
"""

from __future__ import annotations

import dataclasses
import hashlib
import re
from typing import Dict, Optional

from . import gamestate
from .config import settings

KINDS = ("civilian", "army", "navy", "air")
VARS = {"civilian": "civilian_intel", "army": "army_intel", "navy": "navy_intel", "air": "air_intel"}
FIELD_KIND = {"civ": "civilian", "mil": "civilian", "dock": "civilian",       # population is public: exact
              "divisions": "army", "army_k": "army", "manpower_k": "army",
              "navy_k": "navy", "planes": "air", "air_k": "air"}

EXACT = 0.9          # from this intel, numbers are exact
MAX_ERROR = 0.6      # at no intel, an estimate is off by up to this share
JITTER = 0.1         # each number's own error on top of its kind's (at no intel)
ROUGH = 0.5          # below this, an estimate is called a rough guess
FOCUS_DONE = 0.2     # civilian intel to know the completed national focuses
FOCUS_NOW = 0.5      # civilian intel to know the focus under way
POINTS_ERROR = 0.3   # stability and war support (0..1) are off by up to this at no intel


def enabled() -> bool:
    return settings().world.intel


# Letters between two governments are private: a third country knows what was said in them (a threat, talks that
# came to nothing) only with this much civilian intel on one of the two. Without [world] intel, never. What a deal
# put in place is another matter: the world sees that (its own events).
LETTERS = 0.75
_SEEN: Dict[tuple, tuple] = {}     # (observer, target) -> (when read, civilian intel), for a few minutes
_SEEN_SECONDS = 300


def _civilian(observer: str, target: str) -> float:
    import time
    now = time.monotonic()
    hit = _SEEN.get((observer, target))
    if hit and now - hit[0] < _SEEN_SECONDS:
        return hit[1]
    try:
        lv = levels(observer, target)
    except gamestate.StateError:
        lv = None
    value = lv["civilian"] if lv else 0.0
    _SEEN[(observer, target)] = (now, value)
    return value


def overhears(observer: str, a: str, b: str) -> bool:
    """Does `observer` know what passed in private letters between `a` and `b`? Always if it is one of them;
    otherwise only with [world] intel and LETTERS civilian intel on either."""
    if observer in (a, b):
        return True
    if not enabled():
        return False
    return any(_civilian(observer, t) >= LETTERS for t in (a, b) if t)


def levels(observer: str, target: str) -> Optional[Dict[str, float]]:
    """`observer`'s intel over `target`, each kind 0..1, or None when the game doesn't give them."""
    probe = "|".join(f"[?{observer}.{VARS[k]}@{target}]" for k in KINDS)
    for line in gamestate.query(f'{observer} = {{ log = "AIRPI|{probe}" }}'):
        if line.startswith("AIRPI|"):
            try:
                vals = [float(x) for x in line[6:].split("|")]
            except ValueError:
                return None
            if len(vals) == len(KINDS):
                return {k: min(max(v / 100, 0.0), 1.0) for k, v in zip(KINDS, vals)}
    return None


_MONTHS = "January February March April May June July August September October November December".split()


def _quarter(date: str) -> str:
    """The game quarter an estimate holds for, from either date form: '20:00, 17 January, 1936' (a country's state)
    or '1936.1.17.20' (a snapshot) -> '1936-0'."""
    date = date or ""
    m = re.match(r"\s*(\d{4})\.(\d{1,2})", date)
    if m:
        return f"{m.group(1)}-{(int(m.group(2)) - 1) // 3}"
    year = re.search(r"\b(\d{4})\b", date)
    month = re.search("|".join(_MONTHS), date)
    return f"{year.group(1) if year else ''}-{_MONTHS.index(month.group(0)) // 3 if month else 0}"


def _factor(level: float, *seed: str, spread: float = MAX_ERROR) -> float:
    """How far off an estimate is: 1 when exact, else 1 +- up to spread * (1 - level), the same for the same seed."""
    if level >= EXACT:
        return 1.0
    h = int(hashlib.md5("|".join(seed).encode("utf-8")).hexdigest()[:8], 16) / 0xFFFFFFFF   # 0..1
    return 1 + spread * (1 - level) * (2 * h - 1)


def _round(v: float, level: float) -> float:
    """An estimate rounded as a spy would give it: one significant figure for a rough guess, else two."""
    if level >= EXACT or v <= 0:
        return v
    digits = 1 if level < ROUGH else 2
    from math import floor, log10
    return round(v, digits - 1 - int(floor(log10(abs(v)))))


def estimate(observer: str, target: str, key: str, value: float, level: float, date: str) -> float:
    """`value` of `target`'s STRENGTH_FIELDS `key` as `observer` estimates it with intel `level`. A service over- or
    underestimates a whole kind together (planes and airmen alike), each number a little more on its own."""
    q = _quarter(date)
    kind = FIELD_KIND.get(key, key)
    f = _factor(level, observer, target, kind, q) * _factor(level, observer, target, key, q, spread=JITTER)
    return _round(value * f, level)


def view(s: gamestate.CountryState, observer: str) -> gamestate.CountryState:
    """`s` as `observer` knows it: unchanged when intel is off or unreadable, else a copy with `intel` set (the levels)
    and what they don't reach estimated or left out."""
    if not enabled() or observer == s.tag:
        return s
    try:
        lv = levels(observer, s.tag)
    except Exception:              # unreadable: full knowledge, as without the setting
        return s
    if lv is None:
        return s
    civ = lv["civilian"]
    v = dataclasses.replace(s)
    v.intel = lv
    v.strength = {k: estimate(observer, s.tag, k, x, lv[FIELD_KIND[k]], s.date) if k in FIELD_KIND else x
                  for k, x in s.strength.items()}

    def points(x: Optional[float], key: str) -> Optional[float]:
        if x is None or civ >= EXACT:
            return x
        h = _factor(civ, observer, s.tag, key, _quarter(s.date)) - 1          # -err..+err of MAX_ERROR scale
        return min(max(round((x + h / MAX_ERROR * POINTS_ERROR) * 20) / 20, 0.0), 1.0)   # to 5%

    v.stability = points(s.stability, "stability")
    v.war_support = points(s.war_support, "war_support")
    if civ < EXACT:
        v.political_power = None
    if civ < FOCUS_DONE:
        v.focuses = []
    if civ < FOCUS_NOW:
        v.current_focus, v.focus_progress = "", None
    return v


def knows_focuses(s: gamestate.CountryState) -> bool:
    return s.intel is None or s.intel["civilian"] >= FOCUS_DONE


def word(level: float) -> str:
    """How a number known with intel `level` is introduced: '' (exact), 'about ' or 'roughly '."""
    return "" if level >= EXACT else "about " if level >= ROUGH else "roughly "


def note(s: gamestate.CountryState) -> str:
    """The line telling the model how well it knows `s` (empty with full knowledge)."""
    if s.intel is None:
        return ""
    lv = ", ".join(f"{k} {round(s.intel[k] * 100)}%" for k in KINDS)
    hidden = []
    if s.intel["civilian"] < FOCUS_DONE:
        hidden.append("its national focuses")
    elif s.intel["civilian"] < FOCUS_NOW:
        hidden.append("the national focus it is working on")
    if s.intel["civilian"] < EXACT:
        hidden.append("its political power")
    return (f"Your intelligence on {s.name} ({lv}) is all you know of its strength: numbers marked 'about' are "
            f"estimates, 'roughly' a rough guess, and may be well off"
            + (f"; {' and '.join(hidden)} you don't know at all" if hidden else "")
            + ". Speak of them as estimates and don't claim to know more.")
