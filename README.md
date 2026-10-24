# AI Roleplay

An LLM game master for singleplayer vanilla HOI4. The LLM writes flavour and makes small calls on the
story's flow; the HOI4 AI still plays the other countries.

Standalone project. It needs only:
- the console bridge: the `hoi4-bridge` repo cloned next to this one, with its version.dll installed in the HOI4 folder
- Python 3.10+

## Commands (run from this folder)

    python -m hoi4_rp write-mod   # (re)generate the mod in Documents/Paradox Interactive/Hearts of Iron IV/mod/airoleplay
    python -m hoi4_rp test        # fire a test event in the running game and apply the option you pick
    python -m hoi4_rp run         # answer letters + keep the world chronicle (keep it running while you play)
    python -m hoi4_rp state [TAG] # print a country's live game state
    python -m hoi4_rp llm "hi"    # send one message to the LLM and print the reply

Settings live in `config.ini` (model, endpoint, temperature, limits). It's your personal copy and
git-ignored; on first run it's created from `config.example.ini`. The LLM runs through NanoGPT; the
API key is read from the `NANOGPT_API_KEY` environment variable, never from the config file.

After `write-mod`, restart HOI4 (`reload events` is unreliable). Enable the mod once in your playset.

## Layout

    hoi4_rp/mod_files.py   template events + localisation generator (slots, pictures, ids)
    hoi4_rp/letter_mod.py  letter GUI generator: diplomacy button, writing window, reply window
    hoi4_rp/letters.py     reads sent letters through the bridge, writes replies with the LLM
    hoi4_rp/gamestate.py   live game state (names, parties, factions, wars, states, ideas, focuses)
    hoi4_rp/chronicle.py   one-query world snapshots; their differences become dated events
    hoi4_rp/campaign.py    per-playthrough memory on disk (data/campaigns/<id>.json, git-ignored)
    hoi4_rp/llm.py         NanoGPT client; hoi4_rp/config.py loads config.ini
    hoi4_rp/game.py        show an event, wait for the pick, run effects via eval_effect
    hoi4_rp/bridge.py      loads the bridge client (override path with HOI4_BRIDGE_CLIENT)

## Letters

Open another country in the diplomacy view and pick **Write Letter** from its diplomatic actions.
Click the page, write your letter and press Send. With `python -m hoi4_rp run` running, the
country writes back in a reply window. The letter is a real text box; Python reads it through the
bridge's `bridge.editbox` command, so the bridge DLL must be up to date.

## Memory

Each playthrough gets an id stored in the game as `global.airp_campaign`, so it is saved with your
save files. Its memory lives in `data/campaigns/<id>.json`:

- every letter exchanged with each country, dated in game time
- a world chronicle: while `run` is going it snapshots the world every few seconds of play and records
  wars, peace, capitulations, faction changes, subjects, governments, new or vanished countries and
  territory changes

When a country answers a letter it sees your whole correspondence and the events it took part in, plus
those involving you or the major powers. Loading an earlier save drops memories dated after it.
