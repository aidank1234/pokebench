# Team data

The single canonical roster file is [teams/team-specs.json](../../teams/team-specs.json). It contains the exact selected species, levels, moves, abilities, held items, genders, statistical defaults, party order and medicine for all ten teams. Battle payload values and original scored case names are preserved. `display_name` uses the public PokéBench name.

[sources.json](sources.json) gives the ten public source references and their limitations. [fixtures.json](fixtures.json) lists the 30 initial RNG states and original asset hashes. Private saves, ROMs and emulator states are not included. [TEAMS.md](../../docs/TEAMS.md) is the readable catalogue.

`field_provenance` distinguishes reported source values, human team-design choices, builder-imputed fields, normalized names and explicitly selected alternatives. Move provenance entries align positionally with the four moves. A reported level may be a League-entry or finishing level; its `interpretation` states how it was used. All bags and Set mode are benchmark choices.

The configured friendship value is 200 before battle initialization; the game naturally increments friendship when starting the Champion battle. Initial fixture audits used the actual loaded game state. The shared IV/EV/nature values are construction defaults, not claims about a source player's training.
