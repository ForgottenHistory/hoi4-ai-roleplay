# AI Roleplay

An LLM game master for singleplayer vanilla HOI4. The LLM writes flavour and makes small calls on the
story's flow; the HOI4 AI still plays the other countries.

Standalone project. It needs only:
- the console bridge: the `hoi4-bridge` repo cloned next to this one, with its version.dll installed in the HOI4 folder
- Python 3.10+

## Commands (run from this folder)

    python -m hoi4_rp write-mod   # (re)generate the mod in Documents/Paradox Interactive/Hearts of Iron IV/mod/airoleplay
    python -m hoi4_rp test        # fire a test event in the running game and apply the option you pick
    python -m hoi4_rp run         # answer letters + keep the world chronicle (keep it running while you play;
                                  # its output is also kept in data/logs/run-YYYY-MM-DD.log)
    python -m hoi4_rp story       # write a story event about your latest deal or deed now (stop `run` first)
    python -m hoi4_rp state [TAG] # print a country's live game state
    python -m hoi4_rp llm "hi"    # send one message to the LLM and print the reply
    python -m hoi4_rp subjects [TAG]  # subject levels from the game and mod files, or one subject's level and progress

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
    hoi4_rp/actions.py     letter consequences: [[commands]] -> validated game effects; offers
    hoi4_rp/treaties.py    deals over time: terms that end, instalments, war pledges
    hoi4_rp/ultimatums.py  AI threats of war that can come true: deadline, then war, readiness or backing down
    hoi4_rp/story.py       story events: your country reacts to your letter deals and deeds
    hoi4_rp/policy.py      policies toward the player (stance, urgency, offers, asks, breaks)
    hoi4_rp/world.py       the setting the AI reasons from (vanilla / Kaiserreich, lore notes)
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

## Consequences

Letters can change the game (`hoi4_rp/actions.py`). The LLM appends simple commands to its letter;
Python removes them, checks each in the game (agreements need peace and mustn't exist yet; ending one
needs it to exist) and applies what's allowed. What happened is listed under the letter (green good
news, red bad) and the agreements are remembered in the chronicle.

- In a reply: `[[opinion N]]` (-20..+20, mod-defined modifiers fading 1 per month), and the agreements
  you asked for: `[[pact]]`, `[[access]]`, `[[docking]]`, `[[guarantee]]`, `[[market]]`
- Anytime, on their own: `[[end pact]]`, `[[end access]]`, `[[end docking]]`, `[[end guarantee]]`,
  `[[embargo]]`, `[[end embargo]]` (replies: only for serious cause; letters they start: when their
  policy calls for it)
- In any letter, one proposal answered with a single Accept/Decline: a package of up to 3 items, each
  `[[offer X]]` (they grant you X) or `[[ask X]]` (they ask you for military access, docking rights or
  a guarantee), e.g. a guarantee in return for access. Neighbours propose what their policy lists;
  other countries anything currently possible.
- Territory: `[[state N]]` in a reply (they cede a state you asked for), `[[offer state N]]` and
  `[[ask state N]]` in proposals. Only states bordering the other country, never a capital, only in
  peace; the AI sees each candidate with cores and claims and is told territory is a grave matter.
- Factions: `[[faction]]` (they take you into their faction), `[[join]]` (they join yours), as offers or asks
  too; `[[end faction]]` on their own (they leave, or as leader expel you). Only between independent countries
  at peace, the admitting side leading a faction, the joining side in none. `[[offer leave]]` / `[[ask leave]]`:
  they leave their own faction, or ask you to leave yours (not a faction you share; a leader can't leave).
- Wars: `[[war TAG]]` / `[[offer war TAG]]` (they join your war against TAG), `[[ask war TAG]]` (they ask you
  to join theirs). War pledges: `[[offer pledge TAG]]` (they join you whenever you go to war with TAG, whoever
  starts it), `[[ask pledge TAG]]` (they ask you to pledge the same), kept by `treaties.py`. TAG: a major power
  or a neighbour of the side going to war, at peace with both. Any war counts, because the game doesn't call
  joining a guarantee's war "offensive" (the UK and France in 1939); unlike a guarantee, a pledge also covers
  the other side's own attacks.
- Equipment from the stockpile: `[[offer rifles N]]`, `[[ask artillery N]]`, `[[trucks N]]` (vanilla equipment
  archetypes, so mods that keep them work).

## Treaties

A letter deal can run over time (`hoi4_rp/treaties.py`):

- `[[for 12 months]]`: its agreements (pact, access, docking, guarantee, faction, resource rights,
  demilitarized zones) and war pledges end on their own when the term runs out.
- Instalments: `[[offer civ 2 x3]]` = 2 now, then 2 every year, 3 payments in all; `[[ask rifles 500 x4 every 6]]`
  for every 6 months (up to 5 payments, every 3 to 24 months). `in N months` starts a payment later, with
  nothing now: 2 now and 1 in a year is `[[offer civ 2]] [[offer civ 1 in 12 months]]`.

The game plays them out as you play: what the other country owes (instalments, joining your war when a pledge is
called) happens on its own; it can only get out by renouncing the treaty in a letter (`[[end treaty]]`), which is
remembered. A pledge whose side is already in that war counts as kept. What you owe comes up in the reply window: Accept pays or honours it, Decline refuses and breaks the
treaty. You're never forced, but a broken treaty goes into the chronicle, the reputation game master judges it,
and the other country writes about it. A war between the two ends their treaties. Every country's prompts show
its treaties with you: terms, payments made and missed, pledges standing or honoured, treaties broken.

## Ultimatums

A country you've pushed far enough can threaten war and mean it (`hoi4_rp/ultimatums.py`). Its letter carries
`[[ultimatum 6 months: what it demands]]`, usually with the demands as asks in its proposal (`[[ask state 34]]`).
Only at peace, never between a subject and its overlord or faction members, never from a friendly or cordial
policy, and one at a time per country.

- You see it in red under the letter, with its deadline, and HOI4's own AI turns against you at once
  (`add_ai_strategy` antagonize and prepare_for_war): it plans for that war itself.
- Accepting the proposal meets it. Giving what was demanded some other way before the deadline does too; it's
  checked in the game (the state is theirs, the access exists, the party rules...).
- At the deadline the country decides in the background, as a shrewd player of that country: satisfied, war now,
  ready (a war goal on you, and HOI4's AI told to declare when it judges itself ready), more time (once), or back
  down. War now only if it is at peace with everyone else and its army is the stronger; otherwise it waits for
  its generals, so Germany won't turn on you in the middle of Poland.
- Every step goes into the chronicle and the country writes to you about it. Its prompts remember its ultimatums
  and whether it backed down, so an empty threat costs it credibility with you.

## Story events

Your own country reacts to what you did through this mod (`hoi4_rp/story.py`): the deals you made by letter
(land, factories, subjects, governments, peace, guarantees) and your deeds as the reputation game master judged
them (wars you started, guarantees you kept or dropped, peaces you made). Other mods and the base game already
have events for their own story, so these only cover what nothing else knows about.

At most one event every `gap_days` (config `[story]`, default 60 game days), about the most telling moment of the
last 45 days (plain agreements like pacts, guarantees and market access don't count). Each event is a decision,
not news: someone at home wants something because of what you did, someone else wants the opposite. The LLM
writes it from the game state, your letters with that country, your promises and threats to it and your
reputation, and offers 2-3 responses. Each response carries at most two small effects that every
mod has: political power (up to 50), stability, war support or a party's popularity (up to 5%), or the other
country's opinion of you (up to 10). Some change nothing. The tooltip is written from the effects, so it always
says what happens. What you chose goes into the chronicle, so the other country knows it when it next writes.
`events = no` turns them off.

## Policies and offers

Every country that matters to you keeps a policy toward you (`hoi4_rp/policy.py`): neighbours,
your overlord and subjects, enemies, faction members and the major powers. Each has a stance (friendly, cordial,
indifferent, wary, hostile), an urgency 0-3, what it would offer you, what it wants from you, what it
means to end or impose, and a few sentences on what it wants and fears. The LLM writes it from the full
game state and memory as six plain `KEY: value` lines; it's refreshed every 90 game days or after something happens between you, one
at a time in a background thread so letters never wait for it.

Policies shape every letter that country writes. A neighbour writes soon (once per policy) when it's
urgent (3), or has something to propose or do with some urgency (2); quiet-period writers are picked by
urgency. A proposal arrives with Accept/Decline instead of Close; accepting applies the whole package, and
either answer is remembered.

Proposals stay occasional: only an urgent policy (3) makes one the point of a letter, there's at least
`proposal_gap_days` between proposals from anyone, and an item you declined is off the table for that
country for `refusal_days` unless something happens in the world between you (war, territory,
factions, governments). Answering a proposal doesn't refresh the country's policy; world events do.

## Other settings (Kaiserreich, Kaiserredux, ...)

The mod and bridge don't depend on vanilla content: everything is read from the running game, so modded
countries, parties, leaders and states come through as the mod defines them. Parties are read from each
country's own politics block (vanilla's 4 ideology groups or a mod's, e.g. Kaiserreich's 10), with names,
translations and the ruling party's description from the mod's localisation.

`[world]` in config.ini tells the AI which history to reason from:

    setting = auto      # auto detects Kaiserreich/Kaiserredux from the ideologies in play; or vanilla / kaiserreich
    lore_file =         # optional text file with your own setting notes, added to every prompt

Enable "AI Roleplay" alongside the other mod in the launcher playset.

## Letter style

`style` in `[letters]` sets how letters are written (`hoi4_rp/style.py`); gameplay is the same in each:

- `serious`: formal, period-appropriate diplomacy from the leader or foreign office
- `dramatic`: the leader as an over-the-top caricature of their country: stereotypes, bombast, melodrama
- `players`: casual, as if people were playing those countries in a multiplayer roleplay game
