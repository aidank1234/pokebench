"""Host-only, checkout-relative paths; optional local configuration is never task input."""
from dataclasses import dataclass
import os
from pathlib import Path
import tomllib

ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Paths:
    private: Path
    rom: Path
    core: Path
    data: Path

    @property
    def fixtures(self):
        return self.private / 'fixtures'

    @property
    def saves(self):
        return self.private / 'teams'


def runtime_paths(root=ROOT):
    root = Path(root).resolve()
    config = Path(os.environ.get('POKEBENCH_CONFIG', root / 'config/local.toml'))
    values = tomllib.loads(config.read_text()).get('paths', {}) if config.is_file() else {}
    unknown = set(values) - {'private', 'rom', 'core', 'data'}
    if unknown:
        raise ValueError('Unknown local path settings: ' + ', '.join(sorted(unknown)))
    def resolve(value):
        path = Path(value).expanduser()
        return (root / path).resolve() if not path.is_absolute() else path.resolve()
    private = resolve(values.get('private', 'assets/private'))
    return Paths(private=private,
                 rom=resolve(values.get('rom', str(private / 'diamond.nds'))),
                 core=resolve(values.get('core', 'vendor/melonds-ds/build/src/libretro/melondsds_libretro.dylib')),
                 data=resolve(values.get('data', 'data')))
