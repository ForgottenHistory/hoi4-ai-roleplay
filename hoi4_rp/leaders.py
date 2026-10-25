"""A country's leader as a person: name, sub-ideology (with the game's own description of it), traits and biography.

    from hoi4_rp import leaders
    lead = leaders.current("GER", politics_block)    # politics block from the country's save-format block
    lead.name, lead.ideology, lead.traits, lead.bio

Live from the game (bridge.query): the leader character's name and sub-ideology, and which traits it has. Script
can check a trait (has_country_leader_with_trait) but not list them, so every leader trait the game and the mods
define (common/country_leader) is checked in one query.

The biography is the leader's `desc`. Script can't read it, but it is always written in the files: character
definitions (common/characters), or create_country_leader in history, events, focuses and decisions (Kaiserreich
makes most leaders that way). Those are indexed by name once per run; HOI4's own key convention
POLITICS_<NAME>_DESC is tried first.
"""

from __future__ import annotations

import os
import re
import unicodedata
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Dict, List, Optional

from . import gamefiles

_BIO_FOLDERS = ["common/characters", "history/countries", "events", "common/national_focus", "common/decisions",
                "common/scripted_effects", "common/on_actions"]


@dataclass
class Trait:
    id: str
    name: str
    description: str = ""


@dataclass
class Leader:
    name: str
    ideology_id: str = ""            # sub-ideology, e.g. "nazism"
    ideology: str = ""               # its display name, e.g. "Nazism"
    ideology_desc: str = ""          # the game's description of the sub-ideology
    traits: List[Trait] = field(default_factory=list)
    bio: str = ""


# ---------------------------------------------------------------- traits

@lru_cache(maxsize=None)
def trait_ids() -> List[str]:
    """Every country leader trait the game and the enabled mods define."""
    ids: List[str] = []
    for path in gamefiles.files("common/country_leader"):
        try:
            with open(path, encoding="utf-8-sig", errors="replace") as f:
                tree = gamefiles.parse(f.read())
        except OSError:
            continue
        for key, _, body in tree:
            if key == "leader_traits" and isinstance(body, list):
                ids += [k for k, _, v in body if isinstance(v, list)]
    return list(dict.fromkeys(ids))


# ---------------------------------------------------------------- biographies

def _norm(name: str) -> str:
    name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", " ", name.lower()).strip()


def _block_after(text: str, pos: int) -> str:
    """The text of the { } block whose opening brace is at or after pos."""
    start = text.find("{", pos)
    if start < 0:
        return ""
    depth, i = 0, start
    while i < len(text):
        c = text[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[start + 1:i]
        i += 1
    return text[start + 1:]


_VALUE = r'\s*=\s*"?([^"\s}]+(?: [^"\s}=]+)*)"?'


@lru_cache(maxsize=None)
def _bio_index() -> Dict[str, str]:
    """Leader name or name key -> desc key, from character definitions and create_country_leader blocks."""
    out: Dict[str, str] = {}
    for folder in _BIO_FOLDERS:
        for path in gamefiles.files(folder):
            try:
                with open(path, encoding="utf-8-sig", errors="replace") as f:
                    text = re.sub(r"#[^\n]*", "", f.read())
            except OSError:
                continue
            if "desc" not in text:
                continue
            # create_country_leader = { name = "..." desc = "..." ... }
            for m in re.finditer(r"\bcreate_country_leader\s*=\s*\{", text):
                body = _block_after(text, m.end() - 1)
                n, d = re.search(r"\bname" + _VALUE, body), re.search(r"\bdesc" + _VALUE, body)
                if n and d:
                    out.setdefault(n.group(1), d.group(1))
            # characters = { TOKEN = { name = KEY ... country_leader = { desc = KEY ... } } }
            if folder == "common/characters":
                for m in re.finditer(r"\n\t(\w+)\s*=\s*\{", text):
                    body = _block_after(text, m.end() - 1)
                    cl = re.search(r"\bcountry_leader\s*=\s*\{", body)
                    if not cl:
                        continue
                    d = re.search(r"\bdesc" + _VALUE, _block_after(body, cl.end() - 1))
                    if not d:
                        continue
                    n = re.search(r"\bname" + _VALUE, body)
                    out.setdefault(n.group(1) if n else m.group(1), d.group(1))
    return out


@lru_cache(maxsize=None)
def _bio_by_name() -> Dict[str, str]:
    """Normalised display name -> desc key (name keys resolved through the localisation)."""
    from .gamestate import loc
    index = _bio_index()
    keys = list(index)
    shown = []
    for i in range(0, len(keys), 400):
        shown += loc(*keys[i:i + 400])
    out: Dict[str, str] = {}
    for raw, disp in zip(keys, shown):
        for name in (disp, raw):
            if name:
                out.setdefault(_norm(name), index[raw])
    return out


def warm() -> None:
    """Read the files now (they are otherwise read when the first leader is described)."""
    trait_ids()
    _bio_index()


def _bio(name: str) -> str:
    from .gamestate import clean_loc, loc
    norm = _norm(name)
    keys = ["POLITICS_" + norm.upper().replace(" ", "_") + "_DESC"]
    if norm in _bio_by_name():
        keys.append(_bio_by_name()[norm])
    for key, text in zip(keys, loc(*keys)):
        if text and text != key:
            return clean_loc(text)
    return ""


# ---------------------------------------------------------------- the current leader

def current(tag: str, politics: str = "") -> Optional[Leader]:
    """The country's leader, read live. `politics` (the save-format politics block) gives the sub-ideology id."""
    from .gamestate import clean_loc, loc, query
    lines = query(f'{tag} = {{ every_character = {{ limit = {{ is_country_leader = yes }} '
                  f'log = "AIRPLD|[THIS.GetName]|[THIS.GetIdeology]" }} }}')
    head = next((x[7:].split("|") for x in lines if x.startswith("AIRPLD|")), None)
    if not head:
        return None
    lead = Leader(name=head[0].strip(), ideology=(head[1] if len(head) > 1 else "").strip())

    # The ruling party's leader entry: country_leader={ { ideology="nazism" character={ id=1799 type=73 } } }
    rm = re.search(r'\n\t\t\truling_party="?(\w+)', politics)
    if rm:
        party = re.search(r"\n\t\t\t\t" + re.escape(rm.group(1)) + r"=\{", politics)
        if party:
            im = re.search(r'\bideology="?(\w+)', _block_after(politics, party.end() - 1))
            if im:
                lead.ideology_id = im.group(1)
                desc = loc(f"{lead.ideology_id}_desc")[0]
                if desc and desc != f"{lead.ideology_id}_desc":
                    lead.ideology_desc = clean_loc(desc)

    ids = trait_ids()
    held: List[str] = []
    for i in range(0, len(ids), 400):
        checks = " ".join(f'if = {{ limit = {{ has_country_leader_with_trait = {t} }} log = "AIRPLT|{t}" }}'
                          for t in ids[i:i + 400])
        held += [x[7:].strip() for x in query(f"{tag} = {{ {checks} }}") if x.startswith("AIRPLT|")]
    if held:
        texts = loc(*[k for t in held for k in (t, f"{t}_desc")])
        for n, t in enumerate(held):
            name, desc = texts[2 * n], texts[2 * n + 1]
            lead.traits.append(Trait(t, clean_loc(name) if name != t else t.replace("_", " "),
                                     clean_loc(desc) if desc != f"{t}_desc" else ""))
    lead.bio = _bio(lead.name)
    return lead


def describe(lead: Optional[Leader]) -> str:
    """For prompts: who the leader is."""
    if not lead:
        return ""
    out = f"The leader, {lead.name}" + (f", is a follower of {lead.ideology}" if lead.ideology else "")
    out += f" ({lead.ideology_desc})." if lead.ideology_desc else "."
    if lead.traits:
        out += " Leader traits: " + "; ".join(t.name + (f" ({t.description})" if t.description else "")
                                              for t in lead.traits) + "."
    if lead.bio:
        out += f" Biography: {lead.bio}"
    return out
