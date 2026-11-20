# Result data

The current release contains 180 accepted game endings: 30 per system on ten teams with three starting RNG states each. All selected outcomes are genuine wins or losses. Non-game stops are pending and cannot become primary losses.

| File | Contents |
| --- | --- |
| `episodes.csv` | One row for each of the 180 selected model/fixture cells. |
| `systems.csv` | Six system totals, component metrics, composite score and runtime summaries. |
| `by-team.csv` | Sixty model/team aggregates, each covering three starting states. |
| `summary.json` | Machine-readable aggregates, execution qualifications, cost and trace coverage. |
| `study.json` | Declared model settings and protocol notes. |
| `score-definition.json` | PokéBench Score v1 formula, weights, definition timing and sources. |
| `costs.csv` | Sixty per-battle estimate records for Opus and Flash; other systems are unpriced. |
| `provenance.json` | Source SHA-256 commitments, evidence scope and documented replay/metadata exceptions. |

## Episode fields

`model`, `label`, `harness`, `cli_version`, `cli_sha256`, `effort` and `max_output_tokens` identify the requested system. Empty historical CLI hashes and output ceilings mean the public pilot profile did not establish those values. Provider-reported names do not independently attest remote model weights.

`team_id`, `case`, `replicate` and `initial_rng_seed` identify the fixture. Starting states match across systems; different actions can produce different subsequent luck. `trial_id` identifies the retained attempt.

`status=accepted`, `verified_terminal=true` and `termination_category=game_terminal` record the accepted evidence state. The score calculator requires a verified terminal win or loss. `outcome`, `win` and `win_score` preserve the original binary result: `win_score = 100 × win`.

`opponent_hp_depletion` is the equally weighted terminal HP-depletion percentage across Cynthia’s six original Pokémon. `pokebench_score` is 100 for a win and half that HP-depletion percentage for a loss. It is separate from `win_score`. `opponent_fainted` counts zero-HP opponents in the terminal evidence.

`actions` counts control requests, including acknowledgements and forced switches; it is not a battle-turn count. `emulated_frames` counts advanced game frames. `wall_seconds` includes agent/tool/provider time. Game recordings omit thinking pauses. `agent_safety_timeout_sec` and `neutral_continuations` report captured controls and actual continuations; empty historical fields remain unknown rather than being filled in. Six Carbon replays used 14,400-second safety windows, with zero neutral continuations actually needed. Their policy allowed up to 64.

`runtime_revision` distinguishes retained execution cohorts. `reviewed_native_helper_used` records Opus’s permitted native Haiku helper in 28 games. All models retain the same 30 selected fixture cells.

The `_sha256` fields identify starting state, save, ROM, core, terminal evidence, runtime audit and original recording. Original game assets, native sessions and recordings are retained separately. Hashes are commitments, not public access to those files. Historical runtime-audit hashes are not supplied by the old public profile.

## Aggregation

`win_rate_percent = 100 × wins / 30`. `mean_opponent_hp_depletion` includes wins at 100%. `pokebench_score` is their fixed 50/50 arithmetic average. `win_credit` plus `loss_hp_credit` is the equivalent decomposition into full win credit and half-HP credit from losses. The composite was defined after the runs and is fixed as v1 for future cohorts.

A system’s headline score is available only when all 30 declared, distinct fixture cells have accepted terminal outcomes. Missing, unfinished or unreviewed cells do not supply zero-point losses. Team-level `mean_battle_score` is an average over its three states, rather than a separate 30-battle system score. Values are calculated from the unrounded episode measurements; public tables round only for display.

## Costs and traces

Flash cost rows use frozen API rates applied to returned usage. Seven have complete returned usage; 23 have failed-request usage gaps. `known_usage_priced_subtotal_usd` is the recorded subset; `complete_usage_priced_estimate_usd` is blank when incomplete. Opus’s `native_list_price_estimate_usd` includes recorded Haiku helper usage. Those estimates are not actual subscription charges. `invoiced_cost_usd` is unknown throughout. Blank CSV values and JSON `null` mean unknown, never zero.

The summary records native trace availability. Exposed reasoning, assistant messages, commands and host action/state histories are retained separately; raw sessions are not included in this clean data release. The recorded reasoning is not guaranteed to contain complete internal reasoning.

## Recompute

Run `python scripts/summarize_results.py --check` from the repository root. This validates the 180 cells, fixture identities and hashes, terminal-only scoring, published aggregates and artifact commitments without changing files. `--write` regenerates aggregate CSV/JSON tables from the included episode CSV. These operations need no emulator, model access or network connection.

See [results](../../docs/RESULTS.md) and [methodology](../../docs/METHODOLOGY.md).
