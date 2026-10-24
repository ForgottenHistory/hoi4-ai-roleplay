"""python -m hoi4_rp write-mod   (re)generate the mod files
   python -m hoi4_rp test        fire a hand-written event in the running game and apply the pick
   python -m hoi4_rp state [TAG] print the live game state of a country (default: the player's)
   python -m hoi4_rp letters     answer diplomatic letters written in game (keep running while playing)
   python -m hoi4_rp llm "text"  send one message to the LLM (NanoGPT) and print the reply
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
        print(gamestate.describe(s, max_states=1000))
        sys.exit(0)
    if cmd == "letters":
        from . import letters
        try:
            sys.exit(letters.run())
        except KeyboardInterrupt:
            sys.exit(0)
    if cmd == "llm":
        from . import llm
        prompt = " ".join(sys.argv[2:]) or "Say hello in one short sentence."
        print(f"[{llm.model()}]")
        print(llm.chat([{"role": "user", "content": prompt}]))
        sys.exit(0)
    print(__doc__)
    sys.exit(2)
