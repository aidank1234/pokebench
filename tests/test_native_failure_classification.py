import asyncio
import json
from pathlib import Path
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from harbor.job import Job
from harbor.models.job.config import JobConfig, RetryConfig
from harbor.models.trial.config import AgentConfig, EnvironmentConfig, TaskConfig, VerifierConfig

from cynthiabench.native_agent import detect_quota
from cynthiabench.framework_signals import install_framework_signals


def test_quota_only_from_provider_errors_not_tool_prose_or_allowed_warnings(tmp_path):
    events = tmp_path / 'events';stderr = tmp_path / 'stderr';stderr.write_text('Warning: rate limit utilization 90%, requests allowed\n')
    benign = [
        {'type': 'item.completed', 'item': {'type': 'command_execution', 'aggregated_output': 'Webpage: usage_limit_reached; hit your limit'}},
        {'type': 'assistant', 'message': {'content': 'The article says usage limit reached.'}},
        {'type': 'rate_limit_event', 'rate_limit_info': {'status': 'allowed_warning', 'utilization': .9}},
        {'type': 'result', 'subtype': 'success', 'is_error': False, 'result': 'Quotation: insufficient_quota'}]
    events.write_text('\n'.join(map(json.dumps, benign)))
    assert not detect_quota(events, stderr)['quota_blocked']
    for failure in ({'type': 'turn.failed', 'error': {'code': 'usage_limit_reached', 'message': 'Try after reset'}},
                    {'type': 'error', 'message': 'You have exceeded your current quota'},
                    {'type': 'result', 'subtype': 'error_during_execution', 'is_error': True, 'errors': ["You've hit your limit"]}):
        events.write_text('\n'.join(map(json.dumps, [*benign, failure])))
        assert detect_quota(events, stderr)['quota_blocked']
    events.write_text('')
    stderr.write_text("You've hit your limit · resets tomorrow\n")
    assert detect_quota(events, stderr)['quota_blocked']
    stderr.write_text('Warning: "usage limit reached" appears in a downloaded page\n')
    assert not detect_quota(events, stderr)['quota_blocked']


@pytest.mark.parametrize('operator_cancel,already_terminal', [(False, False), (True, False), (True, True)])
def test_real_harbor_signal_distinguishes_deadline_and_operator_interrupt(tmp_path, monkeypatch, operator_cancel, already_terminal):
    from cynthiabench.harbor_smoke import FixtureAgent
    state = {'entered': False, 'closed': False, 'reasons': [], 'valid': already_terminal,
             'terminal': already_terminal, 'outcome': 'win' if already_terminal else 'ongoing'}

    async def wait_agent(self, instruction, environment, context):
        state['entered'] = True
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            environment.framework_cancellation_pending = True
            environment.finalize_reason = 'runtime_error'
            environment.agent_audit = {'cancelled': True}
            environment.agent_context = context
            raise

    monkeypatch.setattr(FixtureAgent, 'run', wait_agent)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):pass
        def do_GET(self):self.handle_request()
        def do_POST(self):self.handle_request()
        def handle_request(self):
            payload = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))) or '{}')
            if self.path == '/admin/sessions':
                data = {'session_id': 'mock-session', 'agent_token': 'mock-agent'}
            elif self.path == '/v1/observe':
                data = {'frame': 1, 'public_state': {}, 'terminal': state['terminal']}
            elif self.path.endswith('/finalize'):
                state['reasons'].append(payload['reason'])
                if not (state['valid'] and state['terminal']):
                    state.update(valid=payload['reason'] == 'timeout', terminal=True,
                                 outcome='timeout' if payload['reason'] == 'timeout' else 'invalid')
                data = {'ok': True}
            elif self.path.endswith('/result'):
                data = {'session_id': 'mock-session', 'valid': state['valid'], 'terminal': state['terminal'],
                        'outcome': state['outcome'], 'progress_score': 100 if already_terminal else 0}
            elif self.path.endswith('/close'):
                state['closed'] = True;data = {'closed': True}
            else:
                self.send_error(404);return
            raw = json.dumps(data).encode();self.send_response(200)
            self.send_header('Content-Length', str(len(raw)));self.end_headers();self.wfile.write(raw)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True);thread.start()
    try:
        admin = tmp_path / 'admin.json'
        admin.write_text(json.dumps({'url': f'http://127.0.0.1:{server.server_port}', 'admin_token': 'mock-admin'}))
        task = tmp_path / 'fixture';(task / 'environment').mkdir(parents=True);(task / 'tests').mkdir()
        (task / 'instruction.md').write_text('Mock lifecycle only.')
        (task / 'task.toml').write_text('schema_version="1.4"\n[agent]\ntimeout_sec=' + ('30' if operator_cancel else '0.05') + '\n[verifier]\ntimeout_sec=5\n')
        (task / 'tests/test.sh').write_text('exit 1\n')
        (task / 'environment/scenario.json').write_text('{"team_id":"mock","seed":1}')
        config = JobConfig(job_name='signals', jobs_dir=tmp_path / 'jobs', n_concurrent_trials=1,
            retry=RetryConfig(max_retries=0), agents=[AgentConfig(import_path='cynthiabench.harbor_smoke:FixtureAgent')],
            environment=EnvironmentConfig(import_path='cynthiabench.harbor_env:CynthiaEnvironment', kwargs={'admin_config_path': str(admin)}),
            verifier=VerifierConfig(import_path='cynthiabench.harbor_env:CynthiaVerifier'), tasks=[TaskConfig(path=task)])

        async def run():
            job = await Job.create(config);install_framework_signals(job)
            running = asyncio.create_task(job.run())
            if operator_cancel:
                for _ in range(500):
                    if state['entered']:break
                    await asyncio.sleep(.01)
                assert state['entered']
                running.cancel()
            try:
                await running
            except asyncio.CancelledError:
                assert operator_cancel

        asyncio.run(run())
        assert state['closed'] and state['reasons']
        expected = 'runtime_error' if operator_cancel else 'timeout'
        assert set(state['reasons']) == {expected}
        assert state['valid'] == (already_terminal or not operator_cancel)
        assert state['outcome'] == ('win' if already_terminal else ('invalid' if operator_cancel else 'timeout'))
        audits = list((tmp_path / 'jobs/signals').glob('*/agent/runtime-audit.json'))
        assert len(audits) == 1
        audit = json.loads(audits[0].read_text())
        assert audit['framework_exception_type'] == ('CancelledError' if operator_cancel else 'AgentTimeoutError')
    finally:
        server.shutdown();server.server_close()
