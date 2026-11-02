# Letters and deals

Open another country in the diplomacy view, pick **Write Letter**, write and press Send. With `run` going, the
country answers in a reply window. A save made while a reply was being written still gets it after loading.

Countries also write on their own: about events involving you, when their policy is urgent, or after a quiet spell
(`[letters]` in config.ini sets the gaps). Each sees your whole correspondence with it and the events it took part in.

You never write commands. You write as you like; the country's reply carries `[[commands]]` that Python strips,
checks in the live game and applies if allowed. What happened is listed under the letter (green good, red bad).

## What a letter can do

In a reply to what you asked:

- opinion of you (-20..+20, fading 1 a month)
- non-aggression pact, military access, docking rights, guarantee, market access
- a state you asked for

On their own (in a reply only for serious cause): end a pact, access, docking or guarantee; embargo, or lift one.

**Proposals** come with Accept/Decline. A package of any number of items, each something they give or ask:

| Kind | Items |
|---|---|
| Agreements | pact, access, docking, guarantee, market access |
| Land | a state, a demilitarized border state, a state's resource rights |
| Wealth | civilian or military factories, rifles/artillery/trucks from the stockpile, political power (up to 200), dismantling military factories |
| Subjects | submit (become a subject), annexation by agreement, handing over a subject, freeing one |
| Politics | a change of government to a named ideology |
| Factions | join theirs, they join yours, leave a faction |
| Wars | join a war against a country (entered 30 days after the deal), peace |
| Third countries | oppose, befriend or back a country; sanction (embargo) it now |

The rules the game would apply still apply: agreements need peace and mustn't exist yet; land must border the other
country, never a capital, never occupied by a third country. Factories, equipment and political power go up to what
the giver has, and can be paid in instalments. In a letter it starts on its own, a country proposes what its policy
lists, goods included; in a reply, anything possible now. A letter
whose text makes a deal but forgot its commands is asked for them in a short follow-up.

Proposals stay occasional: one every `proposal_gap_days` from anyone, and an item you declined is off the table for
that country for `refusal_days` unless something big happens between you.

**You are never bound.** Accepting a deal opens the game mechanics; what you do afterwards is up to you. Breaking
your word is remembered (see [reputation](world.md#reputation)).

## Commitments toward a third country

Oppose, befriend or back a country, as an offer or an ask. A standing line kept as a treaty clause. The AI's own
commitments steer its game AI toward the target. Yours bind nothing, but a step against one (a pact with a country
you promised to oppose, war on one you promised to back) breaks the treaty and is remembered.

## Treaties over time

- `for 12 months`: the deal's agreements end on their own when the term runs out.
- Instalments: "2 factories now and 2 a year, 3 payments in all", or payments starting later.
- What the other country owes happens on its own; it can only get out by renouncing the treaty, which is remembered.
- What you owe comes up as Accept (pay) or Decline (break the treaty). A war between the two ends their treaties.

War pledges (join whenever the other goes to war with X) are switched off (`PLEDGES` in `actions.py`): the AI asked
for them too often, against enemies that made no sense. Pledges made before are still kept.

## Guarantees by letter

A guarantee given by letter lasts 24 months (or the deal's term) and costs its guarantor civilian factories while it
lasts: 1 for the first, 2 more for the second, 3 more for the third. That goes for yours too. A guarantee that runs its
term ended as agreed and isn't held against anyone. The game's own Guarantee button is unchanged.

## World tension

Every prompt knows the world tension and what each country may do at it (guarantee, faction, lend-lease, war goals),
read live per country, so ideology, spirits and mods count. With `tension_rules = yes` (default) letter deals keep to
those thresholds.

## Wagers

Propose a bet in a letter: what you stake against what they stake. The country judges the stakes as a shrewd player
would and accepts or answers with fairer ones. On Accept a coin is tossed and only the loser's stake changes hands. A
news popup tells how it fell, and the chronicle remembers it. Submission or annexation can be staked too.

## Peace by letter

A country at war with you may offer peace (Accept/Decline). Accepting starts the game's own peace conference, with the
winner's share set by how the war stands (each side's surrender progress and occupied land; in faction wars, the
game's whole-war measure). Your AI allies and subjects come to the conference, so the war ends for them, but you make
every demand and can hand states to allies.

How willing a country is depends on its war stance (fight on, open to terms, seek peace, any terms), set from its
surrender progress, capital, war support, stability and the length of the war, and nudged by its leader and ideology.

## Ultimatums

A country you've pushed far enough can threaten war and mean it: a demand with a deadline, usually with the demands
as a proposal. Only at peace, never between subject and overlord or faction members, one at a time.

- It shows in red under the letter, and HOI4's AI turns against you at once.
- Accepting, or giving what was demanded some other way before the deadline, meets it.
- At the deadline the country decides as a shrewd player: satisfied, war now, readied (a war goal, armies prepared,
  reviewed every 3 months), more time once, or back down. War only if its side (allies and your guarantors counted)
  is the stronger and it isn't busy with another war.
- An empty threat costs it credibility with you.

A weak country alone, credibly threatened by a far stronger neighbour, bargains for terms rather than refusing
forever.

## Letter style

`style` in `[letters]` sets the voice; gameplay is the same:

- `serious`: formal, period diplomacy
- `dramatic`: the leader as an over-the-top caricature of their country
- `players`: casual, like people in a multiplayer roleplay game

`diplomacy` sets how readily countries deal: `realistic`, `balanced` (default) or `open`.

## Countries in trouble

Each country is judged secure, strained, dire or desperate (surrender progress, occupied capital, lost cores, enemy
strength, stability, war goals against it, crippling national spirits). A country in trouble shows it in its own
voice, asks plainly, pays more for help and settles for less. It also knows how badly off the other side is.
