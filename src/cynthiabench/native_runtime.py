"""Pin the actual native CLI executable without copying or modifying vendor apps."""
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
from .native_settings import executable as configured_executable


def cli_identity(provider, executable=None):
    if provider not in ('codex', 'claude', 'agy', 'opencode'):
        raise ValueError('Unsupported native CLI provider')
    path = Path(executable or configured_executable(provider)).resolve(strict=True)
    if not path.is_file() or not os.access(path, os.X_OK):
        raise RuntimeError('Native CLI executable is unavailable: ' + str(path))
    before = path.stat()
    with path.open('rb') as binary:
        digest = hashlib.file_digest(binary, 'sha256').hexdigest()
    env = dict(os.environ, DISABLE_AUTOUPDATER='1', CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC='1',
               AGY_CLI_DISABLE_AUTO_UPDATE='true', OPENCODE_DISABLE_AUTOUPDATE='true',
               OPENCODE_DISABLE_MODELS_FETCH='true')
    version = subprocess.check_output([str(path), '--version'], text=True, env=env, timeout=30).strip()
    after = path.stat()
    if (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
        raise RuntimeError('Native CLI changed while its identity was being captured')
    return {'provider': provider, 'executable': str(path), 'sha256': digest,
            'size_bytes': after.st_size, 'version': version}


def validate_cli_pin(expected):
    """Execute the frozen resolved path, even if a vendor later repoints its symlink."""
    actual = cli_identity(expected['provider'], expected['executable'])
    if actual != expected:
        raise RuntimeError('Native CLI executable differs from its frozen path/hash/version pin')
    return actual


def selected_calculation_python():
    """Resolve Apple's selected interpreter without running its /usr/bin shim."""
    selected = subprocess.check_output(
        ['/usr/bin/xcrun', '--find', 'python3'], text=True, timeout=30).strip()
    path = Path(selected).resolve(strict=True)
    if not path.is_file() or not os.access(path, os.X_OK):
        raise RuntimeError('Selected calculation interpreter is unavailable')
    return str(path)


def calculation_python_identity(executable=None):
    path = Path(executable or selected_calculation_python()).resolve(strict=True)
    with path.open('rb') as binary:
        digest = hashlib.file_digest(binary, 'sha256').hexdigest()
    version = subprocess.check_output([str(path), '--version'], text=True, timeout=30).strip()
    return {'executable': str(path), 'sha256': digest, 'version': version,
            'selection': ('Explicit existing interpreter' if executable else
                          'Direct existing xcrun-selected Python; no PATH or sandbox-permission change')}
