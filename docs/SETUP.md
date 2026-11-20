# Setup

The repository contains the emulator bridge, native agent adapters, 30 Harbor tasks, team specifications and result data. Game files and native login stores remain local.

Real battles require **Apple Silicon macOS**, Python 3.13, Apple Command Line Tools, FFmpeg/ffprobe and a locally built melonDS DS libretro core. The Seatbelt boundary uses `sandbox-exec`; Linux, Windows and stock Docker execution are not supported. Many source-only tests run without private game assets.

## Python and emulator

```sh
python3.13 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.lock.txt
python -m pip install --no-deps -e .
python -m pytest -q
python scripts/fetch_sources.py --dry-run --build-core
python scripts/fetch_sources.py --build-core
```

Install FFmpeg and Apple Command Line Tools separately. The fetch helper obtains pinned public emulator/reference sources, not a game ROM or save. Its revisions are recorded in [config/upstream-pins.json](../config/upstream-pins.json); see [third-party notices](../THIRD_PARTY.md).

## Game assets and teams

Supply an original English Pokémon Diamond ROM at `assets/private/diamond.nds` and a raw 512 KiB donor save at `assets/private/base.sav`. The donor must be immediately before Cynthia, facing her, after the Elite Four. The save editor changes the party, bag and options; it does not play the preceding game or move the player there. Other games, languages and modified ROM layouts are not validated.

The recorded ROM hash is `726b2c6b0bdca37bf68881542cf7d1c26f0f8579ae1585491b0c73bc1d3fc63e`. [Fixture data](../data/teams/fixtures.json) records the original private-asset hashes. Rebuilding with different assets or binaries is a new run, not a byte-identical reproduction.

```sh
python scripts/extract_gen4_data.py
python scripts/extract_cynthia.py
python scripts/build_saves.py
python scripts/build_harbor_tasks.py
for team in hard-01 hard-02 hard-03 hard-04 hard-05 hard-06 hard-07 hard-08 hard-09 hard-10; do
  python scripts/build_fixtures.py --team "$team"
done
python scripts/validate_recording.py --case hard-01-seed1
```

Review the loaded party, full HP, inventory, first command menu and initial-state hashes before a scored study. `validate_recording.py` makes a passive capture without a strategic action or model call; it is not a scored game. `teams/team-specs.json` is the canonical payload. The `CynthiaBench-v1` seed prefix is retained because changing it changes the fixtures.

Copy [config/paths.example.toml](../config/paths.example.toml) to ignored `config/local.toml` to override paths. Relative paths resolve against the checkout. `POKEBENCH_CONFIG` selects another local file. Keep private material out of task directories.

## Agent profiles

Only the selected provider's CLI must be installed. The runner records its executable hash and version before and after each trial. Optional executable overrides live in the local TOML `[native]` section.

| Alias | Requested model | Harness | Effort |
| --- | --- | --- | --- |
| `sol` | `gpt-6.1-sol` | Codex | max |
| `astra` | `gpt-6-astra` | Codex | max |
| `opus` | `claude-opus-5-5` | Claude Code | max |
| `neon` | `gemini-4-neon-xhigh` | Antigravity (`agy`) | xhigh |
| `carbon` | `gemini-4-carbon-high` | Antigravity (`agy`) | high |
| `flash` | `deepseek-flash` | OpenCode | max |

These IDs and client protocols describe the recorded systems; current access and CLI compatibility must be checked locally. The published adapters do not attest remote model weights. Gemini and Flash use fresh per-trial client state with no inherited history or resume. Their strict profiles require a separate Homebrew Python 3.13 runtime; setting `native.calculation_python` does not relax the validated runtime boundary. Gemini uses its native Keychain login, while Flash receives only a scoped local proxy token. Authenticate subscribed CLIs through their own login flows.

### Flash transport

Flash uses the host-owned metered proxy so the account key never enters the agent process. Store the key in a local mode-0600 file outside Git, then start a new transport study in a separate terminal:

```sh
python -m cynthiabench.deepseek_proxy \
  --key-file /path/to/local/deepseek.key \
  --admin-config work/deepseek-proxy-admin.json \
  --halt-path work/provider-halt.json \
  --output-root work/flash-provider-01 \
  --new-study --balance-admission reported-balance
```

The output directory must be unused. A restart or continuation with prior liabilities uses `--carry-ledger` instead of `--new-study`; preserve its reviewed request/status evidence. The proxy's historical rate table and USD50 guard are recorded assumptions, not a claim about current provider pricing. Confirm prices and configure a new study before interpreting its estimates. Failed-request usage gaps remain unknown rather than zero. No automatic refill is performed.

OpenCode must support the recorded V1 command/configuration interface. The profile pins a 393,216-token response ceiling and same-model compaction. Use at most two concurrent Flash trials with the supplied proxy.

## Development run

Start the private game host:

```sh
python -m cynthiabench.server --max-sessions 6 --admin-config work/admin.json
```

In another activated terminal, inspect a one-case plan before invoking a model:

```sh
python scripts/run_harbor.py --job dev-01 --models sol --tasks hard-01-seed1 --concurrency 1 --dry-run
python scripts/run_harbor.py --job dev-01 --models sol --tasks hard-01-seed1 --concurrency 1
```

Substitute `neon`, `carbon`, `opus`, `astra` or `flash` as appropriate. A dry run executes CLI identity checks but no inference. A live run uses the relevant subscription or API balance and writes local transcripts, state and recordings under ignored directories.

## Scored run and reporting

Review the environment, controls, recording and isolation before declaring readiness in local `reports/release-gates.json`. Then freeze the selected fixtures, source, CLI versions, concurrency and execution settings:

```sh
python scripts/analyze_harbor.py --freeze --manifest provenance/local-study.json --jobs local-main --retry-jobs local-replay-01 --models sol astra opus --concurrency 6
python scripts/run_harbor.py --job local-main --models sol astra opus --concurrency 6 --freeze-manifest provenance/local-study.json --dry-run
python scripts/run_harbor.py --job local-main --models sol astra opus --concurrency 6 --freeze-manifest provenance/local-study.json
```

The checked-in tasks have 3,600-second safety limits; the host defaults are 1,200 actions and 180,000 advanced frames. A safety stop is unfinished, not a loss. Freeze any different controls before running a new cohort. The six published Carbon replacement games used a longer safety window. Their policy allowed neutral same-session continuations, but none were used. The historical systems did not all use identical completion controls.

An immutable genuine win or loss cannot be replayed. An unfinished cell may receive an explicitly reviewed fresh attempt in a predeclared replay job. The review records `non_game_stop: true` rather than pretending it was an infrastructure failure. Original attempts remain available. `infrastructure_retry_reviews` is the retained ledger field for both review types; entries need reviewer, reason and a prior timezone-aware `reviewed_at` value.

```sh
python scripts/analyze_harbor.py --manifest provenance/local-study.json --reviews reports/reviews.json --output reports/scored-analysis.json
```

Transcript reviews are required before accepting trials. A review records `reviewed`, `reviewer` and `protocol_compliant`; delegation flags and permitted native helper use require their own explicit resolution. Incomplete, unreviewed or mismatched cells keep a report incomplete. Binary outcomes and HP measurements remain separate from the [published composite score](METHODOLOGY.md).

The public source integrates the historical adapters with local configuration and terminal-only acceptance. It is a distribution revision, not the frozen source tree used for every historical run. Source-only tests do not establish that private assets, native authentication or a live game work on a new machine.
