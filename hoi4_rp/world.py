"""The world the game is set in, for the LLM: vanilla history, or a mod's alternate history (Kaiserreich and
Kaiserredux), plus optional setting notes from a file. Chosen in config.ini [world]:

    setting = auto          auto (default) detects Kaiserreich from the player's ideology groups
    lore_file =             optional text file with extra setting notes, added to every prompt

The setting text opens every prompt (letters, replies, policies). The live game data that follows is
always authoritative; the setting only tells the LLM which history to reason from.
"""

from __future__ import annotations

import os
from typing import Optional

from .config import settings

KAISERREICH_IDEOLOGIES = {"totalist", "syndicalist", "radical_socialist", "social_democrat", "social_liberal",
                          "market_liberal", "social_conservative", "authoritarian_democrat", "paternal_autocrat",
                          "national_populist"}

_VANILLA = ("This game follows real history from 1936, but the live situation below may already have diverged "
            "from it: trust the situation over your own historical knowledge.")

_KAISERREICH = (
    "SETTING: this game is set in the Kaiserreich alternate history (the Kaiserredux version). Real history "
    "after 1917 did not happen here; reason from this world, and trust the live situation below over anything "
    "you know of either history.\n"
    "- Germany won the Great War (the Weltkrieg) in 1919; there was no Treaty of Versailles. The German Empire "
    "dominates Europe and leads Mitteleuropa, a bloc of client states carved from Russia's former western lands "
    "(among them Poland, the United Baltic Duchy, Lithuania, White Ruthenia, Ukraine and Finland).\n"
    "- The defeated Entente fell to revolution. France is the syndicalist Commune of France, while the old "
    "government holds on in North Africa as National France. Britain is the syndicalist Union of Britain, while "
    "the monarchy and what remains of the Entente continue from Canada. Syndicalism, organised in the Third "
    "Internationale, is Germany's great ideological rival.\n"
    "- Russia lost the war and its western lands and is weak and resentful. Austria-Hungary survived but strains "
    "under its nationalities. The Ottoman Empire endures.\n"
    "- A stock market crash in Berlin, Black Monday, shook the world economy and Germany's confidence; the United "
    "States is divided and unstable.\n"
    "- China is fragmented among cliques, restorationists and foreign spheres of influence; Japan is the leading "
    "power in Asia.\n"
    "- Politics run along ten ideologies, from left to right: totalist, syndicalist, radical socialist, social "
    "democrat, social liberal, market liberal, social conservative, authoritarian democrat, paternal autocrat "
    "and national populist."
)


def detect(party_ideologies) -> str:
    """'kaiserreich' if the parties use Kaiserreich's ideology groups, else 'vanilla'."""
    return "kaiserreich" if KAISERREICH_IDEOLOGIES & set(party_ideologies) else "vanilla"


def setting_for(party_ideologies) -> str:
    chosen = settings().world.setting
    return detect(party_ideologies) if chosen == "auto" else chosen


def text(party_ideologies) -> str:
    """The setting paragraph(s) for the top of a prompt."""
    body = _KAISERREICH if setting_for(party_ideologies) == "kaiserreich" else _VANILLA
    notes = _lore_notes()
    return body + (f"\n\nSETTING NOTES FROM THE PLAYER:\n{notes}" if notes else "")


def _lore_notes() -> Optional[str]:
    path = settings().world.lore_file
    if not path:
        return None
    if not os.path.isabs(path):
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), path)
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip() or None
    except OSError as e:
        print(f"lore_file {path} could not be read: {e}")
        return None
