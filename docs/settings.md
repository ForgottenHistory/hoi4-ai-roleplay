# Settings and other mods

## config.ini

Your personal copy, created from `config.example.ini` on first run; each key is explained there. Sections:

- `[llm]`: `provider` (nanogpt, openrouter, openai, anthropic, gemini, deepseek, mistral, groq, xai, together,
  ollama, lmstudio or custom), model, optional `base_url` / `api_key_env` overrides, temperature, token cap, timeout, retries, `parallel` (background calls of one kind at
  once), `log_prompts`
- `[letters]`: countries writing on their own, gaps between letters and proposals, `style`, `diplomacy`
- `[story]`: story events and news events, and their gaps
- `[ai_diplomacy]`: `mode`, `driver`, exchanges per month, cooldown, `guarantees`, `factions`, `wars` (see
  [replacing the game's diplomacy](ai.md#replacing-the-games-diplomacy))
- `[world]`: `setting`, `lore_file`, `tension_rules`, `intel` (see [what countries know about you](ai.md#what-countries-know-about-you-intel)),
  `border_divisions` (see [troops on the border](ai.md#troops-on-the-border))

The API key is never in the file: it's read from an environment variable, the provider's own unless `api_key_env`
names another (`NANOGPT_API_KEY`, `OPENROUTER_API_KEY`, `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`...;
the full list is in `config.example.ini`). Local servers (Ollama, LM Studio) need none. `python -m hoi4_rp llm`
sends one test message and prints which provider, model and endpoint answered.

Every provider but Anthropic is spoken to through the OpenAI chat API; Anthropic through its own Messages API. When a
model refuses a parameter (OpenAI's reasoning models take `max_completion_tokens`, not `max_tokens`; some take no
temperature) the call is sent again without it, and that is remembered for the session.

## Lore and other settings

Nothing depends on vanilla content: countries, parties, leaders and states are read from the running game, so a
mod's come through as it defines them (Kaiserreich's 10 ideology groups, for instance).

The history the AI reasons from is in plain-text lore files in `lore/`: `vanilla.txt`, `kaiserreich.txt`,
`halflife.txt` (format in `lore/README.md`). Edit them freely; a new mod is a new file.

    setting = auto      # picks the lore file whose ideologies are in play, or a file name (vanilla, halflife...)
    lore_file =         # optional text file with your own setting notes, added to every prompt

Countries ruled by a creature ideology (a lore file's `VOICELESS` line, e.g. Xen in Unforeseen Consequences) are no
government: they write no letters, answer none, and take no part in AI diplomacy or agendas.

## Total conversions

Enable "AI Roleplay" alongside the other mod. Total conversions often `replace_path` whole folders, which drops those
folders from every mod loaded before them. `write-mod` names such enabled mods as dependencies so AI Roleplay loads
after them: run it again after changing the playset, and keep AI Roleplay below them in the launcher.
