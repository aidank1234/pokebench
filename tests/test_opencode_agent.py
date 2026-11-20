"""Offline pinned OpenCode contracts; fake CLI and proxy, no provider calls."""
import asyncio
import copy
import json
import os
from pathlib import Path
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from cynthiabench import opencode_agent as oc
from cynthiabench import opencode_isolation as isolation


def transcript(workspace, prompt='task', *, finish='stop', agent='build'):
    return {'info': {'id': 'ses_test', 'directory': str(workspace.resolve()), 'title': 'fixed'},
        'messages': [
            {'info': {'id': 'msg_user', 'sessionID': 'ses_test', 'role': 'user', 'agent': 'build',
                      'model': {'providerID': 'deepseek', 'modelID': 'deepseek-flash', 'variant': 'max'}},
             'parts': [{'id': 'p_user', 'type': 'text', 'text': prompt}]},
            {'info': {'id': 'msg_actor', 'sessionID': 'ses_test', 'role': 'assistant',
                      'providerID': 'deepseek', 'modelID': 'deepseek-flash', 'variant': 'max',
                      'agent': agent, 'path': {'cwd': str(workspace.resolve())},
                      'tokens': {'input': 12, 'output': 5, 'reasoning': 3, 'cache': {'read': 7}},
                      'time': {'created': 1, 'completed': 2}, 'finish': finish},
             'parts': [{'id': 'p_actor', 'type': 'text', 'text': 'Done'}]}]}


def parse(tmp_path, export=None, events=None, prompt='task'):
    event_path, export_path = tmp_path / 'events.jsonl', tmp_path / 'native-session.json'
    events = events if events is not None else [{'type': 'step_finish', 'sessionID': 'ses_test',
        'part': {'id': 'step', 'reason': 'stop', 'tokens': {'input': 99999, 'output': 99999}}}]
    event_path.write_text('\n'.join(map(json.dumps, events)) + '\n')
    export_path.write_text(json.dumps(export if export is not None else transcript(tmp_path, prompt)))
    return oc.parse_events(event_path, export_path, tmp_path, prompt)


def registration():
    return {'trial_id': 'trial-test', 'token': 'SCOPED_NOT_ACCOUNT',
            'base_url': 'http://127.0.0.1:56789/v1', 'model': 'deepseek-flash',
            'reasoning_effort': 'max', 'max_tokens': 393216}


def status():
    return {'trial_id': 'trial-test', 'model_request_count': 1, 'completed_request_count': 1,
            'inflight': 0, 'typed_failures': [], 'usage_uncertainty': False,
            'usage': [{'request_id':'request-test', 'raw_provider_usage': {
                'prompt_tokens':19,'completion_tokens':8,'total_tokens':27,
                'prompt_cache_hit_tokens':7,'completion_tokens_details':{'reasoning_tokens':3}}}], 'budget_halted': False,
            'model_mismatch': False,
            'estimated_cost_usd': 0.01, 'estimated_cost_upper_usd': 0.02, 'halt_reason': None}


def recovered_status():
    current = status()
    semantic = 'a' * 64
    failure = {'source': 'proxy_transport', 'code': 'RemoteDisconnected',
        'request_id': 'failed-attempt', 'semantic_request_sha256': semantic,
        'recoverable': True, 'recovery_status': 'recovered', 'consecutive_attempt_index': 1,
        'retained_liability_upper_usd': '0.786432', 'recovered_by_request_id': 'request-test',
        'recovered_at': '2026-11-19T22:00:00+00:00'}
    current.update(model_request_count=2, completed_request_count=2, typed_failures=[failure],
        hard_failures=[], unresolved_transient_failures=[], recovered_transient_failures=[failure],
        request_usage_complete=False, usage_uncertainty=True, liability_bounds_valid=True,
        outstanding_financial_liability_usd='0.786432', recovered_transient_unknown_upper_usd='0.786432')
    current['usage'][0]['semantic_request_sha256'] = semantic
    return current


def test_explicit_same_request_transport_recovery_is_valid_but_tokens_are_only_known_subset():
    current = recovered_status()
    assert oc.recovered_transport_evidence(current)
    assert oc.proxy_status_error(current, 'trial-test') is None
    assert oc.proxy_usage(current)[0]['total_tokens'] == 27
    assert current['request_usage_complete'] is False and current['usage_uncertainty'] is True


@pytest.mark.parametrize('change', ['unresolved', 'hard', 'wrong_hash', 'missing_success',
    'mismatch', 'halted', 'unbounded', 'lost_liability', 'missing_recovery', 'unknown_failure',
    'claims_complete', 'wrong_trial', 'inflight', 'invalid_attempt', 'no_bound_attestation', 'unexplained_attempt'])
def test_recovery_cannot_mask_unresolved_failure_identity_or_unbounded_usage(change):
    current = recovered_status()
    if change == 'unresolved': current['unresolved_transient_failures'] = current['typed_failures']
    if change == 'hard': current['hard_failures'] = current['typed_failures']
    if change == 'wrong_hash': current['usage'][0]['semantic_request_sha256'] = 'b' * 64
    if change == 'missing_success': current['usage'] = []
    if change == 'mismatch': current['model_mismatch'] = True
    if change == 'halted': current['budget_halted'] = True
    if change == 'unbounded': current['outstanding_financial_liability_usd'] = 'NaN'
    if change == 'lost_liability': current['recovered_transient_unknown_upper_usd'] = '0'
    if change == 'missing_recovery': current['recovered_transient_failures'] = []
    if change == 'unknown_failure': current['typed_failures'].append({'source': 'actor_prose'})
    if change == 'claims_complete': current['request_usage_complete'] = True
    if change == 'wrong_trial': current['trial_id'] = 'other'
    if change == 'inflight': current['inflight'] = 1
    if change == 'invalid_attempt': current['typed_failures'][0]['consecutive_attempt_index'] = 6
    if change == 'no_bound_attestation': current.pop('liability_bounds_valid')
    if change == 'unexplained_attempt': current['model_request_count'] = current['completed_request_count'] = 3
    assert oc.proxy_status_error(current, 'trial-test')


def test_recovered_financial_unknown_does_not_invalidate_later_confirmed_deadline():
    current = recovered_status()
    current.update(cancellation_requested=True, pre_cancellation_provider_failure=False,
        pre_cancellation_infrastructure_failure=False, cancellation_usage_uncertainty=True)
    consequence = {'source': 'downstream_client_disconnect', 'cancellation_consequence': True}
    current['typed_failures'] = [*current['typed_failures'], consequence]
    current['hard_failures'] = [consequence]
    assert oc.cancellation_transport_only(current, {'errors': []})
    current['pre_cancellation_infrastructure_failure'] = True
    assert not oc.cancellation_transport_only(current, {'errors': []})


@pytest.mark.skipif(sys.platform != 'darwin', reason='Pinned native macOS CLI/Seatbelt proof')
@pytest.mark.parametrize(('first_status', 'exhausted'), [(502, False), (503, False), (502, True)])
def test_pinned_native_cli_retries_local_failure_in_same_session(tmp_path, first_status, exhausted):
    """Real native CLI; local fake upstream only, no external model calls.

    The exact scored command/config/isolation is reused. The server has no
    forwarding implementation or credential, and never invokes any model.
    """
    import os
    configured = os.environ.get('POKEBENCH_OPENCODE_TEST_BINARY')
    if not configured:
        pytest.skip('Optional pinned native client is not included in the source distribution')
    executable = Path(configured)
    assert hashlib.sha256(executable.read_bytes()).hexdigest() == 'caf965f08f58574a79a9b5b579856d1cf443e8ed55d310e3529ddd5ae8cf5698'
    workspace = tmp_path / 'workspace'; workspace.mkdir()
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args): pass
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            calls.append({'path': self.path, 'body': body, 'time': time.monotonic()})
            assert self.path == '/v1/chat/completions'
            if len(calls) == 1 or exhausted:
                raw = json.dumps({'error': {'message': 'Temporary local transport proof',
                    'type': 'server_error', 'code': 'transient_test'}}).encode()
                self.send_response(first_status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(raw)))
                if exhausted: self.send_header('retry-after-ms', '1')
                self.end_headers();self.wfile.write(raw)
                return
            raw = []
            for choices, usage in [([{'index': 0, 'delta': {'role': 'assistant', 'content': 'Local retry proof.'}, 'finish_reason': None}], None),
                ([{'index': 0, 'delta': {}, 'finish_reason': 'stop'}], None),
                ([], {'prompt_tokens': 10, 'completion_tokens': 2, 'total_tokens': 12})]:
                chunk = {'id': 'local_completion', 'object': 'chat.completion.chunk', 'created': 1,
                         'model': oc.MODEL, 'choices': choices}
                if usage: chunk['usage'] = usage
                raw.append('data: ' + json.dumps(chunk) + '\n\n')
            payload = (''.join(raw) + 'data: [DONE]\n\n').encode()
            self.send_response(200);self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Content-Length', str(len(payload)));self.end_headers()
            self.wfile.write(payload)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True);worker.start()
    prompt = 'Return the exact sentence: Local retry proof.'
    prepared = oc.prepare_client(workspace, proxy_url=f'http://127.0.0.1:{server.server_port}/v1',
                                 proxy_token='LOCAL_FAKE_NO_ACCOUNT_CREDENTIAL')
    profile = tmp_path / 'profile.sb'
    profile.write_text(isolation.strict_sandbox_profile(workspace, 1,
        proxy_port=server.server_port, executable=executable))
    events = tmp_path / 'events.jsonl'; export = tmp_path / 'native-session.json'
    try:
        cmd = ['/usr/bin/sandbox-exec', '-f', str(profile),
               *oc.command(workspace, executable, prompt, f'local-retry-{first_status}')]
        result = subprocess.run(cmd, input=prompt.encode(), capture_output=True,
                                cwd=workspace, env=prepared['env'], timeout=45)
        events.write_bytes(result.stdout);(tmp_path / 'stderr.log').write_bytes(result.stderr)
        assert result.returncode == (1 if exhausted else 0), result.stderr.decode(errors='replace')
        assert len(calls) == (6 if exhausted else 2), calls
        assert all(call['body'] == calls[0]['body'] for call in calls)
        if not exhausted: assert calls[1]['time'] - calls[0]['time'] >= 1.8
        request = calls[1]['body']
        assert request['model'] == oc.MODEL and request['reasoning_effort'] == 'max'
        assert request['max_tokens'] == 393216 and request.get('thinking', {}).get('type') != 'disabled'
        session = oc.session_id_from_events(events)
        exported = subprocess.run(['/usr/bin/sandbox-exec', '-f', str(profile), str(executable),
            'export', session, '--pure'], capture_output=True, cwd=workspace, env=prepared['env'], timeout=30)
        assert exported.returncode == 0
        export.write_bytes(exported.stdout)
        audit = oc.parse_events(events, export, workspace, prompt)
        assert audit['terminal_event'] is not exhausted and not audit['budget_exhausted']
        assert audit['real_user_prompt_count'] == 1 and audit['native_user_message_count'] == 1
        assert bool(audit['errors']) is exhausted
        assert not any(audit[k] for k in ('native_evidence_error', 'model_mismatch', 'delegation_violation'))
        receipt = {'schema_version': 1, 'external_model_calls': 0, 'first_http_status': first_status,
            'native_session_id': session, 'native_cli_sha256': hashlib.sha256(executable.read_bytes()).hexdigest(),
            'requests': calls, 'same_semantic_request': True, 'retry_exhausted': exhausted, 'native_audit': audit}
        (tmp_path / 'retry-proof.json').write_text(json.dumps(receipt, indent=2) + '\n')
        destination = os.environ.get('OPENCODE_LOCAL_RETRY_PROOF_DIR')
        if destination:
            import shutil
            target = Path(destination) / (str(first_status) + ('-exhaustion' if exhausted else ''))
            target.mkdir(parents=True, exist_ok=False)
            for path in (events, export, tmp_path / 'stderr.log', tmp_path / 'retry-proof.json', profile):
                shutil.copyfile(path, target / path.name)
    finally:
        server.shutdown();server.server_close();worker.join(timeout=5)


def test_fresh_setup_max_vision_native_compaction_without_auxiliary_delegation(tmp_path, monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY', 'DO_NOT_INHERIT')
    monkeypatch.setenv('DEEPSEEK_API_KEY', 'ACCOUNT_DO_NOT_INHERIT')
    monkeypatch.setenv('SSH_AUTH_SOCK', '/private/ssh-agent')
    monkeypatch.setenv('CUSTOM_ACCOUNT_TOKEN', 'DO_NOT_INHERIT')
    prepared = oc.prepare_client(tmp_path, proxy_url='http://127.0.0.1:56789/v1', proxy_token='SCOPED')
    env, config = prepared['env'], prepared['settings']
    assert env['HOME'] == os.environ['HOME']
    assert env['DEEPSEEK_API_KEY'] == 'SCOPED'
    assert not {'OPENAI_API_KEY', 'SSH_AUTH_SOCK', 'CUSTOM_ACCOUNT_TOKEN'} & env.keys()
    assert all(str(tmp_path) in env[k] for k in ('XDG_DATA_HOME', 'XDG_CONFIG_HOME', 'XDG_CACHE_HOME',
                   'XDG_STATE_HOME', 'OPENCODE_CONFIG', 'OPENCODE_DB', 'OPENCODE_TEST_HOME'))
    assert env['OPENCODE_DISABLE_PROJECT_CONFIG'] == env['OPENCODE_DISABLE_CLAUDE_CODE'] == '1'
    assert env['OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX'] == '393216'
    assert prepared['max_output_tokens'] == 393216
    assert 'OPENCODE_DISABLE_AUTOCOMPACT' not in env
    assert config['provider']['deepseek']['models']['deepseek-flash']['modalities']['input'] == ['text', 'image']
    assert config['provider']['deepseek']['models']['deepseek-flash']['variants']['max']['reasoningEffort'] == 'max'
    assert config['agent']['compaction']['model'] == config['small_model'] == oc.NATIVE_MODEL
    assert config['agent']['compaction']['variant'] == 'max'
    assert config['agent']['title']['disable'] and config['agent']['summary']['disable']
    assert config['permission']['task'] == 'deny' and config['subagent_depth'] == 0
    assert 'ACCOUNT_DO_NOT_INHERIT' not in json.dumps(config)
    with pytest.raises(ValueError, match='fresh'):
        oc.prepare_client(tmp_path, proxy_url='http://127.0.0.1:56789/v1', proxy_token='SCOPED')


@pytest.mark.parametrize('cap', [32000, 131072, 393217, True, 393216.0, None])
def test_new_experiment_output_limit_is_exact_and_never_inherited(tmp_path, cap, monkeypatch):
    monkeypatch.setenv('OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX','999999')
    with pytest.raises(ValueError,match='393216'):
        oc.prepare_client(tmp_path, proxy_url='http://127.0.0.1:56789/v1',proxy_token='SCOPED',max_output_tokens=cap)
    assert not (tmp_path/'.opencode-state').exists()
    prepared=oc.prepare_client(tmp_path,proxy_url='http://127.0.0.1:56789/v1',proxy_token='SCOPED')
    assert prepared['env']['OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX']=='393216'


def test_exact_v1_command_has_no_resume_or_remote_server(tmp_path):
    cmd = oc.command(tmp_path, '/pinned/opencode', 'original prompt', 'fixed-title')
    assert cmd[:2] == ['/pinned/opencode', 'run']
    assert cmd[cmd.index('--model') + 1] == 'deepseek/deepseek-flash'
    assert cmd[cmd.index('--variant') + 1] == 'max'
    assert not {'--session', '--continue', '--attach', '--standalone'} & set(cmd)
    assert 'original prompt' not in cmd and '--pure' in cmd


@pytest.mark.parametrize('change', ['provider', 'model', 'variant', 'cwd', 'parent', 'prompt', 'missing_actor', 'duplicate'])
def test_native_export_identity_fails_closed(tmp_path, change):
    exported = transcript(tmp_path)
    actor = exported['messages'][1]['info']
    if change == 'provider': actor['providerID'] = 'other'
    if change == 'model': actor['modelID'] = 'deepseek-v4-pro'
    if change == 'variant': actor['variant'] = 'high'
    if change == 'cwd': actor['path']['cwd'] = '/outside'
    if change == 'parent': exported['info']['parentID'] = 'ses_parent'
    if change == 'prompt': exported['messages'][0]['parts'][0]['text'] = 'coaching'
    if change == 'missing_actor': exported['messages'].pop()
    if change == 'duplicate': exported['messages'].append(copy.deepcopy(exported['messages'][1]))
    audit = parse(tmp_path, exported)
    assert audit['model_mismatch'] or audit['native_evidence_error']


def test_native_assistant_usage_not_double_counted_with_events_and_compaction_attributed(tmp_path):
    exported = transcript(tmp_path)
    compact = copy.deepcopy(exported['messages'][1])
    compact['info'].update(id='msg_compact', agent='compaction')
    exported['messages'].insert(1, compact)
    audit = parse(tmp_path, exported)
    assert audit['usage'] == [{'input_tokens': 24, 'output_tokens': 10, 'thinking_tokens': 6, 'cache_read_tokens': 14}]
    assert audit['native_compaction_count'] == 1 and not audit['delegation_violation']
    assert audit['actor_models'] == ['deepseek-flash']


def test_canonical_proxy_tokens_include_cache_and_reasoning_once():
    assert oc.proxy_usage(status())==[{'input_tokens':19,'output_tokens':8,'total_tokens':27,
                                      'cache_read_tokens':7,'thinking_tokens':3}]
    current=status();current['usage']*=2
    with pytest.raises(ValueError,match='duplicate'):oc.proxy_usage(current)
    current=status();current['usage'][0]['raw_provider_usage']['total_tokens']=999
    with pytest.raises(ValueError,match='Inconsistent'):oc.proxy_usage(current)


@pytest.mark.parametrize('tool', ['task', 'mcp_custom', 'subagent_execute'])
def test_delegation_tool_invocation_is_flagged_but_quoted_actor_prose_is_not(tmp_path, tool):
    exported = transcript(tmp_path)
    exported['messages'][1]['parts'][0]['text'] = 'task opencode subscription quota reached'
    assert not parse(tmp_path, exported)['delegation_violation']
    exported['messages'][1]['parts'].append({'type': 'tool', 'tool': tool, 'state': {'input': {}}})
    assert parse(tmp_path, exported)['delegation_violation']


def test_image_attachments_remain_native_media(tmp_path):
    exported = transcript(tmp_path)
    exported['messages'][1]['parts'].append({'type': 'tool', 'tool': 'read', 'state': {
        'input': {'filePath': 'observation.png'}, 'status': 'completed', 'output': 'Image read successfully',
        'attachments': [{'type': 'file', 'mime': 'image/png', 'url': 'data:image/png;base64,AA=='}]}})
    audit = parse(tmp_path, exported)
    assert audit['native_image_attachment_count'] == 1 and not audit['delegation_violation']


@pytest.mark.parametrize('changes', [{'model_request_count': 0}, {'inflight': 1}, {'usage_uncertainty': True},
    {'typed_failures': [{'type': 'insufficient_balance'}]}, {'budget_halted': True}, {'usage': []},
    {'completed_request_count': 0}, {'trial_id': 'other'}])
def test_host_proxy_evidence_is_required(changes):
    current = status();current.update(changes)
    assert oc.proxy_status_error(current, 'trial-test')
    assert oc.proxy_status_error(status(), 'trial-test') is None


def test_proxy_registration_rejects_wrong_model_effort_or_unscoped_remote_origin():
    assert oc.validate_registration(registration(), 'trial-test') == 56789
    for changes in ({'model': 'deepseek-v4-pro'}, {'reasoning_effort': 'high'},
                    {'base_url': 'https://api.deepseek.com/v1'}, {'trial_id': 'other'}):
        current = registration();current.update(changes)
        with pytest.raises(ValueError):oc.validate_registration(current, 'trial-test')


def test_halt_blocks_before_registration_or_workspace_creation(tmp_path):
    halt = tmp_path / 'halt.json';halt.write_text('{}')
    agent = object.__new__(oc.OpenCodeFlashAgent);agent.quota_halt_path = halt
    with pytest.raises(RuntimeError, match='halt'):
        asyncio.run(agent.run('task', SimpleNamespace(), SimpleNamespace()))


@pytest.mark.parametrize('outcome', ['ongoing', 'win', 'loss', 'timeout'])
@pytest.mark.parametrize('transport', ['fatal', 'recovered'])
def test_structured_proxy_failure_preserves_only_real_terminal_game(tmp_path, monkeypatch, outcome, transport):
    workspace = tmp_path / 'workspace';workspace.mkdir()
    pin = {'provider': 'opencode', 'executable': '/fake/opencode', 'version': '1.19.6', 'sha256': 'a'*64}
    first = True
    async def spawn(*args, **kwargs):
        nonlocal first
        if first:
            first = False
            kwargs['stdout'].write((json.dumps({'type': 'step_finish', 'sessionID': 'ses_test', 'part': {'reason': 'stop'}})+'\n').encode())
        else:
            kwargs['stdout'].write(json.dumps(transcript(workspace, spawn.prompt)).encode())
        async def wait():return 0
        async def drain():pass
        def write(raw):spawn.prompt=raw.decode()
        return SimpleNamespace(pid=9999999, returncode=0,
            stdin=SimpleNamespace(write=write,drain=drain,close=lambda:None),wait=wait)
    def proxy(config, path, body=None):
        if path.endswith('/close'):return {'trial_id':'trial-test','closed':True}
        if body is not None:return registration()
        if transport == 'recovered': return recovered_status()
        current=status();current.update(typed_failures=[{'type': 'insufficient_balance'}],budget_halted=True)
        return current
    monkeypatch.setattr(oc.asyncio, 'create_subprocess_exec', spawn)
    monkeypatch.setattr(oc, 'proxy_request', proxy)
    monkeypatch.setattr(oc, 'strict_sandbox_profile', lambda *a,**k:'(version 1)\n(deny default)\n')
    monkeypatch.setattr(oc, 'validate_cli_pin', lambda value:value)
    monkeypatch.setattr(oc, 'calculation_python_identity', lambda value:{'executable':value})
    def result(path):return {'valid':outcome!='ongoing','terminal':outcome!='ongoing','outcome':outcome}
    environment=SimpleNamespace(workspace=workspace,session_id='trial-test',
        admin=SimpleNamespace(url='http://127.0.0.1:12345',request=result),battle_session={'session_id':'game'})
    agent=object.__new__(oc.OpenCodeFlashAgent)
    agent.cli_pin,agent.logs_dir=pin,tmp_path/'logs'
    agent.quota_halt_path,agent.proxy_admin_config_path=tmp_path/'halt.json',tmp_path/'private-admin.json'
    context=SimpleNamespace(metadata={})
    if outcome in ('win','loss') or transport == 'recovered':asyncio.run(agent.run('original instruction',environment,context))
    else:
        with pytest.raises(RuntimeError,match='preserve evidence'):
            asyncio.run(agent.run('original instruction',environment,context))
    audit=json.loads((agent.logs_dir/'runtime-audit.json').read_text())
    assert audit['quota_blocked'] == (transport == 'fatal') and not audit['model_mismatch']
    assert audit.get('provider_failure_after_terminal',False)==(outcome in ('win','loss') and transport == 'fatal')
    assert environment.finalize_reason == ('runtime_error' if transport == 'fatal' else 'agent_stopped')
    assert context.cost_usd == 0.01
    if transport == 'recovered':
        assert context.n_input_tokens is context.n_output_tokens is context.n_cache_tokens is None
        assert audit['request_usage_complete'] is False and audit['usage'] == []
        assert audit['metered_request_usage'][0]['input_tokens'] == 19
    else:
        assert context.n_input_tokens==19 and context.n_output_tokens==8 and context.n_cache_tokens==7
    assert audit['native_exclusive_usage'][0]['input_tokens']==12
    assert audit['proxy_close_receipt']['closed'] is True
    assert 'SCOPED_NOT_ACCOUNT' not in (agent.logs_dir/'invocation.json').read_text()


@pytest.mark.skipif(sys.platform != 'darwin', reason='macOS Seatbelt profile')
def test_strict_profile_has_no_native_credentials_or_unrelated_loopback(tmp_path):
    executable=Path('/usr/bin/true')
    profile=isolation.strict_sandbox_profile(tmp_path,12345,executable=executable,proxy_port=56789)
    assert '(deny default)' in profile
    assert 'login.keychain-db' not in profile and 'securityd' not in profile and 'trustd' not in profile
    assert '(remote tcp "localhost:12345")' in profile and '(remote tcp "localhost:56789")' in profile
    assert '(allow network-bind' not in profile and '(allow network-inbound' not in profile
    assert str(tmp_path) in profile


def test_single_real_prompt_allows_only_native_synthetic_compaction_continuations(tmp_path):
    exported=transcript(tmp_path)
    continuation=copy.deepcopy(exported['messages'][0]);continuation['info']['id']='msg_continue'
    continuation['parts']=[{'type':'text','text':'Continue native context','synthetic':True},
                           {'type':'compaction','auto':True}]
    exported['messages'].insert(1,continuation)
    audit=parse(tmp_path,exported)
    assert audit['real_user_prompt_count']==1 and audit['native_user_message_count']==2
    assert not audit['native_evidence_error']
    continuation['parts']=[{'type':'text','text':'A second externally supplied tactic'}]
    audit=parse(tmp_path,exported)
    assert audit['real_user_prompt_count']==2 and audit['native_continuation_review_required']
    assert audit['native_evidence_error'] and not audit['model_mismatch']


@pytest.mark.parametrize('change', ['none','prior_provider','prior_infrastructure','real_provider_after_marker',
                                   'wrong_response_model','wrong_native_model','delegation','unmarked_uncertainty'])
def test_deadline_candidate_does_not_erase_genuine_provider_or_actor_failures(change):
    current=status();current.update(cancellation_requested=True,
        pre_cancellation_provider_failure=False,pre_cancellation_infrastructure_failure=False,
        cancellation_usage_uncertainty=True,usage_uncertainty=True,budget_halted=True,
        inflight=1,typed_failures=[{'source':'proxy_transport','code':'BrokenPipeError','cancellation_consequence':True}])
    audit={'model_mismatch':False,'delegation_violation':False,'native_evidence_error':None,
           'errors':[{'name':'MessageAbortedError','data':{'message':'Aborted'}}]}
    if change=='prior_provider':current['pre_cancellation_provider_failure']=True
    if change=='prior_infrastructure':current['pre_cancellation_infrastructure_failure']=True
    if change=='real_provider_after_marker':current['typed_failures'].append({'source':'provider_http','http_status':429})
    if change=='wrong_response_model':current['model_mismatch']=True
    if change=='wrong_native_model':audit['model_mismatch']=True
    if change=='delegation':audit['delegation_violation']=True
    if change=='unmarked_uncertainty':current['cancellation_usage_uncertainty']=False
    assert oc.cancellation_transport_only(current,audit) is (change=='none')


@pytest.mark.parametrize('prior_provider_failure', [False, True])
def test_actual_async_deadline_kills_client_with_inflight_proxy_but_consumes_only_genuine_budget(tmp_path, monkeypatch, prior_provider_failure):
    from cynthiabench.harbor_env import CynthiaEnvironment
    from cynthiabench import framework_signals
    workspace=tmp_path/'workspace';workspace.mkdir()
    order=[]; first=True; live=None
    async def spawn(*args,**kwargs):
        nonlocal first,live
        async def drain():pass
        def write(raw):spawn.prompt=raw.decode()
        stdin=SimpleNamespace(write=write,drain=drain,close=lambda:None)
        if first:
            first=False
            kwargs['stdout'].write((json.dumps({'type':'step_start','sessionID':'ses_test','part':{}})+'\n').encode())
            exit_event=asyncio.Event()
            async def wait():
                await exit_event.wait()
                return -15
            live=SimpleNamespace(pid=9999999,returncode=None,stdin=stdin,wait=wait,exit_event=exit_event)
            return live
        exported=transcript(workspace,spawn.prompt)
        actor=exported['messages'][1]['info'];actor.pop('finish');actor['time'].pop('completed')
        actor['error']={'name':'MessageAbortedError','data':{'message':'Interrupted native client'}}
        kwargs['stdout'].write(json.dumps(exported).encode())
        async def done():return 0
        return SimpleNamespace(pid=9999998,returncode=0,stdin=None,wait=done)
    def killpg(pid,sig):
        order.append('kill')
        live.returncode=-15;live.exit_event.set()
    def proxy(config,path,body=None):
        if path.endswith('/cancel'):
            order.append('cancel-marker')
            return {'trial_id':'trial-test','cancellation_requested':True}
        if path.endswith('/close'):
            order.append('close')
            return {'trial_id':'trial-test','closed':True}
        if body is not None:return registration()
        current=status();current.update(completed_request_count=0,inflight=1,
            cancellation_requested=True,pre_cancellation_provider_failure=prior_provider_failure,
            pre_cancellation_infrastructure_failure=prior_provider_failure,
            usage_uncertainty=True,cancellation_usage_uncertainty=True,budget_halted=True,
            typed_failures=([{'source':'provider_http','http_status':429}] if prior_provider_failure else []))
        return current
    monkeypatch.setattr(oc.asyncio,'create_subprocess_exec',spawn)
    monkeypatch.setattr(oc.os,'killpg',killpg)
    monkeypatch.setattr(oc,'proxy_request',proxy)
    monkeypatch.setattr(oc,'strict_sandbox_profile',lambda *a,**k:'(version 1)\n(deny default)\n')
    monkeypatch.setattr(oc,'validate_cli_pin',lambda x:x)
    monkeypatch.setattr(oc,'calculation_python_identity',lambda x:{'executable':x})
    monkeypatch.setattr(framework_signals,'cancellation_classification',lambda:('timeout','AgentTimeoutError'))
    environment=SimpleNamespace(workspace=workspace,session_id='trial-test',
        admin=SimpleNamespace(url='http://127.0.0.1:12345'),battle_session={'session_id':'game'},
        trial_paths=SimpleNamespace(agent_dir=tmp_path/'logs'))
    context=SimpleNamespace(metadata={});agent=object.__new__(oc.OpenCodeFlashAgent)
    agent.cli_pin={'provider':'opencode','executable':'/fake/opencode','version':'1.19.6','sha256':'a'*64}
    agent.logs_dir=tmp_path/'logs';agent.quota_halt_path=tmp_path/'halt.json'
    agent.proxy_admin_config_path=tmp_path/'private-admin.json'
    async def deadline():
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(agent.run('original task',environment,context),timeout=0.05)
    asyncio.run(deadline())
    assert order==['cancel-marker','kill','close']
    assert environment.agent_audit['cancellation_transport_only'] is not prior_provider_failure
    assert environment.agent_audit['quota_blocked']
    CynthiaEnvironment.resolve_framework_cancellation(environment)
    assert environment.finalize_reason==('runtime_error' if prior_provider_failure else 'timeout')
    assert environment.agent_audit.get('financial_halt_after_confirmed_deadline',False) is not prior_provider_failure
