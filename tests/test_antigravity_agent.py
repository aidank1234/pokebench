"""Native ACP contract tests using a fake server, with no provider inference."""
import asyncio
import base64
import hashlib
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest

from cynthiabench import antigravity_agent as agy


def config(spec):
    return {'configOptions': [
        {'id': 'model', 'currentValue': spec['model'].rsplit('-', 1)[0]},
        {'id': 'thought_level', 'currentValue': spec['effort']},
    ]}


def initial(spec, workspace):
    return {'event': 'init', 'session_id': 'fresh-fake-session',
            'init': {'model': spec['model'], 'effort': spec['effort'],
                                    'cwd': str(workspace.resolve())}, 'config': config(spec)}


def parse(tmp_path, events, model='neon'):
    path = tmp_path / 'events.jsonl'
    path.write_text('\n'.join(map(json.dumps, events)) + '\n')
    return agy.parse_events(path, agy.resolve_model(model), tmp_path)


def native_database(workspace, rows=(), session_id='fresh-fake-session'):
    path = workspace / '.client-state/gemini/antigravity-cli/conversations' / (session_id + '.db')
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
        connection.execute('CREATE TABLE steps (idx INTEGER PRIMARY KEY, step_type INTEGER, status INTEGER, step_payload BLOB)')
        connection.executemany('INSERT INTO steps VALUES (?, ?, ?, ?)', rows)
    return path


def test_command_and_state_are_fresh_subscription_only(tmp_path):
    spec = agy.resolve_model('neon')
    cmd = agy.command(spec, tmp_path, '/pinned/agy')
    assert cmd[:2] == ['/pinned/agy', 'acp']
    assert cmd[cmd.index('--gemini-dir') + 1] == str(tmp_path / '.client-state/gemini')
    assert '--include-default-customizations=false' in cmd
    assert not {'--continue', '--conversation', '--remote-agent'} & set(cmd)
    agy.prepare_state(tmp_path)
    settings = json.loads((tmp_path / '.client-state/gemini/antigravity-cli/settings.json').read_text())
    assert settings['useG1Credits'] is False and settings['modelProvider'] == ''
    with pytest.raises(ValueError, match='fresh'):
        agy.prepare_state(tmp_path)


@pytest.mark.parametrize('change', ['missing', 'model', 'effort', 'cwd', 'config'])
def test_missing_or_wrong_selected_model_evidence_fails_closed(tmp_path, change):
    spec = agy.resolve_model('neon')
    init = initial(spec, tmp_path)
    if change == 'missing':
        events = []
    else:
        if change == 'config':
            init['config'] = {}
        else:
            init['init'][change] = 'wrong'
        events = [init]
    audit = parse(tmp_path, events + [{'event': 'result', 'result': {'stopReason': 'end_turn'}}])
    assert audit['model_mismatch']


def test_usage_snapshots_are_not_summed_and_context_size_is_not_tokens(tmp_path):
    spec = agy.resolve_model('neon')
    usage = {'input_tokens': 120, 'output_tokens': 30, 'cache_read_tokens': 80}
    rpc = {'event': 'rpc', 'direction': 'server', 'rpc': {'params': {
        'update': {'sessionUpdate': 'usage_update', 'used': 12345, 'size': 1048576, 'usage': usage}}}}
    audit = parse(tmp_path, [initial(spec, tmp_path), rpc, rpc,
                            {'event': 'result', 'result': {'stopReason': 'end_turn', 'usage': usage}}])
    assert audit['usage'] == [usage]
    assert audit['terminal_event'] and not audit['model_mismatch']
    audit = parse(tmp_path, [initial(spec, tmp_path),
                            {'event': 'rpc', 'rpc': {'params': {'update': {'used': 10, 'size': 100}}}},
                            {'event': 'result', 'result': {'stopReason': 'end_turn'}}])
    assert audit['usage'] == []


def test_actual_native_acp_usage_names_preserve_cached_and_thought_counts(tmp_path):
    spec = agy.resolve_model('neon')
    usage = {'inputTokens': 18652, 'outputTokens': 5161, 'cachedReadTokens': 35306,
             'cachedWriteTokens': 0, 'thoughtTokens': 3906, 'totalTokens': 59119}
    audit = parse(tmp_path, [initial(spec, tmp_path),
        {'event': 'result', 'result': {'stopReason': 'end_turn', 'usage': usage}}])
    assert audit['usage'] == [{'input_tokens': 18652, 'output_tokens': 5161,
        'cache_read_tokens': 35306, 'thinking_tokens': 3906, 'total_tokens': 59119}]


def test_quota_comes_only_from_provider_error_not_tool_or_assistant_text(tmp_path):
    spec = agy.resolve_model('carbon')
    benign = [initial(spec, tmp_path),
              {'event': 'rpc', 'direction': 'server', 'rpc': {'params': {'update': {
                  'sessionUpdate': 'tool_call_update', 'rawOutput': 'quota_exhausted'}}}},
              {'event': 'result', 'result': {'stopReason': 'end_turn', 'response': 'weekly limit'}}]
    assert not parse(tmp_path, benign, 'carbon')['quota_blocked']
    error = {'event': 'rpc', 'direction': 'server', 'rpc': {'error': {
        'code': -32000, 'message': 'subscription quota exhausted; reset tomorrow'}}}
    audit = parse(tmp_path, benign + [error], 'carbon')
    assert audit['quota_blocked'] and audit['quota_evidence'][0]['source'] == 'native_acp_error'


def test_permission_grants_once_and_rejects_delegation(tmp_path):
    options = [{'optionId': 'yes', 'kind': 'allow_once'}, {'optionId': 'no', 'kind': 'reject_once'}]
    result, reason = agy.permission_response({'toolCall': {'title': 'run_command', 'rawInput': {'CommandLine': 'python -c "print(42)"'}}, 'options': options})
    assert reason is None and result['outcome']['optionId'] == 'yes'
    for tool in ({'title': 'invoke_subagent'}, {'name': 'call_mcp_tool'},
                 {'title': 'run_command', 'rawInput': {'CommandLine': 'agy -p "Play for me"'}}):
        result, reason = agy.permission_response({'toolCall': tool, 'options': options})
        assert reason and result['outcome']['optionId'] == 'no'
    assert agy.delegation_reason({'title': 'view_file', 'rawOutput': 'invoke_subagent'}) is None


class FakeProcess:
    """Respond to actual outgoing RPC messages; never execute commands."""
    def __init__(self, spec, *, prompt_error=False, wrong_selection=False, delegate=False,
                 stop_reason='end_turn', native_quota_warning=False):
        self.spec = spec
        self.stdout = asyncio.StreamReader()
        self.stdin = self
        self.pid = 9999999
        self.returncode = None
        self.sent = []
        self.prompt_error = prompt_error
        self.wrong_selection = wrong_selection
        self.delegate = delegate
        self.stop_reason = stop_reason
        self.native_quota_warning = native_quota_warning
        self.session_id = 'fresh-fake-session'

    def emit(self, event):
        self.stdout.feed_data((json.dumps(event) + '\n').encode())

    def write(self, raw):
        request = json.loads(raw)
        self.sent.append(request)
        method = request.get('method')
        if method == 'initialize':
            result = {'protocolVersion': 1, 'agentInfo': {'name': 'fake', 'version': '1'}}
        elif method == 'session/new':
            result = {'sessionId': self.session_id, 'models': {'availableModels': [{'modelId': self.spec['model']}]}}
        elif method == 'session/set_config_option':
            result = config(self.spec)
            if self.wrong_selection:
                result['configOptions'][0]['currentValue'] = 'gemini-4-hydrogen'
        elif method == 'session/prompt':
            if self.native_quota_warning:
                self.emit({'jsonrpc': '2.0', 'method': 'session/update', 'params': {
                    'sessionId': self.session_id, 'update': {'sessionUpdate': 'agent_message_chunk',
                    'content': {'type': 'text', 'text': CAPTURED_QUOTA_WARNING}}}})
            if self.delegate:
                self.emit({'jsonrpc': '2.0', 'method': 'session/update', 'params': {
                    'sessionId': self.session_id, 'update': {'sessionUpdate': 'tool_call', 'title': 'invoke_subagent'}}})
            if self.prompt_error:
                self.emit({'jsonrpc': '2.0', 'id': request['id'], 'error': {'code': -32000, 'message': 'subscription quota exhausted'}})
                return
            result = {'stopReason': self.stop_reason}
        elif method is None:
            return
        else:
            raise AssertionError(method)
        self.emit({'jsonrpc': '2.0', 'id': request['id'], 'result': result})

    async def drain(self):
        pass

    def close(self):
        self.returncode = 0
        self.stdout.feed_eof()

    async def wait(self):
        return self.returncode


def test_acp_selects_and_confirms_before_single_prompt(tmp_path):
    spec = agy.resolve_model('neon')

    async def run():
        process = FakeProcess(spec)
        client = agy.ACPClient(process, tmp_path)
        await client.start(tmp_path, spec)
        await client.prompt('exact task instruction')
        methods = [r.get('method') for r in process.sent]
        assert methods == ['initialize', 'session/new', 'session/set_config_option',
                           'session/set_config_option', 'session/prompt']
        assert process.sent[1]['params'] == {'cwd': str(tmp_path), 'mcpServers': []}
        assert process.sent[0]['params']['clientCapabilities']['terminal'] is False
        assert process.sent[-1]['params']['prompt'] == [{'type': 'text', 'text': 'exact task instruction'}]

    asyncio.run(run())
    audit = agy.parse_events(tmp_path / 'events.jsonl', spec, tmp_path)
    assert audit['terminal_event'] and not audit['model_mismatch']


def test_wrong_config_never_sends_prompt(tmp_path):
    async def run():
        process = FakeProcess(agy.resolve_model('neon'), wrong_selection=True)
        client = agy.ACPClient(process, tmp_path)
        with pytest.raises(ValueError, match='confirm'):
            await client.start(tmp_path, agy.resolve_model('neon'))
        assert all(r.get('method') != 'session/prompt' for r in process.sent)
    asyncio.run(run())


@pytest.mark.parametrize('battle_terminal', [False, True])
def test_provider_failure_preserves_already_terminal_game(tmp_path, monkeypatch, battle_terminal):
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    logs = tmp_path / 'logs'
    spec = agy.resolve_model('carbon')
    pin = {'provider': 'agy', 'executable': '/fake/agy', 'version': 'fake', 'sha256': 'a' * 64}
    process = None

    async def spawn(*args, **kwargs):
        nonlocal process
        native_database(workspace)
        process = FakeProcess(spec, prompt_error=True)
        return process

    monkeypatch.setattr(agy.asyncio, 'create_subprocess_exec', spawn)
    monkeypatch.setattr(agy, 'strict_sandbox_profile', lambda *a, **k: '(version 1)\n(deny default)\n')
    monkeypatch.setattr(agy, 'validate_cli_pin', lambda value: value)
    monkeypatch.setattr(agy, 'calculation_python_identity', lambda value: {'executable': value})
    requests = []

    def result(path):
        requests.append(path)
        return {'valid': battle_terminal, 'terminal': battle_terminal, 'outcome': 'loss' if battle_terminal else 'ongoing'}

    environment = SimpleNamespace(workspace=workspace, admin=SimpleNamespace(url='http://127.0.0.1:12345', request=result),
                                  battle_session={'session_id': 'game-session'})
    context = SimpleNamespace(metadata={})
    agent = object.__new__(agy.AntigravityAgent)
    agent.spec, agent.effort, agent.cli_pin, agent.logs_dir = spec, 'high', pin, logs
    agent.quota_halt_path = tmp_path / 'quota-halt.json'
    if battle_terminal:
        asyncio.run(agent.run('original instruction', environment, context))
    else:
        with pytest.raises(RuntimeError, match='preserve evidence'):
            asyncio.run(agent.run('original instruction', environment, context))
    assert requests == ['/admin/sessions/game-session/result']
    audit = json.loads((logs / 'runtime-audit.json').read_text())
    assert audit['quota_blocked'] and audit.get('provider_failure_after_terminal', False) == battle_terminal
    assert environment.finalize_reason == 'runtime_error'
    assert json.loads((logs / 'delegation-audit.json').read_text())['requires_manual_review'] is False
    assert (logs / 'prompt.txt').read_text().startswith('original instruction\n\nRead CONTROLS.md')


def test_native_delegation_invalidates_audit(tmp_path):
    spec = agy.resolve_model('neon')

    async def run():
        client = agy.ACPClient(FakeProcess(spec, delegate=True), tmp_path)
        await client.start(tmp_path, spec)
        await client.prompt('task')

    asyncio.run(run())
    audit = agy.parse_events(tmp_path / 'events.jsonl', spec, tmp_path)
    assert audit['delegation_violation'] and audit['delegation_review_required']


@pytest.mark.parametrize('stop_reason,expected_reason,budget', [
    ('max_tokens', 'timeout', True), ('max_turn_requests', 'timeout', True),
    ('refusal', 'agent_stopped', False)])
@pytest.mark.parametrize('battle_terminal', [False, True])
def test_native_stop_is_valid_noncompletion(tmp_path, monkeypatch, stop_reason, expected_reason, budget, battle_terminal):
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    spec = agy.resolve_model('neon')
    pin = {'provider': 'agy', 'executable': '/fake/agy', 'version': 'fake', 'sha256': 'b' * 64}

    async def spawn(*a, **k):
        native_database(workspace)
        return FakeProcess(spec, stop_reason=stop_reason)

    monkeypatch.setattr(agy.asyncio, 'create_subprocess_exec', spawn)
    monkeypatch.setattr(agy, 'strict_sandbox_profile', lambda *a, **k: '(version 1)\n(deny default)\n')
    monkeypatch.setattr(agy, 'validate_cli_pin', lambda value: value)
    monkeypatch.setattr(agy, 'calculation_python_identity', lambda value: {'executable': value})
    state = {'valid': battle_terminal, 'terminal': battle_terminal, 'outcome': 'loss' if battle_terminal else 'ongoing'}
    environment = SimpleNamespace(workspace=workspace, admin=SimpleNamespace(url='http://127.0.0.1:12345'),
                                  battle_session={'session_id': 'game-session'})
    agent = object.__new__(agy.AntigravityAgent)
    agent.spec, agent.effort, agent.cli_pin, agent.logs_dir = spec, 'xhigh', pin, tmp_path / 'logs'
    agent.quota_halt_path = tmp_path / 'quota-halt.json'
    asyncio.run(agent.run('instruction', environment, SimpleNamespace(metadata={})))
    assert environment.finalize_reason == expected_reason
    # The actual worker's finalize contract retains any existing terminal result.
    if not state['terminal']:
        state.update(valid=True, terminal=True, outcome='timeout')
    assert state['valid'] and state['outcome'] == ('loss' if battle_terminal else 'timeout')
    assert environment.agent_audit['budget_exhausted'] is budget
    assert environment.agent_audit['refused'] is (stop_reason == 'refusal')
    assert not environment.agent_audit['model_mismatch']


def test_cleanup_missing_group_at_both_signals_is_reaped(monkeypatch):
    process = SimpleNamespace(pid=9999999, stdin=SimpleNamespace(close=lambda: None))
    waits = 0
    signals = []

    async def wait():
        nonlocal waits
        waits += 1
        if waits < 3:
            raise asyncio.TimeoutError()
        return 0

    def killpg(pid, sig):
        signals.append(sig)
        raise ProcessLookupError()

    process.wait = wait
    monkeypatch.setattr(agy.os, 'killpg', killpg)
    asyncio.run(agy.stop_process(process))
    assert signals == [agy.signal.SIGTERM, agy.signal.SIGKILL] and waits == 3


CAPTURED_QUOTA_WARNING = '\n⚠ Individual quota reached. Please upgrade your subscription to increase your limits. Resets in 2h28m6s.\n'


def native_warning_event(kind='agent_message_chunk'):
    return {'event': 'rpc', 'direction': 'server', 'rpc': {'jsonrpc': '2.0',
        'method': 'session/update', 'params': {'sessionId': 'fresh-fake-session',
        'update': {'sessionUpdate': kind, 'content': {'type': 'text', 'text': CAPTURED_QUOTA_WARNING}}}}}


def test_captured_native_quota_with_normal_end_turn_and_exact_reset(tmp_path):
    assert hashlib.sha256(CAPTURED_QUOTA_STEP).hexdigest() == '41b4144bd23127a52e79aeef80625f7561a2608966df8d46e8ccd1ab97ab4c33'
    spec = agy.resolve_model('neon')
    database = native_database(tmp_path, [(1, 17, 3, CAPTURED_QUOTA_STEP)])
    audit = parse(tmp_path, [initial(spec, tmp_path), native_warning_event(),
                           {'event': 'result', 'result': {'stopReason': 'end_turn'}}])
    assert audit['terminal_event'] and audit['quota_blocked'] and not audit['model_mismatch']
    assert audit['native_quota_scan']['status'] == 'complete'
    evidence, = audit['quota_evidence']
    assert evidence['source'] == 'native_client_typed_error'
    assert evidence['http_status'] == 429 and evidence['step_type'] == 17
    assert evidence['error_info']['reason'] == 'QUOTA_EXHAUSTED'
    assert evidence['quota_reset_at'] == '2026-11-19T22:17:32Z'
    assert evidence['quota_reset_delay'] == '2h28m6.26603925s'
    assert evidence['retry_delay'] == '8886.266039250s'
    assert evidence['service_error_sha256'] == '3bf8ac86dfc19d8e9188d16443bc8683b82018fd7698332795e1946efca7d2a6'
    assert evidence['database_sha256'] == hashlib.sha256(database.read_bytes()).hexdigest()


@pytest.mark.parametrize('step_type,kind', [(15, 'agent_message_chunk'), (132, 'tool_call_update')])
def test_identical_quota_prose_and_quoted_native_bytes_cannot_halt(tmp_path, step_type, kind):
    # Even an exact warning and a quoted copy of the complete service error
    # belong to actor/tool content, unless they are a native ERROR step.
    native_database(tmp_path, [(1, step_type, 3, CAPTURED_QUOTA_STEP)])
    spec = agy.resolve_model('neon')
    audit = parse(tmp_path, [initial(spec, tmp_path), native_warning_event(kind),
        {'event': 'result', 'result': {'stopReason': 'end_turn', 'response': CAPTURED_QUOTA_WARNING}}])
    assert not audit['quota_blocked']
    assert audit['native_quota_scan']['status'] == 'complete'


def wire(field, value):
    def varint(number):
        result = bytearray()
        while number > 127:
            result.append((number & 127) | 128)
            number >>= 7
        return bytes(result + bytes([number]))
    if isinstance(value, int):
        return varint(field << 3) + varint(value)
    return varint((field << 3) | 2) + varint(len(value)) + value


def mutated_quota_payload(*, reason='QUOTA_EXHAUSTED', domain='cloudcode-pa.googleapis.com',
                          code=429, http=429, model='gemini-4-neon-xhigh', step_type=17):
    error_details = agy._native_one(agy._native_one(CAPTURED_QUOTA_STEP, 24, 2), 3, 2)
    service = json.loads(agy._native_one(error_details, 5, 2))
    service['error']['code'] = code
    service['error']['details'][0].update(reason=reason, domain=domain)
    service['error']['details'][0]['metadata']['model'] = model
    return wire(1, step_type) + wire(24, wire(3, wire(7, http) + wire(5, json.dumps(service).encode())))


@pytest.mark.parametrize('changes', [{'reason': 'OTHER'}, {'domain': 'actor.example'},
                                    {'code': 403}, {'http': 403}])
def test_service_envelope_requires_matching_quota_types(tmp_path, changes):
    native_database(tmp_path, [(1, 17, 3, mutated_quota_payload(**changes))])
    evidence, scan = agy.native_quota_evidence(tmp_path, 'fresh-fake-session', agy.resolve_model('neon'))
    assert not evidence and scan['status'] == 'complete'


@pytest.mark.parametrize('payload', [b'\x00', b'\xc2\x01\xff', b'\xff' * 12])
def test_corrupt_native_error_fails_closed_without_fabricating_quota(tmp_path, payload):
    native_database(tmp_path, [(1, 17, 3, payload)])
    spec = agy.resolve_model('neon')
    parse(tmp_path, [initial(spec, tmp_path), {'event': 'result', 'result': {'stopReason': 'end_turn'}}])
    audit = agy.parse_events(tmp_path / 'events.jsonl', spec, tmp_path, require_native_quota_scan=True)
    assert audit['native_evidence_error'] and not audit['quota_blocked']
    assert not audit['model_mismatch']


@pytest.mark.parametrize('session_id', ['../outside', '/absolute', None])
def test_native_scan_never_follows_untrusted_session_paths(tmp_path, session_id):
    evidence, scan = agy.native_quota_evidence(tmp_path, session_id, agy.resolve_model('neon'))
    assert not evidence and scan['status'] == 'unavailable'


def test_halt_latch_is_first_writer_persistent_and_blocks_before_client_start(tmp_path):
    path = tmp_path / 'halt.json'
    agy.check_quota_halt(path)
    audit = {'quota_evidence': [{'source': 'native_client_typed_error', 'step_index': 1}]}
    agy.publish_quota_halt(path, audit, tmp_path)
    first = path.read_bytes()
    agy.publish_quota_halt(path, {'quota_evidence': ['second']}, tmp_path)
    assert path.read_bytes() == first
    assert json.loads(first)['quota_blocked'] is True
    agent = object.__new__(agy.AntigravityAgent)
    agent.quota_halt_path = path
    with pytest.raises(RuntimeError, match='quota halt'):
        asyncio.run(agent.run('instruction', SimpleNamespace(), SimpleNamespace()))


@pytest.mark.parametrize('outcome', ['win', 'loss', 'ongoing', 'timeout'])
@pytest.mark.parametrize('native_db_present', [True, False])
def test_native_429_end_turn_halts_and_only_preserves_real_terminal_game(tmp_path, monkeypatch, outcome, native_db_present):
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    spec = agy.resolve_model('neon')
    pin = {'provider': 'agy', 'executable': '/fake/agy', 'version': 'fake', 'sha256': 'b' * 64}
    halt = tmp_path / 'global-halt.json'

    async def spawn(*a, **k):
        if native_db_present:
            native_database(workspace, [(1, 17, 3, CAPTURED_QUOTA_STEP)])
        return FakeProcess(spec, native_quota_warning=True)

    monkeypatch.setattr(agy.asyncio, 'create_subprocess_exec', spawn)
    monkeypatch.setattr(agy, 'strict_sandbox_profile', lambda *a, **k: '(version 1)\n(deny default)\n')
    monkeypatch.setattr(agy, 'validate_cli_pin', lambda value: value)
    monkeypatch.setattr(agy, 'calculation_python_identity', lambda value: {'executable': value})

    def result(path):
        assert halt.exists() is native_db_present and environment.finalize_reason == 'runtime_error'
        return {'valid': outcome != 'ongoing', 'terminal': outcome != 'ongoing', 'outcome': outcome}

    environment = SimpleNamespace(workspace=workspace, admin=SimpleNamespace(url='http://127.0.0.1:12345', request=result),
                                  battle_session={'session_id': 'game-session'})
    context = SimpleNamespace(metadata={})
    agent = object.__new__(agy.AntigravityAgent)
    agent.spec, agent.effort, agent.cli_pin, agent.logs_dir = spec, 'xhigh', pin, tmp_path / 'logs'
    agent.quota_halt_path = halt
    if outcome in ('win', 'loss'):
        asyncio.run(agent.run('unchanged instruction', environment, context))
    else:
        with pytest.raises(RuntimeError, match='preserve evidence'):
            asyncio.run(agent.run('unchanged instruction', environment, context))
    assert environment.agent_audit['quota_blocked'] is native_db_present
    assert bool(environment.agent_audit['native_evidence_error']) is not native_db_present
    assert environment.finalize_reason == 'runtime_error'
    assert environment.agent_audit.get('provider_failure_after_terminal', False) == (outcome in ('win', 'loss'))
    if native_db_present:
        assert json.loads(halt.read_text())['quota_evidence'][0]['quota_reset_at'] == '2026-11-19T22:17:32Z'


def test_uncheckpointed_database_is_not_silently_treated_as_no_quota(tmp_path):
    database = native_database(tmp_path, [(1, 17, 3, CAPTURED_QUOTA_STEP)])
    Path(str(database) + '-wal').write_bytes(b'uncheckpointed')
    evidence, scan = agy.native_quota_evidence(tmp_path, 'fresh-fake-session', agy.resolve_model('neon'))
    assert not evidence and scan['status'] == 'invalid'
    assert 'uncheckpointed' in scan['error']


def test_native_quota_model_mismatch_is_separate_evidence_error(tmp_path):
    native_database(tmp_path, [(1, 17, 3, mutated_quota_payload(model='gemini-4-carbon-high'))])
    spec = agy.resolve_model('neon')
    parse(tmp_path, [initial(spec, tmp_path), {'event': 'result', 'result': {'stopReason': 'end_turn'}}])
    audit = agy.parse_events(tmp_path / 'events.jsonl', spec, tmp_path, require_native_quota_scan=True)
    assert 'model differs' in audit['native_evidence_error']
    assert not audit['model_mismatch'] and not audit['quota_blocked']


# Unmodified native step17 payload from hard-01-seed3__vrzKsVG (v1).
# Contains the structured 429; no credential material. SHA256 asserted below.
CAPTURED_QUOTA_STEP = base64.b64decode(
    'CBEgAyqbAQoMCMWl/dcGEJC+wfECGAWiAVAKJGMxNTA5OThkLTkyNzUtNGFkNi1iZjdlLTY4ZTBkYzM2MDYyNhABGAEiJDRjNjUy'
    'NTE5LTE3YjQtNGQwYy05ZTUxLThmZmFhMWMzYmFmY9IBEgoQCAMSDAjFpf3XBhCY5cHxApoCIGJkZTNhMjgyNmE2ODQyNDliODZi'
    'OTI3NDk2MDFlYmE5wgGAFRr7FApmSW5kaXZpZHVhbCBxdW90YSByZWFjaGVkLiBQbGVhc2UgdXBncmFkZSB5b3VyIHN1YnNjcmlw'
    'dGlvbiB0byBpbmNyZWFzZSB5b3VyIGxpbWl0cy4gUmVzZXRzIGluIDJoMjhtNnMuEoUBUkVTT1VSQ0VfRVhIQVVTVEVEIChjb2Rl'
    'IDQyOSk6IEluZGl2aWR1YWwgcXVvdGEgcmVhY2hlZC4gUGxlYXNlIHVwZ3JhZGUgeW91ciBzdWJzY3JpcHRpb24gdG8gaW5jcmVh'
    'c2UgeW91ciBsaW1pdHMuIFJlc2V0cyBpbiAyaDI4bTZzLhqoCUhUVFAgNDI5IFRvbyBNYW55IFJlcXVlc3RzClNoZXJsb2c6IApU'
    'cmFjZUlEOiAweGVlNDM1MGJiNzIwOTE4ZWUKSGVhZGVyczogeyJBbHQtU3ZjIjpbImgzPVwiOjQ0M1wiOyBtYT0yNTkyMDAwLGgz'
    'LTI5PVwiOjQ0M1wiOyBtYT0yNTkyMDAwIl0sIkNvbnRlbnQtTGVuZ3RoIjpbIjcwOSJdLCJDb250ZW50LVR5cGUiOlsidGV4dC9l'
    'dmVudC1zdHJlYW0iXSwiRGF0ZSI6WyJUaHUsIDE5IE5vdiAyMDI2IDE5OjQ5OjI1IEdNVCJdLCJTZXJ2ZXIiOlsiRVNGIl0sIlNl'
    'cnZlci1UaW1pbmciOlsiZ2ZldDR0NzsgZHVyPTEwOSJdLCJWYXJ5IjpbIk9yaWdpbiIsIlgtT3JpZ2luIiwiUmVmZXJlciJdLCJY'
    'LUNsb3VkYWljb21wYW5pb24tVHJhY2UtSWQiOlsiZWU0MzUwYmI3MjA5MThlZSJdLCJYLUNvbnRlbnQtVHlwZS1PcHRpb25zIjpb'
    'Im5vc25pZmYiXSwiWC1GcmFtZS1PcHRpb25zIjpbIlNBTUVPUklHSU4iXSwiWC1Yc3MtUHJvdGVjdGlvbiI6WyIwIl19Cgp7CiAg'
    'ImVycm9yIjogewogICAgImNvZGUiOiA0MjksCiAgICAiZGV0YWlscyI6IFsKICAgICAgewogICAgICAgICJAdHlwZSI6ICJ0eXBl'
    'Lmdvb2dsZWFwaXMuY29tL2dvb2dsZS5ycGMuRXJyb3JJbmZvIiwKICAgICAgICAiZG9tYWluIjogImNsb3VkY29kZS1wYS5nb29n'
    'bGVhcGlzLmNvbSIsCiAgICAgICAgIm1ldGFkYXRhIjogewogICAgICAgICAgIm1vZGVsIjogImdlbWluaS00LW5lb24teGhpZ2gi'
    'LAogICAgICAgICAgInF1b3RhUmVzZXREZWxheSI6ICIyaDI4bTYuMjY2MDM5MjVzIiwKICAgICAgICAgICJxdW90YVJlc2V0VGlt'
    'ZVN0YW1wIjogIjIwMjYtMTEtMTlUMjI6MTc6MzJaIiwKICAgICAgICAgICJ1aU1lc3NhZ2UiOiAidHJ1ZSIKICAgICAgICB9LAog'
    'ICAgICAgICJyZWFzb24iOiAiUVVPVEFfRVhIQVVTVEVEIgogICAgICB9LAogICAgICB7CiAgICAgICAgIkB0eXBlIjogInR5cGUu'
    'Z29vZ2xlYXBpcy5jb20vZ29vZ2xlLnJwYy5SZXRyeUluZm8iLAogICAgICAgICJyZXRyeURlbGF5IjogIjg4ODYuMjY2MDM5MjUw'
    'cyIKICAgICAgfQogICAgXSwKICAgICJtZXNzYWdlIjogIkluZGl2aWR1YWwgcXVvdGEgcmVhY2hlZC4gUGxlYXNlIHVwZ3JhZGUg'
    'eW91ciBzdWJzY3JpcHRpb24gdG8gaW5jcmVhc2UgeW91ciBsaW1pdHMuIFJlc2V0cyBpbiAyaDI4bTZzLiIsCiAgICAic3RhdHVz'
    'IjogIlJFU09VUkNFX0VYSEFVU1RFRCIKICB9Cn0qxQV7CiAgImVycm9yIjogewogICAgImNvZGUiOiA0MjksCiAgICAibWVzc2Fn'
    'ZSI6ICJJbmRpdmlkdWFsIHF1b3RhIHJlYWNoZWQuIFBsZWFzZSB1cGdyYWRlIHlvdXIgc3Vic2NyaXB0aW9uIHRvIGluY3JlYXNl'
    'IHlvdXIgbGltaXRzLiBSZXNldHMgaW4gMmgyOG02cy4iLAogICAgInN0YXR1cyI6ICJSRVNPVVJDRV9FWEhBVVNURUQiLAogICAg'
    'ImRldGFpbHMiOiBbCiAgICAgIHsKICAgICAgICAiQHR5cGUiOiAidHlwZS5nb29nbGVhcGlzLmNvbS9nb29nbGUucnBjLkVycm9y'
    'SW5mbyIsCiAgICAgICAgInJlYXNvbiI6ICJRVU9UQV9FWEhBVVNURUQiLAogICAgICAgICJkb21haW4iOiAiY2xvdWRjb2RlLXBh'
    'Lmdvb2dsZWFwaXMuY29tIiwKICAgICAgICAibWV0YWRhdGEiOiB7CiAgICAgICAgICAidWlNZXNzYWdlIjogInRydWUiLAogICAg'
    'ICAgICAgIm1vZGVsIjogImdlbWluaS00LW5lb24teGhpZ2giLAogICAgICAgICAgInF1b3RhUmVzZXREZWxheSI6ICIyaDI4bTYu'
    'MjY2MDM5MjVzIiwKICAgICAgICAgICJxdW90YVJlc2V0VGltZVN0YW1wIjogIjIwMjYtMTEtMTlUMjI6MTc6MzJaIgogICAgICAg'
    'IH0KICAgICAgfSwKICAgICAgewogICAgICAgICJAdHlwZSI6ICJ0eXBlLmdvb2dsZWFwaXMuY29tL2dvb2dsZS5ycGMuUmV0cnlJ'
    'bmZvIiwKICAgICAgICAicmV0cnlEZWxheSI6ICI4ODg2LjI2NjAzOTI1MHMiCiAgICAgIH0KICAgIF0KICB9Cn0KMiZjMTUwOTk4'
    'ZC05Mjc1LTRhZDYtYmY3ZS02OGUwZGMzNjA2MjYtMTitA0qFAVJFU09VUkNFX0VYSEFVU1RFRCAoY29kZSA0MjkpOiBJbmRpdmlk'
    'dWFsIHF1b3RhIHJlYWNoZWQuIFBsZWFzZSB1cGdyYWRlIHlvdXIgc3Vic2NyaXB0aW9uIHRvIGluY3JlYXNlIHlvdXIgbGltaXRz'
    'LiBSZXNldHMgaW4gMmgyOG02cy5SjAJ7IkB0eXBlIjoidHlwZS5nb29nbGVhcGlzLmNvbS9nb29nbGUucnBjLkVycm9ySW5mbyIs'
    'ICJyZWFzb24iOiJRVU9UQV9FWEhBVVNURUQiLCAiZG9tYWluIjoiY2xvdWRjb2RlLXBhLmdvb2dsZWFwaXMuY29tIiwgIm1ldGFk'
    'YXRhIjp7Im1vZGVsIjoiZ2VtaW5pLTQtbmVvbi14aGlnaCIsICJxdW90YVJlc2V0RGVsYXkiOiIyaDI4bTYuMjY2MDM5MjVzIiwg'
    'InF1b3RhUmVzZXRUaW1lU3RhbXAiOiIyMDI2LTExLTE5VDIyOjE3OjMyWiIsICJ1aU1lc3NhZ2UiOiJ0cnVlIn19UlR7IkB0eXBl'
    'IjoidHlwZS5nb29nbGVhcGlzLmNvbS9nb29nbGUucnBjLlJldHJ5SW5mbyIsICJyZXRyeURlbGF5IjoiODg4Ni4yNjYwMzkyNTBz'
    'In0oAQ=='
)
