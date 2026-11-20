"""Executable identity checks run only local fake --version programs."""
from pathlib import Path
import pytest

from cynthiabench.native_runtime import cli_identity, validate_cli_pin
from cynthiabench.native_agent import command, resolve_model, clean_env


def executable(path, version):
    path.write_text('#!/bin/sh\nprintf "%s\\n" "' + version + '"\n')
    path.chmod(0o755)
    return path


def test_pinned_resolved_path_survives_symlink_repoint_but_rejects_byte_change(tmp_path):
    old = executable(tmp_path / 'old', 'version-one')
    new = executable(tmp_path / 'new', 'version-two')
    link = tmp_path / 'codex';link.symlink_to(old)
    pin = cli_identity('codex', link)
    assert pin['executable'] == str(old)
    link.unlink();link.symlink_to(new)
    assert validate_cli_pin(pin) == pin
    args = command(resolve_model('sol'), tmp_path, executable=pin['executable'])
    assert args[0] == str(old)
    executable(old, 'version-changed')
    with pytest.raises(RuntimeError, match='frozen path/hash/version'):
        validate_cli_pin(pin)


def test_frozen_config_rejects_changed_executable_identity(tmp_path, monkeypatch):
    from cynthiabench.provenance import validate_freeze
    from cynthiabench import native_agent
    import json
    import sys
    # This check concerns frozen configuration and environment flags, not the
    # macOS interpreter selector, which has its own native execution tests.
    monkeypatch.setattr(native_agent, 'selected_calculation_python', lambda: sys.executable)
    original = {'import_path': 'native', 'kwargs': {'cli_pin': {'sha256': 'old'}}}
    frozen = {'source_sha256': {'source': 'hash'}, 'task_sha256': {'task': 'hash'},
              'agent_configs': {'model': original}, 'execution': {'n_concurrent_trials': 6}}
    path = tmp_path / 'freeze.json';path.write_text(json.dumps(frozen))
    actual = {**frozen, 'agent_configs': {'model': {'import_path': 'native', 'kwargs': {'cli_pin': {'sha256': 'new'}}}}}
    with pytest.raises(ValueError, match='frozen agent configuration'):
        validate_freeze(actual, path)
    assert clean_env(tmp_path)['DISABLE_AUTOUPDATER'] == '1'


def test_zsh_heredoc_works_without_opening_private_sibling(tmp_path):
    """Ordinary shell calculations must work inside the same private boundary."""
    import subprocess
    import tempfile
    from cynthiabench.native_agent import ROOT, sandbox_profile

    with tempfile.TemporaryDirectory(prefix='temp-prefix-test-', dir=ROOT / 'work') as directory:
        base = Path(directory)
        workspace = base / 'workspace'
        workspace.mkdir()
        private = base / 'private-canary.txt'
        private.write_text('private-grader-sentinel')
        public = workspace / 'public.txt'
        public.write_text('public-team-sentinel')
        profile = tmp_path / 'heredoc.sb'
        profile.write_text(sandbox_profile(workspace))
        program = (
            'from pathlib import Path\n'
            'assert Path("public.txt").read_text() == "public-team-sentinel"\n'
            f'p = Path({str(private)!r})\n'
            'for action in (lambda: p.read_text(), lambda: p.write_text("tamper")):\n'
            '    try: action()\n'
            '    except PermissionError: pass\n'
            '    else: raise AssertionError("private sibling accessible")\n'
            'Path(".tmp/calculation.txt").write_text(str(6 * 7))\n'
            'print("heredoc-ok")\n'
        )
        process = subprocess.run(
            ['/usr/bin/sandbox-exec', '-f', str(profile), '/bin/zsh', '-lc',
             "\"$CYNTHIA_PYTHON\" - <<'PY'\n" + program + 'PY\n'],
            cwd=workspace, env=clean_env(workspace), capture_output=True, text=True, timeout=30)
        assert process.returncode == 0, process.stderr
        assert process.stdout.strip() == 'heredoc-ok'
        assert (workspace / '.tmp/calculation.txt').read_text() == '42'
        assert private.read_text() == 'private-grader-sentinel'


def test_direct_python_json_pipeline_is_quiet_and_stays_isolated(tmp_path):
    import os
    import subprocess
    import tempfile
    from cynthiabench.native_agent import ROOT, sandbox_profile
    from cynthiabench.native_runtime import calculation_python_identity

    with tempfile.TemporaryDirectory(prefix='direct-python-test-', dir=ROOT / 'work') as directory:
        base = Path(directory)
        workspace = base / 'workspace'
        workspace.mkdir()
        private = base / 'private-canary.txt'
        private.write_text('private-grader-sentinel')
        profile = tmp_path / 'direct-python.sb'
        profile.write_text(sandbox_profile(workspace))
        env = clean_env(workspace)
        identity = calculation_python_identity(env['CYNTHIA_PYTHON'])
        assert identity['executable'] == env['CYNTHIA_PYTHON']
        assert len(identity['sha256']) == 64 and identity['version'].startswith('Python ')
        assert env.get('PATH') == os.environ.get('PATH')
        program = (
            'import json\nfrom pathlib import Path\n'
            f'p = Path({str(private)!r})\n'
            'for action in (lambda: p.read_text(), lambda: p.write_text("tamper")):\n'
            '    try: action()\n'
            '    except PermissionError: pass\n'
            '    else: raise AssertionError("private sibling accessible")\n'
            'print(json.dumps({"answer": 6 * 7}))\n'
        )
        # Merging stderr reproduced the real pilot failure with Apple's shim.
        command = ('"$CYNTHIA_PYTHON" - <<\'PY\' 2>&1 | "$CYNTHIA_PYTHON" -c '
                   "'import json,sys; assert json.load(sys.stdin)[\"answer\"]==42; print(\"pipeline-ok\")'\n"
                   + program + 'PY\n')
        process = subprocess.run(
            ['/usr/bin/sandbox-exec', '-f', str(profile), '/bin/zsh', '-lc', command],
            cwd=workspace, env=env, capture_output=True, text=True, timeout=30)
        assert process.returncode == 0, process.stderr
        assert process.stdout.strip() == 'pipeline-ok'
        assert process.stderr == ''
        assert private.read_text() == 'private-grader-sentinel'
