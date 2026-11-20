"""Host-only native CLI settings from ignored local configuration."""
import os
from pathlib import Path
import shutil
import tomllib

ROOT = Path(__file__).resolve().parents[2]


def settings(root=ROOT):
    root = Path(root).resolve()
    path = Path(os.environ.get('POKEBENCH_CONFIG', root / 'config/local.toml'))
    values = tomllib.loads(path.read_text()).get('native', {}) if path.is_file() else {}
    known = {'codex_executable', 'claude_executable', 'agy_executable',
             'opencode_executable', 'calculation_python', 'proxy_admin_config'}
    if not isinstance(values, dict) or set(values) - known:
        raise ValueError('Unknown native runtime configuration')
    if any(not isinstance(value, str) or not value for value in values.values()):
        raise ValueError('Native runtime settings must be nonempty strings')
    return values


def executable(provider, root=ROOT):
    """Use an explicit local setting or the selected command on PATH."""
    value = settings(root).get(provider + '_executable', provider)
    if '/' not in value:
        return Path(shutil.which(value) or value)
    path = Path(value).expanduser()
    return path if path.is_absolute() else Path(root) / path


def calculation_python(root=ROOT):
    value = settings(root).get('calculation_python')
    if value:
        path = Path(value).expanduser()
        return path if path.is_absolute() else Path(root) / path
    return Path(shutil.which('python3.13') or '/opt/homebrew/bin/python3.13')


def proxy_admin_config(root=ROOT):
    path = Path(settings(root).get('proxy_admin_config', 'work/deepseek-proxy-admin.json')).expanduser()
    return path if path.is_absolute() else Path(root) / path
