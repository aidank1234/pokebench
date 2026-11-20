"""Real Harbor scheduling/cleanup with a fake agent and HTTP game worker.

These lifecycle tests never start a model client, emulator, or provider request.
"""
import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from harbor.job import Job
from harbor.models.job.config import JobConfig, RetryConfig
from harbor.models.trial.config import AgentConfig, EnvironmentConfig, TaskConfig, VerifierConfig

from cynthiabench.antigravity_agent import publish_quota_halt
from cynthiabench.framework_signals import install_framework_signals
from cynthiabench.quota_control import ProviderQuotaHalt, run_with_quota_halt


@pytest.fixture
def lifecycle(tmp_path, monkeypatch):
    import cynthiabench.harbor_env as environment_module
    from cynthiabench.harbor_smoke import FixtureAgent

    # Every generated sandbox/artifact lives inside pytest's temporary directory.
    monkeypatch.setattr(environment_module, 'ROOT', tmp_path / 'runtime')
    state = {'sessions': 0, 'agent_calls': 0, 'active_workers': 0,
             'closed': 0, 'reasons': [], 'terminal': False,
             'valid': False, 'outcome': 'ongoing'}

    async def pending_agent(self, instruction, environment, context):
        state['agent_calls'] += 1
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            environment.framework_cancellation_pending = True
            environment.finalize_reason = 'runtime_error'
            environment.agent_audit = {'cancelled': True, 'quota_blocked': False}
            environment.agent_context = context
            raise

    monkeypatch.setattr(FixtureAgent, 'run', pending_agent)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.handle_request()

        def do_POST(self):
            self.handle_request()

        def handle_request(self):
            payload = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))) or '{}')
            if self.path == '/admin/sessions':
                state['sessions'] += 1
                state['active_workers'] += 1
                data = {'session_id': 'fake-worker', 'agent_token': 'fake-agent'}
            elif self.path == '/v1/observe':
                data = {'frame': 1, 'public_state': {}, 'terminal': state['terminal']}
            elif self.path.endswith('/finalize'):
                state['reasons'].append(payload['reason'])
                # Mirrors host immutability: later infrastructure cleanup cannot
                # replace a real win/loss already latched by the game worker.
                if not (state['valid'] and state['terminal']):
                    state.update(valid=False, terminal=True, outcome='invalid')
                data = {'ok': True}
            elif self.path.endswith('/result'):
                data = {'session_id': 'fake-worker', 'valid': state['valid'],
                        'terminal': state['terminal'], 'outcome': state['outcome'],
                        'progress_score': 100 if state['outcome'] == 'win' else 0}
            elif self.path.endswith('/close'):
                state['closed'] += 1
                state['active_workers'] -= 1
                data = {'closed': True}
            else:
                self.send_error(404)
                return
            raw = json.dumps(data).encode()
            self.send_response(200)
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    admin = tmp_path / 'admin.json'
    admin.write_text(json.dumps({'url': f'http://127.0.0.1:{server.server_port}',
                                 'admin_token': 'fake-admin'}))
    halt = tmp_path / 'provider-halt.json'
    tasks = []
    for index in range(2):
        task = tmp_path / f'fixture-{index}'
        (task / 'environment').mkdir(parents=True)
        (task / 'tests').mkdir()
        (task / 'instruction.md').write_text('Fake quota lifecycle only.')
        (task / 'task.toml').write_text('schema_version="1.4"\n[agent]\ntimeout_sec=30\n[verifier]\ntimeout_sec=5\n')
        (task / 'tests/test.sh').write_text('exit 1\n')
        (task / 'environment/scenario.json').write_text('{"team_id":"fake","seed":1}')
        tasks.append(TaskConfig(path=task))
    config = JobConfig(job_name='quota-lifecycle', jobs_dir=tmp_path / 'jobs',
        n_concurrent_trials=1, retry=RetryConfig(max_retries=0),
        agents=[AgentConfig(import_path='cynthiabench.harbor_smoke:FixtureAgent')],
        environment=EnvironmentConfig(import_path='cynthiabench.harbor_env:CynthiaEnvironment',
            kwargs={'admin_config_path': str(admin), 'quota_halt_path': str(halt)}),
        verifier=VerifierConfig(import_path='cynthiabench.harbor_env:CynthiaVerifier'),
        tasks=tasks)
    try:
        yield state, config, halt, tmp_path
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize('guard', ['controller', 'environment'])
def test_real_harbor_existing_latch_prevents_workers_and_agent_calls(lifecycle, guard):
    state, config, halt, root = lifecycle
    halt.write_text('{"quota_blocked":true}')

    async def run():
        job = await Job.create(config)
        install_framework_signals(job)
        if guard == 'controller':
            with pytest.raises(ProviderQuotaHalt, match='before scheduling'):
                await run_with_quota_halt(job, halt, root / 'stop.json')
        else:
            # The environment guard must independently fail before worker or
            # workspace creation even without the outer controller wrapper.
            await job.run()

    asyncio.run(run())
    assert state['sessions'] == state['agent_calls'] == state['active_workers'] == 0
    assert state['reasons'] == [] and state['closed'] == 0
    assert not (root / 'runtime/work/harbor-sandboxes').exists()


@pytest.mark.parametrize('latched_outcome', ['ongoing', 'win', 'loss'])
def test_real_harbor_quota_owner_cancel_reaps_worker_and_preserves_games(lifecycle, latched_outcome):
    state, config, halt, root = lifecycle
    if latched_outcome != 'ongoing':
        state.update(valid=True, terminal=True, outcome=latched_outcome)

    async def run():
        job = await Job.create(config)
        install_framework_signals(job)

        async def block_provider():
            for _ in range(500):
                if state['agent_calls']:
                    break
                await asyncio.sleep(.01)
            assert state['agent_calls'] == 1
            publish_quota_halt(halt, {'quota_evidence': [
                {'source': 'fake lifecycle fixture only'}]}, root)

        writer = asyncio.create_task(block_provider())
        with pytest.raises(ProviderQuotaHalt, match='halted new scheduling'):
            await run_with_quota_halt(job, halt, root / 'stop.json', poll_seconds=.005)
        await writer

    asyncio.run(run())
    assert state['sessions'] == state['agent_calls'] == state['closed'] == 1
    assert state['active_workers'] == 0
    assert state['reasons'] and set(state['reasons']) == {'runtime_error'}
    assert state['outcome'] == ('invalid' if latched_outcome == 'ongoing' else latched_outcome)
    assert state['valid'] is (latched_outcome != 'ongoing')
    assert json.loads((root / 'stop.json').read_text())['reason'] == 'provider_quota_halt'
    audits = list((root / 'jobs/quota-lifecycle').glob('*/agent/runtime-audit.json'))
    assert len(audits) == 1
    audit = json.loads(audits[0].read_text())
    assert audit['framework_exception_type'] == 'CancelledError'
    assert audit['cancellation_classification'] == 'runtime_error'
    results = [json.loads(p.read_text()) for p in (root / 'jobs/quota-lifecycle').glob('*/result.json')]
    cancelled = [r for r in results if (r.get('exception_info') or {}).get('exception_type') == 'CancelledError']
    assert len(cancelled) == 1
