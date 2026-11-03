# Development

## Commands

    python -m hoi4_rp run              # the main loop: letters, chronicle, background AI work
    python -m hoi4_rp write-mod        # regenerate the mod in Documents/Paradox Interactive/Hearts of Iron IV/mod/airoleplay
    python -m hoi4_rp test             # fire a test event in the running game and apply the option you pick
    python -m hoi4_rp story            # write a story event now (stop `run` first)
    python -m hoi4_rp state [TAG]      # a country's live game state
    python -m hoi4_rp subjects [TAG]   # subject levels from the game and mod files, or one subject's level and progress
    python -m hoi4_rp agendas [TAG...] # write and print agendas
    python -m hoi4_rp storyteller [--months N]   # blocs and a simulated monthly incident pick
    python -m hoi4_rp plots            # the player's plotlines
    python -m hoi4_rp course [TAG]     # focus under way and the likely next ones
    python -m hoi4_rp intent [TAG]     # what the country's game AI is set on
    python -m hoi4_rp ai-undo          # end what AI-to-AI diplomacy put in place (stop `run` first)
    python -m hoi4_rp usage [DAYS]     # LLM calls and tokens
    python -m hoi4_rp llm "hi"         # one message to the LLM

## Logs

- `data/logs/run-YYYY-MM-DD.log`: everything `run` printed
- `data/logs/llm.jsonl`: every LLM call (kind, country, tokens, seconds, model); with `log_prompts = yes` full
  prompts and replies in `data/logs/prompts/`
- `data/logs/ai_diplomacy.txt`: AI-to-AI exchanges, storyteller picks and quiet months

## How it talks to the game

The `hoi4-bridge` DLL runs console commands and its own `bridge.*` commands (country data in save format, queries
through `log` effects, text boxes, localisation). `hoi4_rp/bridge.py` loads its client (override the path with
`HOI4_BRIDGE_CLIENT`). The letter window is a real text box read through `bridge.editbox`.

LLM output is plain text: `KEY: value` lines or `[[commands]]`, parsed leniently. No JSON.

## Layout

    hoi4_rp/letters.py     the run loop; reads sent letters, writes replies, runs the background jobs
    hoi4_rp/actions.py     letter consequences: [[commands]] -> validated game effects; proposals
    hoi4_rp/treaties.py    deals over time: terms, instalments, war clauses, commitments, wagers
    hoi4_rp/guarantees.py  letter guarantees: factory upkeep, 24-month term; the gate on the game AI's own
    hoi4_rp/peace.py       peace by letter through the game's peace conference
    hoi4_rp/war.py         a country's war stance toward the player
    hoi4_rp/ultimatums.py  threats of war that can come true
    hoi4_rp/policy.py      policies toward the player
    hoi4_rp/steering.py    policies and commitments push HOI4's AI (add_ai_strategy)
    hoi4_rp/diplomacy.py   AI-to-AI exchanges
    hoi4_rp/agendas.py     what each country wants from the world
    hoi4_rp/storyteller.py blocs and the monthly incident pick
    hoi4_rp/crises.py      crises between two countries or inside one (flavour)
    hoi4_rp/plots.py       the player's plotlines
    hoi4_rp/story.py       story events about the player's deals and deeds
    hoi4_rp/news.py        news popups
    hoi4_rp/reputation.py  deeds, promises, threats and how they're judged
    hoi4_rp/ledger.py      the ledger window's text
    hoi4_rp/chronicle.py   world snapshots and their differences as dated events
    hoi4_rp/origins.py     how wars began, from the game
    hoi4_rp/campaign.py    per-playthrough memory (data/campaigns/<id>.json)
    hoi4_rp/gamestate.py   live game state through the bridge
    hoi4_rp/course.py      focus paths from AI strategy plans and focus trees
    hoi4_rp/intent.py      the game AI's own strategies, decoded
    hoi4_rp/plight.py      how much trouble a country is in
    hoi4_rp/spirits.py     national spirits and their effects
    hoi4_rp/leaders.py     leaders: sub-ideology, traits, biography
    hoi4_rp/subjects.py    subject levels and which are open to a subject
    hoi4_rp/gamefiles.py   the game's and mods' script files, and a lenient parser
    hoi4_rp/world.py       the setting: lore files and notes
    hoi4_rp/style.py       letter styles
    hoi4_rp/mod_files.py   the generated mod: events, localisation, modifiers
    hoi4_rp/letter_mod.py  the generated GUI: letter windows, reputation box, ledger, treaty row
    hoi4_rp/game.py        show an event, wait for the pick, run effects
    hoi4_rp/llm.py         NanoGPT client and call log
    hoi4_rp/config.py      config.ini loading
    hoi4_rp/bridge.py      loads the bridge client
