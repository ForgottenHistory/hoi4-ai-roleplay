# AI Roleplay

An LLM game master for singleplayer HOI4 (vanilla or mods like Kaiserreich). You write letters to other
countries and they write back, make deals, threaten and remember. The world around you gets its own story:
policies, agendas, crises, news and AI-to-AI diplomacy. HOI4's own AI still plays the countries; the LLM steers it.

## Setup

1. Clone the `hoi4-bridge` repo next to this one and install its `version.dll` in the HOI4 folder.
2. Python 3.10+, and a NanoGPT API key in the `NANOGPT_API_KEY` environment variable.
3. `python -m hoi4_rp write-mod`, enable "AI Roleplay" in your playset, restart HOI4.
4. `python -m hoi4_rp run` (or `start.bat`) and keep it running while you play.

Settings are in `config.ini`, created from `config.example.ini` on first run (git-ignored). Run `write-mod` again
after updating, and restart HOI4 (`reload events` is unreliable).

## Playing

- **Letters**: open a country in the diplomacy view and pick **Write Letter**. It answers in a reply window, and may
  attach a proposal you Accept or Decline. Countries also write to you on their own.
- **Ledger**: the button in the top-bar reputation box lists everything the campaign recorded; the
  **Factions** button beside it shows the world's factions, where you could stand, and who might join or break one.
- **Memory** is per playthrough, saved with your save game; loading an earlier save forgets what came after.

## Commands

    python -m hoi4_rp run              # answer letters and run the world (log: data/logs/run-YYYY-MM-DD.log)
    python -m hoi4_rp write-mod        # (re)generate the mod
    python -m hoi4_rp state [TAG]      # a country's live game state
    python -m hoi4_rp usage            # LLM calls and tokens, from data/logs/llm.jsonl
    python -m hoi4_rp llm "hi"         # one message to the LLM

More (debugging and dry runs) in [docs/development.md](docs/development.md).

## Docs

- [Letters and deals](docs/letters.md): what a letter can do, treaties, guarantees, wagers, peace, ultimatums
- [The world](docs/world.md): chronicle, reputation, story and news events, crises, plotlines, ledger, faction window
- [How the AI countries think](docs/ai.md): policies, steering HOI4's AI, agendas, the storyteller, AI-to-AI diplomacy
- [Settings and other mods](docs/settings.md): config, lore files, Kaiserreich and total conversions
- [Development](docs/development.md): code layout, debug commands, logs
