# Contributing

Before adding a system, record its model identifier, CLI version and hash, effort, tools, helper policy, response ceiling, execution limits, and fixture hashes. Freeze those settings before collecting results. Keep calibration and development games separate from scored games.

A completed game win or loss is final. Non-game stops remain pending and require a reviewed fresh attempt; they are not primary losses. Preserve earlier attempts and the reason for replacement. Do not repeat a genuine loss to seek a better outcome.

PokéBench Score v1 is fixed: a win earns 100, a loss earns half its terminal opponent HP-depletion percentage, and the system score is the mean over the declared fixture set. Keep raw win and HP measurements. A scoring change requires a new metric version.

For a new team, document the source and every imputed detail, including moves, levels, traits, medicine, and battle rules. Validate the loaded fixture, legal controls, verifier, and isolation before collecting scores. Seek independent review of scenarios and grading.

Run the checks in [SETUP.md](docs/SETUP.md). Asset-free tests do not replace game-engine validation on a new host. Public result tables and charts must be reproducible from the included episode data.

Keep credentials, local configuration, ROMs, saves, emulator states, compiled cores, raw agent sessions, debug logs, and recordings out of Git. Publish structured results with relative identifiers and evidence hashes. Code and documentation should explain the benchmark without local machine paths or internal operating notes.

No general license for the project’s original code has been selected. Retain dependency, source, and logo attribution in [THIRD_PARTY.md](THIRD_PARTY.md).
