# AI Roleplay (Kaiserredux)

An LLM game master for singleplayer HOI4. The LLM writes flavour and makes small calls on the
story's flow; the HOI4 AI still plays the other countries.

Standalone project. It needs only:
- the console bridge: the `hoi4-bridge` repo cloned next to this one, with its version.dll installed in the HOI4 folder
- Python 3.10+

## Commands (run from this folder)

    python -m hoi4_rp write-mod   # (re)generate the mod in Documents/Paradox Interactive/Hearts of Iron IV/mod/airoleplay
    python -m hoi4_rp test        # fire a test event in the running game and apply the option you pick

After `write-mod`, restart HOI4 (`reload events` is unreliable). Enable the mod once in the KX playset.

## Layout

    hoi4_rp/mod_files.py   template events + localisation generator (slots, pictures, ids)
    hoi4_rp/game.py        show an event, wait for the pick, run effects via eval_effect
    hoi4_rp/bridge.py      loads the bridge client (override path with HOI4_BRIDGE_CLIENT)
