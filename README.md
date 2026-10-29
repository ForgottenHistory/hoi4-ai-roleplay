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
    hoi4_rp/treaties.py    deals over time: terms that end, instalments, war pledges, commitments
    hoi4_rp/guarantees.py  letter guarantees: civilian factory upkeep for the guarantor, a 24-month term
    hoi4_rp/ultimatums.py  AI threats of war that can come true: deadline, then war, readiness or backing down
    hoi4_rp/origins.py     how the wars began: who declared on whom and when, from the game, and why
    hoi4_rp/story.py       story events: your country reacts to your letter deals and deeds
    hoi4_rp/policy.py      policies toward the player (stance, urgency, offers, asks, breaks)
    hoi4_rp/steering.py    policies and commitments steer HOI4's own AI (add_ai_strategy)
    hoi4_rp/diplomacy.py   AI-to-AI diplomacy: who talks to whom, short dispatches and answers (dry run)
    hoi4_rp/agendas.py     what each country wants from the world: standing aims, each with a target the map ties it to
    hoi4_rp/storyteller.py blocs from the agendas, and the monthly pick of one AI-to-AI incident (dry run so far)
    hoi4_rp/world.py       the setting the AI reasons from (vanilla / Kaiserreich, lore notes)
    hoi4_rp/campaign.py    per-playthrough memory on disk (data/campaigns/<id>.json, git-ignored)
    hoi4_rp/llm.py         NanoGPT client; hoi4_rp/config.py loads config.ini
    hoi4_rp/game.py        show an event, wait for the pick, run effects via eval_effect
    hoi4_rp/bridge.py      loads the bridge client (override path with HOI4_BRIDGE_CLIENT)

## Letters

Open another country in the diplomacy view and pick **Write Letter** from its diplomatic actions.
Click the page, write your letter and press Send. With `python -m hoi4_rp run` running, the
country writes back in a reply window. The letter is a real text box; Python reads it through the
bridge's `bridge.editbox` command, so the bridge DLL must be up to date. A save made while a reply was
being written still waits for it when loaded: `run` answers the letter again from its own copy (or shows the reply
again, if it was written before the save).

## Memory

Each playthrough gets an id stored in the game as `global.airp_campaign`, so it is saved with your
save files. Its memory lives in `data/campaigns/<id>.json`:

- every letter exchanged with each country, dated in game time
- a world chronicle: while `run` is going it snapshots the world every few seconds of play and records
  wars, peace, capitulations, faction changes, subjects, governments, new or vanished countries and
  territory changes, and your agreements with other countries beginning or ending however they came about
  (the game's diplomacy, a focus, an event: pacts, guarantees, access and market access both ways, embargoes;
  those a letter deal made are told by the deal)

When a country answers a letter it sees your whole correspondence and the events it took part in, plus
those involving you or the major powers. Loading an earlier save drops memories dated after it.

How the wars began comes from the game itself, not the chronicle (`hoi4_rp/origins.py`): for every war either
country is in, who declared war on whom and when, to the hour, and why where it can be told (honouring a
guarantee, called in by a faction, a letter deal or a war pledge). The chronicle notes a war only when a snapshot
first sees it, which can be days late; the game's own record says, for instance, that France declared war on
Germany an hour after Germany declared on Poland.

## Consequences

Letters can change the game (`hoi4_rp/actions.py`). The LLM appends simple commands to its letter;
Python removes them, checks each in the game (agreements need peace and mustn't exist yet; ending one
needs it to exist) and applies what's allowed. What happened is listed under the letter (green good
news, red bad) and the agreements are remembered in the chronicle. A letter whose text makes a deal but that forgot its commands
gets them in one short follow-up, so a deal written in prose can still be accepted, and one whose text speaks of
something its proposal leaves out (a factory, rifles, access...) is asked whether it offers it; when a country may not propose
now (another proposal came recently, or you refused it lately), it's told not to make concrete offers in prose.

- In a reply: `[[opinion N]]` (-20..+20, mod-defined modifiers fading 1 per month), and the agreements
  you asked for: `[[pact]]`, `[[access]]`, `[[docking]]`, `[[guarantee]]`, `[[market]]`
- Anytime, on their own: `[[end pact]]`, `[[end access]]`, `[[end docking]]`, `[[end guarantee]]`,
  `[[embargo]]`, `[[end embargo]]` (replies: only for serious cause; letters they start: when their
  policy calls for it)
- In any letter, one proposal answered with a single Accept/Decline: a package of items (no limit), each
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
  to join theirs). War pledges (join whenever the other goes to war with TAG) are switched off for now
  (`PLEDGES` in actions.py): the AI asked for them too often and against enemies that made no sense. Pledges
  made before are still kept.
- Commitments toward a third country: `[[offer oppose TAG]]` (they work against TAG alongside you),
  `[[offer befriend TAG]]` (they seek TAG's friendship at your wish), `[[offer back TAG]]` (they stand behind TAG
  with you), and the same as asks of you. A standing line, kept as a treaty clause for as long as the treaty
  stands (a term ends it, `[[end treaty]]` renounces it). The AI's own commitments steer its game AI toward TAG
  (`steering.COMMIT`: contain and antagonize, befriend, protect and support, and how it answers TAG's requests).
  Yours bind nothing, but a step against one (a pact with, guarantee of or access for a country you promised to
  oppose; war on or an embargo of one you promised to befriend or back) breaks the treaty and is remembered; the
  AI's steps against its own break it too. Every country's prompts list the standing commitments, the target's
  included. Targets: a threat to the side it is for (oppose), or a neighbour or friend of it (befriend, back), in
  reach of the committing side. `[[offer sanction TAG]]` / `[[ask sanction TAG]]`: a trade embargo on TAG now.
- Equipment from the stockpile: `[[offer rifles N]]`, `[[ask artillery N]]`, `[[trucks N]]` (vanilla equipment
  archetypes, so mods that keep them work).

## Guarantees by letter

A guarantee given in a letter has a price (`hoi4_rp/guarantees.py`): it lasts 24 months (or the deal's own
`[[for N months]]`) and ends on its own, and the guarantor ties up civilian factories while it lasts, more for each it
holds: the 1st costs 1, the 2nd 2 more, the 3rd 3 more (1, 3, 6, 10 in all), as the game's own guarantee costs more
political power for each country already guaranteed. That goes for your guarantees too. The upkeep is a dynamic
modifier on the guarantor ("Guarantees by Treaty"), so run `write-mod` and restart HOI4 once. The proposal says
what the guarantee costs whom; every country's prompts know its upkeep and the price of one more, and a new letter
renews a guarantee at no new cost. One that runs its term ended as agreed and isn't held against anyone. The game's
own Guarantee button is unchanged.

## World tension

Every prompt (letters, replies, policies, ultimatum decisions, AI-to-AI exchanges) knows the world tension and, for
both countries, what the game lets them do at it and from what level: guarantee, join or form a faction, lend-lease,
volunteers, war goals. The thresholds are read live per country, so ideology, national spirits and mods count (the
USA's neutrality puts a guarantee out of reach; Italy may guarantee at any tension). With `tension_rules = yes`
in `[world]` (the default) letter deals keep to them too: a guarantee only once the guarantor may give one, a
faction (joining or admitting) only once both sides may join one, for your deals and AI-to-AI ones alike.
`tension_rules = no` frees deals of them.

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
  ready (a war goal on you and its armies readied), more time (once), or back down. It sees the balance as the
  war would be fought, from the game: its faction and subjects (and whoever already fights you) against yours,
  your guarantors with their factions, and countries pledged to you by treaty. War now only if its side is the
  stronger and it isn't fighting another war, so Germany won't turn on you in the middle of Poland. A readied
  country reconsiders every 3 months (strike, keep waiting, or stand down); HOI4's AI isn't pushed to declare
  on its own, which once had Italy attack Austria and all its guarantors within the hour.
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

## The game's AI follows the letters

A country's policy toward you also steers HOI4's own AI (`hoi4_rp/steering.py`), so what it says it does:

- How it answers your own diplomatic requests (military access, docking, market access, a call to arms, joining its
  faction): friendly +40, cordial +15, wary -20, hostile -50 on the game's acceptance score (shown as "Strategic
  reasons"), +30 more for what its policy says it would grant.
- What it wants with you: a friendly country befriends you (its diplomats improve relations), protects and supports
  you; a cordial one befriends you mildly; a wary one contains you; a hostile one contains and antagonizes you.
  Protecting, supporting and antagonizing act only within the game's rules (a democracy guarantees from 25% world
  tension, lend-leases from 50%; a non-aligned country justifies war goals from 50%).
- Subjects, overlords and faction members only answer your requests (above all a call to arms). At war with you,
  nothing: the war is the game's. War threats stay with ultimatums.

Each change follows a policy refresh; only the difference is pushed and every push is kept, so it can be taken
back exactly, and loading an earlier save forgets what came after. When a country's stance toward your requests
changes, its next letter says so.

## AI-to-AI diplomacy

Countries other than you write to each other (`hoi4_rp/diplomacy.py`, `[ai_diplomacy]` in config.ini), logged to
`data/logs/ai_diplomacy.txt`. `mode = dry` only writes and logs; with `mode = on` what they agree takes effect, for
now pacts, market access, guarantees and commitments (a package holding anything else stays logged, whole: half a trade
is no deal). It enters the chronicle as news, so the countries you write to know of it (all of them when a major
power is in it), and a guarantee costs its guarantor civilian factories for 24 months, as yours do. A commitment
(oppose, befriend, back) is kept as a treaty for 24 months and steers the committed country's AI toward its target;
it lapses with a war between the two, and breaks, as news, when a side steps against it.
`python -m hoi4_rp ai-undo` ends every agreement it put in place (stop `run` first); loading an earlier save
forgets what it did since.

- Each game month a few pairs are drawn (`exchanges_per_month`, default 3), weighted by what ties them: neighbours,
  the same continent, major powers, factions, guarantees, recent events between them and commitments made by
  letter, more on your own continent. Two minor powers far away from you rarely talk; the player is never drawn.
  A pair waits `cooldown_days` (90) before talking again, a country starts at most one exchange a month.
- An exchange is two short LLM calls (under 1k tokens of prompt each): a dispatch with the writer's stance and at
  most one proposal (pact, access, guarantee, market, faction, joining a war, oppose/befriend/back, sanction), and
  the other side's answer: accept, decline, or a counter-proposal (often the same package without what it refuses,
  or with something asked in return), which the proposer accepts or declines in a third short call. The stances
  are kept per pair, and the letters too (marked as dry when nothing took effect).
- It runs while the game runs, in its own queue beside your letters, up to three exchanges at once.
  `python -m hoi4_rp usage` shows its calls as `ai:dispatch`, `ai:answer`, `ai:final` and `ai:ultimatum`.
- Ultimatums between AI countries (`mode = on`): a country at odds with another (no friendly or cordial stance,
  no ultimatum of its own standing) may set one in its dispatch, demanding land only this way: the other's states
  where it holds a core or claim (Bulgaria's Dobruja, Lithuania's Wilno). Its AI turns toward the other at once.
  Accepting gives in (every demand takes effect, land included); a counter it takes satisfies it; otherwise it
  stands until its deadline, when the sender decides in a short call as yours do: met, war (only if its side is
  the stronger, allies and guarantors counted), ready (a war goal and its armies prepared, reviewed every three
  months), more time once, or backing down. All of it is news; both sides' later exchanges remember it.

### Agendas and the storyteller

A replacement for the random pair draw, built in steps. `driver = storyteller` in `[ai_diplomacy]` (default `random`)
makes `run` use it: once a game month a background job writes the agendas that are missing or stale, works out what is
possible between the best pairs (a slice of game queries per tick, so letters stay responsive) and asks the model for
one incident. The incident becomes an ordinary exchange: the writer's dispatch is told it has decided to act on its aim
and its menu is cut to the incident, then the answer, a counter and the last word run as before, with `mode` deciding
whether the result takes effect. Every pick, quiet month and dropped pick is logged to `data/logs/ai_diplomacy.txt`.

Terms used below:

- **Agenda**: what a country wants from the world, one to four aims, written by the model from the live game.
- **Aim**: one line of an agenda, with a target country and a kind: `oppose`, `befriend`, `back`, `land` (wants
  territory), `build` or `neutral`. The target must be a country the map ties to it.
- **Bloc**: countries whose aims line up for or against the same target ("Against Germany: UK, France"). Computed
  from the agendas, no model call; information for the storyteller only.
- **Storyteller**: the monthly model call that looks at agendas, blocs and recent history and picks one incident or none.
- **Incident**: an aim put into action, always one a country already has. Kinds: `guarantee` (one defends the other's
  independence), `commit` (asks the other to join it against or behind a third country), `ultimatum` (a land demand
  with a deadline, needs world tension), `reach_out` (a country writes to you) and `quiet` (nothing this month).
- **Exchange**: what follows an incident: the acting country's letter, the other's answer, a last word. Whether the
  deal takes effect is the `mode`: `off`, `dry` (logged only) or `on`.
- **Driver**: who starts the exchanges: `random` (the old draw) or `storyteller`.

- `python -m hoi4_rp agendas [TAG ...]` (`hoi4_rp/agendas.py`): what each country wants from the world, one to four aims
  such as `contain Germany [oppose GER]` or `win Wilno [land LIT]`, written from the live game. An aim may only name a
  country the map ties to it (a neighbour, an enemy, a war goal, a faction tie, a strong opinion, a rival major power),
  so a preset that scrambles the map gets aims that follow it, not real history. Stored in the campaign, refreshed
  every 180 days or after a war, a new government or the like, dropped when an earlier save is loaded.
- `python -m hoi4_rp storyteller [--months N]` (`hoi4_rp/storyteller.py`): blocs, computed from the agendas
  ("Against Germany: Britain, France, Belgium"), and a simulated monthly pick of one incident or none: a guarantee, a
  commitment or an ultimatum between two countries, each serving an aim of the country that acts. Python works out
  what the game allows between each of the best pairs first (the same rules a dispatch keeps) and the model chooses
  from that list, paced by how long ago anything touched you; a pick that breaks a rule is dropped, never retried.
  Ultimatums need 10% world tension and come at most every 6 months.
  A fourth incident, `reach_out`, makes a country with an aim about you write to you sooner (its policy urgency
  becomes 3); no exchange is made. Countries with an incident in the last 90 days drop to the end of the shortlist.
  With `mode = on`, what an incident came to (agreed, took effect, turned down, countered) is also written to the
  chronicle as news, and a news event (`hoi4_rp/news.py`) reports it in your game: the game's newspaper popup with a
  headline, a short report and one button, no effect. Who reports it and how is rolled in code (a wire service, your
  own paper, a tabloid, the government's organ). `[story] news = yes`, at most one per `news_gap_days`. The news
  templates are in the mod, so run `python -m hoi4_rp write-mod` and restart HOI4 after updating.

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
