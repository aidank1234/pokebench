# Methodology

PokéBench evaluates six model-plus-harness systems on the original Pokémon Diamond battle against Champion Cynthia. The published comparison contains 180 genuine game endings: 30 per system on the same ten teams and three starting RNG states per team.

## Score

PokéBench Score v1 combines victory with partial credit for terminal opponent HP depletion:

```text
battle_score = (100 × win_indicator + opponent_HP_depleted_percent) / 2

PokéBench Score = mean(battle_score)
                = 0.5 × win_rate_percent
                + 0.5 × mean_opponent_HP_depleted_percent
```

A win earns 100 points. A loss earns half its terminal HP-depletion percentage: an 80%-depletion loss earns 40 points. The system score is the arithmetic mean over the same 30 fixtures. Both components already have natural 0–100 endpoints, so no scaling against the observed models is needed. A score of 100 requires winning all battles. It is not a win probability.

For each battle, HP depletion is:

```text
HP_depleted_percent = (100 / 6) × sum(1 − current_HP_j / maximum_HP_j)
```

The sum covers Cynthia’s six original Pokémon, each weighted equally regardless of maximum HP. A win has 100% depletion. Healing can reduce depletion; the metric measures terminal HP state rather than cumulative damage dealt.

Because wins already contribute 100% HP depletion, the two components overlap. The equivalent per-battle rule makes that overlap explicit: full credit for victory, up to 50 points for progress in a loss. In the score chart, the dark segment is `100 × wins / battles`; the lighter segment is the summed partial loss credit divided by all battles.

### Weighting

Equal weighting is a benchmark scoring convention, not an estimate of an objectively optimal tradeoff. It keeps the rule simple and preserves a substantial victory reward. Composite-indicator guidance recommends stating weights and considering overlap between components. [JRC: Weighting](https://knowledge-for-policy.ec.europa.eu/composite-indicators/toolkit_en/navigation-page/10-step-guide_en/step-6-weighting_en)

Arithmetic aggregation supplies the direct per-battle rule above. It permits compensation: improved loss progress can offset some reduction in win rate. A geometric or harmonic combination would assign a zero-win system zero overall credit regardless of its progress. These are design tradeoffs. [JRC: Aggregating indicators](https://knowledge-for-policy.ec.europa.eu/composite-indicators/toolkit_en/navigation-page/10-step-guide_en/step-7-aggregating-indicators_en)

The current model order is unchanged at 25%, 50%, and 75% weight on win rate. This checks dependence on the weighting choice; it does not establish statistical significance or generalization. [JRC: Sensitivity analysis](https://knowledge-for-policy.ec.europa.eu/composite-indicators/toolkit_en/navigation-page/10-step-guide_en/step-8-sensitivity-analysis_en)

**The composite was defined after these runs were complete and their component results were known. It was not preregistered for this cohort.** The v1 formula is fixed for future cohorts. Changes to weights or aggregation require a new metric version. Original wins, losses, binary win scores, and HP measurements remain unchanged. No battle was replayed to construct the composite.

## Tasks and observations

Every fixture specifies a player team, bag, starting save/state, and initial RNG value. Teams are underleveled relative to Cynthia, use Set mode, and start healthy with full base PP. Levels, moves, traits, held items, supplies, and field-level source attribution are documented in [TEAMS.md](TEAMS.md).

Agents receive both DS screens and structured public battle information: their own party statistics, HP, moves, PP, inventory, visible conditions, and public battle events. Opponent information is limited to what a player can observe. Exact hidden opponent statistics, current RNG, future choices, private fixture files, and verifier data remain host-only.

Legal actions include moves, switches, supplied items, learning choices, and menu/button input. Agents may use public references and independent calculations. They may not reset or branch a battle, edit game memory or saves, or use earlier trials and other models’ work. Each run begins with fresh native session state and isolated filesystem access.

Three seeds share the same team and first-decision state except for the initial RNG value. Actions consume RNG differently, so matched starts do not guarantee identical subsequent luck. The original `CynthiaBench-v1` seed prefix is retained to preserve those fixtures.

## Systems and execution controls

| System | Harness | Effort | Agent safety window | Response ceiling |
| --- | --- | --- | --- | --- |
| Opus 5.5 | Claude Code 2.1.319 | max | 3,600 s | Not separately established in historical profile |
| GPT-6 Astra | Codex 0.169.0-alpha.11.1 | max | 3,600 s | Not separately established in historical profile |
| GPT-6.1 Sol | Codex 0.169.0-alpha.11.1 | max | 3,600 s | Not separately established in historical profile |
| Gemini 4 Neon | Antigravity 1.5.12 | xhigh | 3,600 s | Native default; not separately established |
| Gemini 4 Carbon | Antigravity 1.5.12 | high | 3,600 s; six replacements at 14,400 s | Native default; not separately established |
| DeepSeek Flash | OpenCode 1.19.6 | max | 3,600 s | 393,216 tokens per response |

All fixtures retain the 1,200-action and 180,000-advanced-frame limits. Time and other limits are execution controls: a non-game stop is not a primary loss and cannot complete a result cell. The complete comparison contains only authoritative game wins and losses. New incomplete cohorts report pending cells and withhold final scores.

Historical Opus allowed its documented native Haiku 5.5 web/background helper; usage was recorded in 28 games. The new Gemini and Flash studies disallow deliberate additional-model or agent delegation. This difference belongs to the evaluated system and prevents interpreting the table as an isolated comparison of base-model ability. Requested model identifiers, CLI versions, effort settings, and available executable hashes are recorded; these do not attest remote model weights.

### Non-game stops and metadata corrections

All 90 original Opus/Astra/Sol results were genuine game endings. They remain unchanged.

For Gemini, 54 original genuine results were retained. Six Carbon noncompletions were replayed in fresh isolated sessions without previous-attempt history or strategy. The replacement policy allowed a four-hour safety window and up to 64 neutral same-session continuations; actual continuation counts were zero in all six replacements. Those games produced two wins and four genuine losses. No genuine loss was replayed.

Flash’s published 30-game cohort is the separately frozen high-response-limit experiment. Earlier default-limit and infrastructure-interrupted experiments contribute no current primary rows. Eight genuine Flash losses required reconstruction of missing framework reward metadata after a command-directory detector flagged legal commands. The original host losses were retained, not replayed or changed. Corrected cases and source commitments are recorded in the results provenance.

## Verification and retained data

The host verifies the original Cynthia identity and party invariants, then the actual terminal game outcome. Agent text cannot award a win. Results retain case identifiers, fixture hashes, execution controls, outcome and HP measurements, action counts, elapsed wall time, emulated frames, and evidence hashes. The included reporting checks recompute aggregate scores from the episode table and reject incomplete or inconsistent cohorts.

Original sessions retain native events, commands, game-state histories, initial/final images, and recordings. Flash exposes reasoning text; Gemini exposes thought chunks; Opus exposes short thinking updates; Codex records assistant commentary and tools without an explicit reasoning-text stream in these logs. These are provider-exposed records, not a guarantee of complete internal reasoning. Gemini thought chunks are ordered without precise timestamps for each chunk. Recordings omit model-thinking pauses and are not wall-clock execution footage.

The public repository contains structured results and evidence commitments. Recordings, raw sessions, credentials, private game files, and local operational ledgers are retained separately.

## Limitations

- Ten curated teams and related seed variants are a narrow sample. Small score differences do not establish a general ranking.
- Fixture development included early model-assisted work, including a model family later evaluated.
- Harnesses, helper policies, model access, effort settings, runtime revisions, and completion controls differ.
- All selected outcomes passed automated checks and AI protocol/evidence review; independent human validation is not claimed.
- Private ROM, save, emulator state, binary build, and provider access affect reproduction. Rebuilt assets and updated CLIs require new validation and provenance.
- Wall time includes host scheduling and provider behavior. Emulator video duration excludes thinking pauses; neither is a standardized speed score here.

[Results](RESULTS.md) provide the system configurations, all episode rows, cost coverage, and provenance. [Setup](SETUP.md) describes the current public runtime, which incorporates distribution fixes and is not claimed to be byte-identical to every historical run.
