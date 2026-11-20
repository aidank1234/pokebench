# Third-party sources and local builds

PokéBench uses the projects below. Their code and assets retain their own notices and terms. This file is an inventory, not a blanket license grant for PokéBench, upstream code, Pokémon game material or human source posts. No general license for the project’s original code has been selected.

**No ROM, BIOS, save, emulator state, compiled emulator core or upstream source checkout is vendored in this repository.** The `vendor/` directory is ignored. The optional helper fetches public source repositories into that local directory only when explicitly run; it never obtains game files or credentials.

## Main dependencies

| Project | Use | Pinned source / version | Observed upstream notice |
|---|---|---|---|
| [Harbor](https://github.com/harbor-framework/harbor) | Job/trial orchestration and evaluation interfaces | Python package **0.26.0**; [reference commit](https://github.com/harbor-framework/harbor/tree/e26966a026f872925979d8479b7ace236b68303f) | [Apache License 2.0](https://github.com/harbor-framework/harbor/blob/e26966a026f872925979d8479b7ace236b68303f/LICENSE) |
| [melonDS DS](https://github.com/JesseTG/melonds-ds) | Libretro emulator core | **1.4.0**, [commit f394adb](https://github.com/JesseTG/melonds-ds/tree/f394adbacb5722ee97c1b37c8064da9a25818310) | [GNU GPL version 3](https://github.com/JesseTG/melonds-ds/blob/f394adbacb5722ee97c1b37c8064da9a25818310/LICENSE), plus component notices |
| [PKHeX](https://github.com/kwsch/PKHeX) | Save-format reference and English label source files | [commit 6c95838](https://github.com/kwsch/PKHeX/tree/6c9583883c7551ac74a4b74180cead249d4277b8) | [GNU GPL version 3](https://github.com/kwsch/PKHeX/blob/6c9583883c7551ac74a4b74180cead249d4277b8/LICENSE) |
| [pret/pokediamond](https://github.com/pret/pokediamond) | Reference constants and character mapping for original Diamond | [commit b623abe](https://github.com/pret/pokediamond/tree/b623abef1b4dfe204ca50aab42447ac00c134290) | No repository-wide `LICENSE` file at the inspected revision; no blanket license is asserted here. |

The source commit for Harbor documents the reference checkout used during the study. Runtime installation uses the separately pinned Python package, not an assertion that a locally rebuilt checkout is byte-identical to its wheel. Python package pins are in [pyproject.toml](pyproject.toml); other installed packages retain their own distributions' licenses.

The packaged Python extraction script reads PKHeX's English label source files; it does not compile or execute PKHeX. Save construction uses the included Python implementation. PKHeX's own [build instructions at the pinned revision](https://github.com/kwsch/PKHeX/blob/6c9583883c7551ac74a4b74180cead249d4277b8/README.md#building) specify .NET 10, but .NET is not required by this repository's setup steps. The pret repository is a decompilation/reference project; its presence in this inventory does not grant rights to the game or authorize redistribution of derived assets.

## Pinned core dependencies

The historical core build resolved the dependencies below. [config/upstream-pins.json](config/upstream-pins.json) records their complete 40-character commits and source URLs. The helper supplies these hashes to CMake, including replacing the upstream moving `jtg/fix-uninitialized-opengl` branch with its observed commit.

| Dependency | Source revision | License reference |
|---|---|---|
| melonDS | [16f127d](https://github.com/JesseTG/melonDS/tree/16f127dbb587a73371827ce79689c5d237c4b59b) | [GPL version 3 and component notices](https://github.com/JesseTG/melonDS/blob/16f127dbb587a73371827ce79689c5d237c4b59b/LICENSE) |
| libretro-common | [fa8a1b5](https://github.com/libretro/libretro-common/tree/fa8a1b572a444fcb552007019f8caf68281108aa) | Per-file notices; no root license file was present in the inspected checkout. |
| embed-binaries | [078b62b](https://github.com/andoalon/embed-binaries/tree/078b62beba97e8192c99bfb16d5e17220cfc7598) | [MIT](https://github.com/andoalon/embed-binaries/blob/078b62beba97e8192c99bfb16d5e17220cfc7598/LICENSE) |
| GLM | [6f14f47](https://github.com/g-truc/glm/tree/6f14f4792a0cde5d0cf2c910506724d61cb95834) | [Happy Bunny or MIT](https://github.com/g-truc/glm/blob/6f14f4792a0cde5d0cf2c910506724d61cb95834/copying.txt) |
| libslirp mirror | [e61dbd4](https://github.com/JesseTG/libslirp-mirror/tree/e61dbd459c8c06607b3a84694489427e8ec60f17) | [COPYRIGHT and per-file notices](https://github.com/JesseTG/libslirp-mirror/blob/e61dbd459c8c06607b3a84694489427e8ec60f17/COPYRIGHT) |
| pntr | [a47fe16](https://github.com/robloach/pntr/tree/a47fe167703b0ac3bfad00785b20b0dce6712509) | [Zlib license](https://github.com/robloach/pntr/blob/a47fe167703b0ac3bfad00785b20b0dce6712509/LICENSE.md) |
| fmt | [1be298e](https://github.com/fmtlib/fmt/tree/1be298e1bd68957e4cd352e1f676f00e07dcfb57) | [MIT](https://github.com/fmtlib/fmt/blob/1be298e1bd68957e4cd352e1f676f00e07dcfb57/LICENSE) |
| yamc | [4e015a7](https://github.com/yohhoy/yamc/tree/4e015a7e8eb0d61c34e6928676c8c78881a72d73) | [MIT](https://github.com/yohhoy/yamc/blob/4e015a7e8eb0d61c34e6928676c8c78881a72d73/LICENSE) |
| span-lite | [f63fe29](https://github.com/martinmoene/span-lite/tree/f63fe2974943b5f555b928b10d910e457fbb93d4) | [Boost Software License 1.0](https://github.com/martinmoene/span-lite/blob/f63fe2974943b5f555b928b10d910e457fbb93d4/LICENSE.txt) |
| date | [179a6b9](https://github.com/HowardHinnant/date/tree/179a6b921ccd6179b5b04dce812a89f71a51b4df) | [MIT, with per-file author notices](https://github.com/HowardHinnant/date/blob/179a6b921ccd6179b5b04dce812a89f71a51b4df/LICENSE.txt) |
| zlib | [da607da](https://github.com/madler/zlib/tree/da607da739fa6047df13e66a2af6b8bec7c2a498) | [Zlib license](https://github.com/madler/zlib/blob/da607da739fa6047df13e66a2af6b8bec7c2a498/LICENSE) |

This table is not a complete license inventory for every nested file. melonDS DS generates a combined `build/melondsds-LICENSE.txt` from its [attribution inputs](https://github.com/JesseTG/melonds-ds/blob/f394adbacb5722ee97c1b37c8064da9a25818310/cmake/GenerateAttributions.cmake), including bundled components and fonts. Keep that output and upstream notices with local builds. No compiled library is included in this repository.

## Local source setup

From the repository root:

```sh
# Print the plan only: no network, checkouts or compilation.
python scripts/fetch_sources.py --dry-run --build-core

# Fetch three exact public source checkouts: melonDS DS, PKHeX and pokediamond.
python scripts/fetch_sources.py

# Optionally configure and compile the emulator core on macOS.
python scripts/fetch_sources.py --build-core --jobs 2
```

The optional build requires Git, CMake and an appropriate Apple compiler toolchain. It uses Release mode, disables JIT, OpenGL, compute rendering, threaded rendering and tests, and pins every listed CMake dependency. The expected local output is `vendor/melonds-ds/build/src/libretro/melondsds_libretro.dylib`. Harbor is installed through the project's Python dependencies; the helper does not clone or install it.

The helper refuses dirty checkouts, unexpected Git origins and symlinked vendor destinations. It never runs `git reset` or `git clean`. Existing core dependency checkouts must already match the pins; preserve a different build directory and start fresh rather than overwriting it. Interrupted fetches may leave partial local checkouts requiring inspection.

The helper's dry-run and safety behavior are checked offline. A fresh network download and complete build have not been repeated as part of packaging. The recorded source revisions came from the retained pilot workspace; compiler versions, operating system and build environment can still change the binary hash. A newly built core therefore needs its own validation and freeze before producing comparable scored results. See [SETUP.md](docs/SETUP.md) for the remaining asset and fixture steps.

## Game material and human sources

Pokémon names are third-party material and do not imply affiliation with Nintendo, Game Freak or The Pokémon Company. Game files and recordings are excluded. Human roster posts are linked and attributed in [TEAMS.md](docs/TEAMS.md); their inclusion does not relicense those posts or establish independent verification of this benchmark.

## Model logos

The result figures identify model providers with their logos. The Claude spark and Gemini sparkle use first-party assets. The OpenAI knot and DeepSeek whale use @lobehub/icons-static-svg 1.104.0 under the [MIT license](docs/figures/logos/lobe-icons-license.txt). Logo source URLs and hashes are recorded in [the figure sources](docs/figures/sources.json). Trademarks remain with their respective owners; inclusion does not imply endorsement.
