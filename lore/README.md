# Lore files

Each `.txt` file here is one setting: the history and world the AI reasons from. Its text opens every prompt
(letters, replies, policies, AI-to-AI dispatches, story events). Edit them freely; changes are read the next time
`python -m hoi4_rp run` starts. To support another mod, copy a file and change it.

    # comment lines start with #
    NAME: Half-Life (Unforeseen Consequences)
    DETECT: combine_ascensionism, xen_hordes          ideology groups that only this mod has
    VOICELESS: xen_hordes                             optional: ideology groups of creatures, not governments
    DEFAULT: yes                                      optional: used when no file's DETECT matches (vanilla)
    ---
    The lore text itself, as plain text, as long as you like.

- `DETECT`: with `setting = auto` in config.ini `[world]`, a file is picked when the game's ideology groups include
  any of these. Use ideology ids from the mod's `common/ideologies` files, not their display names.
- `VOICELESS`: countries ruled by one of these ideology groups write no letters, answer none, and stay out of
  AI-to-AI diplomacy and agendas.
- `setting = halflife` (the file name without `.txt`) forces a file instead of detecting one.
- Personal notes for one playthrough go in `lore_file` in config.ini instead; they are added after the setting.
