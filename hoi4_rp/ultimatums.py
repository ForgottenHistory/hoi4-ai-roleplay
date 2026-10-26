"""Ultimatums: an AI country's threat of war against the player that can come true.

A country writing to the player may set an ultimatum, usually with its demands as the asks of its proposal:
    [[ultimatum 6 months: Wallonie and the Ardennes ceded]]  [[ask state 34]]  [[ask state 980]]
Python checks it may (allowed(): at peace, not bound, its policy not friendly, no other open ultimatum from it), then
    shows it under the letter in red, with its deadline
    turns HOI4's own AI against the player: add_ai_strategy antagonize + prepare_for_war (the game's AI then plans
    for that war itself: divisions to the border, worse relations)
Accepting the proposal meets it. At the deadline (tick()), if every demanded item has happened it is met; otherwise
the country decides in a background LLM call (decision_messages() / read_decision()):
    MET     the demands were met after all: it is satisfied
    WAR     it declares war now. Only if it is at peace with everyone and its army is the stronger
            (strength_ratio); otherwise it becomes READY: a war it can't start now waits for the game's AI
    READY   a war goal on the player (create_wargoal) and the game's AI told to declare when it judges itself ready
            (add_ai_strategy declare_war); the war comes when it comes
    EXTEND  more time, once, up to MAX_EXTENSION months
    DROP    it backs down: the strategies are withdrawn, and the threat came to nothing
Effects checked in game: create_wargoal (take_state_focus with a generator of state ids, or annex_everything),
remove_wargoal, declare_war_on, strength_ratio; add_ai_strategy with `imgui show ai_strategy` (no trigger reads it).

Each step goes into the chronicle, and the country writes about it. Its prompts show its ultimatums to the player
and how they ended (describe()), so it remembers backing down, and a country whose threats come true is taken
seriously.

Stored in the campaign: data["ultimatums"] = [{"id", "from", "date", "when", "deadline", "demand", "items",
"status": open | met | war | ready | dropped | lapsed, "closed", "how", "extended", "deciding"}].
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

from . import actions, bridge, campaign

MAX_MONTHS = 12          # the longest deadline an ultimatum may set
MIN_MONTHS = 1
MAX_EXTENSION = 6
STRATEGY = 150           # weight given to the game's AI strategies against the player

_ULTIMATUM = re.compile(r"\[\[\s*ultimatum\s+(\d+)\s*(months?|weeks?|days?)?\s*[:\-–,]?\s*([^\]]*)\]\]", re.I)
_STEPS = ("met", "war", "ready", "extend", "drop")


def _all(camp: campaign.Campaign) -> List[Dict]:
    return camp.data.setdefault("ultimatums", [])


def open_from(camp: campaign.Campaign, tag: str) -> Optional[Dict]:
    return next((u for u in _all(camp) if u["from"] == tag and u["status"] in ("open", "ready")), None)


# ---------------------------------------------------------------- setting one

def allowed(camp: campaign.Campaign, tag: str, rel: "actions.Relation", stance: str) -> bool:
    """May `tag` set an ultimatum now? At peace with the player, not its overlord, subject or ally, its policy not
    friendly or cordial, and no ultimatum of its own still standing."""
    return (not rel.at_war and not rel.bound and stance not in ("friendly", "cordial")
            and not open_from(camp, tag))


def help_text(them_name: str, me_name: str) -> str:
    return (f"ULTIMATUM. If {me_name} has pushed you far enough and you are ready to back your words with war, you "
            f"may set an ultimatum: [[ultimatum N months: what you demand]] (N from {MIN_MONTHS} to {MAX_MONTHS}). Put "
            f"what you demand into your proposal as asks where you can ([[ask state N]], [[ask leave]], ...), so "
            f"that accepting it meets the ultimatum. It is real: your armies turn toward {me_name} at once, and when "
            f"the deadline passes unmet you must choose between war and backing down, and the world will see which. "
            f"An ultimatum you don't carry out costs you all credibility. It is the last step before war, not a "
            f"way to haggle: only for a grave quarrel, a demand you truly mean, and a war you could fight. Most "
            f"letters, even angry ones, set none.")


def parse(text: str) -> Tuple[str, Optional[Tuple[int, str]]]:
    """Take the ultimatum out of the letter: (text, (months, demand) or None)."""
    m = _ULTIMATUM.search(text)
    if not m:
        return text, None
    n, unit = int(m.group(1)), (m.group(2) or "months").lower()
    months = round(n / 4) if unit.startswith("week") else round(n / 30) if unit.startswith("day") else n
    months = max(MIN_MONTHS, min(MAX_MONTHS, months))
    return _ULTIMATUM.sub("", text), (months, m.group(3).strip(" .") or "the demands of this letter")


def _strategies(them: str, me: str, kinds: Tuple[str, ...], value: int) -> str:
    return f"{them} = {{ " + " ".join(f"add_ai_strategy = {{ type = {k} id = {me} value = {value} }}"
                                      for k in kinds) + " }"


# The game's AI strategies stack (adding 777 twice gives 1554) and are kept in saves (checked in game with
# `imgui show ai_strategy`; they take effect at the AI's next daily update). So each ultimatum keeps a dated list of
# what it added (u["pushes"]: [date, kind, value]) and takes back exactly that, and a loaded earlier save forgets
# the pushes made after it, as the game itself does.

def _push(camp: campaign.Campaign, u: Dict, kinds: Tuple[str, ...], value: int) -> None:
    if actions._effect(_strategies(u["from"], camp.data["player"], kinds, value)):
        key = camp.data.get("last_date") or u["date"]
        u.setdefault("pushes", []).extend([key, k, value] for k in kinds)


def _stand_down(camp: campaign.Campaign, u: Dict, wargoal: bool = True) -> None:
    """Withdraw what the ultimatum added to the game's AI, and (unless at war) the war goal it gave."""
    tag, player = u["from"], camp.data["player"]
    total: Dict[str, int] = {}
    for _, kind, value in u.get("pushes", []):
        total[kind] = total.get(kind, 0) + value
    script = " ".join(f"add_ai_strategy = {{ type = {k} id = {player} value = {-v} }}" for k, v in total.items() if v)
    if wargoal:
        script += (f" remove_wargoal = {{ type = take_state_focus target = {player} }}"
                   f" remove_wargoal = {{ type = annex_everything target = {player} }}")
    if script.strip() and actions._effect(f"{tag} = {{ {script} }}"):
        key = camp.data.get("last_date") or u["date"]
        u.setdefault("pushes", []).extend([key, k, -v] for k, v in total.items() if v)


def issue(camp: campaign.Campaign, tag: str, player: str, months: int, demand: str, items: List[str], key: str,
          them_name: str, me_name: str) -> Tuple[Dict, str]:
    """Set the ultimatum. Returns it and the line to show under the letter."""
    from . import treaties
    deadline = treaties._add_months(key, months)
    u = {"id": max([x["id"] for x in _all(camp)] or [0]) + 1, "from": tag, "date": key,
         "when": campaign.date_words(key), "deadline": deadline, "demand": demand,
         "items": [k for k in items if k.startswith("ask:")], "status": "open", "closed": "", "how": "",
         "extended": False, "deciding": False, "pushes": []}
    _all(camp).append(u)
    _push(camp, u, ("antagonize", "prepare_for_war"), STRATEGY)
    _event(camp, "issued", [tag, player], f"{them_name} gave {me_name} an ultimatum: {demand}, by "
                                          f"{campaign.date_words(deadline)}, or war.")
    return u, f"Ultimatum from {them_name}: {demand}, by {campaign.date_words(deadline)}, or war."


def _event(camp: campaign.Campaign, kind: str, tags: List[str], text: str) -> None:
    key = camp.data.get("last_date") or ""
    camp.add_events([{"date": key, "when": campaign.date_words(key), "type": f"ultimatum:{kind}", "tags": tags,
                      "text": text, "major": False}])
    print(f"  [ultimatum] {text}")


def _close(camp: campaign.Campaign, u: Dict, status: str, how: str) -> None:
    u["status"], u["closed"], u["how"], u["deciding"] = status, camp.data.get("last_date") or "", how, False


def met_by_deal(camp: campaign.Campaign, tag: str, me_name: str, them_name: str) -> None:
    """The player accepted the proposal that carried the ultimatum."""
    u = open_from(camp, tag)
    if u and u["status"] == "open":
        _close(camp, u, "met", f"{me_name} accepted the demands")
        _stand_down(camp, u)
        _event(camp, "met", [tag, camp.data["player"]], f"{me_name} gave in to {them_name}'s ultimatum: "
                                                        f"{u['demand']}.")


def rejected(camp: campaign.Campaign, tag: str, me_name: str, them_name: str) -> None:
    """The player declined the proposal that carried the ultimatum: it stands until its deadline."""
    u = open_from(camp, tag)
    if u and u["status"] == "open":
        _event(camp, "rejected", [tag, camp.data["player"]], f"{me_name} rejected {them_name}'s ultimatum "
                                                             f"({u['demand']}); its deadline is "
                                                             f"{campaign.date_words(u['deadline'])}.")


# ---------------------------------------------------------------- as time passes

def _names(camp: campaign.Campaign, tag: str) -> Tuple[str, str]:
    c = (camp.snapshot or {}).get("countries", {})
    player = camp.data["player"]
    return c.get(tag, {}).get("name", tag), c.get(player, {}).get("name", player)


_RELATION_DONE = {"ask:access": "my_access", "ask:docking": "my_docking", "ask:guarantee": "my_guarantee"}


def _done(rel: "actions.Relation", them: str, me: str, key: str) -> bool:
    """Has this demand of `them` happened, checked in the game itself? Amounts (factories, equipment) can't be told
    apart from what was there before: only accepting the proposal meets them."""
    if key in _RELATION_DONE:
        return bool(rel.exists.get(_RELATION_DONE[key]))
    parts = key.split(":")
    t = bridge.trigger
    if actions.is_state(key):
        return t(f"{parts[2]} = {{ is_owned_by = {them} }}")
    if actions.is_rights(key):
        return parts[2] in (rel.me_rights or set())
    if actions.is_dmz(key):
        state = (rel.me_border or {}).get(parts[2])
        return bool(state and state.dmz)
    if actions.is_govt_key(key):
        return t(f"{me} = {{ has_government = {parts[2]} }}")
    if actions.is_subject_key(key):
        return t(f"{parts[2]} = {{ is_subject_of = {them} }}")
    if key == "ask:submit":
        return t(f"{me} = {{ is_subject_of = {them} }}")
    if key == "ask:leave":
        return not rel.me_in_faction
    if key in ("ask:join", "ask:faction"):
        return t(f"{me} = {{ is_in_faction_with = {them} }}")
    if actions.is_war(key):
        return t(f"{me} = {{ has_war_with = {parts[2]} }}")
    return False


def _items_done(camp: campaign.Campaign, u: Dict) -> Tuple[List[str], List[str]]:
    """(demanded items that have happened, items still outstanding), checked in the game."""
    if not u["items"]:
        return [], []
    them, me = u["from"], camp.data["player"]
    rel = actions.relation(them, me)
    actions.state_asks(rel), actions.rights_asks(rel), actions.dmz_asks(rel)    # loads names for the labels
    done = [k for k in u["items"] if _done(rel, them, me, k)]
    return done, [k for k in u["items"] if k not in done]


def tick(camp: campaign.Campaign, player: str) -> List[Tuple[str, str]]:
    """Follow the open ultimatums. Returns [(country, what happened)] worth a letter from it. Ultimatums past their
    deadline and unmet are marked "deciding" for the background decision (next_decision())."""
    today = camp.data.get("last_date") or ""
    countries = (camp.snapshot or {}).get("countries", {})
    letters: List[Tuple[str, str]] = []
    changed = False
    for u in _all(camp):
        if u["status"] not in ("open", "ready") or not today:
            continue
        tag = u["from"]
        them_name, me_name = _names(camp, tag)
        if tag not in countries or countries[tag].get("capitulated"):
            _close(camp, u, "lapsed", f"{them_name} has fallen")
            if tag in countries:
                _stand_down(camp, u)
            changed = True
            continue
        if bridge.trigger(f"{player} = {{ has_war_with = {tag} }}"):
            started = bridge.trigger(f"{tag} = {{ has_offensive_war_with = {player} }}")
            _close(camp, u, "war", f"{them_name} made war on {me_name}" if started
                   else f"war came between them ({me_name} struck first)")
            _stand_down(camp, u, wargoal=False)    # the war drives its AI now; nothing left over for after the peace
            _event(camp, "war", [tag, player], f"{them_name}'s ultimatum to {me_name} ({u['demand']}) ended in war.")
            changed = True
            continue
        if u["status"] == "open" and not u["deciding"] and today >= u["deadline"]:
            done, left = _items_done(camp, u)
            if u["items"] and not left:
                _close(camp, u, "met", f"{me_name} gave what was demanded")
                _stand_down(camp, u)
                text = f"{me_name} met {them_name}'s ultimatum in time: {u['demand']}."
                _event(camp, "met", [tag, player], text)
                letters.append((tag, text))
            else:
                u["deciding"] = True
                print(f"  ultimatum {u['id']} from {tag}: deadline passed, deciding")
            changed = True
    if changed:
        camp.save()
    return letters


def next_decision(camp: campaign.Campaign) -> Optional[Dict]:
    return next((u for u in _all(camp) if u["status"] == "open" and u["deciding"]), None)


def decision_messages(camp: campaign.Campaign, u: Dict, context: str, them_name: str, me_name: str,
                      strength: str) -> List[Dict[str, str]]:
    done, left = _items_done(camp, u)
    label = lambda k: actions.get_deal(k).label
    since = [f"- {e['when']}: {e['text']}" for e in camp.data["events"]
             if e["date"] >= u["date"] and u["from"] in e["tags"] and camp.data["player"] in e["tags"]]
    letters = [f"--- {x['when']}, {them_name if x['from'] == u['from'] else me_name} wrote:\n{x['text'][:1500]}"
               for x in camp.letters_with(u["from"]) if x["date"] >= u["date"]]
    options = ("MET (the demands were met after all: you are satisfied), WAR (declare war now), READY (keep your "
               "armies on the border and strike when your generals judge the moment right), "
               + ("" if u["extended"] else f"EXTEND (give them up to {MAX_EXTENSION} more months, once), ")
               + "or DROP (back down)")
    return [{"role": "system", "content":
             f"{context}\n\nYOUR ULTIMATUM: on {u['when']} you gave {me_name} an ultimatum: {u['demand']}, by "
             f"{campaign.date_words(u['deadline'])}, or war. That deadline has passed.\n"
             + (f"Demanded items done: {', '.join(label(k) for k in done) or 'none'}. Still outstanding: "
                f"{', '.join(label(k) for k in left) or 'none'}.\n" if u["items"] else "")
             + f"Since then, between you:\n" + ("\n".join(since) or "- nothing recorded") + "\n\n"
             + "Letters since:\n" + ("\n\n".join(letters) or "none") + f"\n\n{strength}\n\n"
             f"Decide as {them_name}'s government now would, as a shrewd player of your country: war is costly "
             f"and can be lost, but an ultimatum you don't carry out costs you all credibility with {me_name} and "
             f"the world. Your options: {options}."},
            {"role": "user", "content":
             "Answer with exactly these lines:\nDECISION: one of MET, WAR, READY, EXTEND, DROP\n"
             "MONTHS: for EXTEND, how many more months; otherwise 0\nWHY: one sentence"}]


def read_decision(text: str) -> Tuple[str, int, str]:
    d = re.search(r"decision[*_\s]*[:\-–]\s*\**\s*([a-z]+)", text, re.I)
    m = re.search(r"months[*_\s]*[:\-–]\s*(\d+)", text, re.I)
    w = re.search(r"^[\W_]*why[*_\s]*[:\-–]\s*(.+)$", text, re.I | re.M)
    word = (d.group(1).lower() if d else "")
    step = next((s for s in _STEPS if word.startswith(s[:3])), "")      # "": unreadable, ask again later
    return step, int(m.group(1)) if m else 0, (w.group(1).strip(" *") if w else "")


def strength_line(tag: str, player: str, them_name: str, me_name: str) -> str:
    """For the decision: who would win, and what else the country is fighting."""
    stronger = bridge.trigger(f"{tag} = {{ strength_ratio = {{ tag = {player} ratio > 1 }} }}")
    far = bridge.trigger(f"{tag} = {{ strength_ratio = {{ tag = {player} ratio > 2 }} }}")
    busy = bridge.trigger(f"{tag} = {{ has_war = yes }}")
    return (f"STRENGTH: {them_name}'s army is " + ("far stronger than" if far else "stronger than" if stronger
                                                 else "not stronger than") + f" {me_name}'s"
            + (f"; {them_name} is already fighting another war, so a new one would wait for its generals"
               if busy else "") + ".")


def decide(camp: campaign.Campaign, u: Dict, step: str, months: int, why: str) -> str:
    """Carry out the country's decision (Python's rules first). Returns what happened, for the letter it writes."""
    player = camp.data["player"]
    tag = u["from"]
    them_name, me_name = _names(camp, tag)
    u["deciding"] = False
    if step == "extend" and u["extended"]:
        step = "ready"
    if step == "war" and (bridge.trigger(f"{tag} = {{ has_war = yes }}")
                          or not bridge.trigger(f"{tag} = {{ strength_ratio = {{ tag = {player} ratio > 1 }} }}")):
        step = "ready"                                 # it can't strike now: its generals choose the moment
    reason = f" ({why})" if why else ""
    if step == "met":
        _close(camp, u, "met", f"{them_name} judged its demands met")
        _stand_down(camp, u)
        text = f"{them_name} declared itself satisfied: its ultimatum to {me_name} is met{reason}."
        _event(camp, "met", [tag, player], text)
    elif step == "extend":
        from . import treaties
        n = max(1, min(MAX_EXTENSION, months or 3))
        u["deadline"], u["extended"] = treaties._add_months(camp.data.get("last_date") or u["deadline"], n), True
        text = (f"{them_name} gave {me_name} {n} more months to meet its ultimatum, until "
                f"{campaign.date_words(u['deadline'])}{reason}.")
        _event(camp, "extended", [tag, player], text)
    elif step == "drop":
        _close(camp, u, "dropped", f"{them_name} backed down")
        _stand_down(camp, u)
        text = f"{them_name} let its ultimatum to {me_name} pass without war{reason}."
        _event(camp, "dropped", [tag, player], text)
    else:
        wargoal = _wargoal(u)
        actions._effect(f"{tag} = {{ create_wargoal = {{ {wargoal} target = {player} }} }}")
        if step == "war" and actions._effect(f"{tag} = {{ declare_war_on = {{ target = {player} "
                                             f"{wargoal.split(' generator')[0]} }} }}") \
                and bridge.trigger(f"{tag} = {{ has_war_with = {player} }}"):
            _close(camp, u, "war", f"{them_name} declared war when the deadline passed")
            _stand_down(camp, u, wargoal=False)
            text = f"{them_name}'s ultimatum to {me_name} ran out, and {them_name} declared war{reason}."
            _event(camp, "war", [tag, player], text)
        else:
            _close(camp, u, "ready", f"{them_name} readied for war")
            _push(camp, u, ("antagonize", "prepare_for_war", "declare_war", "conquer"), STRATEGY * 2)
            text = (f"{them_name}'s ultimatum to {me_name} ran out unmet. {them_name} holds a war goal against "
                    f"{me_name} and readies its armies to strike when it judges the moment right{reason}.")
            _event(camp, "ready", [tag, player], text)
    camp.save()
    return text


def _wargoal(u: Dict) -> str:
    """The war goal for an ultimatum: the demanded states if it named any, else everything."""
    states = [k.rsplit(":", 1)[1] for k in u["items"] if actions.is_state(k)]
    return (f"type = take_state_focus generator = {{ {' '.join(states)} }}" if states
            else "type = annex_everything")


# ---------------------------------------------------------------- what is remembered

def describe(camp: campaign.Campaign, tag: str, them_name: str, me_name: str) -> str:
    """The country's ultimatums to the player, for its prompts ('' if none)."""
    lines = []
    for u in _all(camp):
        if u["from"] != tag:
            continue
        line = f"- {u['when']}: \"{u['demand']}\", by {campaign.date_words(u['deadline'])}"
        if u["status"] == "open":
            line += " - STILL STANDING"
        elif u["status"] == "ready":
            line += " - unmet; you hold a war goal and your armies are readying to strike"
        else:
            line += f" - {u['status'].upper()}: {u['how']}"
        lines.append(line)
    if not lines:
        return ""
    out = f"YOUR ULTIMATUMS TO {me_name.upper()}:\n" + "\n".join(lines[-5:])
    backed = sum(1 for u in _all(camp) if u["from"] == tag and u["status"] == "dropped")
    if backed:
        out += (f"\nYou have backed down from {backed} of them: {me_name} has reason to doubt your threats, and "
                f"another empty one would make you a laughing stock.")
    return out


def drop_after(camp: campaign.Campaign, key: str) -> None:
    """An earlier save was loaded: forget ultimatums from that moment on, and steps taken since."""
    keep = []
    for u in _all(camp):
        if u["date"] >= key:
            continue
        if u["closed"] and u["closed"] >= key:
            u["status"], u["closed"], u["how"] = "open", "", ""
        u["deciding"] = False
        u["pushes"] = [p for p in u.get("pushes", []) if p[0] < key]   # the save holds the AI as it was then
        keep.append(u)
    camp.data["ultimatums"] = keep
