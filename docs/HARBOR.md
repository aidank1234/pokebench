# Harbor integration

PokéBench uses Harbor 0.26.0 for job scheduling, trial lifecycle, artifacts and verifier execution. [scripts/run_harbor.py](../scripts/run_harbor.py) selects custom native agents, a macOS environment and a host-side verifier. Generic `harbor run -p tasks/...` with the default verifier is unsupported; task `tests/test.sh` fails rather than grading an unverified result.

| Component | Responsibility |
| --- | --- |
| Native agent adapters | Start the selected CLI in a per-trial sandbox; retain messages, tools, identity and usage evidence |
| `CynthiaEnvironment` | Create a host game session and expose a public battle workspace |
| `CynthiaVerifier` | Accept an authoritative game win or loss; leave non-game stops unfinished |
| Local bridge / emulator worker | Execute legal game inputs, capture frames/audio and validate game memory |
| Strict analyzer | Match the frozen cohort, reject missing or mismatched evidence, require transcript review and preserve genuine outcomes |

The agent sees both DS screens and structured public battle information: its own party, moves/PP, inventory and visible opponent information. It can choose moves, switch, use supplied items, advance animations or operate raw menu controls. It cannot reset, edit saves, inspect hidden game state or access peer runs. Public references and independent calculations are permitted.

Administrator and actor tokens are distinct. Administrative methods create, finalize and close sessions; per-trial actor access only observes the live battle and executes legal actions. Credentials, private fixture files and verifier memory remain host-side. Flash uses a separate metered transport whose account credential never reaches OpenCode. Gemini uses native authentication services; its descendants inherit the explicitly documented Keychain permissions.

The macOS Seatbelt profiles restrict private files, old conversations, other workspaces, process inspection and unrelated local services. They are not a claim that every possible delegation path is prevented. Transcript review remains part of acceptance. Historical Opus permits its documented Haiku helper; Gemini and Flash prohibit deliberate additional-model delegation.

The checked-in tasks use a 3,600-second safety timeout, with host defaults of 1,200 action requests and 180,000 advanced game frames. These limits bound execution, not loss scoring. Only genuine game wins and losses complete primary cells. A normal CLI exit, refusal, quota error or safety stop before the battle ends remains unfinished. Reviewed fresh attempts can replace unfinished cells; completed game losses remain final.

Gemini's terminal controller permits up to 64 fixed neutral continuations in the same fresh session while the host remains ongoing. It does not supply a strategy or start another battle. The six published Carbon replacements also used four-hour safety windows. Other historical cohorts used their recorded controls; see [methodology](METHODOLOGY.md).

The runtime pins source/task hashes, fixtures, model configuration and native CLI identity before a study. Initial RNG states match across systems, but choices change later RNG consumption. The cleaned distribution retains the host cleanup correction: dead workers do not occupy admission slots, live workers retain capacity, and cleanup errors cannot overwrite a verified game result.

The [setup guide](SETUP.md) covers dependencies, local assets, adapter configuration, review and execution. [Harbor's task format](https://docs.harborframework.com/tasks/overview) documents the underlying job/task layout.
