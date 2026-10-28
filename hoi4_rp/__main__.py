"""python -m hoi4_rp write-mod   (re)generate the mod files
   python -m hoi4_rp test        fire a hand-written event in the running game and apply the pick
   python -m hoi4_rp state [TAG] print the live game state of a country (default: the player's)
   python -m hoi4_rp run         answer letters and keep the world chronicle (keep running while playing;
                                 `letters` is the same)
   python -m hoi4_rp story       write a story event about the campaign's latest deal or deed now, show it and
                                 apply the pick (for trying events out; stop `run` first)
   python -m hoi4_rp llm "text"  send one message to the LLM (NanoGPT) and print the reply
   python -m hoi4_rp usage [DAYS]  LLM calls and tokens of the last DAYS days (default 1), by kind and country
   python -m hoi4_rp subjects [TAG]  list the subject levels the game and mods define, or one subject's
                                 level, the levels open to it and its progress
   python -m hoi4_rp agendas [TAG ...] [--prompt] [--save]
                                 print what countries want from the world (standing aims, from the live game);
                                 --prompt shows each prompt, --save keeps the agendas (stop `run` first)
   python -m hoi4_rp storyteller [--months N] [--prompt]
                                 simulate the storyteller's monthly pick of an AI-to-AI incident, dry: nothing
                                 reaches the game (--months N: N picks in a row, each remembering the earlier ones)
   python -m hoi4_rp ai-undo     end every agreement AI-to-AI diplomacy put in place in the running campaign
                                 (pacts, market access, guarantees, commitments) that still holds (stop `run` first)
"""

import sys

from . import mod_files


def _test() -> int:
    from . import game
    ev = game.RPEvent(
        "A Quiet Word",
        "After the cabinet meeting, the Minister of the Interior lingers by the door.\n\n"
        "\"A word, if I may. Not here.\"",
        "conference",
        [
            game.Option("Hear him out in private.", "Gain 25 political power.", "add_political_power = 25"),
            game.Option("Send him away.", "Lose 2% stability.", "add_stability = -0.02"),
            game.Option("Have him followed.", "Gain 10 command power.", "add_command_power = 10"),
        ],
    )
    slot = game.show(ev)
    print(f"popup open in slot {slot}; pick an option in game...")
    n = game.wait_for_pick(slot)
    print(f"player picked option {n}: {ev.options[n - 1].text}")
    game.run_effect(ev.options[n - 1].effect)
    print(f"ran: {ev.options[n - 1].effect}")
    return 0


if __name__ == "__main__":
    # Game text has characters a Windows console codepage can't show (e.g. Polish names).
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "write-mod":
        mod_files.write_mod()
        sys.exit(0)
    if cmd == "test":
        sys.exit(_test())
    if cmd == "state":
        from . import gamestate
        tag = sys.argv[2] if len(sys.argv) > 2 else gamestate.player_tag()
        s = gamestate.country(tag)
        print(f"[{s.date}]")
        print(gamestate.describe(s))
        sys.exit(0)
    if cmd == "subjects":
        from . import gamestate, subjects
        if len(sys.argv) > 2:
            s = gamestate.country(sys.argv[2])
            if not s.overlord:
                print(f"{s.name} is not a subject")
                sys.exit(1)
            print(subjects.relation_text(s.tag, s.name, s.overlord, s.name_of(s.overlord),
                                         s.autonomy.get(s.overlord, "")))
            sys.exit(0)
        print(f"game folder: {subjects.game_dir()}")
        for lv in sorted(subjects.levels().values(), key=lambda lv: lv.freedom):
            print(f"{lv.freedom:.2f}  {lv.name} ({lv.id}): {subjects.rules_text(lv.id, 'it', 'the overlord')}")
        sys.exit(0)
    if cmd in ("run", "letters"):
        from . import letters
        try:
            sys.exit(letters.run())
        except KeyboardInterrupt:
            sys.exit(0)
    if cmd == "story":
        from . import letters
        sys.exit(letters.story_now())
    if cmd == "ai-undo":
        from . import campaign, chronicle, diplomacy, gamestate
        player = gamestate.player_tag()
        camp = campaign.Campaign(campaign.read_id(player), player)
        camp.snapshot = chronicle.snapshot(player)
        ended = diplomacy.undo(camp)
        print("\n".join(ended) or "nothing to end")
        sys.exit(0)
    if cmd == "agendas":
        from . import agendas
        sys.exit(agendas.main(sys.argv[2:]))
    if cmd == "storyteller":
        from . import storyteller
        sys.exit(storyteller.main(sys.argv[2:]))
    if cmd == "usage":
        from . import llm
        print(llm.usage_report(float(sys.argv[2]) if len(sys.argv) > 2 else 1.0))
        sys.exit(0)
    if cmd == "llm":
        from . import llm
        prompt = " ".join(sys.argv[2:]) or "Say hello in one short sentence."
        print(f"[{llm.model()}]")
        print(llm.chat([{"role": "user", "content": prompt}]))
        sys.exit(0)
    print(__doc__)
    sys.exit(2)
