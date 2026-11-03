"""How letters are written: config.ini [letters] style = serious | dramatic | players.

Only the voice and form of letters change. Policies and the [[commands]] that change the game are the same in
every style, so gameplay doesn't depend on it.

A letter's length and shape are rolled in code for each letter (roll(): a short note, a letter, now and then a
long one), as news reports' are, so that letters don't all come out the same size. The style asks for the one
feeling that drives the letter, and names the habits letters had fallen into (WORN), so that leaders don't all
sound alike: across 199 letters, a quarter said "plainly", a quarter "watches", most closed on a balanced maxim,
and one had an exclamation mark.
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass
from typing import List, Tuple

from .config import settings


@dataclass(frozen=True)
class Style:
    voice: str       # who writes and how; {name} = writing country, {leader}, {other} = recipient
    manner: str      # the range of attitudes
    rules: str       # form; {name}, {other}, {length} (the rolled form)
    lengths: Tuple[Tuple[str, int], ...] = (("one or two short paragraphs", 1),)   # (form, weight): roll()
    plight: str = ""  # how a country in trouble sounds (plight.py)


STYLES = {
    "serious": Style(
        voice="in the voice of {leader} or {name}'s foreign office: formal, period-appropriate diplomatic prose",
        manner="Be in character: warm, cold, evasive or hostile as fits. " + "{feeling}",
        rules="Rules: plain text only, no markdown. The letter itself is {length}, whatever length earlier letters "
              "had. You write from {name}, so any place line is your own capital, not {other}'s. Open with a short "
              "salutation and end with a signature.",
        lengths=(("a short note: one paragraph of 20 to 50 words between salutation and signature", 30),
                 ("one or two paragraphs of 50 to 90 words in all between salutation and signature", 55),
                 ("a full letter of 90 to 140 words in up to three paragraphs between salutation and signature, "
                  "for when there is much to say", 15)),
        plight="the fear shows through the formality: fewer courtesies, plainer and more urgent requests, a "
               "readiness to name a price, and the dignity of a government that knows how bad things are.",
    ),
    "dramatic": Style(
        voice="as {leader} in person, turned up to a stereotype of themselves: take who this leader really is, "
              "their ideology and their country's national character, and exaggerate exactly those traits, the way "
              "a caricature exaggerates a real face. A stiff aristocrat becomes stiffer and grander, a revolutionary "
              "more fiery and sloganeering, a liberal assembly more procedural and high-minded, a dictator more "
              "vain. Use the idiom, obsessions and pet themes that fit them. It is the same person with the volume "
              "up, not a generic actor: no theatre, slapstick or meta jokes. Still pursuing {name}'s real "
              "interests and policy",
        manner="Strong, colourful and unmistakably in character: pride, suspicion, warmth, menace or desperation "
               "as this leader would show it, a notch further than real diplomacy would allow. " + "{feeling}",
        rules="Rules: plain text only, no markdown, no words in capitals for shouting, no stage directions. The "
              "letter itself is {length}, whatever length earlier letters had. You write from {name}, so any "
              "place line is your own capital, not {other}'s. Open with a salutation and end with a signature, "
              "both in this leader's own manner.",
        lengths=(("a short note: one paragraph of 20 to 50 words between salutation and signature", 30),
                 ("one to three paragraphs of 50 to 110 words in all between salutation and signature", 50),
                 ("a full letter of 110 to 170 words in up to four paragraphs between salutation and signature, "
                  "for when this leader has much to say or cannot stop", 20)),
        plight="the caricature cracks under it. The same leader, cornered: pride fighting the need for help, "
               "bravado that keeps slipping, pleading and bargaining, promises bigger than they should be, grief or "
               "fury over what has been lost, the dread of what comes next. Name the concrete horrors from the "
               "situation (the lost cities, the dead, the enemy at the gates). Raw, not comic.",
    ),
    "players": Style(
        voice="as the human player running {name} in a multiplayer roleplay game, messaging the player running "
              "{other}: casual, natural modern English, short sentences, contractions, a bit of personality and "
              "humour. You may use common HOI4 player shorthand (NAP, mil access, guarantee, justify, faction). "
              "You still play {name}'s interests, policy and attitude",
        manner="Sound like a real person: friendly, blunt, joking, suspicious or salty as fits. No 'left on read' "
               "jokes: the other player is busy playing and rarely answers, and that's fine. " + "{feeling}",
        rules="Rules: plain text only, no markdown and no emoji (the game can't show them). {length}, like a chat "
              "message or short note: no formal salutation or signature needed (sign off with a name or just end).",
        lengths=(("one quick message of 8 to 30 words", 35), ("one short paragraph of 30 to 70 words", 65)),
        plight="a stressed player whose run is going badly: blunt, asks for help straight out ('I'm getting rolled "
               "here, can you send anything?'), offers whatever it takes, a bit of gallows humour.",
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


# The one feeling a letter is written in: named by the writer before it writes, from the situation
FEELING = ("FEELING. Before you write, decide the one feeling that drives this letter, from what has happened "
           "between you (only what the information above shows) and where your country stands now: fury, fear, triumph, gratitude, contempt, grief, glee, "
           "wounded pride, impatience, affection, suspicion, relief, shame, greed, or another. Let it show in the "
           "words and in the rhythm, not only in what you say you feel: {how} A leader in a rage doesn't write "
           "like one at ease, and a grateful one doesn't hedge. Many letters are calm: that is a feeling too, but "
           "choose it, don't fall into it.")
_HOW = {"serious": "a sharper sentence, a sudden question, warmth or coldness that breaks the formality.",
        "dramatic": "exclamations, sharp questions, outbursts, a sentence broken off, repetition, boasting, "
                    "pleading, wherever this leader would.",
        "players": "exclamations, caps-free shouting, sarcasm, a quick 'lol' or 'ugh', wherever a real player "
                   "would."}

# Habits letters had fallen into, every country alike (see the module docstring)
WORN = (" Avoid the habits these letters have fallen into, whoever writes them: the words 'plainly', 'frankly', "
        "'bluntly', 'I confess', 'I ask only', 'we note', 'we observe', a country or capital that 'watches' or "
        "'stands', a nation that 'does not forget' or a kindness that 'will be remembered'; the shape of an observation followed by a measured request; and above all ending on a balanced "
        "maxim, proverb or antithesis ('Words warm the heart; treaties warm the economy.'). End where the feeling "
        "ends: on a demand, a question, a threat, thanks, a curse, a joke, or mid-thought.")

ONE_LETTER = (" Write exactly one letter: no drafts, alternatives or notes about it. Don't repeat stock phrases, "
              "images, sayings, sign-offs or arguments from your earlier letters (a proverb, a metaphor, a battle's "
              "dead, a closing line used once is spent); say what is new, and if your answer is the same, say it "
              "in one short sentence instead of restating why. Write in English (a greeting or phrase in your "
              "own language is fine).")


def _name() -> str:
    name = settings().letters.style
    if name not in STYLES:
        print(f"unknown letter style {name!r}; using serious")
        name = "serious"
    return name


def current() -> Style:
    name = _name()
    st = STYLES[name]
    feeling = FEELING.format(how=_HOW[name])
    return Style(st.voice, st.manner.replace("{feeling}", feeling), st.rules + ONE_LETTER + WORN, st.lengths,
                 st.plight)


FEELINGS_KEPT = 3           # a country's latest feelings toward the player, named in its next letter's prompt


def last_word(form: str, before: List[str] = ()) -> str:
    """The note at the very end of a letter prompt, where it carries most weight: the form, the feeling named first
    on a line of its own (strip_feeling() takes it out), no closing maxim. `before`: the feelings of this country's
    latest letters to the player, so that it doesn't fall into one (impatience, cold fury)."""
    earlier = (f" Your latest letters to them were written in {', '.join(before)}: feel what this moment brings, "
               f"and not the same again unless it truly is." if before else "")
    return (f"{form}. Hold to that length. Start with a line FEELING: the one feeling that drives this letter, in a "
            f"word or two (it is taken out before the letter is sent); then the letter, written in that feeling, "
            f"ending where the feeling ends, not on a maxim, and without 'confess', 'plainly', 'frankly', 'we note' "
            f"or 'does not forget'.{earlier}")


_FEELING_LINE = re.compile(r"^[\W_]*feeling[*_\s]*[:\-–].*$\n?", re.I | re.M)


def strip_feeling(raw: str, who: str = "", camp=None) -> str:
    """The letter without the FEELING line last_word() asked for. The feeling is printed for the log, and with
    `camp` kept as the country's latest (feelings())."""
    m = _FEELING_LINE.search(raw)
    if not m:
        return raw
    feeling = m.group(0).split(":", 1)[-1].strip(" *_.\n").lower()
    if who:
        print(f"  {who} writes in {feeling or '?'}")
    if camp is not None and who and feeling:
        kept = camp.data.setdefault("feelings", {})
        kept[who] = (kept.get(who, []) + [feeling[:40]])[-FEELINGS_KEPT:]
    return _FEELING_LINE.sub("", raw, count=1).lstrip("\n")


def feelings(camp, who: str) -> List[str]:
    """The feelings of `who`'s latest letters to the player, oldest first."""
    return list((camp.data.get("feelings") or {}).get(who, []))


def roll(first: bool = False) -> str:
    """The form of one letter (its length and shape), rolled: used both in the rules and at the end of the prompt,
    where it carries most weight. A first letter between two countries is never the short note (the first of the
    lengths): a curt note reads as the sequel to something that never happened."""
    lengths = STYLES[_name()].lengths[1:] if first else STYLES[_name()].lengths
    return random.choices([f for f, _ in lengths], weights=[w for _, w in lengths], k=1)[0]
