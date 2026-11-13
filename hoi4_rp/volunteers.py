"""Volunteers between two countries, told to the one writing a letter.

A country sends volunteer divisions to fight under another country's command (the game has no script to send them;
the player does it in the interface, an AI country by its own wish). The numbers come from gamestate (volunteers_to,
volunteers_from); this adds what they mean for a letter: who fights for whom, what the game does with them, and that
a host may pay for them with the same goods a proposal can carry.

    text(them, me)   prompt paragraph about the volunteers between `them` (the writer) and `me`, '' when there are none
"""

from __future__ import annotations

from . import gamestate

RULES = ("How volunteers work: the divisions stay the sender's own men, only commanded by the host in its war. The "
         "game recalls them at once when the sender itself goes to war, and the sender can recall them any time. "
         "The host cannot send them away or keep them. Sending them is not joining the war: the sender stays at peace, "
         "and no war command is ever right for them. A host that wants them to stay, or more of them, can offer "
         "goods in a proposal (factories, equipment from its stockpile, political power, a state, a pact), also in "
         "instalments; a sender that wants more for them can ask the same. Nothing is owed beyond what a deal says. A host at war may ask for a volunteer treaty with [[ask volunteers]] in a proposal: the sender then commits to keep volunteers fighting for it while the treaty stands (the game has no count; the sender sends them itself). If none come, the treaty is broken. It is never [[ask war]].")


def _n(n: int) -> str:
    return f"{n}{'+' if n > 99 else ''} division{'s' if n != 1 else ''}"


def text(them: gamestate.CountryState, me: gamestate.CountryState) -> str:
    """The volunteers that stand between the two, from `them`'s side."""
    sent = dict(me.volunteers_to).get(them.tag, 0)          # me's men fighting for them
    got = dict(them.volunteers_to).get(me.tag, 0)           # their men fighting for me
    if not sent and not got:
        return ""
    lines = []
    if sent:
        lines.append(f"{me.name} has {_n(sent)} of volunteers fighting under YOUR command, in your war"
                     + (f" against {them.names_of(them.enemies)}" if them.enemies else " (you are at peace now)") + ".")
    if got:
        lines.append(f"YOU have sent {_n(got)} of volunteers to fight for {me.name}.")
    return "VOLUNTEERS BETWEEN YOU:\n" + " ".join(lines) + "\n" + RULES
