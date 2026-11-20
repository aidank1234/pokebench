"""Real SQLite + fake ACP tests for prospective terminal-only control.

No provider inference, native model process, game, or actor-authored code runs.
"""
import asyncio
import copy
import hashlib
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest

from cynthiabench import antigravity_agent as agy
from test_antigravity_agent import FakeProcess, native_database, CAPTURED_QUOTA_STEP, mutated_quota_payload


def read_events(logs):
    return [json.loads(line) for line in (logs / 'events.jsonl').read_text().splitlines()]


def write_events(logs, events):
    (logs / 'events.jsonl').write_text(''.join(json.dumps(e) + '\n' for e in events))


def setup(tmp_path, *, rows=(), stop_reason='end_turn', prompt_error=False, delegate=False, model='carbon'):
    workspace, logs = tmp_path / 'fresh-workspace', tmp_path / 'host-logs'
    workspace.mkdir()
    logs.mkdir()
    spec = agy.resolve_model(model)
    native_database(workspace, rows)
    prompt = 'Fresh current-case instruction. No prior results or state.'
    (logs / 'prompt.txt').write_text(prompt)
    return [workspace, logs, spec, prompt,
            {'stop_reason': stop_reason, 'prompt_error': prompt_error, 'delegate': delegate}, None]


async def run_controller(parts, results):
    workspace, logs, spec, prompt, options, _ = parts
    process = FakeProcess(spec, **options)
    client = agy.ACPClient(process, logs)
    parts[4:] = [process, client]
    requested = []

    def result(path):
        requested.append(path)
        return copy.deepcopy(results[min(len(requested) - 1, len(results) - 1)])

    env = SimpleNamespace(battle_session={'session_id': 'fresh-battle'},
                          admin=SimpleNamespace(request=result))
    await client.start(workspace, spec)
    await agy.terminal_controller(client, prompt, env, spec, workspace, logs, logs / 'quota-halt.json')
    return requested


ONGOING = {'valid': False, 'terminal': False, 'outcome': 'ongoing', 'team_id': 'hard-09', 'seed': 1}
WIN = {'valid': True, 'terminal': True, 'outcome': 'win', 'team_id': 'hard-09', 'seed': 1}
LOSS = {**WIN, 'outcome': 'loss'}


def audit(parts):
    workspace, logs, spec, *_ = parts
    return agy.parse_events(logs / 'events.jsonl', spec, workspace,
                            require_native_quota_scan=True, require_controller=True)


@pytest.mark.parametrize('terminal', [WIN, LOSS])
def test_same_fresh_session_exact_neutral_chain_until_real_terminal(tmp_path, terminal):
    parts = setup(tmp_path)
    requested = asyncio.run(run_controller(parts, [ONGOING, ONGOING, terminal]))
    workspace, logs, spec, prompt, process, client = parts
    prompts = [e for e in process.sent if e.get('method') == 'session/prompt']
    assert len(prompts) == 3
    assert [p['params']['prompt'][0]['text'] for p in prompts] == [prompt, agy.NEUTRAL_CONTINUATION, agy.NEUTRAL_CONTINUATION]
    assert {p['params']['sessionId'] for p in prompts} == {'fresh-fake-session'}
    assert sum(e.get('method') == 'session/new' for e in process.sent) == 1
    assert not any(e.get('method') in ('session/load', 'session/resume', 'session/fork') for e in process.sent)
    assert requested == ['/admin/sessions/fresh-battle/result'] * 3
    result = audit(parts)
    assert not result['model_mismatch'] and not result['quota_blocked']
    assert result['terminal_event']
    assert result['controller_lifecycle']['neutral_continuations'] == 2
    assert result['controller_lifecycle']['stop_cause'] == 'true_game_terminal'
    assert result['controller_lifecycle']['valid']


@pytest.mark.parametrize('terminal', [WIN, LOSS, {'valid': True, 'terminal': True, 'outcome': 'timeout', 'reason': 'action_limit'}])
def test_never_continues_after_host_terminal(tmp_path, terminal):
    parts = setup(tmp_path)
    asyncio.run(run_controller(parts, [terminal]))
    assert sum(e.get('method') == 'session/prompt' for e in parts[4].sent) == 1
    assert audit(parts)['controller_lifecycle']['neutral_continuations'] == 0
    assert audit(parts)['controller_lifecycle']['valid']


@pytest.mark.parametrize('kind', ['strategy', 'missing_gate', 'wrong_session', 'wrong_result_id',
    'wrong_native_response', 'host_terminal', 'host_digest', 'gate_model', 'gate_effort',
    'host_path', 'missing_start', 'wrong_neutral_pin', 'duplicate_result', 'extra_session', 'prior_runtime_error'])
def test_parser_rejects_malicious_missing_or_mismatched_continuations(tmp_path, kind):
    parts = setup(tmp_path)
    asyncio.run(run_controller(parts, [ONGOING, WIN]))
    logs = parts[1]
    events = read_events(logs)
    prompts = [e for e in events if e.get('direction') == 'client' and (e.get('rpc') or {}).get('method') == 'session/prompt']
    gate = next(e for e in events if e.get('event') == 'continuation_gate')
    start = next(e for e in events if e.get('event') == 'controller_start')
    second_result = [e for e in events if e.get('event') == 'result'][1]
    if kind == 'strategy':
        prompts[1]['rpc']['params']['prompt'][0]['text'] += ' Use a revive loop.'
    elif kind == 'missing_gate':
        events.remove(gate)
    elif kind == 'wrong_session':
        prompts[1]['rpc']['params']['sessionId'] = 'prior-case'
    elif kind == 'wrong_result_id':
        second_result['request_id'] += 100
    elif kind == 'wrong_native_response':
        response = next(e for e in events if e.get('direction') == 'server' and (e.get('rpc') or {}).get('id') == prompts[1]['rpc']['id'])
        response['rpc']['result']['stopReason'] = 'max_tokens'
    elif kind == 'host_terminal':
        gate['host_result'] = LOSS
        gate['host_result_sha256'] = agy._json_digest(LOSS)
    elif kind == 'host_digest':
        gate['host_result']['seed'] = 99
    elif kind == 'gate_model':
        gate['model'] = agy.resolve_model('neon')['model']
    elif kind == 'gate_effort':
        gate['effort'] = 'low'
    elif kind == 'host_path':
        gate['host_result_path'] = '/admin/sessions/other-battle/result'
    elif kind == 'missing_start':
        events.remove(start)
    elif kind == 'wrong_neutral_pin':
        start['neutral_prompt_sha256'] = agy._digest('secret advice')
    elif kind == 'duplicate_result':
        events.append(copy.deepcopy(second_result))
    elif kind == 'extra_session':
        events.insert(events.index(prompts[1]), {'event': 'rpc', 'direction': 'client', 'rpc': {'id': 777, 'method': 'session/new', 'params': {}}})
    elif kind == 'prior_runtime_error':
        events.insert(events.index(prompts[1]), {'event': 'runtime_error', 'error': 'native failure'})
    write_events(logs, events)
    result = audit(parts)
    assert result['model_mismatch']
    assert not result['controller_lifecycle']['valid']
    assert result['controller_lifecycle']['validation_errors']


def test_live_backup_includes_wal_and_does_not_change_source_database(tmp_path):
    parts = setup(tmp_path, model='neon')
    workspace, logs, spec, *_ = parts
    database = workspace / '.client-state/gemini/antigravity-cli/conversations/fresh-fake-session.db'
    with sqlite3.connect(database) as writer:
        writer.execute('PRAGMA journal_mode=WAL')
        writer.execute('INSERT INTO steps VALUES (?, ?, ?, ?)', (1, 17, 3, CAPTURED_QUOTA_STEP))
        writer.commit()
        assert Path(str(database) + '-wal').stat().st_size
        before = hashlib.sha256(database.read_bytes()).hexdigest()
        snapshot = agy.live_native_snapshot(workspace, 'fresh-fake-session', spec, logs, 0)
        assert hashlib.sha256(database.read_bytes()).hexdigest() == before
        assert writer.execute('SELECT COUNT(*) FROM steps').fetchone()[0] == 1
        assert snapshot['scan']['status'] == 'complete'
        assert snapshot['scan']['error_steps_inspected'] == 1
        assert snapshot['quota_evidence'][0]['source'] == 'native_client_typed_error'
        assert snapshot['scan']['database_sha256'] != before


@pytest.mark.parametrize('failure', ['quota', 'malformed_native', 'missing_database', 'rpc_error', 'delegation'])
def test_no_continuation_or_host_poll_after_provider_or_runtime_failure(tmp_path, failure):
    rows = [(1, 17, 3, CAPTURED_QUOTA_STEP if failure == 'quota' else b'malformed')] if failure in ('quota', 'malformed_native') else ()
    parts = setup(tmp_path, rows=rows, prompt_error=failure == 'rpc_error', delegate=failure == 'delegation', model='neon')
    workspace, logs, *_ = parts
    if failure == 'missing_database':
        (workspace / '.client-state/gemini/antigravity-cli/conversations/fresh-fake-session.db').unlink()
    with pytest.raises(agy.ACPError):
        asyncio.run(run_controller(parts, []))
    assert sum(e.get('method') == 'session/prompt' for e in parts[4].sent) == 1
    assert (logs / 'quota-halt.json').exists() is (failure == 'quota')
    if failure == 'quota':
        assert audit(parts)['quota_blocked']


@pytest.mark.parametrize('reason', ['refusal', 'max_tokens', 'max_turn_requests'])
def test_native_non_end_turn_does_not_get_mechanical_continuation(tmp_path, reason):
    parts = setup(tmp_path, stop_reason=reason)
    assert asyncio.run(run_controller(parts, [])) == []
    result = audit(parts)
    assert result['controller_lifecycle']['valid']
    assert result['controller_lifecycle']['stop_cause'] == 'native_stop_' + reason
    assert sum(e.get('method') == 'session/prompt' for e in parts[4].sent) == 1


def test_controller_limit_is_bounded_and_audited_without_game_loss_claim(tmp_path, monkeypatch):
    monkeypatch.setattr(agy, 'MAX_NEUTRAL_CONTINUATIONS', 2)
    parts = setup(tmp_path)
    asyncio.run(run_controller(parts, [ONGOING]))
    result = audit(parts)
    assert result['controller_lifecycle']['valid']
    assert result['controller_lifecycle']['neutral_continuations'] == 2
    assert result['controller_lifecycle']['stop_cause'] == 'continuation_limit'
    assert not result['budget_exhausted'] and not result['quota_blocked']
    assert sum(e.get('method') == 'session/prompt' for e in parts[4].sent) == 3


def test_snapshot_tampering_or_cross_case_redirection_fails_closed(tmp_path):
    parts = setup(tmp_path, model='neon')
    asyncio.run(run_controller(parts, [ONGOING, WIN]))
    events = read_events(parts[1])
    gate = next(e for e in events if e.get('event') == 'continuation_gate')
    snapshot = Path(gate['native_snapshot']['scan']['database'])
    snapshot.chmod(0o600)
    with sqlite3.connect(snapshot) as writer:
        writer.execute('INSERT INTO steps VALUES (?, ?, ?, ?)', (1, 17, 3, CAPTURED_QUOTA_STEP))
    evidence, scan = agy.native_quota_evidence(gate['native_snapshot']['workspace'], 'fresh-fake-session', parts[2])
    # Even an attacker updating all claimed hashes cannot authorize a prompt
    # after the actual typed quota event in the immutable host snapshot.
    gate['native_snapshot'].update(scan=scan, quota_evidence=evidence)
    write_events(parts[1], events)
    result = audit(parts)
    assert result['model_mismatch'] and result['quota_blocked']


def test_cancelled_last_inflight_prompt_keeps_prior_chain_but_not_extra_result(tmp_path):
    parts = setup(tmp_path)
    asyncio.run(run_controller(parts, [ONGOING, WIN]))
    events = read_events(parts[1])
    second = [i for i, e in enumerate(events) if e.get('direction') == 'client' and (e.get('rpc') or {}).get('method') == 'session/prompt'][1]
    write_events(parts[1], events[:second + 1])
    result = audit(parts)
    assert not result['model_mismatch']
    assert result['controller_lifecycle']['pending_prompt']
    assert result['controller_lifecycle']['completed_prompts'] == 1
    assert not result['terminal_event']
    assert not result['controller_lifecycle']['final_prompt_completed']


def test_error_on_last_prompt_cannot_inherit_prior_end_turn(tmp_path):
    parts = setup(tmp_path)
    asyncio.run(run_controller(parts, [ONGOING, WIN]))
    events = read_events(parts[1])
    second = [i for i, e in enumerate(events) if e.get('direction') == 'client' and (e.get('rpc') or {}).get('method') == 'session/prompt'][1]
    request_id = events[second]['rpc']['id']
    events = events[:second + 1] + [{'event': 'rpc', 'direction': 'server', 'rpc': {
        'jsonrpc': '2.0', 'id': request_id, 'error': {'code': -32000, 'message': 'native failure'}}}]
    write_events(parts[1], events)
    result = audit(parts)
    assert result['controller_lifecycle']['valid']
    assert not result['terminal_event'] and not result['controller_lifecycle']['final_prompt_completed']


@pytest.mark.parametrize('kind', ['wrong_model', 'nonquota_service_error'])
def test_typed_native_identity_or_other_service_error_never_gets_continuation(tmp_path, kind):
    payload = mutated_quota_payload(model='wrong-model') if kind == 'wrong_model' else mutated_quota_payload(
        model=agy.resolve_model('carbon')['model'], code=403, http=403)
    parts = setup(tmp_path, rows=[(1, 17, 3, payload)])
    with pytest.raises(agy.ACPError):
        asyncio.run(run_controller(parts, []))
    assert sum(e.get('method') == 'session/prompt' for e in parts[4].sent) == 1
    assert not (parts[1] / 'quota-halt.json').exists()


def test_full_agent_run_reaps_single_client_and_preserves_final_audit(tmp_path, monkeypatch):
    workspace, logs = tmp_path / 'workspace', tmp_path / 'logs'
    workspace.mkdir()
    spec = agy.resolve_model('carbon')
    pin = {'provider': 'agy', 'executable': '/fake/agy', 'version': 'fake', 'sha256': 'c' * 64}
    processes, requests = [], []

    async def spawn(*args, **kwargs):
        native_database(workspace)
        process = FakeProcess(spec)
        processes.append(process)
        return process

    def result(path):
        requests.append(path)
        return ONGOING if len(requests) == 1 else WIN

    monkeypatch.setattr(agy.asyncio, 'create_subprocess_exec', spawn)
    monkeypatch.setattr(agy, 'strict_sandbox_profile', lambda *a, **k: '(version 1)\n(deny default)\n')
    monkeypatch.setattr(agy, 'validate_cli_pin', lambda value: value)
    monkeypatch.setattr(agy, 'calculation_python_identity', lambda value: {'executable': value})
    environment = SimpleNamespace(workspace=workspace, battle_session={'session_id': 'fresh-battle'},
        admin=SimpleNamespace(url='http://127.0.0.1:12345', request=result))
    context = SimpleNamespace(metadata={})
    agent = object.__new__(agy.AntigravityAgent)
    agent.spec, agent.effort, agent.cli_pin, agent.logs_dir = spec, spec['effort'], pin, logs
    agent.quota_halt_path = tmp_path / 'quota-halt.json'
    asyncio.run(agent.run('Fresh prospective task', environment, context))
    assert len(processes) == 1 and processes[0].returncode == 0
    report = json.loads((logs / 'runtime-audit.json').read_text())
    assert report['controller_lifecycle']['valid']
    assert report['controller_lifecycle']['completed_prompts'] == 2
    assert report['controller_lifecycle']['true_terminal_observed']
    assert report['native_quota_scan']['status'] == 'complete'
    assert not report['model_mismatch'] and not report['quota_blocked']
    assert environment.finalize_reason == 'agent_stopped'
    assert context.metadata['completion_policy'].startswith('verified-game-win-or-loss-only')
