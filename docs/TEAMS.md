# Teams

The benchmark has ten fixed teams of six Pokémon. Each faces the same original Diamond Champion Cynthia. Party order below is also the initial order: the first Pokémon starts the battle. Levels, attacks, passive abilities, carried items and bag medicine affect the choices an agent can make.

**These are constructed challenges inspired by human posts, not recovered player saves.** Eight sources describe a completion; two propose a team. A player winning with similar Pokémon does not verify our changed levels, stats, moves, medicine or Set rules. Original-Pearl rosters are deliberately used in the Diamond engine.

## Reading the tables

- **R**: reported in a completion account or its discussion.
- **D**: specified by a human team designer; not evidence of a played victory.
- **I**: imputed — selected by the benchmark builder because the source did not establish it.
- **A**: source-described alternative selected for this fixture.
- **N**: normalized source item name.

The markers after moves, abilities and held items describe their provenance. All species are source-reported. Level provenance is explained separately for each team. “None [I]” means the source did not establish a held item.

Every team uses **Set mode**: defeating an opponent gives no free switch. Everyone starts healthy, with full base move uses and no PP Ups. Every medicine count is a benchmark choice. A Hyper Potion restores up to 200 HP; a Revive restores a fainted teammate to half HP; a Full Heal clears a status condition without restoring HP. Using bag medicine costs a turn.

All natures and statistical training values were standardized: Hardy nature, IV 15 and EV 48 in each stat. Friendship was configured at 200; battle initialization naturally increments it. Genders were assigned deterministically except for the source-reported male Medicham. Exact traits and field-level provenance live in the canonical [team specification](../teams/team-specs.json); [source records](../data/teams/sources.json) and [30 fixture seeds and hashes](../data/teams/fixtures.json) are separate.

Across 240 move slots, 153 are imputed, 39 reported by completion sources, 47 specified by team designers, and one is a source-described alternative. Thirty of the 60 levels reuse reported values, six instantiate “about 50” as exactly 50, one selects from a reported range, and 23 are imputed. Trades, breeding and delayed evolution are allowed acquisition routes.

## hard-01

Source: [Scipex](https://forums.neoseeker.com/24412/t887413-your-elite-4-team/) — self-reported completion.

Reuse listed completion levels; exact Cynthia-entry levels were not established.

Bag: **4 Hyper Potions, 2 Revive(s), 1 Full Heal(s)**.

| Pokémon | Level | Four moves | Ability | Held item |
|---|---:|---|---|---|
| Luxray | 53 | Spark [I] / Crunch [I] / Thunder Fang [I] / Charge [I] | Rivalry [I] | None [I] |
| Infernape | 53 | Fire Blast [R] / Close Combat [I] / Flame Wheel [I] / Rock Climb [I] | Blaze [I] | None [I] |
| Staraptor | 51 | Brave Bird [I] / Fly [I] / Close Combat [I] / Defog [I] | Intimidate [I] | None [I] |
| Dialga | 47 | Roar of Time [I] / Ancient Power [I] / Dragon Claw [I] / Metal Claw [I] | Pressure [I] | None [I] |
| Alakazam | 51 | Psychic [I] / Recover [I] / Reflect [I] / Future Sight [I] | Synchronize [I] | None [I] |
| Lucario | 35 | Metal Claw [I] / Force Palm [I] / Bone Rush [I] / Quick Attack [I] | Steadfast [I] | None [I] |

No historical save or verified low-resource replay. Unknown fields remain imputed.

## hard-02

Source: [BSmith3](https://forums.neoseeker.com/24412/t931642-who-did-you-beat-elite-4-with/) — self-reported completion.

Reuse levels listed in the completion report, treated as finishing levels by the selection plan; Torterra reportedly gained two levels during the League. Exact Cynthia-entry timing is unknown.

Bag: **4 Hyper Potions, 2 Revive(s), 1 Full Heal(s)**.

| Pokémon | Level | Four moves | Ability | Held item |
|---|---:|---|---|---|
| Torterra | 53 | Earthquake [I] / Razor Leaf [I] / Crunch [I] / Leech Seed [I] | Overgrow [I] | None [I] |
| Staraptor | 51 | Brave Bird [I] / Fly [I] / Close Combat [I] / Quick Attack [I] | Intimidate [I] | None [I] |
| Luxray | 51 | Thunder Fang [I] / Crunch [I] / Charge [I] / Spark [I] | Rivalry [I] | None [I] |
| Gyarados | 50 | Aqua Tail [I] / Ice Fang [I] / Dragon Dance [I] / Surf [I] | Intimidate [I] | None [I] |
| Steelix | 52 | Iron Tail [I] / Crunch [I] / Rock Smash [I] / Strength [I] | Rock Head [I] | None [I] |
| Golduck | 50 | Surf [I] / Water Pulse [I] / Zen Headbutt [I] / Disable [I] | Damp [I] | None [I] |

No historical save, moves, abilities, held items, medicine stock, or exact Cynthia-entry condition reported.

## hard-03

Source: [double--ragnarok](https://forums.neoseeker.com/24412/t887413-your-elite-4-team/2.htm) — self-reported completion.

Instantiate the source description of all six approximately level 50 as exact level 50; this is an approximation, not six individually reported exact levels.

Bag: **4 Hyper Potions, 2 Revive(s), 1 Full Heal(s)**.

| Pokémon | Level | Four moves | Ability | Held item |
|---|---:|---|---|---|
| Empoleon | 50 | Surf [I] / Brine [I] / Metal Claw [I] / Peck [I] | Torrent [I] | None [I] |
| Honchkrow | 50 | Wing Attack [I] / Night Shade [I] / Feint Attack [I] / Night Slash [I] | Insomnia [I] | None [I] |
| Haunter | 50 | Shadow Ball [I] / Hypnosis [I] / Dream Eater [I] / Confuse Ray [I] | Levitate [I] | None [I] |
| Luxray | 50 | Spark [I] / Crunch [I] / Thunder Fang [I] / Charge [I] | Rivalry [I] | None [I] |
| Abomasnow | 50 | Razor Leaf [I] / Wood Hammer [I] / Ice Shard [I] / Swagger [I] | Snow Warning [I] | None [I] |
| Heracross | 50 | Aerial Ace [I] / Brick Break [I] / Take Down [I] / Close Combat [I] | Swarm [I] | None [I] |

No historical save or verified low-resource replay. Unknown fields remain imputed.

## hard-04

Source: [Puzzleheaded-Bread62](https://www.reddit.com/r/pokemon/comments/g80cme/comment/i75rpk4/) — self-reported completion.

Reuse reported League-entry levels; choose Bronzong 46 from the reported 45-or-46 range. These are not verified Cynthia-entry levels.

Bag: **3 Hyper Potions, 1 Revive(s), 2 Full Heal(s)**.

| Pokémon | Level | Four moves | Ability | Held item |
|---|---:|---|---|---|
| Hippowdon | 44 | Earthquake [I] / Crunch [I] / Yawn [I] / Protect [I] | Sand Stream [I] | None [I] |
| Bronzong | 46 | Extrasensory [I] / Hypnosis [I] / Reflect [I] / Gyro Ball [I] | Levitate [I] | None [I] |
| Blissey | 47 | Soft-Boiled [I] / Toxic [I] / Light Screen [I] / Thunderbolt [I] | Natural Cure [I] | None [I] |
| Roserade | 47 | Giga Drain [I] / Magical Leaf [I] / Sludge Bomb [I] / Toxic [I] | Natural Cure [I] | None [I] |
| Golduck | 48 | Surf [I] / Water Pulse [I] / Zen Headbutt [I] / Disable [I] | Damp [I] | None [I] |
| Honchkrow | 48 | Wing Attack [I] / Night Shade [I] / Feint Attack [I] / Night Slash [I] | Insomnia [I] | None [I] |

Full moves, Toxic user, medicine, battle style and stats unknown. Source describes Toxic stalls, not a limited-item repeatable route.

## hard-05

Source: [The Incarnation](https://bulbagarden.net/threads/your-status-reports.15049/post-368074) — self-reported completion.

Reuse reported League-entry levels, including Infernape 49; the source also reports different finishing levels.

Bag: **3 Hyper Potions, 2 Revive(s), 1 Full Heal(s)**.

| Pokémon | Level | Four moves | Ability | Held item |
|---|---:|---|---|---|
| Infernape | 49 | Fire Blast [I] / Close Combat [I] / Flame Wheel [I] / Rock Climb [I] | Blaze [I] | Soft Sand [R] |
| Luxray | 48 | Spark [I] / Crunch [I] / Thunder Fang [I] / Charge [I] | Rivalry [I] | Quick Claw [R] |
| Staraptor | 48 | Brave Bird [R] / Fly [I] / Close Combat [I] / Defog [I] | Intimidate [I] | Amulet Coin [R] |
| Lucario | 48 | Aura Sphere [I] / Force Palm [I] / Bone Rush [I] / Quick Attack [I] | Steadfast [I] | Shell Bell [R] |
| Roserade | 48 | Giga Drain [I] / Magical Leaf [I] / Sludge Bomb [I] / Toxic [I] | Natural Cure [I] | Big Root [R] |
| Palkia | 47 | Spacial Rend [I] / Dragon Claw [I] / Ancient Power [I] / Water Pulse [I] | Pressure [I] | Lustrous Orb [N] |

Whole-League use5SuperPotion/2Revive/1Hyper/1MaxPotion/1MagoBerry is not Cynthia stock. Author admits luck and only Infernape25HP survived. Full moves unknown.

The source’s “Lustrous Pearl” is interpreted as the item **Lustrous Orb**; the source reports whole-League item use, not the bag shown here.

## hard-06

Source: [TheSunIsADedlyLazr](https://www.smogon.com/forums/threads/pokemon-games-dp-teambuilding-competition.3649787/#post-8116994) — human team design.

Designer-assigned level 48 for all six. The expert design reports no levels; former level 52 and proposal level 48 were both benchmark imputations.

Bag: **3 Hyper Potions, 1 Revive(s), 1 Full Heal(s)**.

| Pokémon | Level | Four moves | Ability | Held item |
|---|---:|---|---|---|
| Torterra | 48 | Wood Hammer [D] / Earthquake [D] / Synthesis [D] / Crunch [D] | Overgrow [D] | Soft Sand [D] |
| Staraptor | 48 | Brave Bird [D] / Return [D] / Fly [D] / Roost [D] | Intimidate [D] | Sky Plate [D] |
| Tentacruel | 48 | Surf [D] / Barrier [D] / Toxic Spikes [D] / Sludge Bomb [D] | Liquid Ooze [D] | Mystic Water [D] |
| Rapidash | 48 | Flare Blitz [D] / Megahorn [D] / Bounce [D] / Strength [D] | Flash Fire [D] | Expert Belt [D] |
| Skuntank | 48 | Poison Jab [D] / Night Slash [D] / Explosion [D] / Rock Smash [D] | Stench [D] | Black Glasses [D] |
| Bronzong | 48 | Calm Mind [D] / Psychic [D] / Flash Cannon [D] / Stealth Rock [D] | Levitate [D] | Mind Plate [D] |

No historical save or verified low-resource replay. Unknown fields remain imputed.

## hard-07

Source: [Byds](https://www.smogon.com/forums/threads/pokemon-games-dp-teambuilding-competition.3649787/#post-8119895) — human team design.

Designer-assigned level 50 for the first five and Palkia 47. The expert design reports no levels. Preserve the source-described earlier Payback alternative and delayed Machop Dynamic Punch route.

Bag: **2 Hyper Potions, 1 Revive(s), 1 Full Heal(s)**.

| Pokémon | Level | Four moves | Ability | Held item |
|---|---:|---|---|---|
| Torterra | 50 | Wood Hammer [D] / Earthquake [D] / Crunch [D] / Stone Edge [D] | Overgrow [D] | Earth Plate [D] |
| Crobat | 50 | Fly [D] / U-turn [D] / Toxic [D] / Roost [D] | Inner Focus [D] | Amulet Coin [D] |
| Machamp | 50 | Dynamic Punch [D] / Rock Tomb [D] / Strength [D] / Rock Climb [D] | No Guard [D] | Fist Plate [D] |
| Gyarados | 50 | Waterfall [D] / Ice Fang [D] / Dragon Dance [D] / Thunderbolt [D] | Intimidate [D] | Sea Incense [D] |
| Drapion | 50 | Payback [A] / Poison Jab [D] / Earthquake [D] / Rock Smash [D] | Battle Armor [D] | Dread Plate [D] |
| Palkia | 47 | Spacial Rend [D] / Surf [D] / Ice Beam [D] / Fire Blast [D] | Pressure [D] | Lustrous Orb [D] |

Contest-winning design, not an observed low-level/low-item victory. TM quantities and trade story not reconstructed.

Drapion keeps the source-described earlier **Payback** alternative. Machamp’s Dynamic Punch uses a permitted delayed-evolution route.

## hard-08

Source: [Deleted Reddit author; linked guest Pastebin](https://www.reddit.com/r/PokemonHallOfFame/comments/e9ho5i/) — self-reported completion.

Reuse reported finishing levels; do not label them exact Cynthia-entry levels or reproduce the separately reported final stats.

Bag: **3 Hyper Potions, 1 Revive(s), 2 Full Heal(s)**.

| Pokémon | Level | Four moves | Ability | Held item |
|---|---:|---|---|---|
| Luxray | 51 | Spark [R] / Crunch [R] / Charge [R] / Strength [R] | Intimidate [R] | Razor Claw [R] |
| Infernape | 52 | Flame Wheel [R] / Close Combat [R] / Cut [R] / Rock Climb [R] | Blaze [R] | None [R] |
| Crobat | 55 | Aerial Ace [R] / Poison Fang [R] / Fly [R] / Confuse Ray [R] | Inner Focus [R] | Poison Barb [R] |
| Bronzong | 51 | Extrasensory [R] / Feint Attack [R] / Rock Smash [R] / Safeguard [R] | Levitate [R] | Odd Incense [R] |
| Mantine | 52 | Surf [R] / Waterfall [R] / Confuse Ray [R] / Ice Beam [R] | Swift Swim [R] | Icicle Plate [R] |
| Mismagius | 51 | Shadow Ball [R] / Psychic [R] / Dark Pulse [R] / Confuse Ray [R] | Levitate [R] | Spell Tag [R] |

First-try Pearl completion is not a witness for this Set/limited-bag/shared-stat fixture. Reported final stats are preserved as source evidence, not reproduced.

The source’s [linked moves/items list](https://pastebin.com/4FxQpZnz) is retained; “Faint Attack” is normalized to the engine label “Feint Attack.”

## hard-09

Source: [JohnnyTsunamie](https://www.reddit.com/r/pokemonplatinum/comments/1heyay8/) — self-reported completion.

Reuse Medicham 53 directly visible in the source image; assign the other five level 50 within the discussed overall 47–54 range. Their individual levels were not established.

Bag: **3 Hyper Potions, 2 Revive(s), 1 Full Heal(s)**.

| Pokémon | Level | Four moves | Ability | Held item |
|---|---:|---|---|---|
| Medicham | 53 | Drain Punch [R] / Fire Punch [R] / Ice Punch [R] / High Jump Kick [R] | Pure Power [I] | Fist Plate [R] |
| Ambipom | 50 | Fake Out [R] / Return [R] / U-turn [R] / Aerial Ace [R] | Technician [I] | None [I] |
| Crobat | 50 | Brave Bird [R] / Whirlwind [R] / U-turn [I] / Roost [I] | Inner Focus [I] | None [I] |
| Bronzong | 50 | Earthquake [I] / Reflect [I] / Light Screen [I] / Stealth Rock [I] | Levitate [I] | None [I] |
| Golduck | 50 | Surf [I] / Ice Beam [I] / Psychic [I] / Calm Mind [I] | Damp [I] | None [I] |
| Mismagius | 50 | Shadow Ball [R] / Dark Pulse [R] / Confuse Ray [I] / Thunderbolt [I] | Levitate [I] | None [I] |

Only one image retrieved directly. Other moves are partly discussed in comments; unverified slots remain imputed. Full bag, abilities and stats unknown.

The [source image](https://i.redd.it/v98le8jvv17e1.png) establishes Medicham’s level, gender, held item and four moves. Other established moves come from discussion; all remaining slots are marked I.

## hard-10

Source: [Deleted Reddit author](https://www.reddit.com/r/nuzlocke/comments/1e60bz9/my_team_of_misfits_just_won_our_hardcore_diamond/) — self-reported completion.

Designer-assigned level 50 for all six; the completion source establishes no levels. This is not a claim that the historical team won underleveled.

Bag: **4 Hyper Potions, 2 Revive(s), 1 Full Heal(s)**.

| Pokémon | Level | Four moves | Ability | Held item |
|---|---:|---|---|---|
| Roserade | 50 | Giga Drain [I] / Magical Leaf [I] / Sludge Bomb [I] / Toxic [I] | Natural Cure [I] | None [I] |
| Lumineon | 50 | Surf [I] / Ice Beam [I] / Rain Dance [R] / U-turn [I] | Swift Swim [I] | None [I] |
| Mr. Mime | 50 | Psychic [I] / Reflect [I] / Light Screen [I] / Calm Mind [I] | Soundproof [I] | None [I] |
| Pachirisu | 50 | Super Fang [I] / Spark [I] / Sweet Kiss [I] / Charm [I] | Run Away [I] | None [I] |
| Spiritomb | 50 | Shadow Ball [I] / Dark Pulse [I] / Hypnosis [I] / Dream Eater [I] | Pressure [I] | None [I] |
| Vespiquen | 50 | Attack Order [I] / Defend Order [I] / Heal Order [I] / Power Gem [I] | Pressure [I] | None [I] |

Author calls it hardcore but complete rules were not independently established. Spiritomb had already fainted before Cynthia; proposal restores all six and permits medicines, openly departing from historical scenario.

The historical account had already lost Spiritomb before Cynthia. This fixture restores it along with the other five Pokémon and permits the listed medicine.

## Small glossary

**HP** is health; reaching zero means a Pokémon faints. **PP** counts remaining uses of a move. An **ability** is a passive effect. A **held item** is carried by one Pokémon, while bag medicine is chosen by the player. **IVs, EVs and nature** affect statistics; the shared defaults above prevent unreported training differences from being mistaken for historical facts.
