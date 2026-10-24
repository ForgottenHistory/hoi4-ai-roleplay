"""AI Roleplay: an LLM game master for singleplayer HOI4 (vanilla).

The LLM writes flavour and makes small calls on the story's flow. It never replaces the HOI4 AI.
Everything reaches the game through the console bridge (version.dll + named pipe):

  mod_files.py   writes the mod's blank template events + localisation (`python -m hoi4_rp write-mod`)
  game.py        shows an event with LLM text and options, waits for the player's pick, applies effects

How one event round-trips:
  1. setloc the slot's title/desc/option/tooltip keys to the generated text
  2. eval_effect: show only the options in use, reset the slot's pick variable to 0
  3. `event airp.<slot><picture>` opens the popup
  4. clicking option N sets the country variable airp_s<slot>_pick = N
  5. Python polls the live country block for that variable, then runs that option's effect
"""
