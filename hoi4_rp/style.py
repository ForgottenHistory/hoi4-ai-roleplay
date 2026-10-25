"""How letters are written: config.ini [letters] style = serious | dramatic | players.

Only the voice and form of letters change. Policies and the [[commands]] that change the game are the same in
every style, so gameplay doesn't depend on it.
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import settings


@dataclass(frozen=True)
class Style:
    voice: str       # who writes and how; {name} = writing country, {leader}, {other} = recipient
    manner: str      # the range of attitudes
    rules: str       # form; {name}, {other}
    length: str = "one or two short paragraphs"   # repeated at the end of the prompt, where it carries most weight


STYLES = {
    "serious": Style(
        voice="in the voice of {leader} or {name}'s foreign office: formal, period-appropriate diplomatic prose",
        manner="Be in character: warm, cold, evasive or hostile as fits.",
        rules="Rules: plain text only, no markdown. The letter itself is one or two short paragraphs, at most 80 "
              "words, whatever length earlier letters had. You write from {name}, so any place line is your own "
              "capital, not {other}'s. Open with a short salutation and end with a signature.",
        length="at most two short paragraphs between salutation and signature, 80 words in all (shorter than earlier letters, which ran too long)",
    ),
    "dramatic": Style(
        voice="as {leader} in person, turned up to a stereotype of themselves: take who this leader really is, "
              "their ideology and their country's national character, and exaggerate exactly those traits, the way "
              "a caricature exaggerates a real face. A stiff aristocrat becomes stiffer and grander, a revolutionary "
              "more fiery and sloganeering, a liberal assembly more procedural and high-minded, a dictator more "
              "vain. Use the idiom, obsessions and pet themes that fit them. It is the same person with the volume "
              "up, not a generic actor: no theatre, slapstick or meta jokes. Still pursuing {name}'s real "
              "interests and policy",
        manner="Strong, colourful and unmistakably in character: pride, suspicion, warmth or menace as this "
               "leader would show it, a notch further than real diplomacy would allow.",
        rules="Rules: plain text only, no markdown, no words in capitals for shouting, no stage directions. The "
              "letter itself is one or two short paragraphs, at most 100 words, whatever length earlier letters "
              "had. You write from {name}, so any place line is your own capital, not {other}'s. Open with a "
              "salutation and end with a signature, both in this leader's own manner.",
        length="at most two short paragraphs between salutation and signature, 100 words in all (shorter than earlier letters, which ran too long)",
    ),
    "players": Style(
        voice="as the human player running {name} in a multiplayer roleplay game, messaging the player running "
              "{other}: casual, natural modern English, short sentences, contractions, a bit of personality and "
              "humour. You may use common HOI4 player shorthand (NAP, mil access, guarantee, justify, faction). "
              "You still play {name}'s interests, policy and attitude",
        manner="Sound like a real person: friendly, blunt, joking, suspicious or salty as fits. No 'left on read' "
               "jokes: the other player is busy playing and rarely answers, and that's fine.",
        rules="Rules: plain text only, no markdown and no emoji (the game can't show them). One short paragraph, "
              "15 to 60 words, like a chat message or short note: no formal salutation or signature needed (sign "
              "off with a name or just end).",
        length="one short paragraph of 15 to 60 words",
    ),
}


DIPLOMACY = {
    "realistic": ("HOW DEALS GO: as in history. Governments yield land or sovereignty only under real pressure, and "
                  "refuse on principle when they can afford to."),
    "balanced": ("HOW DEALS GO: this is a game, played for good stories as well as realism. Decide as a shrewd "
                 "player of your country would, wanting it to survive and prosper: a good offer is usually taken, a "
                 "fair one negotiated, a poor one refused courteously. A small country beside a far stronger one "
                 "usually gives it a modest demand, a border province, at a price, and gets what it can in return "
                 "(a guarantee, factories, a pact), rather than risk being crushed; it refuses only what would end "
                 "it as a nation. Your own core land is dear, not untouchable: answer a real offer with your price "
                 "rather than a flat no. Principle alone ('land is not merchandise') is no reason here: price, "
                 "safety and survival are. Your character still shows in how you bargain."),
    "open": ("HOW DEALS GO: governments here are pragmatic dealmakers. Almost anything has a price: a generous offer "
             "is accepted unless it would destroy the country, and a fair one is met with a counter-offer, not a "
             "refusal."),
}


def diplomacy() -> str:
    """How readily countries deal (config [letters] diplomacy), for letter and policy prompts."""
    name = settings().letters.diplomacy
    if name not in DIPLOMACY:
        print(f"unknown diplomacy setting {name!r}; using balanced")
        name = "balanced"
    return DIPLOMACY[name]


ONE_LETTER = (" Write exactly one letter: no drafts, alternatives or notes about it. Don't repeat stock phrases or "
              "arguments from your earlier letters; say what is new. Write in English (a greeting or phrase in your "
              "own language is fine).")


def current() -> Style:
    name = settings().letters.style
    if name not in STYLES:
        print(f"unknown letter style {name!r}; using serious")
        name = "serious"
    st = STYLES[name]
    return Style(st.voice, st.manner, st.rules + ONE_LETTER, st.length)
