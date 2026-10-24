"""python -m hoi4_rp write-mod   (re)generate the mod files
   python -m hoi4_rp test        fire a hand-written event in the running game and apply the pick
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
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "write-mod":
        mod_files.write_mod()
        sys.exit(0)
    if cmd == "test":
        sys.exit(_test())
    print(__doc__)
    sys.exit(2)
