"""Gameplay from letters: the LLM appends simple commands to its letter, Python validates them in the game
and applies what's allowed. `them` is the AI country writing, `me` the player.

Commands (double brackets, anywhere in the letter; removed before it's shown):
    [[opinion +10]]           them's opinion of me changes (-20..+20; replies only)
    [[pact]] [[access]] [[docking]] [[guarantee]] [[market]]
                              them grants it to me now (replies, only when I asked)
    [[end pact]] [[end access]] [[end docking]] [[end guarantee]]
                              them ends an agreement with me
    [[embargo]] [[end embargo]]
                              them embargoes me / lifts its embargo
    [[offer X]]               a proposal: them offers to grant X        (letters them starts; Accept/Decline)
    [[ask X]]                 a proposal: them asks me to grant X to them (access, docking, guarantee)
    [[offer submit]]          a proposal: them becomes my subject (puppet)
    [[ask submit]]            a proposal: them demands that I become its subject
    [[offer annex]] / [[annex]]   them joins me entirely (annexed by agreement, like the Anschluss)
    [[offer subject TAG]]     a proposal item: them hands me its subject TAG (it becomes my subject)
    [[ask subject TAG]]       a proposal item: them asks me to hand it my subject TAG
    [[offer govt IDEOLOGY]] [[ask govt IDEOLOGY]] [[govt IDEOLOGY]]   a change of government (GOVT_NOTE): them
                              puts that party in power, or asks me to; [[govt X]] grants it when asked
    [[offer civ N]] [[ask civ N]] [[offer mil N]] [[ask mil N]]   factories as currency (FACTORY_NOTE): N
                              civilian or military factories, taken from the giver's states at random and built
                              instantly in the receiver's core states at random; [[civ N]] grants them when asked
    [[faction]] [[join]] [[end faction]]   factions (FACTION_NOTE): I join them's faction / them joins mine (also as
                              offer/ask items); them leaves the faction we share, or as its leader expels me
    [[offer leave]] [[ask leave]]   them leaves its own faction / asks me to leave mine (not one we share)
    [[war TAG]] [[offer war TAG]] [[ask war TAG]]   joining a war (WAR_NOTE): them joins mine against TAG, or asks me
                              to join its war against TAG
    [[offer pledge TAG]] [[ask pledge TAG]] [[pledge TAG]]   a war pledge, kept by treaties.py: them joins my
                              war whenever I go to war with TAG, or asks me to pledge the same
    [[offer oppose TAG]] [[offer befriend TAG]] [[offer back TAG]]   a standing commitment toward a third country
                              (ALIGN_NOTE), kept by treaties.py, steering them's AI (steering.py); ask/plain forms too
    [[offer sanction TAG]] [[ask sanction TAG]] [[sanction TAG]]   a trade embargo on a third country, now
    [[offer rifles N]] [[ask artillery N]] [[trucks N]]   stockpiled equipment as currency (EQUIPMENT)
    Terms over time ([[for 12 months]], instalments [[offer civ 2 x3]]) and [[end treaty]]: treaties.py

Each deal checks the game first: it must be possible right now (agreements need peace and must not exist
yet; ending one needs it to exist). Effect directions were checked in game:
    A = { add_opinion_modifier = { target = B ... } }                          A's opinion of B changes
    A = { diplomatic_relation = { country = B relation = military_access } }   B gives A access
    A = { diplomatic_relation = { country = B relation = docking_rights } }    B gives A docking rights
                              (stored as docking_rights={ first=A second=B } in A's block; no trigger exists)
    A = { diplomatic_relation = { country = B relation = non_aggression_pact } }   mutual
    A = { give_guarantee = B }          A guarantees B; ended by A = { diplomatic_relation = { country = B
                                        relation = guarantee active = no } }
    A = { give_market_access = B }      mutual
    A = { send_embargo = B }            A embargoes B; lifted by A = { diplomatic_relation = { country = B
                                        relation = embargo active = no } } (break_embargo acts from ROOT,
                                        which in a console eval_effect is the UI's selected country)
    active = no on the same diplomatic_relation ends a pact, access or docking rights.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Set, Tuple

from . import bridge, gamestate

OPINION_TIERS = [-20, -10, -5, 5, 10, 20]
OPINION_DECAY = 1          # opinion lost per month


def opinion_modifier(value: int) -> str:
    return f"airp_letter_opinion_{'p' if value > 0 else 'm'}{abs(value)}"


def opinion_modifiers_file() -> str:
    """common/opinion_modifiers for the mod."""
    lines = ["# GENERATED by hoi4_rp/actions.py. Don't edit by hand.", "", "opinion_modifiers = {"]
    for v in OPINION_TIERS:
        lines.append(f"\t{opinion_modifier(v)} = {{ value = {v} decay = {OPINION_DECAY} }}")
    lines.append("}")
    return "\n".join(lines) + "\n"


def opinion_loc() -> List[str]:
    return [f' {opinion_modifier(v)}:0 "Diplomatic letter ({v:+d})"' for v in OPINION_TIERS]


# ---------------------------------------------------------------- the relations between them and me

def _docking_given(giver: str, receiver: str) -> bool:
    """`giver` gives `receiver` docking rights (read from the receiver's save-format block)."""
    rel = gamestate._relations(bridge.country_block(receiver)).get(giver, "")
    return bool(re.search(r'docking_rights=\{[^}]*first="' + receiver + r'"[^}]*second="' + giver + '"', rel))


# relation name -> (them, me) -> does it exist right now?
_CHECKS: Dict[str, Callable[[str, str], bool]] = {
    "pact": lambda t, m: bridge.trigger(f"{m} = {{ has_non_aggression_pact_with = {t} }}"),
    "they_access": lambda t, m: bridge.trigger(f"{t} = {{ gives_military_access_to = {m} }}"),
    "my_access": lambda t, m: bridge.trigger(f"{m} = {{ gives_military_access_to = {t} }}"),
    "they_docking": lambda t, m: _docking_given(t, m),
    "my_docking": lambda t, m: _docking_given(m, t),
    "they_guarantee": lambda t, m: bridge.trigger(f"{t} = {{ has_guaranteed = {m} }}"),
    "my_guarantee": lambda t, m: bridge.trigger(f"{m} = {{ has_guaranteed = {t} }}"),
    "market": lambda t, m: bridge.trigger(f"{m} = {{ has_market_access_with = {t} }}"),
    "embargo": lambda t, m: bridge.trigger(f"{t} = {{ is_embargoing = {m} }}"),
}


@dataclass
class BorderState:
    """A state of its owner, with what it is worth. Any but the capital can be ceded; resource rights may be
    given in the capital too."""
    id: str
    name: str
    core: bool          # the other country holds a core on it
    claim: bool         # the other country claims it
    own_core: bool = False    # the owner's own core land (heartland), not a colony or other non-core land
    border: bool = False      # it borders the other country
    civ: int = 0
    mil: int = 0
    pop_k: int = 0
    resources: str = ""       # e.g. "oil 12, steel 4"
    capital: bool = False
    dmz: bool = False         # already a demilitarized zone

    def note(self, other_name: str) -> str:
        worth = [f"{self.civ} civilian / {self.mil} military factories", f"{self.pop_k:,}k people"]
        if self.resources:
            worth.append(self.resources)
        bits = ((["its capital"] if self.capital else [])
                + ["its own core land" if self.own_core else "not its core land (a colony or other holding)"]
                + (["borders " + other_name] if self.border else ["does not border " + other_name])
                + ([f"{other_name} holds a core there"] if self.core else [])
                + ([f"{other_name} claims it"] if self.claim else []))
        return f"{self.name} ({'; '.join(worth + bits)})"


@dataclass
class Relation:
    """What letters can change between two countries, read live."""
    at_war: bool
    exists: Dict[str, bool]            # relation name -> exists now
    them_border: Dict[str, BorderState] = None   # them's non-capital states bordering me (flags: my cores/claims)
    me_border: Dict[str, BorderState] = None     # my non-capital states bordering them (flags: their cores/claims)
    peace_ok: bool = False                       # at war, and peace can be made now (peace.possible)
    bound: str = ""          # "subject" (them is my subject), "overlord" (them is my overlord), "faction", or ""
    them_subject: bool = False   # them is a subject of anyone
    me_subject: bool = False     # I am a subject of anyone
    them_subjects: Dict[str, str] = None   # them's subjects: tag -> name
    me_subjects: Dict[str, str] = None     # my subjects: tag -> name
    them_factories: Dict[str, int] = None  # "civ"/"mil" -> how many them has
    me_factories: Dict[str, int] = None
    them_parties: Dict[str, str] = None    # parties not in power: ideology id -> "Name (ideology)" (set_parties)
    me_parties: Dict[str, str] = None
    them_rights: Set[str] = None    # them's states whose resource rights I already hold
    me_rights: Set[str] = None      # my states whose resource rights them already holds
    them_leader: bool = False       # them leads a faction
    me_leader: bool = False
    them_in_faction: bool = False   # them is in any faction
    me_in_faction: bool = False
    me_enemies: Dict[str, str] = None     # my enemies them could join a war against: tag -> name (war_gives)
    them_enemies: Dict[str, str] = None   # them's enemies I could join a war against
    third: Dict[str, Dict] = None         # countries a pledge or commitment may name: tag -> {name, flags}
    them_equipment: Dict[str, int] = None  # stockpiles: "rifles"/"artillery"/"trucks" -> amount
    me_equipment: Dict[str, int] = None
    pledged: frozenset = frozenset()      # pledges, commitments standing between the two (treaties.standing)
    them_ports: bool = True               # has a coast: docking rights in its ports mean something
    me_ports: bool = True
    them_navy: bool = True                # has ships to dock
    me_navy: bool = True
    tension: float = 0.0                  # world tension 0..1, and each side's thresholds (world.tension)
    them_tension: Dict[str, float] = None
    me_tension: Dict[str, float] = None


# Between a subject and its overlord, or within a faction, agreements come with the relationship (access, docking,
# market, protection) or make no sense (a pact, an embargo): none can be agreed, ended or imposed by letter. Only
# lifting an embargo from before stays possible.
BOUND_ALLOWED = ("end:embargo",)


def bound_note(them_name: str, me_name: str, bound: str) -> str:
    """Why no agreements are offered between the two (empty if they aren't bound)."""
    why = {"subject": f"{them_name} is {me_name}'s subject",
           "overlord": f"{them_name} is {me_name}'s overlord",
           "faction": f"{them_name} and {me_name} are in the same faction"}.get(bound)
    if not why:
        return ""
    return (f"{why}: military access, docking rights, market access and protection already come with that "
            f"relationship, and a pact or an embargo between you makes no sense, so none of them are offered, asked "
            f"for or promised in letters.")

_STATE_NAMES: Dict[str, str] = {}      # state id -> name, filled as border states are read


_RESOURCES = ["oil", "aluminium", "rubber", "tungsten", "steel", "chromium"]
_STATE_FLAGS = {"AIRPC": "core", "AIRPK": "claim", "AIRPO": "own_core", "AIRPN": "border", "AIRPX": "capital",
                "AIRPZ": "dmz"}


def border_states(owner: str, other: str) -> Dict[str, BorderState]:
    """Every state of `owner`, near or far, with its worth and how it stands: `owner`'s own core or not, its
    capital, bordering `other` or not, `other`'s cores and claims."""
    from .gamestate import query
    res = "|".join(f"[?THIS.resource@{r}]" for r in _RESOURCES)
    eff = (f'{owner} = {{ every_owned_state = {{ '
           f'log = "AIRPB|[THIS.GetID]|[THIS.GetName]|[?THIS.industrial_complex_level]|[?THIS.arms_factory_level]|'
           f'[?THIS.state_population_k]|{res}" '
           f'if = {{ limit = {{ is_capital = yes }} log = "AIRPX|[THIS.GetID]" }} '
           f'if = {{ limit = {{ is_demilitarized_zone = yes }} log = "AIRPZ|[THIS.GetID]" }} '
           f'if = {{ limit = {{ is_core_of = {other} }} log = "AIRPC|[THIS.GetID]" }} '
           f'if = {{ limit = {{ is_claimed_by = {other} }} log = "AIRPK|[THIS.GetID]" }} '
           f'if = {{ limit = {{ is_core_of = {owner} }} log = "AIRPO|[THIS.GetID]" }} '
           f'if = {{ limit = {{ any_neighbor_state = {{ is_owned_by = {other} }} }} log = "AIRPN|[THIS.GetID]" }} }} }}')
    num = lambda x: int(float(x)) if x.strip().replace(".", "", 1).isdigit() else 0
    out: Dict[str, BorderState] = {}
    for line in query(eff):
        kind, _, rest = line.partition("|")
        f = [x.strip() for x in rest.split("|")]
        sid = f[0]
        if kind == "AIRPB" and len(f) >= 5:
            amounts = [num(x) for x in f[5:5 + len(_RESOURCES)]]
            out[sid] = BorderState(sid, f[1], False, False, civ=num(f[2]), mil=num(f[3]), pop_k=num(f[4]),
                                   resources=", ".join(f"{r} {a}" for r, a in zip(_RESOURCES, amounts) if a))
            _STATE_NAMES[sid] = f[1]
        elif sid in out and kind in _STATE_FLAGS:
            setattr(out[sid], _STATE_FLAGS[kind], True)
    return out


def relation(them: str, me: str) -> Relation:
    from . import peace, world
    at_war = bridge.trigger(f"{me} = {{ has_war_with = {them} }}")
    try:
        level, rules = world.tension([them, me])
    except gamestate.StateError:
        level, rules = 0.0, {}
    bound = ("subject" if bridge.trigger(f"{them} = {{ is_subject_of = {me} }}") else
             "overlord" if bridge.trigger(f"{me} = {{ is_subject_of = {them} }}") else
             "faction" if bridge.trigger(f"{me} = {{ is_in_faction_with = {them} }}") else "")
    return Relation(at_war=at_war,
                    exists={name: check(them, me) for name, check in _CHECKS.items()},
                    them_border=border_states(them, me), me_border=border_states(me, them),
                    peace_ok=at_war and peace.possible(me, them), bound=bound,
                    them_subject=bridge.trigger(f"{them} = {{ is_subject = yes }}"),
                    me_subject=bridge.trigger(f"{me} = {{ is_subject = yes }}"),
                    them_subjects=subjects_of(them), me_subjects=subjects_of(me),
                    them_factories=factory_counts(them), me_factories=factory_counts(me),
                    them_rights=rights_held(me, them), me_rights=rights_held(them, me),
                    them_leader=bridge.trigger(f"{them} = {{ is_faction_leader = yes }}"),
                    me_leader=bridge.trigger(f"{me} = {{ is_faction_leader = yes }}"),
                    them_in_faction=bridge.trigger(f"{them} = {{ is_in_faction = yes }}"),
                    me_in_faction=bridge.trigger(f"{me} = {{ is_in_faction = yes }}"),
                    me_enemies=joinable_enemies(me, them), them_enemies=joinable_enemies(them, me),
                    third=third_countries(them, me),
                    them_equipment=equipment_counts(them), me_equipment=equipment_counts(me),
                    them_ports=_coastal(them), me_ports=_coastal(me), them_navy=_navy(them), me_navy=_navy(me),
                    tension=level, them_tension=rules.get(them), me_tension=rules.get(me))


# World tension (config [world] tension_rules): the deal keys the game ties to it, and whose threshold must be
# reached: a guarantee the guarantor's, a faction both sides' (the one joining and the one admitting). Below it the
# item isn't possible, as in the game's own diplomacy window.
TENSION_GATED = {"give:guarantee": (("them", "guarantee_tension"),), "ask:guarantee": (("me", "guarantee_tension"),),
                 **{k: (("them", "join_faction_tension"), ("me", "join_faction_tension"))
                    for k in ("give:faction", "give:join", "ask:faction", "ask:join")}}


def tension_allows(rel: "Relation", key: str) -> bool:
    from .config import settings
    if key not in TENSION_GATED or not settings().world.tension_rules:
        return True
    for side, rule in TENSION_GATED[key]:
        rules = rel.them_tension if side == "them" else rel.me_tension
        if rules is not None and rel.tension < rules.get(rule, 0):
            return False
    return True


def _coastal(tag: str) -> bool:
    return bridge.trigger(f"{tag} = {{ any_owned_state = {{ is_coastal = yes }} }}")


def _navy(tag: str) -> bool:
    return bridge.trigger(f"{tag} = {{ has_navy_size = {{ size > 0 }} }}")


# ---------------------------------------------------------------- deals

@dataclass(frozen=True)
class Deal:
    key: str              # "give:pact", "ask:access", "end:pact", "embargo", "end:embargo"
    relation: str         # the relation in _CHECKS it creates or ends
    ends: bool            # True: needs the relation to exist and removes it; False: needs it absent
    peace: bool           # only possible when not at war
    prompt: str           # for the LLM (them's point of view)
    label: str            # neutral name of the thing (window, chronicle)
    effect: str           # script with {them} {me}
    shown: str            # line under the letter
    remembered: str       # chronicle text
    hostile: bool = False


def _rel(on: str, other: str, rel: str, active: str) -> str:
    return f"{{{on}}} = {{{{ diplomatic_relation = {{{{ country = {{{other}}} relation = {rel} active = {active} }}}} }}}}"


DEALS: List[Deal] = [
    # them grants me (replies when I asked; offers in letters them starts)
    Deal("give:pact", "pact", False, True, "sign a non-aggression pact with {me_name}", "a non-aggression pact",
         _rel("me", "them", "non_aggression_pact", "yes"),
         "Non-aggression pact signed with {them_name}.", "{them_name} and {me_name} signed a non-aggression pact."),
    Deal("give:access", "they_access", False, True, "grant {me_name} military access through your territory",
         "military access", _rel("me", "them", "military_access", "yes"),
         "{them_name} granted us military access.", "{them_name} granted {me_name} military access."),
    Deal("give:docking", "they_docking", False, True, "grant {me_name} docking rights in your ports",
         "docking rights", _rel("me", "them", "docking_rights", "yes"),
         "{them_name} granted us docking rights.", "{them_name} granted {me_name} docking rights."),
    Deal("give:guarantee", "they_guarantee", False, True,
         "guarantee {me_name}'s independence (you would defend them if attacked)", "a guarantee of independence",
         "{them} = {{ give_guarantee = {me} }}",
         "{them_name} guaranteed our independence.", "{them_name} guaranteed the independence of {me_name}."),
    Deal("give:market", "market", False, True, "open market access between you and {me_name}", "market access",
         "{them} = {{ give_market_access = {me} }}",
         "Market access opened with {them_name}.", "{them_name} and {me_name} opened market access to each other."),
    # Peace (peace.py): only at war, only on its own; the effect is peace.make, not script
    Deal("give:peace", "peace", False, False, "end the war with {me_name}: if they accept, a peace conference "
         "settles the terms according to how the war stands", "peace (a peace conference)", "",
         "Peace with {them_name}: the peace conference begins.",
         "{me_name} accepted {them_name}'s offer of peace, and a peace conference began."),
    # them asks me to grant (proposals in letters them starts)
    Deal("ask:access", "my_access", False, True, "ask {me_name} for military access through their territory",
         "military access through our territory", _rel("them", "me", "military_access", "yes"),
         "We granted {them_name} military access.", "{me_name} granted {them_name} military access."),
    Deal("ask:docking", "my_docking", False, True, "ask {me_name} for docking rights in their ports",
         "docking rights in our ports", _rel("them", "me", "docking_rights", "yes"),
         "We granted {them_name} docking rights.", "{me_name} granted {them_name} docking rights."),
    Deal("ask:guarantee", "my_guarantee", False, True, "ask {me_name} to guarantee your independence",
         "our guarantee of their independence", "{me} = {{ give_guarantee = {them} }}",
         "We guaranteed the independence of {them_name}.", "{me_name} guaranteed the independence of {them_name}."),
    # Submission (peace only, never between a subject and its overlord; SUBMIT_NOTE): OVERLORD = { puppet = SUBJECT }
    Deal("give:submit", "submit", False, True,
         "submit to {me_name}: become its subject (puppet), giving up an independent foreign policy for its "
         "protection", "to become our subject", "{me} = {{ puppet = {them} }}",
         "{them_name} became our subject.", "{them_name} submitted to {me_name} and became its subject."),
    Deal("ask:submit", "submit", False, True,
         "demand that {me_name} submit to you and become your subject (puppet)", "our submission as their subject",
         "{them} = {{ puppet = {me} }}",
         "We submitted to {them_name} and became its subject.", "{me_name} submitted to {them_name} and became its "
         "subject.", True),
    # Annexation by agreement (peace only; them may be my own subject, not anyone else's; SUBMIT_NOTE)
    Deal("give:annex", "annex", False, True,
         "join {me_name} entirely: your country is annexed, and its land, army and people become part of "
         "{me_name} (the end of your state)", "to be annexed into our country",
         "{me} = {{ annex_country = {{ target = {them} transfer_troops = yes }} }}",
         "{them_name} has joined us: its land, army and people are now ours.",
         "{them_name} was annexed into {me_name} by agreement."),
    # Factions (peace between the two, neither a subject; possible() has the rules). "faction": I join them's
    # faction; "join": them joins mine. give = them acts (admits me, or joins), ask = them asks me to act.
    #     LEADER = { add_to_faction = TAG }      (checked in game)
    Deal("give:faction", "faction", False, True, "take {me_name} into your faction as a member (you would fight "
         "each other's wars)", "membership of their faction", "{them} = {{ add_to_faction = {me} }}",
         "{them_name} took us into its faction.", "{them_name} took {me_name} into its faction."),
    Deal("give:join", "faction", False, True, "join {me_name}'s faction as a member (you would fight each "
         "other's wars)", "to join our faction", "{me} = {{ add_to_faction = {them} }}",
         "{them_name} joined our faction.", "{them_name} joined {me_name}'s faction."),
    Deal("ask:faction", "faction", False, True, "ask {me_name} to take you into its faction", "a place in our "
         "faction", "{me} = {{ add_to_faction = {them} }}",
         "We took {them_name} into our faction.", "{me_name} took {them_name} into its faction.", True),
    Deal("ask:join", "faction", False, True, "ask {me_name} to join your faction", "our membership of their "
         "faction", "{them} = {{ add_to_faction = {me} }}",
         "We joined {them_name}'s faction.", "{me_name} joined {them_name}'s faction.", True),
    # Leaving a faction at the other's wish (not one they share: that is end:faction). A leader can't leave its own.
    #     X = { leave_faction = yes }      (checked in game)
    Deal("give:leave", "leave", False, True, "leave your faction, so that you no longer stand with its members against "
         "{me_name}", "to leave their faction", "{them} = {{ leave_faction = yes }}",
         "{them_name} left its faction.", "{them_name} left its faction at {me_name}'s wish."),
    Deal("ask:leave", "leave", False, True, "ask {me_name} to leave its faction", "our leaving our faction",
         "{me} = {{ leave_faction = yes }}",
         "We left our faction.", "{me_name} left its faction at {them_name}'s wish.", True),
    # them acts alone
    Deal("end:faction", "faction", True, False, "leave the faction you share with {me_name} (as its leader: "
         "expel them from it)", "the faction",
         "{them} = {{ if = {{ limit = {{ is_faction_leader = yes }} remove_from_faction = {me} }} "
         "else = {{ leave_faction = yes }} }}",
         "{them_name} broke with us: we no longer share a faction.",
         "{them_name} broke with {me_name}: they no longer share a faction.", True),
    Deal("end:pact", "pact", True, False, "end your non-aggression pact with {me_name}", "the non-aggression pact",
         _rel("me", "them", "non_aggression_pact", "no"),
         "{them_name} ended our non-aggression pact.", "{them_name} ended its non-aggression pact with {me_name}.", True),
    Deal("end:access", "they_access", True, False, "withdraw the military access you give {me_name}",
         "military access", _rel("me", "them", "military_access", "no"),
         "{them_name} withdrew our military access.", "{them_name} withdrew {me_name}'s military access.", True),
    Deal("end:docking", "they_docking", True, False, "withdraw the docking rights you give {me_name}",
         "docking rights", _rel("me", "them", "docking_rights", "no"),
         "{them_name} withdrew our docking rights.", "{them_name} withdrew {me_name}'s docking rights.", True),
    Deal("end:guarantee", "they_guarantee", True, False, "withdraw your guarantee of {me_name}'s independence",
         "the guarantee of independence", _rel("them", "me", "guarantee", "no"),
         "{them_name} withdrew its guarantee of our independence.",
         "{them_name} withdrew its guarantee of {me_name}'s independence.", True),
    Deal("embargo", "embargo", False, False, "impose a trade embargo on {me_name}", "a trade embargo",
         "{them} = {{ send_embargo = {me} }}",
         "{them_name} imposed a trade embargo on us.", "{them_name} imposed a trade embargo on {me_name}.", True),
    Deal("end:embargo", "embargo", True, False, "lift your trade embargo on {me_name}", "the trade embargo",
         _rel("them", "me", "embargo", "no"),
         "{them_name} lifted its trade embargo on us.", "{them_name} lifted its trade embargo on {me_name}."),
]
BY_KEY = {d.key: d for d in DEALS}
GIVES = [d.key for d in DEALS if d.key.startswith("give:") and d.key != "give:peace"]   # peace: peace.py rules
ASKS = [d.key for d in DEALS if d.key.startswith("ask:")]
UNILATERAL = [d.key for d in DEALS if d.key.startswith("end:") or d.key == "embargo"]


# ---------------------------------------------------------------- territory
# "give:state:35": them cedes state 35 to me; "ask:state:34": them asks me to cede state 34. Any state but the
# capital, near or far (Relation.them_border / me_border, named for history: they hold every tradeable state, each
# with its worth), and only in peace.
#     A = { transfer_state = ID }      A becomes owner and controller of state ID (checked in game)

_BORDER_INFO: Dict[str, BorderState] = {}       # territory key -> state info, from the latest relation()

SUBMIT_NOTE = ("Submission is the gravest step of all. A government offers it only when it cannot survive alone, "
               "or its people and rulers truly want the other's protection; it demands it only of a country far "
               "weaker than itself that it could crush, and knows a proud nation may sooner fight. Annexation goes "
               "further still: it ends the state for ever, so a government agrees to it only when its people and "
               "rulers truly want union, or it has no other way out. Never offer, demand or grant either lightly, "
               "or in the same breath as small favours.")

TERRITORY_NOTE = ("Land differs. A country's own core land is dear: ceded at a high price, under real pressure, "
                  "or where the other side holds a core, and its capital and heart hardly at all. A "
                  "colony or other non-core holding can be sold or traded at a fair price, as states have always done "
                  "(weigh its factories, people and resources against what is offered, and what is offered against "
                  "your own economy). If an offer is substantial but not enough, haggle: ask for more, or for "
                  "something alongside, rather than refuse outright. A government demands land only where it has a "
                  "core, a claim or real leverage. Real pressure counts: a country facing a far "
                  "stronger neighbour that has shown it will use force, with no ally willing to fight for it, may "
                  "judge that giving up a province now saves the rest, as states have done throughout history; "
                  "a proud or reckless government may still refuse.")


def is_state(key: str) -> bool:
    return key.startswith(("give:state:", "ask:state:"))


# ---------------------------------------------------------------- disarmament
# "give:dmz:57": them demilitarizes its state 57 on the border with me; "ask:dmz:64": them asks me to demilitarize
# mine. "give:dismantle:5": them dismantles 5 of its military factories (destroyed, not handed over);
# "ask:dismantle:5": them asks me to. As in a peace conference, but agreed by letter, and without an end date.
#     STATE = { set_demilitarized_zone = yes }     (checked in game; = no undoes it)
#     STATE = { remove_building = { type = arms_factory level = N } }

DISARM_NOTE = ("Demilitarizing a border state or dismantling military factories is disarmament at another power's "
               "wish: a humiliation and a lasting weakness, accepted under real pressure or for something substantial "
               "(a guarantee, land, peace), and demanded of a country you fear or could compel. A demilitarized state "
               "can hold no troops, fortifications or working airbases, for good; dismantled factories are destroyed, not "
               "handed over. Ask for one only against a neighbour that threatens you today, for a reason that still "
               "stands: a past deal, or a danger that has since gone (a state that no longer exists, a war that is "
               "over), is no reason to ask again.")


def is_dmz(key: str) -> bool:
    return key.startswith(("give:dmz:", "ask:dmz:"))


def dmz_gives(rel: "Relation") -> List[str]:
    keys = [f"give:dmz:{sid}" for sid, s in (rel.them_border or {}).items() if s.border and not s.dmz and not s.capital]
    _BORDER_INFO.update({k: rel.them_border[k.rsplit(":", 1)[1]] for k in keys})
    return keys


def dmz_asks(rel: "Relation") -> List[str]:
    keys = [f"ask:dmz:{sid}" for sid, s in (rel.me_border or {}).items() if s.border and not s.dmz and not s.capital]
    _BORDER_INFO.update({k: rel.me_border[k.rsplit(":", 1)[1]] for k in keys})
    return keys


def dismantle_factories(tag: str, n: int) -> Optional[Tuple[int, List[str]]]:
    """Destroy n of `tag`'s military factories, from its states at random. Returns (destroyed, state names)."""
    import random
    have = []
    for line in gamestate.query(f'{tag} = {{ every_owned_state = {{ '
                                f'log = "AIRPG|[THIS.GetID]|[THIS.GetName]|[?THIS.arms_factory_level]" }} }}'):
        f = line.split("|")
        if f[0] == "AIRPG" and len(f) >= 4:
            have += [(f[1], f[2])] * int(float(f[3] or 0))
    if not have:
        return None
    taken = random.sample(have, min(n, len(have)))
    script = " ".join(f"{sid} = {{ remove_building = {{ type = arms_factory level = "
                      f"{sum(1 for s, _ in taken if s == sid)} }} }}" for sid in dict.fromkeys(s for s, _ in taken))
    if not _effect(script):
        return None
    return len(taken), list(dict.fromkeys(name for _, name in taken))


# ---------------------------------------------------------------- resource rights
# "give:rights:46": them gives me the rights to the resources of its state 46 (the output ships to me, the land
# stays theirs); "ask:rights:12": them asks me for mine. Any tradeable state with resources, in peace, without end.
#     GIVER = { give_resource_rights = { receiver = RECEIVER state = ID } }     (checked in game: no end date)
#     RECEIVER = { remove_resource_rights = ID }                                 (undoes it)

RIGHTS_NOTE = ("Resource rights hand over a state's whole output of oil, steel, aluminium and the rest, for good, "
               "while the land stays yours: a real loss to your own industry, but far cheaper than ceding the "
               "state. Weigh the amounts against what you get, and against how much you need them yourself. When you "
               "receive a state in the package and the other side wants its resources kept (or you promised them), put "
               "the rights over that state in the same package: the land passes to you first, then the rights go back. "
               "A promise of rights that is not in the commands never happens.")


def is_rights(key: str) -> bool:
    return key.startswith(("give:rights:", "ask:rights:"))


def rights_held(receiver: str, giver: str) -> Set[str]:
    """`giver`'s states whose resource rights `receiver` already holds (from `receiver`'s supply routes)."""
    try:
        block = bridge.country_block(receiver)
    except Exception:
        return set()
    out = set()
    for m in re.finditer(r'from_state=(\d+)\s+to_state=\d+\s+sender="(\w+)"\s+receiver="(\w+)"', block):
        if m.group(2) == giver and m.group(3) == receiver and \
                "given_resource_rights" in block[m.end():m.end() + 600]:
            out.add(m.group(1))
    return out


def rights_gives(rel: "Relation") -> List[str]:
    keys = [f"give:rights:{sid}" for sid, s in (rel.them_border or {}).items() if s.resources]
    _BORDER_INFO.update({k: rel.them_border[k.rsplit(":", 1)[1]] for k in keys})
    return keys


_AFTER: Set[str] = set()      # "give:rights:N" keys that name a state not theirs yet (see rights_after)


def rights_after(rel: "Relation") -> List[str]:
    """Rights over a state of mine that they ask for in the same package: I cede the land and keep its resources.
    Only meaningful with "ask:state:N" in the package (package() drops them otherwise, and apply() takes them after
    the land has changed hands)."""
    _AFTER.clear()                                  # rebuilt with each letter: a state that changed hands is no longer one
    keys = [f"give:rights:{sid}" for sid, s in (rel.me_border or {}).items()
            if s.resources and not s.capital and sid not in (rel.them_border or {})]
    _BORDER_INFO.update({k: rel.me_border[k.rsplit(":", 1)[1]] for k in keys})
    _AFTER.update(keys)
    return keys


def rights_asks(rel: "Relation") -> List[str]:
    keys = [f"ask:rights:{sid}" for sid, s in (rel.me_border or {}).items() if s.resources]
    _BORDER_INFO.update({k: rel.me_border[k.rsplit(":", 1)[1]] for k in keys})
    return keys


# ---------------------------------------------------------------- joining a war, and war pledges
# "give:war:ETH": them joins my war against Ethiopia; "ask:war:ETH": them asks me to join its war against Ethiopia.
# "give:pledge:GER": a war pledge: whenever I go to war with Germany, them joins (treaties.py keeps it and carries
# it out); "ask:pledge:GER": them asks me to pledge the same. Only between countries at peace. "Goes to war" is any
# war: declaring it, being attacked, or being called in (the UK and France "declared war" on Germany in 1939 by
# joining Poland's defensive war, which the game doesn't count as an offensive war). Unlike a guarantee it binds the
# pledger to the other's own attacks too, and it is aimed at one country.
#     JOINER = { add_to_war = { targeted_alliance = SIDE enemy = ENEMY hostility_reason = asked_to_join } }
#                                                                       (checked in game; undone by white_peace)

_COUNTRY_NAMES: Dict[str, str] = {}     # tag -> name, for war and pledge keys


def joinable_enemies(side: str, joiner: str) -> Dict[str, str]:
    """`side`'s enemies that `joiner` could join the war against: not already at war with them, not their ally,
    overlord or subject."""
    out = {}
    for line in gamestate.query(
            f'{side} = {{ every_enemy_country = {{ limit = {{ NOT = {{ tag = {joiner} }} '
            f'NOT = {{ has_war_with = {joiner} }} NOT = {{ is_in_faction_with = {joiner} }} '
            f'NOT = {{ is_subject_of = {joiner} }} NOT = {{ {joiner} = {{ is_subject_of = PREV }} }} }} '
            f'log = "AIRPW|[THIS.GetTag]|[THIS.GetName]" }} }}'):
        if line.startswith("AIRPW|"):
            tag, _, name = line[6:].partition("|")
            out[tag.strip()] = name.strip()
    _COUNTRY_NAMES.update(out)
    return out


def third_countries(them: str, me: str) -> Dict[str, Dict]:
    """Countries a war pledge between them and me may name: the major powers and both sides' neighbours, with
    whether they are at war with, allied to, friends of or rivals of either side, and whether each side is a major
    (flags me_major / them_major on every entry)."""
    out: Dict[str, Dict] = {}
    side = lambda s: (
        f'if = {{ limit = {{ is_neighbor_of = {s} }} log = "AIRPTF|[THIS.GetTag]|near_{"me" if s == me else "them"}" }} '
        f'if = {{ limit = {{ has_war_with = {s} }} log = "AIRPTF|[THIS.GetTag]|war_{"me" if s == me else "them"}" }} '
        f'if = {{ limit = {{ is_in_faction_with = {s} }} log = "AIRPTF|[THIS.GetTag]|ally_{"me" if s == me else "them"}" }} '
        f'if = {{ limit = {{ OR = {{ has_guaranteed = {s} {s} = {{ has_guaranteed = PREV }} }} }} '
        f'log = "AIRPTF|[THIS.GetTag]|friend_{"me" if s == me else "them"}" }} '
        f'if = {{ limit = {{ {s} = {{ is_embargoing = PREV }} }} '
        f'log = "AIRPTF|[THIS.GetTag]|embargoed_{"me" if s == me else "them"}" }} '
        # a quarrel the game knows: claims or cores on each other's land, or a war goal either way
        f'if = {{ limit = {{ OR = {{ any_owned_state = {{ OR = {{ is_claimed_by = {s} is_core_of = {s} }} }} '
        f'any_state = {{ is_owned_by = {s} OR = {{ is_claimed_by = PREV is_core_of = PREV }} }} '
        f'has_wargoal_against = {s} {s} = {{ has_wargoal_against = PREV }} }} }} '
        f'log = "AIRPTF|[THIS.GetTag]|claim_{"me" if s == me else "them"}" }} ')
    lines = gamestate.query(
        f'every_country = {{ limit = {{ OR = {{ tag = {them} tag = {me} }} }} '
        f'log = "AIRPTS|[THIS.GetTag]|[THIS.GetRulingIdeology]|[THIS.Capital.GetContinent]" '
        f'if = {{ limit = {{ is_major = yes }} log = "AIRPTM|[THIS.GetTag]" }} }} '
        f'every_country = {{ limit = {{ NOT = {{ tag = {them} }} NOT = {{ tag = {me} }} '
        f'OR = {{ is_major = yes is_neighbor_of = {me} is_neighbor_of = {them} }} }} '
        f'log = "AIRPT|[THIS.GetTag]|[THIS.GetName]|[THIS.GetRulingIdeology]|[THIS.Capital.GetContinent]" '
        f'if = {{ limit = {{ is_major = yes }} log = "AIRPTF|[THIS.GetTag]|major" }} '
        + side(me) + side(them) +
        f'if = {{ limit = {{ OR = {{ is_subject_of = {me} is_subject_of = {them} {me} = {{ is_subject_of = PREV }} '
        f'{them} = {{ is_subject_of = PREV }} }} }} log = "AIRPTF|[THIS.GetTag]|bound" }} }}')
    ideology: Dict[str, str] = {}
    continent: Dict[str, str] = {}
    majors = set()
    for line in lines:
        f = [x.strip() for x in line.split("|")]
        if f[0] == "AIRPTS" and len(f) >= 4:
            ideology[f[1]], continent[f[1]] = f[2], f[3]
        elif f[0] == "AIRPTM" and len(f) >= 2:
            majors.add(f[1])
        elif f[0] == "AIRPT" and len(f) >= 5:
            out[f[1]] = {"name": f[2], "ideology": f[3], "continent": f[4], "flags": set()}
            _COUNTRY_NAMES[f[1]] = f[2]
        elif f[0] == "AIRPTF" and len(f) >= 3 and f[1] in out:
            out[f[1]]["flags"].add(f[2])
    for info in out.values():
        fl = info["flags"]
        for who, tag in (("me", me), ("them", them)):
            if tag in majors:
                fl.add(f"{who}_major")
            if continent.get(tag) == info["continent"]:          # capitals on the same continent
                fl.add(f"continent_{who}")
            # A threat to this side: a quarrel over land or a war goal; or a major power of another ideology next
            # door (or, for a major, anywhere on its continent). Two non-aligned governments can be anything to
            # each other, so they count as different.
            differ = info["ideology"] != ideology.get(tag, "") or info["ideology"] in ("non-aligned", "")
            close = f"near_{who}" in fl or (tag in majors and f"continent_{who}" in fl)
            if f"claim_{who}" in fl or ("major" in fl and differ and close):
                fl.add(f"threat_{who}")
    return out


# New war pledges are off: the AI asked for them too often and against enemies that made no sense. Pledges already
# made are still kept by treaties.py. True brings them back (pledge_gives/asks, PLEDGE_NOTE in WAR_NOTE).
PLEDGES = False

_WAR_JOIN = ("Joining a war is the gravest commitment short of submission: your soldiers die in another's cause, and "
             "the enemy will treat you as one. A government joins only where its own interests are at stake (an "
             "enemy it fears or covets, a friend it cannot let fall) or for a great price, and only if it can fight; "
             "it asks it of a country able to help.")
PLEDGE_NOTE = ("A war pledge is a promise for later: whenever one side goes to war with "
            "the named country (declaring war itself, being attacked, or called in by an ally or guarantee), the "
            "other joins at once, as Italy pledged itself to Germany in the Pact of Steel. Unlike a guarantee it "
            "binds you to that side's own attacks too: give it only to a power whose wars against that enemy you "
            "want to share. The game holds you to the pledges you give for as long as the treaty stands. Pledges are "
            "rare: a country seeks one only against an enemy both sides expect to fight, when the other's army would "
            "count in that war, and most often as a mutual pledge between allies; asking a country to join your wars "
            "while pledging nothing back is what a patron asks of a client. Most policies and letters have none.")
WAR_NOTE = _WAR_JOIN + (" " + PLEDGE_NOTE if PLEDGES else "")


def is_war(key: str) -> bool:
    return key.startswith(("give:war:", "ask:war:"))


def is_pledge(key: str) -> bool:
    return key.startswith(("give:pledge:", "ask:pledge:"))


def war_gives(rel: "Relation") -> List[str]:
    return [f"give:war:{t}" for t in (rel.me_enemies or {})]


def war_asks(rel: "Relation") -> List[str]:
    return [f"ask:war:{t}" for t in (rel.them_enemies or {})]


def _targetable(info: Dict, attacker: str, joiner: str) -> bool:
    """May a war pledge name this country, `attacker` ("me"/"them") the side going to war with it and `joiner` the
    side pledging to join? Only a common enemy: a threat to both sides (a quarrel over land or a war goal, or a major
    power of another ideology next door or on a major's continent), and no friend, ally, enemy already, overlord or
    subject of either."""
    f = info["flags"]
    if {f"war_{attacker}", f"war_{joiner}", f"ally_{attacker}", f"ally_{joiner}", f"friend_{attacker}",
            f"friend_{joiner}", "bound"} & f:
        return False
    return {f"threat_{attacker}", f"threat_{joiner}"} <= f


def pledge_gives(rel: "Relation") -> List[str]:
    if not PLEDGES:
        return []
    return [k for t, i in (rel.third or {}).items()
            if _targetable(i, "me", "them") and (k := f"give:pledge:{t}") not in rel.pledged]


def pledge_asks(rel: "Relation") -> List[str]:
    if not PLEDGES:
        return []
    return [k for t, i in (rel.third or {}).items()
            if _targetable(i, "them", "me") and (k := f"ask:pledge:{t}") not in rel.pledged]


# ---------------------------------------------------------------- commitments toward a third country
# "give:oppose:ITA": them commits to work against Italy alongside me; "give:befriend:HUN": to seek Hungary's friendship
# at my wish; "give:back:YUG": to stand behind Yugoslavia with me; "ask:...": them asks me for the same. A standing
# line, kept as a treaty clause for as long as its treaty stands (treaties.py); them's own commitments steer HOI4's AI
# toward that country (steering.py). The player's are never enforced: treaties.py notices a step against one (a pact
# with a country it promised to oppose, war on one it promised to back) and remembers it.
# "give:sanction:ITA": them embargoes Italy now, at my wish ("ask:": I do). A step, not a clause.
#     A = { send_embargo = X }
ALIGN_KINDS = ("oppose", "befriend", "back")
THIRD_KINDS = ALIGN_KINDS + ("sanction",)
ALIGN_NOTE = ("A commitment toward a third country (oppose, befriend, back) is a standing diplomatic line, not a war and "
              "not a promise to fight: like the Stresa Front against German expansion, or a great power taking a small "
              "state under its wing. The committed government's diplomacy follows it for as long as the treaty stands: "
              "opposing a country means no favours to it (no access, no trade, no help) and support for those it "
              "threatens; befriending one means courting it; backing one means protecting it and sending it aid once "
              "the world's tension allows. It costs little at once but binds a government's hands: give one only where "
              "it serves your own interests too, and ask it of a country whose stance would count. A pact with, a "
              "guarantee of or military access for a country one committed to oppose, or war on or an embargo of one "
              "it committed to befriend or back, breaks the commitment, and it is remembered. A sanction (a trade "
              "embargo on a third country) is a step taken at once, like the League's sanctions on Italy in 1935.")
# kind -> (give prompt, ask prompt, give label, ask label, give shown, ask shown, give remembered, ask remembered);
# {x} the third country's name, {t} its tag
_COMMIT = {
    "oppose": ("commit to work against {x} ({t}) alongside {me_name}: a standing diplomatic line against it (no "
               "favours to it, support for those it threatens), not a war",
               "ask {me_name} to commit to working against {x} ({t}) alongside you",
               "a commitment to work against {x}", "our commitment to work against {x}",
               "{them_name} committed to work against {x} with us.",
               "We committed to work against {x} alongside {them_name}.",
               "{them_name} committed to work against {x} alongside {me_name}.",
               "{me_name} committed to work against {x} alongside {them_name}."),
    "befriend": ("commit to seek friendship with {x} ({t}), as {me_name} wishes: court it, improve relations with it "
                 "and look kindly on its requests",
                 "ask {me_name} to commit to seeking friendship with {x} ({t})",
                 "a commitment to befriend {x}", "our commitment to befriend {x}",
                 "{them_name} committed to seek friendship with {x}.",
                 "We committed to seek friendship with {x}, as {them_name} wished.",
                 "{them_name} committed to seek friendship with {x}, as {me_name} wished.",
                 "{me_name} committed to seek friendship with {x}, as {them_name} wished."),
    "back": ("commit to stand behind {x} ({t}) with {me_name}: protect it and support it (guarantees and aid once the "
             "world's tension allows)",
             "ask {me_name} to commit to standing behind {x} ({t}) with you",
             "a commitment to back {x}", "our commitment to back {x}",
             "{them_name} committed to stand behind {x} with us.",
             "We committed to stand behind {x} alongside {them_name}.",
             "{them_name} committed to stand behind {x} alongside {me_name}.",
             "{me_name} committed to stand behind {x} alongside {them_name}."),
    "sanction": ("impose a trade embargo on {x} ({t}) at once, at {me_name}'s wish",
                 "ask {me_name} to impose a trade embargo on {x} ({t})",
                 "a trade embargo on {x}", "our trade embargo on {x}",
                 "{them_name} imposed a trade embargo on {x}.", "We imposed a trade embargo on {x}.",
                 "{them_name} imposed a trade embargo on {x} at {me_name}'s wish.",
                 "{me_name} imposed a trade embargo on {x} at {them_name}'s wish."),
}


def third_kind(key: str) -> str:
    """'give:oppose:ITA' -> 'oppose' ('' for anything but a commitment or sanction)."""
    parts = key.split(":")
    return parts[1] if len(parts) == 3 and parts[0] in ("give", "ask") and parts[1] in THIRD_KINDS else ""


def is_align(key: str) -> bool:
    return third_kind(key) in ALIGN_KINDS


def is_sanction(key: str) -> bool:
    return third_kind(key) == "sanction"


def is_third(key: str) -> bool:
    """A deal item naming a third country: a war pledge, a commitment or a sanction."""
    return is_pledge(key) or bool(third_kind(key))


def _commit_ok(kind: str, info: Dict, actor: str, ben: str) -> bool:
    """May `actor` ("me"/"them") take this line toward the country, for `ben`? Never toward a country bound to
    either side or at war with `actor`. Opposing or sanctioning: a threat to `ben` that is no ally or friend of either
    side (and not embargoed by `actor` already). Befriending or backing: no threat to or enemy of `ben`, not in
    `actor`'s faction or embargoed by it; backing only a lesser power near one of them or a friend of `ben`."""
    f = info["flags"]
    if "bound" in f or f"war_{actor}" in f:
        return False
    # within reach of the committing side: a major, or a country next to it or on its continent (Britain borders
    # Guatemala through a colony; Austria has no line toward it)
    if not ("major" in f or {f"near_{actor}", f"continent_{actor}"} & f):
        return False
    if kind in ("oppose", "sanction"):
        if {f"ally_{actor}", f"friend_{actor}", f"ally_{ben}", f"friend_{ben}"} & f:
            return False
        # and a stake of `actor`'s own: its neighbour, a threat to it, or a great power facing a great power (Britain
        # took no line against Bulgaria for Romania's sake; Romania collected four such lines in a test year)
        stake = bool({f"near_{actor}", f"threat_{actor}"} & f) or ("major" in f and f"{actor}_major" in f)
        return f"threat_{ben}" in f and stake and not (kind == "sanction" and f"embargoed_{actor}" in f)
    if {f"threat_{ben}", f"war_{ben}", f"ally_{actor}", f"embargoed_{actor}"} & f:
        return False
    if kind == "befriend" and f"friend_{actor}" in f:
        return False            # a guarantee either way already binds them (Czechoslovakia "courting" France, 1936)
    # and one that matters to the side it is for: its neighbour or friend, or a major on its continent
    near_ben = bool({f"near_{ben}", f"friend_{ben}", f"ally_{ben}"} & f)
    if kind == "back":
        return "major" not in f and near_ben
    return near_ben or ("major" in f and f"continent_{ben}" in f)


def commit_gives(rel: "Relation") -> List[str]:
    return [k for t, i in (rel.third or {}).items() for kind in THIRD_KINDS
            if _commit_ok(kind, i, "them", "me") and (k := f"give:{kind}:{t}") not in rel.pledged]


def commit_asks(rel: "Relation") -> List[str]:
    return [k for t, i in (rel.third or {}).items() for kind in THIRD_KINDS
            if _commit_ok(kind, i, "me", "them") and (k := f"ask:{kind}:{t}") not in rel.pledged]


def third_gives(rel: "Relation") -> List[str]:
    """Items naming a third country them could give: pledges (when on), commitments, sanctions."""
    return pledge_gives(rel) + commit_gives(rel)


def third_asks(rel: "Relation") -> List[str]:
    return pledge_asks(rel) + commit_asks(rel)


def war_script(joiner: str, side: str, enemy: str) -> str:
    return (f"{joiner} = {{ add_to_war = {{ targeted_alliance = {side} enemy = {enemy} "
            f"hostility_reason = asked_to_join }} }}")


# ---------------------------------------------------------------- equipment as currency
# "give:rifles:2000": them sends me 2000 infantry equipment from its stockpile; "ask:trucks:300": them asks me for
# 300 trucks. Only what lies in the stockpile (not what its divisions hold).
#     GIVER = { send_equipment = { equipment = infantry_equipment amount = N target = RECEIVER } }  (checked in game)

EQUIPMENT = {"rifles": ("infantry_equipment", "infantry equipment (rifles)"),
             "artillery": ("artillery_equipment", "artillery pieces"),
             "trucks": ("motorized_equipment", "trucks")}
EQUIPMENT_NOTE = ("Equipment is sent from the stockpile, not taken from divisions: arms aid of the kind governments "
                  "send friends and clients, or pay for what they want. Weigh it against your own army's needs.")


def equipment_counts(tag: str) -> Dict[str, int]:
    fields = "|".join(f"[?THIS.num_equipment@{arch}]" for arch, _ in EQUIPMENT.values())
    for line in gamestate.query(f'{tag} = {{ log = "AIRPQ|{fields}" }}'):
        if line.startswith("AIRPQ|"):
            vals = line[6:].split("|")
            try:
                return {k: int(float(v)) for k, v in zip(EQUIPMENT, vals)}
            except ValueError:
                pass
    return {k: 0 for k in EQUIPMENT}


def is_equipment_key(key: str) -> bool:
    parts = key.split(":")
    return len(parts) >= 2 and parts[0] in ("give", "ask") and parts[1] in EQUIPMENT


# "give:subject:SLO": them hands me its subject; "ask:subject:SLO": them asks me to hand it my subject.
#     OLD = { end_puppet = SUB } NEW = { puppet = SUB }
_SUBJECT_NAMES: Dict[str, str] = {}


# ---------------------------------------------------------------- factories as currency
# "give:civ:10": them gives me 10 civilian factories; "ask:mil:5": them asks me for 5 military factories. The
# factories come out of the giver's states at random (weighted by how many each has) and are built instantly in the
# receiver's owned, controlled core states at random, with an extra building slot where a state has none free.

FACTORIES = {"civ": ("industrial_complex", "civilian factories"), "mil": ("arms_factory", "military factories")}
MAX_FACTORIES = 30
FACTORY_NOTE = ("Factories are a nation's wealth and its power to arm: giving them away weakens you for years. Trade "
                "them only for something worth as much (a province, a subject, real protection), in amounts that "
                "match what is at stake.")


# ---------------------------------------------------------------- a change of government
# "give:govt:fascism": them puts its fascist party in power; "ask:govt:democratic": them asks me to.
#     X = { set_politics = { ruling_party = IDEOLOGY elections_allowed = yes/no } add_popularity = { ... } }

GOVT_NOTE = ("A change of government at a foreign power's demand is a humiliation few regimes accept: a government "
             "agrees only under real threat, when far weaker, or when that party is already strong at home or close "
             "to it in ideology; it demands one only of a country it could compel.")


def set_parties(rel: "Relation", them: "gamestate.CountryState", me: "gamestate.CountryState") -> None:
    """Fill in the parties each side could put in power (all but the ruling one)."""
    pick = lambda s: {p.ideology: f"{p.name} ({p.ideology_name or p.ideology})" for p in s.parties if not p.ruling}
    rel.them_parties, rel.me_parties = pick(them), pick(me)
    _PARTY_LABELS.update({f"give:govt:{i}": n for i, n in rel.them_parties.items()})
    _PARTY_LABELS.update({f"ask:govt:{i}": n for i, n in rel.me_parties.items()})


def is_govt_key(key: str) -> bool:
    return key.startswith(("give:govt:", "ask:govt:"))


def govt_gives(rel: "Relation") -> List[str]:
    return [f"give:govt:{i}" for i in (rel.them_parties or {})]


def govt_asks(rel: "Relation") -> List[str]:
    return [f"ask:govt:{i}" for i in (rel.me_parties or {})]


_PARTY_LABELS: Dict[str, str] = {}


def _govt_script(tag: str, ideology: str) -> str:
    elections = "yes" if ideology == "democratic" else "no"
    return (f"{tag} = {{ set_politics = {{ ruling_party = {ideology} elections_allowed = {elections} }} "
            f"add_popularity = {{ ideology = {ideology} popularity = 0.3 }} }}")


FACTORY_KEYS = ("give:civ", "give:mil", "ask:civ", "ask:mil",    # amount chosen in the command
                "give:dismantle", "ask:dismantle")
EQUIPMENT_KEYS = tuple(f"{v}:{k}" for k in EQUIPMENT for v in ("give", "ask"))
AMOUNT_KEYS = FACTORY_KEYS + EQUIPMENT_KEYS
AMOUNT_KINDS = ("civ", "mil", "dismantle") + tuple(EQUIPMENT)
EQUIPMENT_GIVES = [k for k in EQUIPMENT_KEYS if k.startswith("give:")]
EQUIPMENT_ASKS = [k for k in EQUIPMENT_KEYS if k.startswith("ask:")]
ECONOMY: Dict[str, str] = {}   # {"text": both sides' industry} for the help of the letter being written (economy())
UNION: Dict[str, str] = {}     # {"note": ...} while the letter being written is from a country whose aim is union (union())


def union(me_name: str, wants: bool) -> None:
    """Set (or clear) the note that replaces SUBMIT_NOTE for the letter being written: the country's own aim is to
    become one state with the player, so annexation is where its path leads, not a last resort."""
    UNION.clear()
    if wants:
        UNION["note"] = (f"UNION. Your government has decided on its own that it wants union with {me_name}: to become "
                         f"one state (annexation by agreement). For you that is the end of a path, not a last "
                         f"resort. Weigh the terms as a proud government would: your name and rights, your "
                         f"institutions, your people's voice, the timing, what the other powers will do. Agree "
                         f"when the terms are met, or say plainly what is still missing; do not agree in the same "
                         f"breath as small favours, and do not pretend you have not decided.")


def economy(them_name: str, me_name: str, rel: "Relation") -> str:
    """Both sides' industry, so offers of land or factories can be weighed against what each has."""
    t, m = rel.them_factories or {}, rel.me_factories or {}
    ECONOMY["text"] = (f"Industry: you ({them_name}) have {t.get('civ', 0)} civilian and {t.get('mil', 0)} military "
                       f"factories; {me_name} has {m.get('civ', 0)} civilian and {m.get('mil', 0)} military. Weigh "
                       f"factories offered or asked as a share of what the giver has.")
    stock = lambda e: ", ".join(f"{(e or {}).get(k, 0):,} {EQUIPMENT[k][1]}" for k in EQUIPMENT)
    ECONOMY["equipment"] = (f"In stock: you ({them_name}) have {stock(rel.them_equipment)}; {me_name} has "
                            f"{stock(rel.me_equipment)}.")
    return ECONOMY["text"]


def is_factory_key(key: str) -> bool:
    parts = key.split(":")
    return len(parts) == 3 and parts[0] in ("give", "ask") and parts[1] in AMOUNT_KINDS and parts[2].isdigit()


def factory_base(key: str) -> str:
    """'give:civ:10' -> 'give:civ' (what the allowed lists hold); other keys unchanged."""
    return key.rsplit(":", 1)[0] if is_factory_key(key) else key


def factory_counts(tag: str) -> Dict[str, int]:
    lines = gamestate.query(f'{tag} = {{ log = "AIRPF|[?THIS.num_of_civilian_factories]|'
                            f'[?THIS.num_of_military_factories]" }}')
    for line in lines:
        if line.startswith("AIRPF|"):
            civ, _, mil = line[6:].partition("|")
            try:
                return {"civ": int(float(civ)), "mil": int(float(mil))}
            except ValueError:
                pass
    return {"civ": 0, "mil": 0}


def transfer_factories(giver: str, receiver: str, kind: str, n: int) -> Optional[Tuple[int, List[str], List[str]]]:
    """Move n factories of `kind` ("civ"/"mil"). Returns (moved, giver state names, receiver state names)."""
    import random
    building = FACTORIES[kind][0]
    lvl = f"{building}_level" if kind == "civ" else "arms_factory_level"
    have, room = [], []
    for line in gamestate.query(
            f'{giver} = {{ every_owned_state = {{ log = "AIRPG|[THIS.GetID]|[THIS.GetName]|[?THIS.{lvl}]" }} }} '
            f'{receiver} = {{ every_owned_state = {{ limit = {{ is_core_of = {receiver} is_controlled_by = {receiver} }} '
            f'log = "AIRPR|[THIS.GetID]|[THIS.GetName]" }} }}'):
        f = line.split("|")
        if f[0] == "AIRPG" and len(f) >= 4:
            have += [(f[1], f[2])] * int(float(f[3] or 0))
        elif f[0] == "AIRPR" and len(f) >= 3:
            room.append((f[1], f[2]))
    if not have or not room:
        return None
    taken = random.sample(have, min(n, len(have)))
    placed = [random.choice(room) for _ in taken]
    script = []
    for sid in dict.fromkeys(s for s, _ in taken):
        script.append(f"{sid} = {{ remove_building = {{ type = {building} level = {sum(1 for s, _ in taken if s == sid)} }} }}")
    for sid, _ in placed:
        script.append(f"{sid} = {{ if = {{ limit = {{ NOT = {{ free_building_slots = {{ building = {building} size > 0 "
                      f"include_locked = yes }} }} }} add_extra_state_shared_building_slots = 1 }} "
                      f"add_building_construction = {{ type = {building} level = 1 instant_build = yes }} }}")
    if not _effect(" ".join(script)):
        return None
    names = lambda pairs: list(dict.fromkeys(name for _, name in pairs))
    return len(taken), names(taken), names(placed)


def is_subject_key(key: str) -> bool:
    return key.startswith(("give:subject:", "ask:subject:"))


def subject_gives(rel: Relation) -> List[str]:
    _SUBJECT_NAMES.update(rel.them_subjects or {})
    return [f"give:subject:{t}" for t in (rel.them_subjects or {})]


def subject_asks(rel: Relation) -> List[str]:
    _SUBJECT_NAMES.update(rel.me_subjects or {})
    return [f"ask:subject:{t}" for t in (rel.me_subjects or {})]


def state_gives(rel: Relation) -> List[str]:
    keys = [f"give:state:{sid}" for sid, s in (rel.them_border or {}).items() if not s.capital]
    _BORDER_INFO.update({k: rel.them_border[k.rsplit(":", 1)[1]] for k in keys})
    return keys


def state_asks(rel: Relation) -> List[str]:
    keys = [f"ask:state:{sid}" for sid, s in (rel.me_border or {}).items() if not s.capital]
    _BORDER_INFO.update({k: rel.me_border[k.rsplit(":", 1)[1]] for k in keys})
    return keys


def _amount(n: str, plural: str) -> str:
    """'1 civilian factory', '3 civilian factories', '1 truck'."""
    if n != "1":
        return f"{n} {plural}"
    return "1 " + plural.replace("factories", "factory").replace("pieces", "piece").replace("trucks", "truck")


def get_deal(key: str) -> Optional[Deal]:
    if is_govt_key(key):
        verb, _, ideology = key.split(":")
        label = _PARTY_LABELS.get(key, ideology)
        if verb == "give":
            return Deal(key, "govt", False, True, f"change your government: put the {label} in power",
                        f"a new government in their country: the {label}", "",
                        f"{{them_name}} changed its government: the {label} now rules.",
                        f"{{them_name}} put the {label} in power at {{me_name}}'s wish.")
        return Deal(key, "govt", False, True, f"demand that {{me_name}} change its government, putting its {label} "
                    f"in power", f"a new government in our country: the {label}", "",
                    f"We changed our government: the {label} now rules.",
                    f"{{me_name}} put its {label} in power at {{them_name}}'s demand.", True)
    if is_dmz(key):
        sid = key.rsplit(":", 1)[1]
        info = _BORDER_INFO.get(key)
        name = info.name if info else _STATE_NAMES.get(sid, f"state {sid}")
        label = f"a demilitarized {name}"
        if key.startswith("give:"):
            return Deal(key, "dmz", False, True, f"demilitarize your state {name} on the border with {{me_name}}: "
                        f"no troops or fortifications there, for good", label,
                        f"{sid} = {{{{ set_demilitarized_zone = yes }}}}",
                        f"{{them_name}} demilitarized {name}.", f"{{them_name}} demilitarized {name} at "
                        f"{{me_name}}'s wish.")
        return Deal(key, "dmz", False, True, f"demand that {{me_name}} demilitarize its state {name} on your "
                    f"border: no troops or fortifications there, for good", label,
                    f"{sid} = {{{{ set_demilitarized_zone = yes }}}}",
                    f"We demilitarized {name}.", f"{{me_name}} demilitarized {name} at {{them_name}}'s demand.", True)
    if key.startswith(("give:dismantle", "ask:dismantle")):
        n = (key + ":N").split(":")[2]
        what = _amount(n, "military factories")
        if key.startswith("give:"):
            return Deal(key, "dismantle", False, True, f"dismantle {what} of yours (destroyed, not handed over)",
                        f"dismantling {what}", "", f"{{them_name}} dismantled {what}.",
                        f"{{them_name}} dismantled {what} at {{me_name}}'s wish.")
        return Deal(key, "dismantle", False, True, f"demand that {{me_name}} dismantle {what} of its own "
                    f"(destroyed, not handed over)", f"dismantling {what}", "", f"We dismantled {what}.",
                    f"{{me_name}} dismantled {what} at {{them_name}}'s demand.", True)
    if is_war(key) or is_pledge(key):
        verb, what, tag = key.split(":")
        name = _COUNTRY_NAMES.get(tag, tag)
        if what == "war":
            if verb == "give":
                return Deal(key, "war", False, True, f"join {{me_name}}'s war against {name} ({tag}) at once",
                            f"to join our war against {name}", "",
                            f"{{them_name}} joined our war against {name}.",
                            f"{{them_name}} joined {{me_name}}'s war against {name}.")
            return Deal(key, "war", False, True, f"ask {{me_name}} to join your war against {name} ({tag})",
                        f"our joining their war against {name}", "",
                        f"We joined {{them_name}}'s war against {name}.",
                        f"{{me_name}} joined {{them_name}}'s war against {name}.", True)
        if verb == "give":
            return Deal(key, "pledge", False, True, f"pledge to join {{me_name}} at once whenever {{me_name}} goes "
                        f"to war with {name} ({tag}), whoever starts it", f"a pledge to join our wars with {name}", "",
                        f"{{them_name}} pledged to join us whenever we go to war with {name}.",
                        f"{{them_name}} pledged to join {{me_name}} whenever {{me_name}} goes to war with {name}.")
        return Deal(key, "pledge", False, True, f"ask {{me_name}} to pledge to join you at once whenever you go to "
                    f"war with {name} ({tag}), whoever starts it", f"our pledge to join their wars with {name}", "",
                    f"We pledged to join {{them_name}} whenever it goes to war with {name}.",
                    f"{{me_name}} pledged to join {{them_name}} whenever {{them_name}} goes to war with {name}.", True)
    if third_kind(key):
        verb, kind, tag = key.split(":")
        sub = lambda s: s.replace("{x}", _COUNTRY_NAMES.get(tag, tag)).replace("{t}", tag)
        g = 0 if verb == "give" else 1
        texts = _COMMIT[kind]
        actor = "{them}" if g == 0 else "{me}"
        effect = f"{actor} = {{{{ send_embargo = {tag} }}}}" if kind == "sanction" else ""
        return Deal(key, "sanction" if kind == "sanction" else "commit", False, True, sub(texts[g]),
                    sub(texts[2 + g]), effect, sub(texts[4 + g]), sub(texts[6 + g]))
    if is_equipment_key(key):
        verb, kind, n = (key + ":0").split(":")[:3]
        what = _amount(n if n != '0' else 'N', EQUIPMENT[kind][1])
        if verb == "give":
            return Deal(key, "equipment", False, True, f"send {{me_name}} {what} from your stockpile", what, "",
                        f"{{them_name}} sent us {what}.", f"{{them_name}} sent {{me_name}} {what}.")
        return Deal(key, "equipment", False, True, f"ask {{me_name}} to send you {what}", what, "",
                    f"We sent {{them_name}} {what}.", f"{{me_name}} sent {{them_name}} {what}.", True)
    if is_factory_key(key) or key in FACTORY_KEYS:
        verb, kind, n = (key + ":0").split(":")[:3]
        what = _amount(n if n != "0" else "N", FACTORIES[kind][1])
        if verb == "give":
            return Deal(key, "factories", False, True, f"give {{me_name}} {what} of yours", what, "",
                        f"{{them_name}} gave us {what}.", f"{{them_name}} gave {{me_name}} {what}.")
        return Deal(key, "factories", False, True, f"ask {{me_name}} to give you {what}", what, "",
                    f"We gave {{them_name}} {what}.", f"{{me_name}} gave {{them_name}} {what}.", True)
    if is_subject_key(key):
        tag = key.rsplit(":", 1)[1]
        name = _SUBJECT_NAMES.get(tag, tag)
        if key.startswith("give:"):
            return Deal(key, "subject", False, True, f"hand your subject {name} ({tag}) over to {{me_name}}, as its "
                        f"subject", f"their subject {name}", f"{{them}} = {{{{ end_puppet = {tag} }}}} "
                        f"{{me}} = {{{{ puppet = {tag} }}}}", f"{{them_name}} handed {name} over to us.",
                        f"{{them_name}} handed its subject {name} over to {{me_name}}.")
        return Deal(key, "subject", False, True, f"ask {{me_name}} to hand you its subject {name} ({tag})",
                    f"our subject {name}", f"{{me}} = {{{{ end_puppet = {tag} }}}} {{them}} = {{{{ puppet = {tag} }}}}",
                    f"We handed {name} over to {{them_name}}.", f"{{me_name}} handed its subject {name} over to "
                    f"{{them_name}}.", True)
    if is_rights(key):
        sid = key.rsplit(":", 1)[1]
        info = _BORDER_INFO.get(key)
        name = info.name if info else _STATE_NAMES.get(sid, f"state {sid}")
        what = f"{name} ({info.resources})" if info and info.resources else name
        label = f"the resource rights in {what}"
        if key.startswith("give:"):
            return Deal(key, "rights", False, True, f"give {{me_name}} the rights to the resources of your state "
                        f"{what}: its output goes to them for good, the land stays yours", label,
                        f"{{them}} = {{{{ give_resource_rights = {{{{ receiver = {{me}} state = {sid} }}}} }}}}",
                        f"{{them_name}} gave us the resource rights in {name}.",
                        f"{{them_name}} gave {{me_name}} the resource rights in {name}.")
        return Deal(key, "rights", False, True, f"ask {{me_name}} for the rights to the resources of its state "
                    f"{what}: its output would come to you for good", label,
                    f"{{me}} = {{{{ give_resource_rights = {{{{ receiver = {{them}} state = {sid} }}}} }}}}",
                    f"We gave {{them_name}} the resource rights in {name}.",
                    f"{{me_name}} gave {{them_name}} the resource rights in {name}.", True)
    if not is_state(key):
        return BY_KEY.get(key)
    sid = key.rsplit(":", 1)[1]
    info = _BORDER_INFO.get(key)
    name = info.name if info else _STATE_NAMES.get(sid, f"state {sid}")
    if key.startswith("give:"):
        note = info.note("{me_name}") if info else name
        return Deal(key, "state", False, True, f"cede {note} to {{me_name}}", name,
                    f"{{me}} = {{{{ transfer_state = {sid} }}}}",
                    f"{{them_name}} ceded {name} to us.", f"{{them_name}} ceded {name} to {{me_name}}.")
    note = info.note("{them_name}") if info else name
    return Deal(key, "state", False, True, f"ask {{me_name}} to cede {note} to you", name,
                f"{{them}} = {{{{ transfer_state = {sid} }}}}",
                f"We ceded {name} to {{them_name}}.", f"{{me_name}} ceded {name} to {{them_name}}.", True)


def possible(rel: Relation, key: str) -> bool:
    if not tension_allows(rel, key):
        return False
    if is_govt_key(key):
        verb, _, ideology = key.split(":")
        parties = rel.them_parties if verb == "give" else rel.me_parties
        # Not at war; a subject's government is its overlord's affair (the player's own subject may be made to)
        subject = rel.them_subject if verb == "give" else rel.me_subject
        allowed_subject = rel.bound == ("subject" if verb == "give" else "overlord")
        return not rel.at_war and ideology in (parties or {}) and (not subject or allowed_subject)
    if is_dmz(key):
        sid = key.rsplit(":", 1)[1]
        give = key.startswith("give:")
        if (rel.them_subject and rel.bound != "subject") if give else (rel.me_subject and rel.bound != "overlord"):
            return False                               # a subject's land is its overlord's affair
        state = ((rel.them_border if give else rel.me_border) or {}).get(sid)
        return not rel.at_war and bool(state and state.border and not state.dmz and not state.capital)
    if is_equipment_key(key):                                  # stockpiled equipment; amount in the command
        parts = key.split(":")
        have = ((rel.them_equipment if parts[0] == "give" else rel.me_equipment) or {}).get(parts[1], 0)
        n = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 1
        return not rel.at_war and 0 < n <= have
    if is_war(key):
        verb, _, tag = key.split(":")
        joiner_subject = rel.them_subject if verb == "give" else rel.me_subject
        enemies = (rel.me_enemies if verb == "give" else rel.them_enemies) or {}
        return not rel.at_war and not joiner_subject and tag in enemies
    if is_pledge(key):
        if not PLEDGES:
            return False
        verb, _, tag = key.split(":")
        info = (rel.third or {}).get(tag)
        joiner_subject = rel.them_subject if verb == "give" else rel.me_subject
        return (not rel.at_war and not joiner_subject and rel.bound not in ("subject", "overlord")
                and bool(info) and _targetable(info, "me" if verb == "give" else "them",
                                               "them" if verb == "give" else "me"))
    if third_kind(key):
        verb, kind, tag = key.split(":")
        info = (rel.third or {}).get(tag)
        actor, ben = ("them", "me") if verb == "give" else ("me", "them")
        return not rel.at_war and bool(info) and _commit_ok(kind, info, actor, ben)
    if key in ("give:faction", "ask:join"):                    # I join them's faction
        return (not rel.at_war and not rel.bound and rel.them_leader and not rel.me_in_faction
                and not rel.me_subject and not rel.them_subject)
    if key in ("give:join", "ask:faction"):                    # them joins mine
        return (not rel.at_war and not rel.bound and rel.me_leader and not rel.them_in_faction
                and not rel.me_subject and not rel.them_subject)
    if key == "end:faction":
        return rel.bound == "faction"
    if key == "give:leave":                                    # them leaves its own faction (not ours)
        return not rel.at_war and rel.bound != "faction" and rel.them_in_faction and not rel.them_leader
    if key == "ask:leave":                                     # I leave mine
        return not rel.at_war and rel.bound != "faction" and rel.me_in_faction and not rel.me_leader
    if key in FACTORY_KEYS:                                                # the amount comes with the command
        side = rel.them_factories if key.startswith("give:") else rel.me_factories
        kind = key.split(":")[1]
        return not rel.at_war and (side or {}).get("mil" if kind == "dismantle" else kind, 0) > 0
    if is_factory_key(key):
        verb, kind, n = key.split(":")
        side = rel.them_factories if verb == "give" else rel.me_factories
        have = (side or {}).get("mil" if kind == "dismantle" else kind, 0)
        return not rel.at_war and 0 < int(n) <= min(MAX_FACTORIES, have)
    if is_subject_key(key):
        tag = key.rsplit(":", 1)[1]
        return not rel.at_war and tag in ((rel.them_subjects if key.startswith("give:") else rel.me_subjects) or {})
    if is_rights(key):
        sid = key.rsplit(":", 1)[1]
        give = key.startswith("give:")
        if (rel.them_subject and rel.bound != "subject") if give else (rel.me_subject and rel.bound != "overlord"):
            return False                               # a subject's resources are its overlord's to give
        state = ((rel.them_border if give else rel.me_border) or {}).get(sid)
        if not state and give and key in _AFTER:         # the land is still mine: it is theirs once the package runs
            state = (rel.me_border or {}).get(sid)
        held = (rel.them_rights if give else rel.me_rights) or set()
        return not rel.at_war and bool(state and state.resources) and sid not in held
    if is_state(key):
        sid = key.rsplit(":", 1)[1]
        if key.startswith("give:") and rel.them_subject and rel.bound != "subject":
            return False                               # a subject's land is its overlord's to give
        if key.startswith("ask:") and rel.me_subject and rel.bound != "overlord":
            return False
        border = (rel.them_border if key.startswith("give:") else rel.me_border) or {}
        return not rel.at_war and sid in border and not border[sid].capital
    if key == "give:peace":
        return rel.at_war and rel.peace_ok
    if key == "give:annex":
        # Peace only. Them may be my subject (a puppet integrated by agreement), not anyone else's; I must be
        # independent.
        return (not rel.at_war and rel.bound in ("", "faction", "subject")
                and (not rel.them_subject or rel.bound == "subject") and not rel.me_subject)
    if key in ("give:submit", "ask:submit"):
        # Never at war or between a subject and its overlord; both sides must be independent (a subject can't
        # take subjects of its own). Fellow faction members may.
        return (not rel.at_war and rel.bound in ("", "faction")
                and not rel.them_subject and not rel.me_subject)
    if key == "give:docking" and not (rel.them_ports and rel.me_navy):     # a coast to give, ships to use it
        return False
    if key == "ask:docking" and not (rel.me_ports and rel.them_navy):
        return False
    if rel.bound and key not in BOUND_ALLOWED:
        return False
    d = BY_KEY[key]
    if d.peace and rel.at_war:
        return False
    return rel.exists[d.relation] == d.ends


def possible_keys(rel: Relation, keys: List[str]) -> List[str]:
    return [k for k in keys if possible(rel, k)]


def word(key: str) -> str:
    """'give:pact' -> 'pact', 'end:embargo' -> 'end embargo', 'give:state:35' -> 'state 35' (policy vocabulary)."""
    verb, _, what = key.partition(":")
    return what.replace(":", " ") if verb in ("give", "ask") else key.replace(":", " ")


def described(key: str) -> str:
    """Policy vocabulary with the state name where there is one: 'state 35 (Brabant)'."""
    if is_state(key) or is_rights(key) or is_dmz(key):
        return f"{word(key)} ({get_deal(key).label})"
    if is_subject_key(key):
        return f"subject {key.rsplit(':', 1)[1]} ({_SUBJECT_NAMES.get(key.rsplit(':', 1)[1], '')})"
    if is_war(key) or is_pledge(key):     # whose wars, against whom: "pledge ITA" isn't a pledge to Italy
        enemy = _COUNTRY_NAMES.get(key.rsplit(':', 1)[1], '') or key.rsplit(':', 1)[1]
        who = "you join their" if key.startswith("give:") else "they join your"
        return (f"{word(key)} ({who} war against {enemy})" if is_war(key)
                else f"{word(key)} ({who} wars against {enemy}, whenever they come)")
    if third_kind(key):
        x = _COUNTRY_NAMES.get(key.rsplit(":", 1)[1], "") or key.rsplit(":", 1)[1]
        gloss = ({"oppose": f"you work against {x} alongside them", "befriend": f"you seek friendship with {x} at "
                  f"their wish", "back": f"you stand behind {x} with them", "sanction": f"you embargo {x} at their wish"}
                 if key.startswith("give:") else
                 {"oppose": f"they work against {x} alongside you", "befriend": f"they seek friendship with {x} at your "
                  f"wish", "back": f"they stand behind {x} with you", "sanction": f"they embargo {x} at your wish"})
        return f"{word(key)} ({gloss[third_kind(key)]})"
    if key in ("give:faction", "ask:join"):
        return f"{word(key)} (they would join your faction)"
    if key in ("give:join", "ask:faction"):
        return f"{word(key)} (you would join their faction)"
    if is_factory_key(key) or key in FACTORY_KEYS:
        return word(key)
    if is_govt_key(key):
        return f"{word(key)} ({_PARTY_LABELS.get(key, '')})"
    if key == "give:submit":
        return "submit (become their subject)"
    if key == "ask:submit":
        return "submit (they become your subject)"
    if key == "give:annex":
        return "annex (join them entirely, ending your state)"
    return word(key)


def described_list(keys: List[str]) -> List[str]:
    """described() for each key, with items naming a third country folded into one per kind: 'oppose TAG (you work
    against TAG alongside them); TAG one of ITA (Italy), GER (German Reich)'."""
    out, groups = [], {}
    for k in keys:
        if is_third(k):
            groups.setdefault(k.rsplit(":", 1)[0], []).append(k.rsplit(":", 1)[1])
        else:
            out.append(described(k))
    for base, tags in groups.items():
        out.append(described(f"{base}:TAG") + "; TAG one of "
                   + " / ".join(f"{t} ({_COUNTRY_NAMES.get(t, t)})" for t in tags))
    return out


# ---------------------------------------------------------------- prompts

def _fmt(them_name: str, me_name: str) -> Dict[str, str]:
    return {"me_name": me_name, "them_name": them_name}


def _near(key: str) -> bool:
    """A state or resource-rights item worth a line of its own: on the border, or where the other side holds a core
    or claim. The rest (a great power's far-off provinces and colonies) are listed compactly (_far)."""
    info = _BORDER_INFO.get(key)
    return info is None or info.border or info.core or info.claim


def _far(key: str) -> str:
    """'412 Tunisia (3c 0m, 2,100k, steel 4, colony)': a state in a few words."""
    info = _BORDER_INFO.get(key)
    sid = key.rsplit(":", 1)[1]
    if not info:
        return sid
    bits = [f"{info.civ}c {info.mil}m", f"{info.pop_k:,}k"] + ([info.resources] if info.resources else []) \
        + ([] if info.own_core else ["colony"])
    return f"{sid} {info.name} ({', '.join(bits)})"


# One line for the far-off states of a kind (_near): what the command does, N the state's number
_FAR_TEXT = {"give:state": "cede one of these states of yours to {me_name}",
             "ask:state": "ask {me_name} to cede one of these states of its own to you",
             "give:rights": "give {me_name} the resource rights of one of these states of yours (its output for "
                            "good, the land stays yours)",
             "ask:rights": "ask {me_name} for the resource rights of one of these states of its own"}


def _item_lines(verb: str, keys: List[str], f: Dict[str, str], grants=()) -> List[str]:
    """One help line per deal item. War, pledge and commitment items, one per country, are folded into one line
    per kind, and so are states and resource rights away from the border (_near). `grants`: items that may also be
    granted at once in a reply; their line names the plain command too."""
    lines, groups, far = [], {}, {}
    pre = "offer to " if verb == "offer " else ""
    grant = lambda cmd, ks: f" or grant now [[{cmd}]]" if any(k in grants for k in ks) else ""
    for k in keys:
        if is_war(k) or is_third(k):
            groups.setdefault(k.rsplit(":", 1)[0], []).append(k)
        elif (is_state(k) or is_rights(k)) and not _near(k):
            far.setdefault(k.rsplit(":", 1)[0], []).append(k)
        else:
            cmd = f"{word(k)}{' N' if k in AMOUNT_KEYS else ''}"
            lines.append(f"- [[{verb}{cmd}]]{grant(cmd, [k])}: {pre}{get_deal(k).prompt.format(**f)}")
    for base, ks in groups.items():
        prompt = get_deal(f"{base}:TAG").prompt.replace("TAG (TAG)", "TAG").format(**f)
        cmd = f"{word(base)} TAG"
        lines.append(f"- [[{verb}{cmd}]]{grant(cmd, ks)}: {pre}{prompt}; TAG one of: "
                     + ", ".join(f"{t} ({_COUNTRY_NAMES.get(t, t)})" for t in (k.rsplit(":", 1)[1] for k in ks)))
    for base, ks in far.items():
        cmd = f"{word(base)} N"
        lines.append(f"- [[{verb}{cmd}]]{grant(cmd, ks)}: {pre}{_FAR_TEXT[base].format(**f)}; N one of (civilian "
                     f"and military factories, people): " + "; ".join(_far(k) for k in ks))
    return lines


def _notes(keys: List[str]) -> List[str]:
    """The background notes the items among `keys` need, each once."""
    out = []
    if any(k in FACTORY_KEYS or is_factory_key(k) for k in keys):
        out.append(FACTORY_NOTE + f" N is a whole number up to {MAX_FACTORIES}, at most what the giver has.")
    if any(is_state(k) for k in keys):
        out.append(TERRITORY_NOTE)
    if any(is_rights(k) for k in keys):
        out.append(RIGHTS_NOTE)
    if any(is_dmz(k) or factory_base(k) in ("give:dismantle", "ask:dismantle") for k in keys):
        out.append(DISARM_NOTE)
    if any(is_state(k) or is_rights(k) or k in FACTORY_KEYS for k in keys) and ECONOMY.get("text"):
        out.append(ECONOMY["text"])
    if any(k in keys for k in ("give:submit", "ask:submit", "give:annex")):
        out.append(UNION.get("note") or SUBMIT_NOTE)
    if any(is_govt_key(k) for k in keys):
        out.append(GOVT_NOTE)
    return out + _new_goods_notes(keys)


def _unilateral_lines(them_name: str, me_name: str, keys: List[str], when: str) -> List[str]:
    if not keys:
        return []
    f = _fmt(them_name, me_name)
    lines = [f"Steps {them_name} can take on its own ({when}):"]
    for k in keys:
        cmd = k.replace(":", " ")
        lines.append(f"- [[{cmd}]]: {get_deal(k).prompt.format(**f)}")
    return lines


def reply_grants(rel: Relation) -> List[str]:
    """What them could grant at once in a reply, when asked."""
    return (possible_keys(rel, GIVES) + state_gives(rel) + possible_keys(rel, ["give:civ", "give:mil"])
            + possible_keys(rel, govt_gives(rel)) + possible_keys(rel, rights_gives(rel))
            + possible_keys(rel, dmz_gives(rel) + ["give:dismantle"])
            + possible_keys(rel, EQUIPMENT_GIVES + war_gives(rel) + third_gives(rel)))


def reply_help(them_name: str, me_name: str, rel: Relation) -> str:
    """Rules for a reply to the player's letter (the items themselves are in proposal_help, with `grants`)."""
    f = _fmt(them_name, me_name)
    lines = [
        "ACTIONS. Your reply can have real consequences in the game. After the letter, you may add these "
        "commands in double square brackets (they are removed before the letter is shown):",
        f"- [[opinion N]]: how this letter changed {them_name}'s opinion of {me_name}, N from -20 to +20. "
        "Use it when the letter was notably friendly or offensive; leave it out for ordinary letters.",
    ]
    if reply_grants(rel):
        lines.append(f"GRANTING. An item below marked 'or grant now [[X]]' can also be granted at once, with that plain "
                     f"command, ONLY if {me_name} asked for it in their letter and you agree; if they didn't ask but "
                     f"you want it, propose it instead with its [[offer ...]] command.")
    lines += _unilateral_lines(them_name, me_name, possible_keys(rel, UNILATERAL),
                               "only when your policy calls for it or their letter gives serious cause")
    lines.append("Decide as your government would, considering your interests, ideology, alliances and your "
                 "opinion of them; refusing is fine. Whatever you do, also say so in the letter.")
    if rel.at_war:
        lines.append(f"You are at war with {me_name}, so no agreements are possible.")
    if rel.bound:
        lines.append(bound_note(them_name, me_name, rel.bound))
    return "\n".join(lines)


FACTION_NOTE = ("A faction is a standing alliance: its members are called into each other's wars, and the leader "
                "speaks for it. A government joins one whose enemies are its own, or whose protection it needs; a "
                "leader admits a country that adds strength, not one that drags it into wars it doesn't want. "
                "Leaving or expelling a member is a break that the world notices.")


def _new_goods_notes(keys: List[str]) -> List[str]:
    """Notes for war, war pledge, faction and equipment items among `keys`."""
    out = []
    if any(is_war(k) or is_pledge(k) for k in keys):
        out.append(WAR_NOTE)
    if any(third_kind(k) for k in keys):
        out.append(ALIGN_NOTE)
    if any(k in ("give:faction", "give:join", "ask:faction", "ask:join", "give:leave", "ask:leave") for k in keys):
        out.append(FACTION_NOTE)
    if any(is_equipment_key(k) for k in keys):
        out.append(f"{EQUIPMENT_NOTE} N is a whole number of pieces, at most what the sender has in stock. "
                   f"{ECONOMY.get('equipment', '')}".strip())
    return out


def proposal_help(them_name: str, me_name: str, offers: List[str], asks: List[str], expected: bool,
                  grants: List[str] = ()) -> str:
    """One proposal (offer or request) the player answers with Accept/Decline.
    expected: the letter is written because of the policy, so a proposal is its point.
    grants (a reply): what may also be granted at once when asked, marked on its line (reply_help explains)."""
    f = _fmt(them_name, me_name)
    grants = list(grants)
    if not (offers or asks):
        out = (f"Make no formal proposal in this letter, and offer or ask for no specific agreement in its text "
               f"either: none can be agreed now. Speak of friendship or intentions in general if you like; a "
               f"concrete deal waits for a later letter, or for {me_name} to ask for it.")
        if grants:
            out += "\nWhat you could grant if asked:\n" + "\n".join(_item_lines("", grants, f) + _notes(grants))
        return out
    when = ("Your policy is to make such a proposal now: make it the purpose of this letter, unless the "
            "situation has changed so much that it no longer suits you."
            if expected else
            "Propose only what your government actually wants, and only if it suits this letter.")
    lines = [f"PROPOSAL. You may attach ONE formal proposal to {me_name}: a package of items, offers and requests "
             f"mixed (for example something you give in return for something you ask). Keep it to what a real "
             f"treaty of this kind would hold: usually one to four items; more only for a grand settlement (a "
             f"border treaty, an alliance), never every agreement you could think of. {me_name} accepts or declines "
             f"the whole package with one Accept/Decline, so one item they'd refuse sinks all of it. {when} "
             f"Write the proposal "
             f"into the letter, then add one command per item after the letter. Every item the letter proposes "
             f"MUST have its command, or it has no effect; propose nothing in the text that isn't listed here. "
             f"Say plainly what the proposal holds and what it doesn't: if you won't give something {me_name} asked "
             f"for, say no clearly, never as a hint ('we can scarcely spare one') that reads like a promise."]
    only = [g for g in grants if g not in offers and factory_base(g) not in offers]   # grantable, not offered
    lines += _item_lines("offer ", offers, f, grants) + _item_lines("ask ", asks, f)
    if only:
        lines += ["Also grantable if asked (not to be proposed):"] + _item_lines("", only, f)
    lines += _notes(offers + asks + only)
    return "\n".join(lines)


# What a deal item is about, and the words a letter uses for it: a letter that speaks of one its commands leave out
# gets asked about it (letters._unmatched). Only kinds a letter can name without doubt.
TOPIC_WORDS = {
    "factories": r"\bfactor(y|ies)\b",
    "rifles": r"\b(rifles?|small arms|infantry equipment)\b",
    "artillery": r"\b(artillery|field guns?|howitzers?)\b",
    "trucks": r"\b(trucks?|lorries|motor vehicles)\b",
    "access": r"\b(military access|passage|transit)\b",
    "docking": r"\b(docking|harbou?rs?)\b",
    "pact": r"\b(non-aggression|pacts?)\b",
    "guarantee": r"\bguarantee",
    "market": r"\b(market access|open markets?|markets)\b",
    "faction": r"\bfaction\b",
}


def topic(key: str) -> str:
    """'give:civ:2' -> 'factories', 'ask:rifles:500' -> 'rifles', 'give:pact' -> 'pact' ('' for other kinds)."""
    parts = factory_base(key).split(":")
    what = parts[1] if len(parts) > 1 else ""
    if what in ("civ", "mil", "dismantle"):
        return "factories"
    if what in ("join", "faction"):
        return "faction"
    return what if what in TOPIC_WORDS else ""


def unmatched_topics(text: str, covered: List[str], allowed: List[str]) -> List[str]:
    """Kinds of deal the letter's text speaks of that its commands (`covered` keys) leave out, among those it could
    have proposed (`allowed` keys)."""
    have = {topic(k) for k in covered}
    return [t for t in dict.fromkeys(topic(k) for k in allowed)
            if t and t not in have and re.search(TOPIC_WORDS[t], text, re.I)]


def letter_help(them_name: str, me_name: str, offers: List[str], asks: List[str], steps: List[str],
                expected: bool) -> str:
    """Rules for a letter `them` starts: one proposal (offer or request), plus steps its policy calls for."""
    lines = [proposal_help(them_name, me_name, offers, asks, expected)]
    lines += _unilateral_lines(them_name, me_name, steps, "your policy calls for these; take them only if you "
                               "still mean to, and say so in the letter")
    return "\n".join(lines)


# ---------------------------------------------------------------- parsing and applying

_COMMAND = re.compile(r"\[\[\s*(?:(offer|ask|end)\s+)?([a-z_]+)\s*([+-]?\d+)?\s*\]\]", re.I)
_CEDE = re.compile(r"\[\[\s*state\s+(\d+)\s+to\s+([^\]]+?)\s*\]\]", re.I)   # [[state 35 to Lithuania]]
_SUBJECT_DEAL = re.compile(r"\[\[\s*(offer|ask)\s+subject\s+([A-Za-z0-9]{3})\s*\]\]", re.I)
_GOVT = re.compile(r"\[\[\s*(offer\s+|ask\s+)?govt\s+([a-z_]+)\s*\]\]", re.I)   # [[ask govt fascism]]
_SUBJECT = re.compile(r"\[\[\s*(free|transfer)\s+([^\]]+?)\s*\]\]", re.I)        # [[free Slovakia]]
_WAR = re.compile(r"\[\[\s*(offer\s+|ask\s+)?(war|pledge|" + "|".join(THIRD_KINDS)
                  + r")\s+(?:against\s+)?([^\]]+?)\s*\]\]", re.I)   # also commitments and sanctions


def _country_tag(text: str) -> str:
    """A country named in a war or pledge command: its tag, or the name if it isn't a known one (the command is
    then not possible and ignored)."""
    text = re.sub(r"\(.*?\)", "", text).strip()
    tag = _recipient(text, {t: {"name": n} for t, n in _COUNTRY_NAMES.items()})
    return tag or text.upper()


# ---------------------------------------------------------------- land for a third country
# At the player's request, them cedes one of its states to another country: "cede:35:Lithuania" (the recipient as
# the LLM named it; resolved against the countries in the world). Never the capital, never to a country at war
# with them.

def own_states(them: str) -> Dict[str, str]:
    """them's states other than its capital: id -> name."""
    out = {}
    for line in gamestate.query(f'{them} = {{ every_owned_state = {{ limit = {{ is_capital = no }} '
                                f'log = "AIRPO|[THIS.GetID]|[THIS.GetName]" }} }}'):
        if line.startswith("AIRPO|"):
            sid, _, name = line[6:].partition("|")
            out[sid.strip()] = name.strip()
            _STATE_NAMES[sid.strip()] = name.strip()
    return out


def cede_help(them_name: str, me_name: str, states: Dict[str, str]) -> str:
    if not states:
        return ""
    return (f"Land for a third country: ONLY if {me_name} asked you to cede one of your states to ANOTHER country "
            f"(not to {me_name}) and you agree, add [[state N to COUNTRY]], with N from your states below and the "
            f"country's name. One command per state. {TERRITORY_NOTE}\nYour states (number: name): "
            + ", ".join(f"{sid}: {name}" for sid, name in states.items()) + ".")


def _recipient(name: str, countries: Dict[str, Dict]) -> Optional[str]:
    """A country by tag or by (part of) its name."""
    key = name.strip().strip(".").upper()
    if key in countries:
        return key
    low = name.strip().strip(".").lower()
    for tag, c in countries.items():
        if c.get("name", "").lower() == low:
            return tag
    hits = [tag for tag, c in countries.items() if low and low in c.get("name", "").lower()]
    if len(hits) == 1:
        return hits[0]
    # "Kingdom of Lithuania" for the game's "Lithuania": the longest country name found inside the given one
    inside = [(len(c.get("name", "")), tag) for tag, c in countries.items()
              if len(c.get("name", "")) > 3 and re.search(r"\b" + re.escape(c["name"].lower()) + r"\b", low)]
    return max(inside)[1] if inside else None


def subjects_of(them: str) -> Dict[str, str]:
    """them's subjects: tag -> name."""
    out = {}
    for line in gamestate.query(f'{them} = {{ every_subject_country = {{ log = "AIRPSJ|[THIS.GetTag]|[THIS.GetName]" }} }}'):
        if line.startswith("AIRPSJ|"):
            tag, _, name = line[7:].partition("|")
            out[tag.strip()] = name.strip()
    return out


def subject_help(them_name: str, me_name: str, subjects: Dict[str, str]) -> str:
    if not subjects:
        return ""
    return (f"Your subjects: {', '.join(subjects.values())}. ONLY if {me_name} asked you to give one up and you "
            f"agree: [[free COUNTRY]] releases it to full independence; [[transfer COUNTRY]] hands it to {me_name} "
            f"as {me_name}'s subject. Giving up a subject is a grave loss of power and prestige: do it only under "
            f"real pressure or for something substantial.")


def subject_note(them_name: str, overlord_name: str) -> str:
    """For a subject: it can't give away land or make such deals on its own."""
    return (f"{them_name} is a subject of {overlord_name}: its land is not its own to give. It cannot cede states, "
            f"submit or be annexed by its own decision; only {overlord_name} can. Say so if asked.")


def apply_subjects(them: str, me: str, them_name: str, me_name: str,
                   commands: List[Tuple[str, int]]) -> Tuple[List[Tuple[str, bool]], List[Dict]]:
    """Carry out [[free COUNTRY]] and [[transfer COUNTRY]] for them's subjects."""
    shown: List[Tuple[str, bool]] = []
    events: List[Dict] = []
    wanted = [k for k, _ in commands if k.startswith(("free:", "transfer:"))]
    if not wanted:
        return shown, events
    subs = subjects_of(them)
    lookup = {t: {"name": n} for t, n in subs.items()}
    for key in dict.fromkeys(wanted):
        verb, _, who = key.partition(":")
        sub = _recipient(who, lookup)
        if not sub:
            print(f"  [[{verb} {who}]]: not one of {them}'s subjects, ignored")
            continue
        sname = subs[sub]
        if verb == "free":
            script = f"{them} = {{ end_puppet = {sub} }}"
            line, text = (f"{them_name} released {sname}, now independent.",
                          f"At {me_name}'s request, {them_name} released its subject {sname} to independence.")
        else:
            script = f"{them} = {{ end_puppet = {sub} }} {me} = {{ puppet = {sub} }}"
            line, text = (f"{them_name} handed {sname} over to us: it is now our subject.",
                          f"{them_name} handed its subject {sname} over to {me_name}.")
        if _effect(script):
            shown.append((line, False))
            events.append({"type": f"subject:{verb}", "tags": [them, sub, me], "text": text})
    return shown, events


def apply_cedes(them: str, me: str, them_name: str, me_name: str, commands: List[Tuple[str, int]],
                countries: Dict[str, Dict]) -> Tuple[List[Tuple[str, bool]], List[Dict]]:
    """Carry out [[state N to COUNTRY]] commands. `countries`: the chronicle snapshot's countries (tag -> info)."""
    shown: List[Tuple[str, bool]] = []
    events: List[Dict] = []
    cedes = [k for k, _ in commands if k.startswith("cede:")]
    if not cedes:
        return shown, events
    if bridge.trigger(f"{them} = {{ is_subject = yes }}"):
        print(f"  {them} is a subject: its land is its overlord's to give, cessions ignored")
        return shown, events
    mine = own_states(them)
    for key in dict.fromkeys(cedes):
        _, sid, who = key.split(":", 2)
        rec = _recipient(who, countries)
        if sid not in mine:
            print(f"  [[state {sid} to {who}]]: not one of {them}'s states (or its capital), ignored")
            continue
        if not rec or rec == them or rec == me:
            print(f"  [[state {sid} to {who}]]: no such third country, ignored")
            continue
        if bridge.trigger(f"{them} = {{ has_war_with = {rec} }}"):
            print(f"  [[state {sid} to {who}]]: {them} is at war with {rec}, ignored")
            continue
        rec_name = countries.get(rec, {}).get("name", rec)
        if _effect(f"{rec} = {{ transfer_state = {sid} }}"):
            shown.append((f"{them_name} ceded {mine[sid]} to {rec_name}.", False))
            events.append({"type": "cede:third", "tags": [them, rec, me],
                           "text": f"At {me_name}'s request, {them_name} ceded {mine[sid]} to {rec_name}."})
            mine.pop(sid)
    return shown, events


_BARE = re.compile(r"^[ \t]*((?:offer|ask|end)[ \t]+[a-z_]+(?:[ \t]+[A-Za-z0-9_]+)?)[ \t]*$", re.I)


def _bracket_bare(text: str) -> str:
    """Commands written without their brackets on lines of their own after the letter ("offer guarantee BEL" once
    stood under a French letter): bracket them, dropping a trailing country tag. Only in the last lines, so a line
    of prose is never taken for one."""
    lines = text.rstrip().split("\n")
    i = len(lines)
    while i > 0 and (not lines[i - 1].strip() or "[[" in lines[i - 1] or _BARE.match(lines[i - 1])):
        i -= 1
    def bracket(m: re.Match) -> str:
        words = m.group(1).split()
        if len(words) == 3 and re.fullmatch(r"[A-Z][A-Z0-9]{2}", words[2]) and \
                words[1].lower() not in ("war", "pledge") + THIRD_KINDS:
            words = words[:2]                                  # "offer guarantee BEL": the tag is the letter's
        return f"[[{' '.join(words)}]]"
    tail = [(bracket(m) if (m := _BARE.match(x)) else x) for x in lines[i:]]
    return "\n".join(lines[:i] + tail)


def parse(text: str) -> Tuple[str, List[Tuple[str, int]], List[str]]:
    """Strip all commands. Returns (clean text, immediate [(key or 'opinion', number)], proposals [key])."""
    text = _bracket_bare(text)
    immediate: List[Tuple[str, int]] = [(f"cede:{m.group(1)}:{m.group(2)}", 0) for m in _CEDE.finditer(text)]
    immediate += [(f"{m.group(1).lower()}:{m.group(2)}", 0) for m in _SUBJECT.finditer(text)]
    immediate += [(f"give:govt:{m.group(2).lower()}", 0) for m in _GOVT.finditer(text) if not m.group(1)]
    proposals: List[str] = [f"{'give' if m.group(1).lower() == 'offer' else 'ask'}:subject:{m.group(2).upper()}"
                            for m in _SUBJECT_DEAL.finditer(text)]
    proposals += [f"{'give' if m.group(1).strip().lower() == 'offer' else 'ask'}:govt:{m.group(2).lower()}"
                  for m in _GOVT.finditer(text) if m.group(1)]
    for m in _WAR.finditer(text):                      # [[offer war ETH]], [[ask pledge Germany]], [[war ETH]]
        verb = (m.group(1) or "").strip().lower()
        key = f"{'ask' if verb == 'ask' else 'give'}:{m.group(2).lower()}:{_country_tag(m.group(3))}"
        if verb:
            proposals.append(key)
        else:
            immediate.append((key, 0))
    text = _WAR.sub("", _GOVT.sub("", _SUBJECT.sub("", _CEDE.sub("", _SUBJECT_DEAL.sub("", text)))))
    for m in _COMMAND.finditer(text):
        verb, what, num = (m.group(1) or "").lower(), m.group(2).lower(), int(m.group(3) or 0)
        what = f"{what}:{num}" if what in ("state", "rights", "dmz") or (what in AMOUNT_KINDS and num) else what
        if verb == "offer":
            proposals.append(f"give:{what}")
        elif verb == "ask":
            proposals.append(f"ask:{what}")
        elif verb == "end":
            immediate.append((f"end:{what}", 0))
        elif what in ("opinion", "embargo"):
            immediate.append((what, num))
        else:
            immediate.append((f"give:{what}", 0))
    clean = re.sub(r"[ \t]+\n", "\n", _COMMAND.sub("", text)).strip()
    return clean, immediate, proposals


def _snap(value: int) -> int:
    if value == 0:
        return 0
    return min(OPINION_TIERS, key=lambda t: (abs(t - value), -abs(t)))


def _effect(script: str) -> bool:
    reply = bridge.send(f"eval_effect {script}")[0]
    if not reply.startswith("OK"):
        print("  effect failed:", reply)
        return False
    return True


def apply(them: str, me: str, them_name: str, me_name: str, commands: List[Tuple[str, int]],
          allowed: Optional[List[str]] = None) -> Tuple[List[Tuple[str, bool]], List[Dict]]:
    """Validate and apply. `allowed` limits which deal keys may be used ('opinion' included).
    Returns ([(line to show the player, hostile)], chronicle events)."""
    shown: List[Tuple[str, bool]] = []
    events: List[Dict] = []
    fmt = {"them": them, "me": me, "them_name": them_name, "me_name": me_name}
    rel = relation(them, me)
    done = set()
    for key, value in commands:
        if key.startswith(("cede:", "free:", "transfer:")):
            continue                                   # apply_cedes(), apply_subjects()
        if key in done or (allowed is not None and key not in allowed and factory_base(key) not in allowed):
            if key not in done:
                print(f"  [[{key}]] not allowed here, ignored")
            continue
        done.add(key)
        if key == "opinion":
            v = _snap(max(-20, min(20, value)))
            if v and _effect(f"{them} = {{ add_opinion_modifier = {{ target = {me} modifier = {opinion_modifier(v)} }} }}"):
                shown.append((f"{them_name}'s opinion of us {'improved' if v > 0 else 'worsened'} ({v:+d}).", v < 0))
            continue
        deal = get_deal(key)
        if not deal or (key not in BY_KEY and not is_state(key) and not is_subject_key(key)
                        and not is_factory_key(key) and not is_govt_key(key) and not is_rights(key)
                        and not is_dmz(key) and not is_war(key) and not is_pledge(key) and not third_kind(key)):
            print(f"  unknown command {key} ignored")
            continue
        if not possible(rel, key):
            why = "not a border state" if is_state(key) else f"{deal.relation} exists: {rel.exists.get(deal.relation)}"
            print(f"  {key} not possible now (at war: {rel.at_war}, {why})")
            continue
        if key in _AFTER and key.startswith("give:") and key.rsplit(":", 1)[1] not in (rel.them_border or {}):
            print(f"  {key} skipped: the state has not changed hands")     # rights follow the land they came with
            continue
        if is_govt_key(key):
            verb, _, ideology = key.split(":")
            if _effect(_govt_script(them if verb == "give" else me, ideology)):
                shown.append((deal.shown.format(**fmt), deal.hostile))
                events.append({"type": key, "tags": [them, me], "text": deal.remembered.format(**fmt)})
                (rel.them_parties if verb == "give" else rel.me_parties).pop(ideology, None)
            continue
        if is_pledge(key) or is_align(key):        # a promise for later: treaties.py keeps it
            shown.append((deal.shown.format(**fmt), False))
            events.append({"type": key, "tags": [them, me, key.rsplit(":", 1)[1]], "text": deal.remembered.format(**fmt)})
            continue
        if is_sanction(key):
            verb, _, target = key.split(":")
            if _effect(deal.effect.format(**fmt)):
                shown.append((deal.shown.format(**fmt), False))
                events.append({"type": key, "tags": [them, me, target], "text": deal.remembered.format(**fmt)})
                if info := (rel.third or {}).get(target):
                    info["flags"].add(f"embargoed_{'them' if verb == 'give' else 'me'}")
            continue
        if is_war(key):
            verb, _, enemy = key.split(":")
            joiner, side = (them, me) if verb == "give" else (me, them)
            if _effect(war_script(joiner, side, enemy)) and bridge.trigger(f"{joiner} = {{ has_war_with = {enemy} }}"):
                shown.append((deal.shown.format(**fmt), verb == "ask"))
                events.append({"type": key, "tags": [them, me, enemy], "text": deal.remembered.format(**fmt)})
                (rel.me_enemies if verb == "give" else rel.them_enemies).pop(enemy, None)
            continue
        if is_equipment_key(key):
            verb, kind, n = key.split(":")
            giver, receiver = (them, me) if verb == "give" else (me, them)
            if _effect(f"{giver} = {{ send_equipment = {{ equipment = {EQUIPMENT[kind][0]} amount = {n} "
                       f"target = {receiver} }} }}"):
                shown.append((deal.shown.format(**fmt), verb == "ask"))
                events.append({"type": key, "tags": [them, me], "text": deal.remembered.format(**fmt)})
                side = rel.them_equipment if verb == "give" else rel.me_equipment
                side[kind] = max(0, side.get(kind, 0) - int(n))
            continue
        if is_factory_key(key) and key.split(":")[1] == "dismantle":
            verb, _, n = key.split(":")
            done_ = dismantle_factories(them if verb == "give" else me, int(n))
            if done_:
                count, where = done_
                what = f"{count} military factor{'y' if count == 1 else 'ies'}"
                shown.append(((f"{them_name} dismantled {what}" if verb == "give" else f"We dismantled {what}")
                              + f" (in {', '.join(where[:4])}).", verb == "ask"))
                events.append({"type": key, "tags": [them, me], "text": (
                    f"{them_name} dismantled {what} at {me_name}'s wish." if verb == "give" else
                    f"{me_name} dismantled {what} at {them_name}'s demand.")})
                side = rel.them_factories if verb == "give" else rel.me_factories
                side["mil"] = max(0, side.get("mil", 0) - count)
            continue
        if is_factory_key(key):
            verb, kind, n = key.split(":")
            giver, receiver = (them, me) if verb == "give" else (me, them)
            moved = transfer_factories(giver, receiver, kind, int(n))
            if moved:
                count, frm, to = moved
                what = _amount(str(count), FACTORIES[kind][1])
                line = (f"{them_name} gave us {what} (from {', '.join(frm[:4])}; built in {', '.join(to[:4])})."
                        if verb == "give" else f"We gave {them_name} {what} (from {', '.join(frm[:4])}).")
                shown.append((line, verb == "ask"))
                events.append({"type": key, "tags": [them, me], "text": (
                    f"{them_name} gave {me_name} {what}." if verb == "give" else f"{me_name} gave {them_name} {what}.")})
                side = rel.them_factories if verb == "give" else rel.me_factories
                side[kind] = max(0, side.get(kind, 0) - count)
            continue
        if key == "give:peace":
            from . import peace
            outcome = peace.make(me, them)
            if outcome:
                rel.at_war = rel.peace_ok = False
                shown.append((deal.shown.format(**fmt) + " " + outcome, False))
                events.append({"type": key, "tags": [them, me], "text": deal.remembered.format(**fmt)})
            continue
        if _effect(deal.effect.format(**fmt)):
            if is_state(key):
                sid = key.rsplit(":", 1)[1]
                src, dst = ((rel.them_border, rel.me_border) if key.startswith("give:")
                            else (rel.me_border, rel.them_border))
                info = src.pop(sid, None)
                if info is not None and dst is not None:      # it is the other's now: what follows (rights) sees it
                    dst[sid] = info
            elif is_dmz(key):
                state = (rel.them_border if key.startswith("give:") else rel.me_border or {}).get(key.rsplit(":", 1)[1])
                if state:
                    state.dmz = True
            elif is_rights(key):
                held = rel.them_rights if key.startswith("give:") else rel.me_rights
                if held is not None:
                    held.add(key.rsplit(":", 1)[1])
            elif is_subject_key(key):
                ((rel.them_subjects if key.startswith("give:") else rel.me_subjects) or {}).pop(key.rsplit(":", 1)[1], None)
            elif deal.relation == "faction":
                rel.bound = "" if deal.ends else "faction"
            else:
                rel.exists[deal.relation] = not deal.ends
            shown.append((deal.shown.format(**fmt), deal.hostile))
            events.append({"type": key, "tags": [them, me], "text": deal.remembered.format(**fmt)})
    return shown, events


def outcome_text(shown: List[Tuple[str, bool]]) -> str:
    """Lines for under the letter: green for good news, red for bad."""
    return "\n".join(f"§{'R' if hostile else 'G'}{line}§!" for line, hostile in shown)


def package(proposals: List[str], allowed: List[str]) -> List[str]:
    """The proposal items to keep: allowed ones, each once, as many as the letter proposes (no limit: a cut once
    left Italy's 8-item deal with only the 3 things it gave). The resource rights or demilitarization of a state
    that changes hands in the same package go (the state carries them)."""
    kept = list(dict.fromkeys(p for p in proposals if factory_base(p) in allowed))
    for alone in ("give:peace", "give:submit", "ask:submit", "give:annex"):         # offered on their own, never bundled
        if alone in kept:
            return [alone]
    moved = {(k.split(":")[0], k.rsplit(":", 1)[1]) for k in kept if is_state(k)}
    kept = [k for k in kept if not ((is_rights(k) or is_dmz(k)) and (k.split(":")[0], k.rsplit(":", 1)[1]) in moved)]
    asked = {k.rsplit(":", 1)[1] for k in kept if k.startswith("ask:state:")}
    kept = [k for k in kept if not (k in _AFTER and k.rsplit(":", 1)[1] not in asked)]     # rights need their land
    return [k for k in kept if not k in _AFTER] + [k for k in kept if k in _AFTER]          # the land goes first


def package_text(keys: List[str], who: str = "they") -> str:
    """'they offer a guarantee of independence; they ask for military access through our territory'
    With a country name as `who`: 'France offers ...; France asks for military access' (asks keep a neutral label)."""
    plural = who == "they"
    parts = []
    s = lambda verb: verb if plural else verb + "s"
    their = "their" if plural else "its"
    for k in keys:
        if is_war(k) or is_third(k):
            name = _COUNTRY_NAMES.get(k.rsplit(":", 1)[1], k.rsplit(":", 1)[1])
            them_it = "them" if plural else "it"
            parts.append({"give:oppose": f"{who} {s('commit')} to work against {name} with us",
                          "ask:oppose": f"{who} {s('ask')} us to work against {name} with {them_it}",
                          "give:befriend": f"{who} {s('commit')} to seek friendship with {name}",
                          "ask:befriend": f"{who} {s('ask')} us to seek friendship with {name}",
                          "give:back": f"{who} {s('commit')} to stand behind {name} with us",
                          "ask:back": f"{who} {s('ask')} us to stand behind {name} with {them_it}",
                          "give:sanction": f"{who} {s('offer')} to impose a trade embargo on {name}",
                          "ask:sanction": f"{who} {s('ask')} us to impose a trade embargo on {name}",
"give:war": f"{who} {s('offer')} to join our war against {name}",
                          "ask:war": f"{who} {s('ask')} us to join {their} war against {name}",
                          "give:pledge": f"{who} {s('pledge')} to join us whenever we go to war with {name}",
                          "ask:pledge": f"{who} {s('ask')} us to pledge to join {'them' if plural else 'it'} whenever "
                                        f"{'they go' if plural else 'it goes'} to war with {name}"}[k.rsplit(":", 1)[0]])
            continue
        if k in ("give:faction", "give:join", "ask:faction", "ask:join", "give:leave", "ask:leave"):
            parts.append({"give:faction": f"{who} {s('offer')} to take us into {their} faction",
                          "give:join": f"{who} {s('offer')} to join our faction",
                          "ask:faction": f"{who} {s('ask')} to join our faction",
                          "ask:join": f"{who} {s('ask')} us to join {their} faction",
                          "give:leave": f"{who} {s('offer')} to leave {their} faction",
                          "ask:leave": f"{who} {s('ask')} us to leave our faction"}[k])
            continue
        if is_govt_key(k):
            party = _PARTY_LABELS.get(k, k.rsplit(":", 1)[1])
            parts.append(f"{who} {'demand' if plural else 'demands'} that we put our {party} in power"
                         if k.startswith("ask:") else
                         f"{who} {'offer' if plural else 'offers'} to put {'their' if plural else 'its'} {party} in power")
        elif is_dmz(k):
            name = get_deal(k).label.replace("a demilitarized ", "")
            parts.append(f"{who} {'demand' if plural else 'demands'} that we demilitarize {name}"
                         if k.startswith("ask:") else f"{who} {'offer' if plural else 'offers'} to demilitarize {name}")
        elif is_factory_key(k) and k.split(":")[1] == "dismantle":
            n = k.split(":")[2]
            parts.append(f"{who} {'demand' if plural else 'demands'} that we dismantle {n} of our military factories"
                         if k.startswith("ask:") else
                         f"{who} {'offer' if plural else 'offers'} to dismantle {n} of {'their' if plural else 'its'} "
                         f"military factories")
        elif is_factory_key(k):
            what = get_deal(k).label
            parts.append(f"{who} {'ask' if plural else 'asks'} us for {what}" if k.startswith("ask:") else
                         f"{who} {'offer' if plural else 'offers'} {what}")
        elif is_subject_key(k):
            name = _SUBJECT_NAMES.get(k.rsplit(":", 1)[1], k.rsplit(":", 1)[1])
            parts.append(f"{who} {'ask' if plural else 'asks'} for our subject {name}" if k.startswith("ask:") else
                         f"{who} {'offer' if plural else 'offers'} {'their' if plural else 'its'} subject {name}")
        elif is_rights(k):
            what = get_deal(k).label
            parts.append(f"{who} {'ask' if plural else 'asks'} for {what.replace('the ', 'our ', 1)}"
                         if k.startswith("ask:") else f"{who} {'offer' if plural else 'offers'} {what}")
        elif is_state(k):
            name = get_deal(k).label
            parts.append(f"{who} {'ask' if plural else 'asks'} for {name}" if k.startswith("ask:") else
                         f"{who} {'cede' if plural else 'cedes'} {name}")
        elif k == "ask:submit":
            parts.append(f"{who} {'demand' if plural else 'demands'} that we submit and become "
                         f"{'their' if plural else 'its'} subject")
        elif k == "give:submit":
            parts.append(f"{who} {'offer' if plural else 'offers'} to submit and become our subject")
        elif k == "give:annex":
            parts.append(f"{who} {'offer' if plural else 'offers'} to join us entirely, to be annexed into our "
                         f"country")
        elif k.startswith("ask:"):
            label = BY_KEY[k].label if plural else BY_KEY[f"give:{word(k)}"].label
            parts.append(f"{who} {'ask' if plural else 'asks'} for {label}")
        else:
            parts.append(f"{who} {'offer' if plural else 'offers'} {BY_KEY[k].label}")
    return "; ".join(parts)


def proposal_line(keys: List[str], them_name: str) -> str:
    return f"§YProposal from {them_name}: {package_text(keys)}. Accept or decline below.§!"
