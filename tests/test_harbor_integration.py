"""Contract and actual-Harbor lifecycle tests; the HTTP engine here is a fixture."""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from harbor.job import Job
from harbor.models.job.config import JobConfig, RetryConfig
from harbor.models.trial.config import AgentConfig, EnvironmentConfig, TaskConfig, VerifierConfig

from cynthiabench.harbor_env import validate_battle_result
from cynthiabench.native_agent import ROOT, clean_env, sandbox_profile


def test_verifier_requires_host_terminal_evidence():
    valid = {'session_id': 's', 'valid': True, 'terminal': True, 'outcome': 'win', 'progress_score': 100}
    assert validate_battle_result(valid, 's') == {'win': 1, 'score': 100, 'progress': 100.0}
    for modification in ({'session_id': 'other'}, {'terminal': False}, {'valid': False},
                         {'outcome': 'ongoing'}, {'progress_score': 99}, {'progress_score': float('nan')}):
        with pytest.raises(ValueError):
            validate_battle_result({**valid, **modification}, 's')
    assert validate_battle_result({**valid, 'outcome': 'loss', 'progress_score': 78.5}, 's')['score'] == 0
    for outcome in ('timeout', 'draw', 'ongoing'):
        with pytest.raises(ValueError, match='pending'):
            validate_battle_result({**valid, 'outcome': outcome, 'progress_score': 40}, 's')


@pytest.mark.parametrize('outcome', ['win', 'loss'])
@pytest.mark.parametrize('contradiction', [
    {'reason':'action_limit'}, {'termination_reason':'timeout'},
    {'evidence':{'game_outcome_code':0}}, {'evidence':{'game_outcome_code':True}},
    {'evidence':{'terminal_state':{'terminal_confirmed':False}}},
    {'evidence':{'terminal_state':{'terminal_confirmed':1}}},
    {'evidence':{'terminal_state':{'outcome_code':0}}},
    {'evidence':{'terminal_state':{'outcome_code':True}}},
    {'evidence':{'terminal_state':{'outcome':'ongoing'}}},
    {'evidence':None}, {'evidence':{'terminal_state':None}},
])
def test_declared_win_loss_cannot_override_contradictory_host_evidence(outcome, contradiction):
    code = 1 if outcome == 'win' else 2
    valid = {'session_id':'s','valid':True,'terminal':True,'outcome':outcome,
             'progress_score':100 if outcome == 'win' else 50,'reason':'game_terminal',
             'evidence':{'game_outcome_code':code,'terminal_state':{
                 'terminal_confirmed':True,'outcome_code':code,'outcome':outcome}}}
    assert validate_battle_result(valid,'s')['win'] == (outcome == 'win')
    with pytest.raises(ValueError):
        validate_battle_result({**valid,**contradiction},'s')


@pytest.mark.parametrize(('outcome','code'), [('win',2),('loss',1)])
def test_game_code_must_match_the_declared_winner(outcome, code):
    with pytest.raises(ValueError, match='outcome code'):
        validate_battle_result({'session_id':'s','valid':True,'terminal':True,
            'outcome':outcome,'progress_score':100,'evidence':{'game_outcome_code':code}},'s')


def test_agent_cannot_read_or_write_private_benchmark(tmp_path):
    workspace = ROOT / 'work/isolation-test' / uuid.uuid4().hex
    workspace.mkdir(parents=True)
    private = workspace.parent / (workspace.name + '-private.txt')
    private.write_text('private-grader-sentinel')
    public = workspace / 'public.txt'
    public.write_text('public-team-sentinel')
    profile = tmp_path / 'profile.sb'
    profile.write_text(sandbox_profile(workspace))
    command = ['/usr/bin/sandbox-exec', '-f', str(profile), '/usr/bin/python3', '-c',
        'from pathlib import Path\n'
        f'p=Path({str(private)!r})\n'
        f'assert Path({str(public)!r}).read_text()=="public-team-sentinel"\n'
        'for action in (lambda:p.read_text(),lambda:p.write_text("tamper")):\n'
        ' try: action()\n'
        ' except PermissionError: pass\n'
        ' else: raise AssertionError("private data accessible")\n'
        f'Path({str(workspace / "result.txt")!r}).write_text("isolated")\n']
    process = subprocess.run(command, cwd=workspace, env=clean_env(workspace), capture_output=True, text=True)
    assert process.returncode == 0, process.stderr
    assert private.read_text() == 'private-grader-sentinel'


@pytest.mark.skipif(sys.platform != 'darwin', reason='Real environment execution requires macOS Seatbelt')
def test_real_harbor_job_uses_custom_environment_and_verifier(tmp_path):
    run_harbor_fixture(tmp_path)


def run_harbor_fixture(tmp_path, configured_agents=None):
    session_id = 'mock-' + uuid.uuid4().hex
    state = {'completed': False, 'closed': False, 'created': False}
    admin_token = uuid.uuid4().hex
    agent_token = uuid.uuid4().hex

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.handle_request()

        def do_POST(self):
            self.handle_request()

        def handle_request(self):
            token = self.headers.get('Authorization', '').removeprefix('Bearer ')
            if self.path.startswith('/admin/') and token != admin_token:
                self.send_error(403)
                return
            if self.path.startswith('/v1/') and token != agent_token:
                self.send_error(403)
                return
            if self.path == '/admin/sessions':
                state['created'] = True
                data = {'session_id': session_id, 'agent_token': agent_token}
            elif self.path == '/v1/observe':
                data = {'frame': 1, 'mock': True}
            elif self.path == '/v1/action':
                state['completed'] = True
                data = {'frame': 2, 'mock': True, 'terminal': True, 'outcome': 'win'}
            elif self.path.endswith('/finalize'):
                data = {'finalized': True}
            elif self.path.endswith('/result'):
                data = {'session_id': session_id, 'valid': True, 'terminal': state['completed'],
                        'outcome': 'win' if state['completed'] else 'ongoing', 'progress_score': 100}
            elif self.path.endswith('/close'):
                state['closed'] = True
                data = {'closed': True}
            else:
                self.send_error(404)
                return
            raw = json.dumps(data).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        admin_path = tmp_path / 'admin.json'
        admin_path.write_text(json.dumps({'url': f'http://127.0.0.1:{server.server_port}', 'admin_token': admin_token}))
        task = tmp_path / 'fixture'
        (task / 'environment').mkdir(parents=True)
        (task / 'tests').mkdir()
        (task / 'instruction.md').write_text('This is an infrastructure smoke check, not a real Pokemon battle. Read CONTROLS.md, run python3 battle.py observe, then run python3 battle.py press A --frames 1. The mock reports terminal win. Stop and report that this was only a mock infrastructure check.')
        (task / 'task.toml').write_text('schema_version="1.4"\n[agent]\ntimeout_sec=180\n[verifier]\ntimeout_sec=30\n')
        (task / 'tests/test.sh').write_text('exit 1\n')
        (task / 'environment/scenario.json').write_text(json.dumps({'team_id': 'mock', 'seed': 42}))
        if configured_agents is not None:
            agents = configured_agents
        else:
            agents = [AgentConfig(import_path='cynthiabench.harbor_smoke:FixtureAgent')]
        config = JobConfig(job_name='harbor-smoke', jobs_dir=tmp_path / 'jobs', n_concurrent_trials=1,
            retry=RetryConfig(max_retries=0),
            agents=agents,
            environment=EnvironmentConfig(import_path='cynthiabench.harbor_env:CynthiaEnvironment',
                                         kwargs={'admin_config_path': str(admin_path)}),
            verifier=VerifierConfig(import_path='cynthiabench.harbor_env:CynthiaVerifier'),
            tasks=[TaskConfig(path=task)])

        async def run():
            job = await Job.create(config)
            return await job.run()

        result = asyncio.run(run())
        assert state == {'completed': True, 'closed': True, 'created': True}
        trial_results = list((tmp_path / 'jobs/harbor-smoke').glob('*/result.json'))
        if not trial_results:
            trial_results = list((tmp_path / 'jobs/harbor-smoke').glob('*/results.json'))
        assert len(trial_results) == len(agents)
        for path in trial_results:
            trial_result = json.loads(path.read_text())
            assert trial_result['verifier_result']['rewards']['score'] == 100
            assert not trial_result.get('exception_info')
    finally:
        server.shutdown()
        server.server_close()
