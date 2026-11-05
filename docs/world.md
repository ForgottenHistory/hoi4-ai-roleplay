# The world

## Memory and the chronicle

Each playthrough gets an id stored in the game (`global.airp_campaign`), so it travels with your saves. Its memory is
`data/campaigns/<id>.json` (git-ignored): every letter, dated in game time, and a world chronicle.

While `run` is going it snapshots the world every few seconds of play and records what changed: wars, peace,
capitulations, civil wars, factions, subjects, governments, leaders, new or vanished countries, territory, focuses of
the major powers and of countries in your story, and your agreements beginning or ending however they came about.
Loading an earlier save drops memories dated after it.

How each war began comes from the game itself, to the hour: who declared on whom, and why where it can be told
(a guarantee honoured, a faction's call, a letter deal).

Background work (AI letters, popups, LLM jobs) waits until the game has run a few days unpaused.

## Reputation

An impartial game master keeps a record of whether your word can be relied on, known to every country. It is about
trust, not about being good: war or conquest where you promised nothing counts for nothing, and doing what you
threatened shows your word means something. Breaking a deal openly, with notice and a reason others can see (they
broke it first, the circumstances are gone), weighs little; breaking it by surprise, after fresh assurances or by
deceit is a betrayal. Say so in a letter before you break one.

- **Deeds**, found from the chronicle: wars you declared (and what bound you to the victim), wars fought for a country
  you guaranteed, guarantees and pacts you ended in peacetime, peaces you made. Each is judged in the background.
- **Promises and threats**, read from your own letters. A threat stays open until you carry it out or it goes empty;
  countries know how often your threats came true.

Judged deeds reach the game's own opinion: the countries concerned feel the full weight, everyone else a third,
fading over years. The top-bar reputation box shows your standing; its tooltip has the record.

## Ledger

The button in the reputation box opens what the campaign recorded, newest first, in tabs: yours (your events, letters
and judged deeds), diplomacy (deals, treaties, ultimatums, crises, news, stories), world headlines, reputation.

## Faction window

The Factions button beside it shows the world's factions and what this mod does with them, in tabs:

- **Factions**: each faction's leader, members and wars, its share of the world's industry against the size limit for
  factions the AI leads (20% in a calm world, up to 35% at full tension; two major powers), and the faction DLC's
  joining rules.
- **Yours**: your faction and how its members stand toward you; out of one, which factions could take you in by
  letter; what each country's policy toward you would offer or ask about factions.
- **Prospects**: the joins, foundings and breaks the storyteller would weigh (made with `factions = ours`), and the
  blocs: countries whose aims or commitments point the same way.
- **History**: factions founded, joined, left and dissolved, deals by letter about factions, and the storyteller's
  faction talks with their reasons.

`python -m hoi4_rp factions [TAB]` prints a tab.

## Story events

Your own country reacts to what you did through this mod: deals by letter (land, factories, subjects, governments,
peace) and your judged deeds. Only what other mods' events don't already cover. At most one every `gap_days` (60).

Each is a decision: someone at home wants one thing, someone else the opposite. 2-3 responses, each with at most two
small effects any mod has (political power up to 50, stability, war support or party popularity up to 5%, the other
country's opinion up to 10). The tooltip is written from the effects. `events = no` in `[story]` turns them off.
`python -m hoi4_rp story` writes one now (stop `run` first).

## News events

A newspaper popup (headline, report, one button, no effect) about what happened between other countries: the
storyteller's incidents, wagers. Who reports it (wire service, your own paper, a tabloid, a government organ), the tone
and length are rolled in code so reports differ. At most one per `news_gap_days` (10). Needs `[ai_diplomacy]
mode = on` and `driver = storyteller`.

## Crises

Once a month or so the LLM may open a crisis between two neighbours (a border clash, an insulted envoy, smuggled arms)
or inside one country (a strike wave, a mutiny, a cabinet scandal). Flavour only: nothing in the game changes, but
agendas, the storyteller, AI letters and news all see it. A crisis lasts 2-6 months. Your country is never in one.

## Plotlines

Every so often one LLM call reads your letters, promises, deeds and treaties and writes down the plotlines you're
playing: what each is, its goal, the countries in it, how far it has come. The storyteller and the crisis pick read
them and answer as a game master would: building conditions, chances and obstacles, never the outcome.
`python -m hoi4_rp plots` shows them.
