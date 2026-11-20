"""Selection and accounting startup checks without model inference."""
from decimal import Decimal
from pathlib import Path
import pytest

from cynthiabench import agent_config, native_settings
from cynthiabench.deepseek_proxy import new_study_ledger


def test_only_selected_provider_is_pinned(monkeypatch):
    called = []
    monkeypatch.setattr(agent_config, 'cli_identity', lambda provider:
        called.append(provider) or {'provider': provider, 'executable': '/synthetic/' + provider})
    monkeypatch.setattr(agent_config, 'calculation_python_identity', lambda path: {'executable': path})
    for aliases, expected in [(['sol'], ['codex']), (['neon', 'carbon'], ['agy']), (['flash'], ['opencode'])]:
        called.clear()
        result = agent_config.effective_agent_configs(aliases)
        assert called == expected
        assert len(result) == len(aliases)
    assert len(agent_config.ALL_MODELS) == 6
    with pytest.raises(ValueError):
        agent_config.effective_agent_configs(['unknown'])


def test_local_executable_override_is_checkout_relative(tmp_path, monkeypatch):
    config = tmp_path / 'local.toml'
    config.write_text('[native]\nopencode_executable="tools/opencode"\nproxy_admin_config="work/proxy.json"\n')
    monkeypatch.setenv('POKEBENCH_CONFIG', str(config))
    assert native_settings.executable('opencode', tmp_path) == tmp_path / 'tools/opencode'
    assert native_settings.proxy_admin_config(tmp_path) == tmp_path / 'work/proxy.json'


def test_new_study_accounting_never_overwrites_existing_state(tmp_path):
    root = tmp_path / 'provider'
    ledger = new_study_ledger(root)
    assert ledger.known_metered == ledger.unknown_upper == ledger.committed_upper == Decimal(0)
    assert not ledger.development_only
    assert not Path(ledger.path).stat().st_mode & 0o222
    with pytest.raises(FileExistsError):
        new_study_ledger(root)
    root.mkdir()
    with pytest.raises(ValueError, match='unused'):
        new_study_ledger(root)


@pytest.mark.parametrize(('outcome', 'reason', 'expected'), [
    ('win', 'game_terminal', 100), ('loss', 'game_terminal', 0),
    ('timeout', 'action_limit', None), ('timeout', 'emulated_frame_limit', None),
    ('timeout', 'agent_stopped', None), ('draw', 'game_terminal', None),
])
def test_worker_scores_only_a_completed_game(tmp_path, outcome, reason, expected):
    from types import SimpleNamespace
    import time
    from cynthiabench.worker import BattleSession
    session = object.__new__(BattleSession)
    session.result_value = None
    session.e = SimpleNamespace(halted=False, frame=30, png=lambda:b'png', ram=lambda:b'ram')
    session.state = {'progress_percent':100 if outcome == 'win' else 40,
                     'opponent_knocked_out':6 if outcome == 'win' else 2, 'outcome_code':0}
    session.team='synthetic';session.seed=1;session.trial='synthetic'
    session.actions=3;session.last_error=None;session.manifest={'state_sha256':'synthetic'}
    session.started=time.time();session.output=tmp_path
    session._event=lambda *args:None;session._persist_result=lambda:None
    session._finish(outcome,True,reason)
    assert session.result_value['score'] == expected
    if expected is None:
        assert session.result_value['win'] is None
