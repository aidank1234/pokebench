"""Filesystem allowlist for a fresh, local OpenCode benchmark episode.

The profile is inherited by command tools. Only the episode workspace is
writable; historical benchmark trees, native histories and peer episodes are
absent from the read allowlist. This does not claim to isolate public web data,
attest a remote model, or grant access to native login stores.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
from .native_settings import calculation_python, executable


CALCULATION_PYTHON = calculation_python()
DEFAULT_CLIENT = executable('opencode')


def _quoted(path: Path | str) -> str:
    return json.dumps(str(path))


def _homebrew_runtime_paths() -> list[Path]:
    """Allow installed formula runtimes, never the whole Homebrew prefix."""
    result = []
    cellar = Path('/opt/homebrew/Cellar')
    for name in ('python@3.13', 'openssl@3', 'openssl@4', 'ca-certificates',
                 'readline', 'sqlite', 'xz', 'mpdecimal', 'bzip2', 'libffi'):
        opt = Path('/opt/homebrew/opt') / name
        if opt.exists():
            resolved = opt.resolve()
            if not resolved.is_relative_to(cellar):
                raise ValueError(f'Unexpected Homebrew runtime location: {name}')
            result.extend((opt, resolved))
    return result


def _base_sandbox_profile(workspace: Path, battle_port: int, *,
                           executable: Path = DEFAULT_CLIENT,
                           calculation_python: Path = CALCULATION_PYTHON) -> str:
    """Return a deny-by-default macOS Seatbelt profile.

    Account authentication is performed by the host proxy. Native login stores,
    native authentication services, and internal listeners are not granted.
    """
    if type(battle_port) is not int or not 1 <= battle_port <= 65535:
        raise ValueError('Battle port must be an integer in 1..65535')
    workspace = Path(workspace).resolve()
    if not workspace.is_dir() or workspace in (Path('/'), Path.home().resolve()):
        raise ValueError('An existing dedicated episode workspace is required')
    client_alias = Path(executable).absolute()
    python_alias = Path(calculation_python).absolute()
    executable = client_alias.resolve(strict=True)
    calculation_python = python_alias.resolve(strict=True)
    if not calculation_python.is_relative_to(Path('/opt/homebrew/Cellar/python@3.13')):
        raise ValueError('Calculation Python must use the separate Homebrew runtime')
    python_app = calculation_python.parent.parent / 'Resources/Python.app/Contents/MacOS/Python'
    if not python_app.is_file():
        raise ValueError('Expected Homebrew Python application executable is missing')

    lines = ['(version 1)', '(deny default)',
             '(allow process-fork)', '(allow sysctl-read)',
             '(allow process-info* (target self))',
             '(deny process-info* (require-not (target self)))',
             '(allow signal (target same-sandbox))',
             '(deny signal (require-not (target same-sandbox)))',
             '(deny mach-priv-task-port (require-not (target same-sandbox)))']
    # Frameworks and OS command-line programs are read-only. Private user data,
    # application bundles, /usr/local, user startup files and login stores are
    # deliberately absent. Canonicalization prevents a workspace symlink escape.
    readonly = [Path(p) for p in ('/System', '/usr/bin', '/usr/sbin', '/usr/lib',
                                  '/usr/share', '/bin', '/sbin')]
    readonly += _homebrew_runtime_paths()
    for path in readonly:
        lines.append(f'(allow file-read* (subpath {_quoted(path)}))')
        lines.append(f'(allow file-map-executable (subpath {_quoted(path)}))')
    # New macOS dyld/ignition opens the root directory to inspect its xattrs.
    # This literal permits root entries only, not the files beneath them.
    lines.append('(allow file-read* (literal "/") (literal "/var") '
                 '(literal "/etc") (literal "/private/etc"))')
    for path in (client_alias, python_alias, executable, calculation_python, python_app):
        lines.append(f'(allow file-read* (literal {_quoted(path)}))')
        lines.append(f'(allow file-map-executable (literal {_quoted(path)}))')
    # Homebrew symlink and TLS configuration metadata is needed for imports;
    # no site-packages or arbitrary /opt/homebrew/lib content is exposed.
    for path in ('/opt/homebrew', '/opt/homebrew/bin', '/opt/homebrew/opt',
                 '/opt/homebrew/Cellar', '/opt/homebrew/lib',
                 '/opt/homebrew/lib/python3.13'):
        lines.append(f'(allow file-read-metadata (literal {_quoted(path)}))')
    for path in ('/etc/hosts', '/etc/resolv.conf',
                 '/private/etc/hosts', '/private/etc/resolv.conf',
                 '/private/etc/localtime', '/private/etc/passwd',
                 '/private/etc/group', '/private/etc/services',
                 '/private/etc/protocols', '/private/etc/ssl/cert.pem',
                 '/opt/homebrew/etc/ca-certificates/cert.pem',
                 '/opt/homebrew/etc/openssl@3/cert.pem',
                 '/opt/homebrew/etc/openssl@4/cert.pem'):
        lines.append(f'(allow file-read* (literal {_quoted(path)}) '
                     f'(literal {_quoted(Path(path).resolve())}))')
    # Native libraries may stat ancestors, without listing their contents.
    ancestors = set(workspace.parents)
    for path in [*readonly, client_alias, python_alias, executable, calculation_python, python_app]:
        ancestors.update(path.parents)
    for parent in sorted(ancestors):
        lines.append(f'(allow file-read-metadata (literal {_quoted(parent)}))')
    lines += [f'(allow file-read* file-write* file-map-executable (subpath {_quoted(workspace)}))',
              '(allow file-read* file-write* (literal "/dev/null") (literal "/dev/tty") '
              '(literal "/dev/zero") (literal "/dev/random") (literal "/dev/urandom"))']
    # Match Apple's application.sb PTY capability pattern. Opening ptmx issues
    # a sandbox extension for that new slave; unrelated host terminals do not
    # satisfy this filter and remain inaccessible to the agent's commands.
    lines += ['(allow pseudo-tty)',
              '(allow file-read* file-write* file-ioctl (literal "/dev/ptmx"))',
              '(allow file-read* file-write* file-ioctl (require-all '
              '(regex #"^/dev/ttys[0-9]+$") (extension "com.apple.sandbox.pty")))']
    # A script is an executable too. Its interpreter must independently satisfy
    # this allowlist, and inherited sandbox rules still constrain all its work.
    lines += [f'(allow process-exec (subpath {_quoted(workspace)}) '
              '(subpath "/usr/bin") (subpath "/usr/sbin") (subpath "/bin") '
              f'(subpath "/sbin") (literal {_quoted(executable)}) '
              f'(literal {_quoted(client_alias)}) (literal {_quoted(python_alias)}) '
              f'(literal {_quoted(calculation_python)}) (literal {_quoted(python_app)}))',
              '(deny process-exec (literal "/usr/bin/osascript") '
              '(literal "/usr/bin/open"))']
    lines.append('(deny process-exec (literal "/usr/bin/security"))')
    rg = shutil.which('rg')
    if rg:
        rg_path = Path(rg).resolve()
        lines += [f'(allow file-read* (literal {_quoted(rg_path)}))',
                  f'(allow process-exec (literal {_quoted(rg_path)}))']
    # No inbound listeners, arbitrary Unix sockets, AppleEvents, launch jobs,
    # host process inspection or foreign task ports receive permission.
    # Public HTTP(S) is legal benchmark research. Localhost remains restricted
    # even when a local service happens to use port 80 or 443.
    lines += ['(allow network-outbound (require-all '
              '(require-any (remote tcp "*:80") (remote tcp "*:443")) '
              '(require-not (remote ip "localhost:*"))))',
              f'(allow network-outbound (remote tcp "localhost:{battle_port}"))',
              '(allow network-outbound (remote udp "*:53") (remote tcp "*:53"))',
              '(allow network-outbound (remote unix-socket '
              '(literal "/private/var/run/mDNSResponder")))',
              '(allow mach-lookup (global-name "com.apple.system.logger") '
              '(global-name "com.apple.system.notification_center") '
              '(global-name "com.apple.SystemConfiguration.configd"))']
    return '\n'.join(lines) + '\n'


def strict_sandbox_profile(workspace: Path, battle_port: int, *,
                           executable: Path = DEFAULT_CLIENT,
                           calculation_python: Path = CALCULATION_PYTHON,
                           proxy_port: int) -> str:
    """Allow this trial's native tools, battle service, and scoped model proxy."""
    if type(proxy_port) is not int or not 1 <= proxy_port <= 65535 or proxy_port == battle_port:
        raise ValueError('Proxy port must be explicit and differ from the battle port')
    profile = _base_sandbox_profile(workspace, battle_port, executable=executable,
                                    calculation_python=calculation_python)
    return profile + f'(allow network-outbound (remote tcp "localhost:{proxy_port}"))\n'


def isolated_agent_env(workspace: Path, token: str, *, max_output_tokens: int = 393216) -> dict[str, str]:
    """Redirect every native state root without changing the user's HOME.

    Only a per-trial proxy token reaches native tools. The DeepSeek account key
    and proxy administrator token remain outside the process and its workspace.
    OPENCODE_TEST_HOME is the pinned client's own path resolver override, not an
    operating-system HOME override; its source is included in preflight notes.
    """
    if type(max_output_tokens) is not int or max_output_tokens != 393216:
        raise ValueError('This separate experiment pins the native output limit to 393216')
    workspace = Path(workspace).resolve()
    state = workspace / '.opencode-state'
    temp = workspace / '.tmp'
    for path in (temp, state / 'config/opencode', state / 'cache', state / 'data',
                 state / 'state', state / 'home', state / 'managed'):
        path.mkdir(parents=True, exist_ok=True)
    env = {k: os.environ[k] for k in ('HOME', 'USER', 'LOGNAME', 'LANG', 'LC_ALL') if k in os.environ}
    env.update(PATH='/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin',
        SHELL='/bin/zsh', TERM='dumb', NO_COLOR='1', ZDOTDIR=str(state),
        TMPDIR=str(temp), TMP=str(temp), TEMP=str(temp), TMPPREFIX=str(temp / 'zsh'),
        XDG_CONFIG_HOME=str(state / 'config'), XDG_CACHE_HOME=str(state / 'cache'),
        XDG_DATA_HOME=str(state / 'data'), XDG_STATE_HOME=str(state / 'state'),
        OPENCODE_CONFIG_DIR=str(state / 'config/opencode'),
        OPENCODE_CONFIG=str(state / 'config/opencode/opencode.json'),
        OPENCODE_DB=str(state / 'data/opencode/opencode.db'),
        OPENCODE_TEST_HOME=str(state / 'home'), OPENCODE_TEST_MANAGED_CONFIG_DIR=str(state / 'managed'),
        OPENCODE_DISABLE_AUTOUPDATE='1', OPENCODE_DISABLE_MODELS_FETCH='1',
        OPENCODE_DISABLE_PROJECT_CONFIG='1', OPENCODE_DISABLE_CLAUDE_CODE='1',
        OPENCODE_DISABLE_EXTERNAL_SKILLS='1', OPENCODE_DISABLE_DEFAULT_PLUGINS='1',
        OPENCODE_PURE='1', OPENCODE_DISABLE_LSP_DOWNLOAD='1',
        OPENCODE_EXPERIMENTAL_DISABLE_FILEWATCHER='1',
        OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX=str(max_output_tokens),
        PYTHONNOUSERSITE='1', PYTHONDONTWRITEBYTECODE='1',
        CYNTHIA_PYTHON=str(CALCULATION_PYTHON), DEEPSEEK_API_KEY=token)
    return env
