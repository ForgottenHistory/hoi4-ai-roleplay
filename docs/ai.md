# How the AI countries think

HOI4's own AI plays the countries. This mod decides where they want to go (policies, agendas, the storyteller) and
pushes HOI4's AI that way. What the game AI is already doing is feedback, so letters don't contradict it.

## Policies toward you

Every country that matters to you (neighbours, overlord and subjects, enemies, faction members, major powers) keeps a
policy toward you:

- a stance: friendly, cordial, indifferent, wary, hostile
- urgency 0-3
- what it would offer you, what it wants from you, what it means to end or impose
- a few sentences on what it wants and fears

Refreshed every 90 game days (180 when its urgency is 0 or 1 and it isn't at war with you) or after something
happens between you, in the background. A policy remembers everything between the two of you, but the major
powers' doings only from the last year. Policies shape every letter
the country writes and when it writes: urgent ones (3) write soon and may make a proposal the point of the letter.

## Steering HOI4's AI

A policy steers the country's game AI, so what it says it does:

- How it answers your diplomatic requests (access, docking, market, call to arms, joining its faction): friendly +40,
  cordial +15, wary -20, hostile -50 on the game's acceptance score ("Strategic reasons"), +30 for what its policy
  says it would grant.
- What it does with you: friendly befriends, protects and supports you; cordial befriends mildly; wary contains;
  hostile contains and antagonizes. Within the game's own rules (tension thresholds etc).
- Subjects, overlords and faction members only answer requests. At war with you, nothing: the war is the game's.

Commitments made by letter steer toward their target too. Only differences are pushed and every push is kept, so it
can be undone exactly; loading an earlier save forgets later pushes.

## What countries know about themselves

- **Course**: the focus under way and the likely next ones, from the AI strategy plans in force, followed through the
  focus tree, with rival paths at a fork. Taking a side at a fork is a chronicle event.
- **Intent**: what the country's game AI is set on toward others (its AI strategies, decoded). For realism only;
  agendas never read it.
- **National spirits** with their effects, read from the game's and mods' files.
- **Leader**: name, sub-ideology, traits and biography from the game files.

`python -m hoi4_rp course [TAG]` and `intent [TAG]` print them.

## What countries know about you (intel)

With `intel = yes` in `[world]` (off by default), a country knows your country only as well as HOI4's intel system
allows: its civilian, army, navy and air intel on you, read live. Your factories, forces and manpower become
estimates, the less intel the rougher (up to 60% off at none, exact from 90%), steady over a game quarter. A whole
branch is over- or underestimated together (planes and airmen alike), and each country's service errs its own way. Stability
and war support are estimated too, and political power is known only at 90%+. Your national focuses are unknown below
20% civilian intel, and the one under way below 50%. Whether your divisions are short of equipment is known only from
50% army intel, and then only roughly. Population, spirits, parties and wars stay public.

Each country knows its own equipment: what lies in storage and how fully its divisions are equipped (rifles, support
equipment, artillery, trucks), so it knows when it needs arms and when it has some to spare.

This covers its letters and replies to you, its policy toward you, and its ultimatum decisions (it weighs your
estimated strength when deciding on war). AI countries' knowledge of each other is unchanged.

## Agendas

What each country wants from the world: one to four aims, each with a target and a kind (`oppose`, `befriend`,
`back`, `land`, `build`, `neutral`), e.g. `contain Germany [oppose GER]`. An aim may only name a country the map ties
to it (neighbour, enemy, war goal, faction tie, strong opinion, rival major), so a scrambled map gets aims that follow
it, not real history. Refreshed every 180 days or after a war, a new government and the like.
`python -m hoi4_rp agendas [TAG ...]`

Countries in your story may aim at union with you, and treat annexation as the end of their own path.

## AI-to-AI diplomacy

Countries other than you write to each other (`[ai_diplomacy]` in config.ini), logged to `data/logs/ai_diplomacy.txt`.

- `mode`: `off` (default), `dry` (written and logged only) or `on` (every deal kind they agree takes effect, as it
  would in a letter to you, and enters the chronicle as news).
- `driver`: who starts the exchanges.
  - `random` (default): a few pairs a month (`exchanges_per_month`), weighted by what ties them; more on your continent.
  - `storyteller`: once a month one incident is picked from the agendas, blocs (countries whose aims line up) and your
    plotlines: a guarantee, a commitment, an ultimatum (needs 10% tension, at most every 6 months), a country reaching
    out to you, or a quiet month. Python lists what the game allows first; the LLM picks from that list.
    `python -m hoi4_rp storyteller [--months N]` simulates it.

An exchange is a dispatch with at most one proposal, the other side's answer (accept, decline or a counter) and a last
word. A proposal draws on the same items as a letter to you: agreements, factories, equipment, political power, land,
resource rights, demilitarization, submission, annexation, handing over a subject, a change of government, wars and
commitments, as many items as the deal needs. A storyteller incident's proposal holds its guarantee, commitment or
faction, and may add what is paid or asked for it. A pair waits `cooldown_days` (90) before talking again. AI ultimatums demand only land the sender has a core or
claim on, and play out like those against you. A guarantee costs its guarantor factories, as yours do.

**Peace between AI countries.** HOI4's AI ends a war only by capitulation, so once a game month up to two wars
between AI countries get peace talks: wars with no front between the two sides (no border between them or their
allies, and one has no coast), wars whose front hasn't moved in three months, and wars one side is clearly losing.
The side worse off may offer peace and the other accepts or declines, both knowing whether they can reach each other,
how the war has gone and the terms (the game's own measure, as for your letter peace: a white peace when even). With
`mode = on` an accepted peace starts the game's peace conference and is world news. Wars whose conference you would
sit at (your faction, your overlord) are left to you.

## Replacing the game's diplomacy

Optional, off by default: by default HOI4's AI keeps its own diplomacy beside the letters. Each part can be handed
over to the deals between countries instead.

- `guarantees = ours` in `[ai_diplomacy]`: AI countries can't use the game's Guarantee action, so guarantees between
  them come only from deals (needs `mode = on` and `driver = storyteller`). Focuses and events still guarantee, and
  your own Guarantee button is unchanged. HOI4 gates each diplomatic action with a scripted trigger; `write-mod`
  writes the guarantee one in force (the game's, or your mod's with its own rules) again with one condition in
  front, in a file loaded last, and `run` turns it on with a global flag, so changing the setting needs no new
  `write-mod`.

  Each month, after its incident, the storyteller gives the world a guarantee (or none). Python lists the countries
  under threat (a war goal held or being justified on them, an ultimatum, an aim to take their land) and who has a
  stake in protecting them: an aim against the aggressor or to back the threatened, the same faction, the same
  threat; a minor power only for a neighbour, a faction member or a country it aims to back. Distance counts: a
  guarantee from the same continent is ordinary; one from a power that only holds land there (Britain in Asia) needs
  an aim at stake and comes at most once every 6 months in the whole world; one from across the world only from a
  major power with an aim at stake, at most once a year. The storyteller picks one, offered by the guarantor or
  asked by the threatened, and it is an exchange like any other: the other side may refuse or name a price. It also
  renews the guarantees by agreement that are running out while their reason stands; the rest end.
  `python -m hoi4_rp storyteller --guarantees [--prompt]` shows the threats, the possible guarantees and a pick, dry.

- `factions = ours`: AI countries can't invite into a faction, ask to join one, expel a member or leave, so the
  world's factions change through the storyteller (same needs, same gate). Focuses, decisions and events still found,
  fill and dissolve factions, the great historical ones among them; you invite, join and leave as before.

  Each month, after its incident and guarantee, the storyteller may make one faction move. Python lists what is
  possible:
  - **join**: a country outside any faction and a faction's leader, bound by an aim against the faction's foe, a war
    against its enemy, an aim to back the leader or a threat in common. Another ideology joins only for a war or a
    threat in common. From another continent only with an aim at stake, from across the world only at war together
    or to stand by the leader. With the faction DLC, the faction's joining rules apply (same continent, ideology...).
  - **found**: two neighbours (or near ones) outside any faction with a foe, a war or a threat in common, founded by
    the major power or the stronger. Never by a country whose own national focuses may still found a faction (the
    Axis, the Co-Prosperity Sphere...), so the historical factions aren't pre-empted. The storyteller names it.
  - **break**: a member and its leader that no longer belong together (an aim against each other, backing the
    faction's enemy, another ideology), never while they fight a war side by side. The member leaves or the leader
    expels it; at most one in the world every 6 months.

  Joining and founding are exchanges like any other: the other side may refuse or name a price. A break happens at
  once. `python -m hoi4_rp storyteller --factions [--prompt]` shows the possible moves and a pick, dry. The names of
  the factions we found live only while `run` sets them (kept in `data/faction_names.json`); a game loaded without it
  shows their key.

- `wars = ours`: AI countries don't start wars on their own. HOI4 has no scripted trigger for declaring war, so the
  bridge does it (`bridge.wars`, needs a hoi4-bridge with it): every declaration an AI country sends is dropped unless
  our side allowed that pair, and each stopped attempt is counted. AI countries may also justify war goals only on
  countries we let them prepare against (the scripted-trigger gate, with an exception list per country). Untouched:
  wars from national focuses, events and decisions, civil wars, guarantors and allies joining a war, your own wars,
  and the wars our ultimatums end in.

  Each month, in a call beside its incident, guarantee and faction move, the storyteller weighs the would-be wars: countries
  holding a war goal (a focus's, an event's, their own, or an ultimatum's that ran out unmet), countries whose AI
  tried to declare war, and countries with
  an aim to take land and no war goal yet (only once world tension lets that country justify, and never against
  its own subject or overlord). For each it sees the aims, the course of its focuses and the balance of
  both sides as they would fight (allies, subjects, the defender's guarantors and their factions: divisions, soldiers,
  military factories, and whether it is already at war), and decides as that government's shrewd player: **war**
  (allowed for 90 days; its AI declares when the armies are ready), **ultimatum** (a deadline first, for a land
  aim, never to you: yours come by letter), **prepare** (it may justify a war goal, and its AI is set on it: the `conquer` and `prepare_for_war`
  strategies) or **wait** (the pair comes up
  again after 60 days). A war at less than 0.7 of the other side's strength becomes a wait. A non-aggression pact
  is respected everywhere: no war decision, ultimatum, war pledge or deal to join a war between two countries while
  one stands (also with wars = game), and with wars = ours deals between AI countries hold no war joining at all.
  `python -m hoi4_rp storyteller --wars [--prompt]` shows the would-be wars and the decisions, dry.

`python -m hoi4_rp ai-undo` ends pacts, market access, guarantees and commitments it put in place (stop `run` first).
