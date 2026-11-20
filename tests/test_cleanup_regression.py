"""Public-distribution fixes; no game assets, provider calls, or real signals."""
import io
import json
from pathlib import Path
import subprocess
import threading
from unittest.mock import Mock

import pytest
from cynthiabench.server import WorkerSession, SessionManager, write_json


def session(tmp_path):
    s = WorkerSession.__new__(WorkerSession)
    s.session_id, s.team_id, s.seed, s.trial_id = 'session', 'team', 17, 'trial'
    s.output, s.lock = tmp_path, threading.RLock()
    s.closed = s.failed = False
    s.command_count = 0
    s.stderr = io.BytesIO()
    s.process = Mock(pid=987654321, stdin=io.BytesIO(), stdout=io.BytesIO())
    s.last_result = {'valid': True, 'terminal': True, 'outcome': 'loss', 'team_id': 'team',
                     'seed': 17, 'trial_id': 'trial', 'recording': {'frames': 10}}
    write_json(tmp_path / 'bridge-result.json', s.last_result)
    return s


def test_missing_process_group_during_escalation_is_reaped(tmp_path, monkeypatch):
    s = session(tmp_path)
    s.process.poll.return_value = None
    s.process.wait.side_effect = [subprocess.TimeoutExpired('fake', 3), 0]
    kill = Mock(side_effect=[None, ProcessLookupError()])
    monkeypatch.setattr('cynthiabench.server.os.killpg', kill)
    assert s._terminate() is True
    assert s.process.wait.call_count == 2 and kill.call_count == 2


def test_dead_worker_close_cannot_erase_verified_loss(tmp_path, monkeypatch):
    s = session(tmp_path)
    before = (tmp_path / 'bridge-result.json').read_bytes()
    s.process.poll.return_value = 0
    s.process.stdin = Mock()
    s.process.stdin.write.side_effect = BrokenPipeError('already exited')
    assert s.close()['closed'] is True
    assert s.close()['closed'] is True
    assert (tmp_path / 'bridge-result.json').read_bytes() == before
    assert s.result()['outcome'] == 'loss'
    assert json.loads((tmp_path / 'bridge-errors.json').read_text())[0]['terminal_result_preserved']
    s.process.stdin.write.assert_called_once()


def test_live_failed_worker_still_occupies_admission_slot(tmp_path):
    manager = SessionManager(team_ids=['team'], output_root=tmp_path/'runs', max_sessions=1)
    s = session(tmp_path)
    s.failed = True
    s.process.poll.return_value = None
    manager.sessions['session'] = s
    assert manager.status()['active_sessions'] == 1
    with pytest.raises(Exception, match='Active session limit'):
        manager.create({'team_id':'team','seed':3,'trial_id':'new'})
    s.process.poll.return_value = 0
    assert manager.status()['active_sessions'] == 0
