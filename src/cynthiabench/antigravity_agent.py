"""Fresh native Antigravity ACP sessions for the Gemini extension study.

ACP is used because the print CLI does not honor the state-directory overrides.
The server and all of its native tools run inside the outer Seatbelt boundary.
Model selection evidence is client configuration, not remote-model attestation.
"""
from __future__ import annotations

import asyncio
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import sqlite3
import subprocess
import tempfile
import time
from urllib.parse import urlsplit

from harbor.agents.base import BaseAgent
from harbor.models.agent.context import AgentContext

from .agent_config import AGY_MODELS
from .delegation_audit import AI_COMMAND, audit_agent_logs
from .gemini_isolation import isolated_agent_env, strict_sandbox_profile
from .native_runtime import cli_identity, validate_cli_pin, calculation_python_identity


DELEGATION_TOOLS = frozenset({
    'invoke_subagent', 'define_subagent', 'manage_subagents', 'browser_subagent',
    'run_workflow', 'send_message', 'manage_inbox', 'schedule', 'call_mcp_tool',
})
EXTRA_AI_COMMAND = re.compile(r'(?<![\w-])agy(?=\s|$)|aiplatform\.googleapis\.com', re.I)
QUOTA_CODES = ('usage_limit_reached', 'insufficient_quota', 'weekly limit',
               'quota exhausted', 'quota_exhausted', 'credit_balance_exhausted',
               'out of credits', 'subscription quota', 'daily quota exceeded')
DEFAULT_QUOTA_HALT_PATH = Path(__file__).resolve().parents[2] / 'work/provider-quota-halt.json'
NATIVE_QUOTA_SCHEMA = 'antigravity-native-step17-error-v1'
CONTROLLER_SCHEMA = 'antigravity-terminal-controller-v1'
MAX_NEUTRAL_CONTINUATIONS = 64
NEUTRAL_CONTINUATION = (
    'The battle has not reached a verified terminal outcome. Continue the current '
    'battle using its live observations. If your own calculation is still running, '
    'wait for or collect its result before continuing. Continue until a verified '
    'terminal outcome; a strategy explanation alone does not complete the task.'
)


def _digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def _json_digest(value):
    return _digest(json.dumps(value, sort_keys=True, separators=(',', ':')))


def check_quota_halt(path=None):
    """Fail before starting another client after a study-wide quota block.

    This latch is deliberately persistent; reset timestamps are evidence, not
    permission to clear it or automatically resume an interrupted study.
    """
    path = Path(path) if path is not None else DEFAULT_QUOTA_HALT_PATH
    if path.exists() or path.is_symlink():
        raise RuntimeError(f'Antigravity provider quota halt is active: {path}')


def publish_quota_halt(path, audit, workspace):
    """Atomically publish the first quota event without replacing its evidence."""
    path = Path(path) if path is not None else DEFAULT_QUOTA_HALT_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {'schema_version': 1, 'provider': 'agy', 'quota_blocked': True,
              'detected_at': datetime.now(timezone.utc).isoformat(),
              'workspace': str(Path(workspace).resolve()),
              'quota_evidence': audit['quota_evidence']}
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', dir=path.parent,
                prefix='.provider-quota-halt-', delete=False) as output:
            temporary = Path(output.name)
            json.dump(record, output, indent=2)
            output.write('\n')
            output.flush()
            os.fsync(output.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            pass
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return str(path)


def _native_fields(data):
    """Decode bounded protobuf wire fields from the pinned native client DB.

    Only the installed client's observed step/error envelope is interpreted.
    Actor prose, tool output, HTTP text, and the displayed warning are ignored.
    """
    if not isinstance(data, bytes) or len(data) > 8 * 1024 * 1024:
        raise ValueError('Invalid or oversized native error payload')
    offset, result = 0, []

    def varint():
        nonlocal offset
        value = 0
        for shift in range(0, 70, 7):
            if offset >= len(data):
                break
            byte = data[offset]
            offset += 1
            if shift == 63 and byte > 1:
                break
            value |= (byte & 127) << shift
            if not byte & 128:
                return value
        raise ValueError('Invalid native protobuf varint')

    while offset < len(data):
        if len(result) >= 4096:
            raise ValueError('Too many native protobuf fields')
        tag = varint()
        field, wire = tag >> 3, tag & 7
        if not field:
            raise ValueError('Invalid native protobuf field')
        if wire == 0:
            value = varint()
        elif wire in (1, 2, 5):
            size = varint() if wire == 2 else (8 if wire == 1 else 4)
            if size > len(data) - offset:
                raise ValueError('Truncated native protobuf field')
            value = data[offset:offset + size]
            offset += size
        else:
            raise ValueError('Unsupported native protobuf wire type')
        result.append((field, wire, value))
    return result


def _native_one(data, field, wire):
    values = [value for number, kind, value in _native_fields(data)
              if number == field and kind == wire]
    if len(values) != 1:
        raise ValueError(f'Missing or duplicate native field {field}')
    return values[0]


def native_quota_evidence(workspace, session_id, spec):
    """Inspect only this fresh session's closed, read-only native database.

    The captured 429 is a typed native ERROR step (17), whose error details
    carry Google's structured ErrorInfo. ACP projects it as ordinary actor
    text and returns end_turn, so that text cannot identify its provenance.
    """
    scan = {'schema': NATIVE_QUOTA_SCHEMA, 'session_id': session_id,
            'status': 'unavailable', 'error_steps_inspected': 0}
    evidence = []
    if not isinstance(session_id, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,127}', session_id):
        scan['error'] = 'Missing or unsafe native session identifier'
        return evidence, scan
    workspace = Path(workspace).resolve()
    database = workspace / '.client-state/gemini/antigravity-cli/conversations' / (session_id + '.db')
    scan['database'] = str(database)
    try:
        if database.resolve() != database or not database.is_file():
            scan['error'] = 'Fresh native session database is absent or redirected'
            return evidence, scan
        if any(Path(str(database) + suffix).exists()
               and Path(str(database) + suffix).stat().st_size > 0
               for suffix in ('-wal', '-journal')):
            raise ValueError('Native session database has uncheckpointed journal state')
        # Reaped native client has closed/checkpointed this database. Immutable
        # prevents SQLite from opening or creating journal/sidecar state.
        with sqlite3.connect(database.as_uri() + '?mode=ro&immutable=1', uri=True) as connection:
            rows = connection.execute('SELECT idx, step_type, status, step_payload '
                'FROM steps WHERE step_type=17 ORDER BY idx LIMIT 10001').fetchall()
        if len(rows) > 10000:
            raise ValueError('Too many native error steps')
        with database.open('rb') as source:
            scan['database_sha256'] = hashlib.file_digest(source, 'sha256').hexdigest()
        for index, step_type, status, payload in rows:
            scan['error_steps_inspected'] += 1
            if status != 3 or _native_one(payload, 1, 0) != 17:
                raise ValueError('Unexpected native error step envelope')
            error_details = _native_one(_native_one(payload, 24, 2), 3, 2)
            http_status = _native_one(error_details, 7, 0)
            raw = _native_one(error_details, 5, 2)
            service = json.loads(raw)
            error = service.get('error') if isinstance(service, dict) else None
            if not isinstance(error, dict):
                raise ValueError('Native error lacks structured service error')
            if http_status != 429 or error.get('code') != 429 or error.get('status') != 'RESOURCE_EXHAUSTED':
                continue
            details = error.get('details', [])
            if not isinstance(details, list):
                raise ValueError('Invalid native service error details')
            infos = [item for item in details if isinstance(item, dict)
                     and item.get('@type') == 'type.googleapis.com/google.rpc.ErrorInfo'
                     and item.get('reason') == 'QUOTA_EXHAUSTED'
                     and item.get('domain') == 'cloudcode-pa.googleapis.com']
            for info in infos:
                metadata = info.get('metadata') or {}
                if not isinstance(metadata, dict) or metadata.get('model') != spec['model']:
                    raise ValueError('Native quota error model differs from selected actor')
                item = {'source': 'native_client_typed_error', 'schema': NATIVE_QUOTA_SCHEMA,
                        'session_id': session_id, 'database': str(database), 'step_index': index,
                        'step_type': step_type, 'step_status': status, 'http_status': http_status,
                        'service_status': error['status'], 'error_info': info,
                        'native_payload_sha256': hashlib.sha256(payload).hexdigest(),
                        'service_error_sha256': hashlib.sha256(raw).hexdigest(),
                        'database_sha256': scan['database_sha256']}
                reset = metadata.get('quotaResetTimeStamp')
                if isinstance(reset, str):
                    try:
                        parsed = datetime.fromisoformat(reset.replace('Z', '+00:00'))
                        if parsed.utcoffset() is None:
                            raise ValueError('Native quota reset lacks timezone')
                        item['quota_reset_at'] = parsed.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')
                    except ValueError as exc:
                        # Missing/bad reset metadata cannot erase a typed quota
                        # error or prevent the global halt from being latched.
                        item['quota_reset_parse_error'] = str(exc)
                if isinstance(metadata.get('quotaResetDelay'), str):
                    item['quota_reset_delay'] = metadata['quotaResetDelay']
                retries = [detail.get('retryDelay') for detail in details if isinstance(detail, dict)
                           and detail.get('@type') == 'type.googleapis.com/google.rpc.RetryInfo'
                           and isinstance(detail.get('retryDelay'), str)]
                if retries:
                    item['retry_delay'] = retries[0]
                evidence.append(item)
        scan['status'] = 'complete'
    except (OSError, sqlite3.Error, ValueError, TypeError, UnicodeDecodeError) as exc:
        scan['status'] = 'invalid'
        scan['error'] = str(exc)
    return evidence, scan


def live_native_snapshot(workspace, session_id, spec, logs_dir, prompt_index):
    """Back up this live session read-only, including its committed WAL state.

    The native writer is neither stopped nor checkpointed. SQLite's backup API
    provides a consistent closed destination, which the existing typed scanner
    can inspect without treating actor prose as provider error evidence.
    """
    if not isinstance(session_id, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,127}', session_id):
        raise ACPError('Missing or unsafe native session identifier at continuation gate')
    workspace = Path(workspace).resolve()
    database = workspace / '.client-state/gemini/antigravity-cli/conversations' / (session_id + '.db')
    if database.resolve() != database or not database.is_file():
        raise ACPError('Fresh native session database is absent or redirected at continuation gate')
    snapshot_root = Path(logs_dir).resolve() / 'continuation-snapshots' / f'prompt-{prompt_index:03d}'
    destination = snapshot_root / '.client-state/gemini/antigravity-cli/conversations' / (session_id + '.db')
    destination.parent.mkdir(parents=True, exist_ok=False)
    deadline = time.monotonic() + 10

    def progress(status, remaining, total):
        if time.monotonic() >= deadline:
            raise ACPError('Timed out creating consistent native continuation snapshot')

    # mode=ro sees the live WAL; immutable=1 would silently omit it. Only the
    # closed backup is subsequently read immutable by native_quota_evidence.
    with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True, timeout=2)) as source:
        with closing(sqlite3.connect(destination)) as output:
            source.backup(output, pages=256, progress=progress, sleep=.01)
    destination.chmod(0o400)
    evidence, scan = native_quota_evidence(snapshot_root, session_id, spec)
    return {'workspace': str(snapshot_root), 'source_database': str(database),
            'session_id': session_id, 'prompt_index': prompt_index,
            'scan': scan, 'quota_evidence': evidence}


def resolve_model(model):
    for alias, spec in AGY_MODELS.items():
        if model in (alias, spec['model']):
            return dict(spec)
    raise ValueError(f'Unsupported Antigravity actor: {model}')


def command(spec, workspace, executable):
    """Build the native server command; callers must apply strict Seatbelt."""
    resolve_model(spec['model'])
    root = Path(workspace).resolve() / '.client-state/gemini'
    return [str(executable), 'acp', '--stdio', '--process-per-session',
            '--gemini-dir', str(root), '--app-data-dir', 'antigravity-cli',
            '--include-default-customizations=false', '--disable-telemetry',
            '--dangerously-skip-permissions']


def prepare_state(workspace):
    """Refuse inherited state and configure subscription-only native access."""
    root = Path(workspace) / '.client-state'
    if root.exists() and any(p.is_file() or p.is_symlink() for p in root.rglob('*')):
        raise ValueError('Antigravity episode state must be fresh; no resume or reuse')
    settings = root / 'gemini/antigravity-cli/settings.json'
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(json.dumps({
        'useG1Credits': False, 'modelProvider': '', 'notifications': False,
        'allowNonWorkspaceAccess': False, 'toolPermission': 'always-proceed',
        'artifactReviewPolicy': 'always-proceed', 'disableSlashCommands': True,
    }, indent=2) + '\n')
    return root


def verified_selection(config, spec):
    options = {item.get('id'): item.get('currentValue')
               for item in config.get('configOptions', []) if isinstance(item, dict)}
    base = spec['model'].rsplit('-', 1)[0]
    if options.get('model') != base or options.get('thought_level') != spec['effort']:
        raise ValueError('Antigravity did not confirm the requested model and effort')
    return options


def tool_command(tool):
    raw = tool.get('rawInput', tool.get('parameters', {}))
    if isinstance(raw, dict):
        for name in ('CommandLine', 'command', 'cmd'):
            if isinstance(raw.get(name), str):
                return raw[name]
    return None


def delegation_reason(tool):
    """Inspect invocation fields, never arbitrary tool output or assistant prose."""
    names = {str(tool.get(k, '')).lower() for k in ('tool_name', 'name', 'title')}
    meta = tool.get('_meta') or {}
    if isinstance(meta, dict):
        names.update(str(meta.get(k, '')).lower() for k in ('toolName', 'tool_name', 'name'))
    for name in names:
        if name in DELEGATION_TOOLS or any(name.startswith(n + ' ') for n in DELEGATION_TOOLS):
            return 'prohibited_native_tool:' + name
    if tool.get('subagent_info') or tool.get('subagentInfo'):
        return 'subagent_invocation'
    if any(re.search(r'\b(subagent|subagents|teamwork|orchestrator)\b', n) for n in names):
        return 'possible_native_delegation'
    cmd = tool_command(tool)
    if cmd and (AI_COMMAND.search(cmd) or EXTRA_AI_COMMAND.search(cmd)):
        return 'possible_ai_command'
    return None


def permission_response(params):
    """Grant one native action inside Seatbelt, and reject delegation requests."""
    tool = params.get('toolCall') or {}
    reason = delegation_reason(tool)
    options = params.get('options') or []
    kinds = ('reject_once', 'reject_always') if reason else ('allow_once',)
    selected = next((o.get('optionId') for kind in kinds for o in options
                     if o.get('kind') == kind and o.get('optionId')), None)
    outcome = ({'outcome': 'selected', 'optionId': selected} if selected
               else {'outcome': 'cancelled'})
    return {'outcome': outcome}, reason


class ACPError(RuntimeError):
    pass


async def stop_process(process):
    """Close and reap, tolerating exit races at either process-group signal."""
    if process.stdin:
        process.stdin.close()
    try:
        await asyncio.wait_for(process.wait(), timeout=5)
        return
    except asyncio.TimeoutError:
        pass
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(process.wait(), timeout=5)
            return
        except asyncio.TimeoutError:
            pass
    raise ACPError('Antigravity process did not exit after cleanup')


class ACPClient:
    """A single-session JSON-RPC client; never loads or resumes conversations."""
    def __init__(self, process, logs_dir):
        self.process = process
        self.logs_dir = Path(logs_dir)
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.request_id = 0
        self.session_id = None
        self.selected = False
        self.spec = None
        self.flags = []

    def log(self, event):
        with (self.logs_dir / 'events.jsonl').open('a') as output:
            output.write(json.dumps(event) + '\n')

    async def send(self, event):
        self.log({'event': 'rpc', 'direction': 'client', 'rpc': event})
        self.process.stdin.write((json.dumps(event) + '\n').encode())
        await self.process.stdin.drain()

    async def request(self, method, params):
        self.request_id += 1
        request_id = self.request_id
        await self.send({'jsonrpc': '2.0', 'id': request_id, 'method': method, 'params': params})
        while True:
            raw = await self.process.stdout.readline()
            if not raw:
                raise ACPError('Antigravity ACP closed before its response')
            try:
                event = json.loads(raw)
            except (ValueError, UnicodeDecodeError) as exc:
                raise ACPError('Invalid Antigravity ACP JSON') from exc
            if not isinstance(event, dict) or event.get('jsonrpc') != '2.0':
                raise ACPError('Invalid Antigravity ACP envelope')
            self.log({'event': 'rpc', 'direction': 'server', 'rpc': event})
            if event.get('id') == request_id and 'method' not in event:
                if 'error' in event:
                    raise ACPError('Antigravity ACP request failed: ' + json.dumps(event['error']))
                if not isinstance(event.get('result'), dict):
                    raise ACPError('Missing Antigravity ACP result object')
                return event['result']
            if 'method' in event and 'id' in event:
                if event['method'] == 'session/request_permission':
                    response, reason = permission_response(event.get('params') or {})
                    if reason:
                        self.flags.append(reason)
                        self.log({'event': 'delegation_flag', 'reason': reason})
                    await self.send({'jsonrpc': '2.0', 'id': event['id'], 'result': response})
                else:
                    await self.send({'jsonrpc': '2.0', 'id': event['id'],
                                     'error': {'code': -32601, 'message': 'Unsupported client capability'}})
                    raise ACPError('Unexpected native request: ' + event['method'])
            elif event.get('method') == 'session/update':
                params = event.get('params') or {}
                if self.session_id and params.get('sessionId') != self.session_id:
                    raise ACPError('Antigravity update belongs to another session')
                update = params.get('update') or {}
                kind = update.get('sessionUpdate')
                if kind in ('tool_call', 'tool_call_update'):
                    reason = delegation_reason(update)
                    if reason:
                        self.flags.append(reason)
                        self.log({'event': 'delegation_flag', 'reason': reason})
                    cmd = tool_command(update)
                    if cmd:
                        with (self.logs_dir / 'tools.jsonl').open('a') as output:
                            output.write(json.dumps({'command': cmd, 'toolCallId': update.get('toolCallId')}) + '\n')
                if self.selected and kind == 'config_option_update':
                    self.log({'event': 'config_update', 'config': update})
                    verified_selection(update, self.spec)
            else:
                raise ACPError('Unexpected Antigravity ACP response or notification')

    async def start(self, workspace, spec):
        initialized = await self.request('initialize', {
            'protocolVersion': 1,
            'clientCapabilities': {'fs': {'readTextFile': False, 'writeTextFile': False}, 'terminal': False},
            'clientInfo': {'name': 'pokebench-harbor', 'version': '1'},
        })
        if initialized.get('protocolVersion') != 1:
            raise ACPError('Unsupported Antigravity ACP protocol version')
        created = await self.request('session/new', {'cwd': str(Path(workspace).resolve()), 'mcpServers': []})
        self.session_id = created.get('sessionId')
        available = {m.get('modelId') for m in created.get('models', {}).get('availableModels', [])}
        if not self.session_id or spec['model'] not in available:
            raise ACPError('Requested Antigravity model is not available in this fresh session')
        await self.request('session/set_config_option', {
            'sessionId': self.session_id, 'configId': 'model', 'value': spec['model'].rsplit('-', 1)[0]})
        configured = await self.request('session/set_config_option', {
            'sessionId': self.session_id, 'configId': 'thought_level', 'value': spec['effort']})
        verified_selection(configured, spec)
        self.spec = dict(spec)
        self.selected = True
        self.log({'event': 'init', 'session_id': self.session_id,
                  'init': {'model': spec['model'], 'effort': spec['effort'],
                           'cwd': str(Path(workspace).resolve()), 'transport': 'native-acp'},
                  'config': configured, 'agent_info': initialized.get('agentInfo')})
        return initialized

    async def prompt(self, prompt):
        request_id = self.request_id + 1
        result = await self.request('session/prompt', {
            'sessionId': self.session_id, 'prompt': [{'type': 'text', 'text': prompt}]})
        self.log({'event': 'result', 'request_id': request_id,
                  'session_id': self.session_id, 'result': result})
        return result


async def terminal_controller(client, prompt, environment, spec, workspace, logs_dir, halt_path):
    """Continue only a normal native end_turn in this same fresh live battle.

    Neither host results nor native DB contents are forwarded to the actor. The
    sole added actor input is the exact fixed neutral string above, once the
    independent host and typed native gates have both passed.
    """
    battle_id = environment.battle_session['session_id']
    result_path = '/admin/sessions/' + battle_id + '/result'
    client.log({'event': 'controller_start', 'schema': CONTROLLER_SCHEMA,
                'session_id': client.session_id, 'battle_session_id': battle_id,
                'host_result_path': result_path, 'model': spec['model'], 'effort': spec['effort'],
                'initial_prompt_sha256': _digest(prompt),
                'neutral_prompt_sha256': _digest(NEUTRAL_CONTINUATION),
                'max_continuations': MAX_NEUTRAL_CONTINUATIONS})
    for prompt_index in range(MAX_NEUTRAL_CONTINUATIONS + 1):
        if prompt_index:
            try:
                check_quota_halt(halt_path)
            except RuntimeError as exc:
                raise ACPError(str(exc)) from exc
        result = await client.prompt(prompt if prompt_index == 0 else NEUTRAL_CONTINUATION)
        reason = result.get('stopReason')
        if reason != 'end_turn':
            client.log({'event': 'controller_stop', 'reason': 'native_stop_' + str(reason),
                        'after_request_id': client.request_id, 'prompt_index': prompt_index})
            return
        gate = {'event': 'continuation_gate', 'schema': CONTROLLER_SCHEMA,
                'session_id': client.session_id, 'battle_session_id': battle_id,
                'host_result_path': result_path, 'model': spec['model'], 'effort': spec['effort'],
                'after_request_id': client.request_id, 'next_prompt_index': prompt_index + 1,
                'decision': 'stop'}
        try:
            check_quota_halt(halt_path)
            if client.flags:
                raise ACPError('Native delegation flag forbids continuation')
            snapshot = await asyncio.to_thread(live_native_snapshot,
                workspace, client.session_id, spec, logs_dir, prompt_index)
            gate['native_snapshot'] = snapshot
            if snapshot['quota_evidence']:
                publish_quota_halt(halt_path, {'quota_evidence': snapshot['quota_evidence']}, workspace)
                raise ACPError('Typed native provider quota error forbids continuation')
            scan = snapshot['scan']
            if scan['status'] != 'complete' or scan['error_steps_inspected']:
                raise ACPError('Incomplete native scan or native runtime error forbids continuation')
            host = await asyncio.to_thread(environment.admin.request, result_path)
            if not isinstance(host, dict):
                raise ACPError('Non-object host result forbids continuation')
            gate.update(host_result=host, host_result_sha256=_json_digest(host))
            if host.get('terminal') is True:
                gate['reason'] = ('true_game_terminal' if host.get('valid') is True
                    and host.get('outcome') in ('win', 'loss') else 'host_terminal_non_game')
            elif host.get('terminal') is False and host.get('outcome') == 'ongoing':
                if prompt_index == MAX_NEUTRAL_CONTINUATIONS:
                    gate['reason'] = 'continuation_limit'
                else:
                    gate.update(decision='continue', reason='same_case_ongoing')
            else:
                raise ACPError('Unexpected host result forbids continuation')
        except (RuntimeError, OSError, sqlite3.Error, ValueError, TypeError) as exc:
            gate.update(reason='gate_failure', error=str(exc))
            client.log(gate)
            raise ACPError(str(exc)) from exc
        client.log(gate)
        if gate['decision'] != 'continue':
            client.log({'event': 'controller_stop', 'reason': gate['reason'],
                        'after_request_id': client.request_id, 'prompt_index': prompt_index})
            return


def validate_controller(events, spec, workspace, logs_dir, *, required=False):
    """Reconcile each prompt/result pair and every host-authorized transition."""
    starts = [(index, event) for index, event in enumerate(events)
              if event.get('event') == 'controller_start']
    results = [(index, event) for index, event in enumerate(events) if event.get('event') == 'result']
    report = {'schema': CONTROLLER_SCHEMA, 'present': bool(starts), 'valid': True,
              'validation_errors': [], 'completed_prompts': len(results),
              'neutral_continuations': 0, 'max_continuations': MAX_NEUTRAL_CONTINUATIONS,
              'stop_cause': None, 'pending_prompt': False, 'true_terminal_observed': False,
              'final_prompt_completed': False, 'requested_prompts': 0,
              'primary_completion_policy': 'verified-game-win-or-loss-only'}
    errors, quota = report['validation_errors'], []
    if not starts:
        if required or len(results) > 1:
            errors.append('Missing terminal controller provenance')
        report['valid'] = not errors
        return report, quota
    if len(starts) != 1:
        errors.append('Multiple terminal controller starts')
        report['valid'] = False
        return report, quota
    start_index, start = starts[0]
    initial = [event for event in events[:start_index] if event.get('event') == 'init']
    session_id = initial[0].get('session_id') if len(initial) == 1 else None
    battle_id = start.get('battle_session_id')
    result_path = '/admin/sessions/' + battle_id + '/result' if isinstance(battle_id, str) else None
    try:
        prompt = (Path(logs_dir) / 'prompt.txt').read_text()
    except OSError:
        prompt = None
    if (start.get('schema') != CONTROLLER_SCHEMA or not session_id
            or start.get('session_id') != session_id or not battle_id
            or start.get('host_result_path') != result_path
            or start.get('model') != spec['model'] or start.get('effort') != spec['effort']
            or prompt is None or start.get('initial_prompt_sha256') != _digest(prompt)
            or start.get('neutral_prompt_sha256') != _digest(NEUTRAL_CONTINUATION)
            or start.get('max_continuations') != MAX_NEUTRAL_CONTINUATIONS):
        errors.append('Invalid terminal controller identity or fixed prompt pins')
    outgoing = [(i, e['rpc']) for i, e in enumerate(events)
        if e.get('event') == 'rpc' and e.get('direction') == 'client'
        and isinstance(e.get('rpc'), dict) and e['rpc'].get('method') == 'session/prompt']
    if any(e.get('direction') == 'client' and (e.get('rpc') or {}).get('method')
           not in (None, 'session/prompt') for e in events[start_index + 1:]):
        errors.append('Unexpected native request after controller start')
    if any(e.get('direction') == 'client' and (e.get('rpc') or {}).get('method')
           in ('session/load', 'session/resume', 'session/fork') for e in events):
        errors.append('Resumed or forked native session is forbidden')
    response_ids = [e['rpc']['id'] for e in events if e.get('direction') == 'server'
                    and isinstance(e.get('rpc'), dict) and 'id' in e['rpc'] and 'method' not in e['rpc']]
    if any(type(value) is not int for value in response_ids) or len(response_ids) != len(set(
            value for value in response_ids if type(value) is int)):
        errors.append('Duplicated native response IDs')
    report['neutral_continuations'] = max(0, len(outgoing) - 1)
    report['requested_prompts'] = len(outgoing)
    if not outgoing or len(outgoing) > MAX_NEUTRAL_CONTINUATIONS + 1:
        errors.append('Invalid number of native prompt requests')
    matched_results, used_gates, request_ids = set(), set(), set()
    previous = None
    for turn, (position, rpc) in enumerate(outgoing):
        request_id = rpc.get('id')
        params = rpc.get('params') or {}
        next_position = outgoing[turn + 1][0] if turn + 1 < len(outgoing) else len(events)
        expected_prompt = prompt if turn == 0 else NEUTRAL_CONTINUATION
        if (position <= start_index or type(request_id) is not int or request_id in request_ids
                or params != {'sessionId': session_id, 'prompt': [{'type': 'text', 'text': expected_prompt}]}):
            errors.append(f'Prompt {turn} identity, text, or request ID differs')
        request_ids.add(request_id)
        responses = [(i, e['rpc']) for i, e in enumerate(events[position + 1:next_position], position + 1)
            if e.get('event') == 'rpc' and e.get('direction') == 'server'
            and isinstance(e.get('rpc'), dict) and e['rpc'].get('id') == request_id
            and 'method' not in e['rpc']]
        matching = [(i, e) for i, e in results if position < i < next_position
                    and e.get('request_id') == request_id and e.get('session_id') == session_id]
        successful = len(responses) == 1 and isinstance(responses[0][1].get('result'), dict) and not responses[0][1].get('error')
        if successful:
            if (len(matching) != 1 or matching[0][0] <= responses[0][0]
                    or matching[0][1].get('result') != responses[0][1]['result']):
                errors.append(f'Prompt {turn} lacks its exact native result correspondence')
            else:
                matched_results.add(matching[0][0])
                if turn == len(outgoing) - 1:
                    report['final_prompt_completed'] = True
        elif (turn != len(outgoing) - 1 or matching or len(responses) > 1):
            errors.append(f'Prompt {turn} has an invalid incomplete response chain')
        else:
            report['pending_prompt'] = not responses
        if turn:
            gates = [(i, e) for i, e in enumerate(events)
                     if e.get('event') == 'continuation_gate'
                     and previous is not None and previous[0] < i < position]
            if len(gates) != 1:
                errors.append(f'Continuation {turn} lacks exactly one preceding host gate')
            else:
                gate_index, gate = gates[0]
                used_gates.add(gate_index)
                host = gate.get('host_result')
                if (previous[2] != 'end_turn' or gate.get('schema') != CONTROLLER_SCHEMA
                        or gate.get('decision') != 'continue' or gate.get('reason') != 'same_case_ongoing'
                        or gate.get('after_request_id') != previous[1] or gate.get('next_prompt_index') != turn
                        or gate.get('session_id') != session_id or gate.get('battle_session_id') != battle_id
                        or gate.get('host_result_path') != result_path or gate.get('model') != spec['model']
                        or gate.get('effort') != spec['effort'] or not isinstance(host, dict)
                        or host.get('terminal') is not False or host.get('outcome') != 'ongoing'
                        or gate.get('host_result_sha256') != _json_digest(host)):
                    errors.append(f'Continuation {turn} host gate is invalid or not ongoing')
                if any(e.get('event') in ('runtime_error', 'delegation_flag')
                       or (e.get('direction') == 'server' and (e.get('rpc') or {}).get('error'))
                       for e in events[:position]):
                    errors.append(f'Continuation {turn} follows a runtime, delegation, or RPC error')
        previous = (matching[0][0] if len(matching) == 1 else position, request_id,
                    matching[0][1].get('result', {}).get('stopReason') if len(matching) == 1 else None)
    if matched_results != {i for i, _ in results}:
        errors.append('Unmatched, duplicated, or unbound native result records')
    # Re-open every immutable host-only snapshot. A claimed scan result is not
    # authority; its contained bytes and typed payloads must independently agree.
    for gate_index, gate in enumerate(events):
        if gate.get('event') != 'continuation_gate':
            continue
        gate_turn = gate.get('next_prompt_index')
        bound = (type(gate_turn) is int and 1 <= gate_turn <= len(outgoing)
                 and gate.get('after_request_id') == outgoing[gate_turn - 1][1].get('id'))
        if (not bound or gate.get('schema') != CONTROLLER_SCHEMA
                or gate.get('session_id') != session_id or gate.get('battle_session_id') != battle_id
                or gate.get('host_result_path') != result_path or gate.get('model') != spec['model']
                or gate.get('effort') != spec['effort']):
            errors.append('Continuation gate is not bound to this native session and battle')
        if bound:
            earlier = [e for i, e in results if outgoing[gate_turn - 1][0] < i < gate_index
                       and e.get('request_id') == gate.get('after_request_id')]
            if len(earlier) != 1 or earlier[0].get('result', {}).get('stopReason') != 'end_turn':
                errors.append('Continuation gate lacks a preceding normal end_turn result')
        snapshot = gate.get('native_snapshot')
        if not isinstance(snapshot, dict):
            if gate_index in used_gates:
                errors.append('Allowed continuation lacks native snapshot evidence')
            continue
        try:
            root = Path(snapshot['workspace'])
            expected_root = Path(logs_dir).resolve() / 'continuation-snapshots' / f"prompt-{gate['next_prompt_index'] - 1:03d}"
            source = Path(workspace).resolve() / '.client-state/gemini/antigravity-cli/conversations' / (session_id + '.db')
            if (root != expected_root or root.resolve() != root or snapshot.get('session_id') != session_id
                    or snapshot.get('source_database') != str(source)
                    or snapshot.get('prompt_index') != gate['next_prompt_index'] - 1):
                raise ValueError('Native continuation snapshot path or identity differs')
            evidence, scan = native_quota_evidence(root, session_id, spec)
            quota.extend(evidence)
            if scan != snapshot.get('scan') or evidence != snapshot.get('quota_evidence'):
                raise ValueError('Native continuation snapshot bytes or typed evidence differ')
            if gate_index in used_gates and (scan['status'] != 'complete'
                    or scan['error_steps_inspected'] != 0 or evidence):
                raise ValueError('Allowed continuation follows native error or incomplete evidence')
        except (KeyError, OSError, TypeError, ValueError) as exc:
            errors.append(str(exc))
    stops = [e for e in events if e.get('event') == 'controller_stop']
    if len(stops) > 1:
        errors.append('Multiple terminal controller stops')
    report['stop_cause'] = stops[-1].get('reason') if stops else None
    if stops and outgoing:
        stop_index = next(i for i, e in enumerate(events) if e is stops[-1])
        if (stop_index <= outgoing[-1][0] or stops[-1].get('after_request_id') != outgoing[-1][1].get('id')
                or stops[-1].get('prompt_index') != len(outgoing) - 1):
            errors.append('Controller stop is not bound to the last native prompt')
        last_results = [e.get('result') or {} for i, e in results if i > outgoing[-1][0]]
        last_reason = last_results[-1].get('stopReason') if last_results else None
        stop_gates = [e for i, e in enumerate(events) if e.get('event') == 'continuation_gate'
                      and outgoing[-1][0] < i < stop_index]
        if last_reason == 'end_turn':
            if len(stop_gates) != 1 or stop_gates[0].get('decision') != 'stop' or stop_gates[0].get('reason') != report['stop_cause']:
                errors.append('Normal controller stop lacks its exact denied continuation gate')
            else:
                gate = stop_gates[0]
                host = gate.get('host_result') or {}
                if not isinstance(host, dict):
                    errors.append('Controller stop host result is not an object')
                    host = {}
                true_terminal = (host.get('valid') is True and host.get('terminal') is True
                                 and host.get('outcome') in ('win', 'loss'))
                report['true_terminal_observed'] = true_terminal
                if (report['stop_cause'] == 'true_game_terminal') != true_terminal:
                    errors.append('Controller stop misstates the observed game terminal')
                if report['stop_cause'] == 'continuation_limit' and (len(outgoing) != MAX_NEUTRAL_CONTINUATIONS + 1
                        or host.get('terminal') is not False or host.get('outcome') != 'ongoing'):
                    errors.append('Continuation limit stop is not an ongoing capped chain')
                if host and gate.get('host_result_sha256') != _json_digest(host):
                    errors.append('Controller stop host result hash differs')
        elif report['stop_cause'] != 'native_stop_' + str(last_reason):
            errors.append('Controller stop does not match the native stop reason')
    report['valid'] = not errors
    return report, quota


def _usage(value):
    if not isinstance(value, dict):
        return None
    aliases = {'input_tokens': ('input_tokens', 'inputTokens'),
               'output_tokens': ('output_tokens', 'outputTokens'),
               'cache_read_tokens': ('cache_read_tokens', 'cacheReadTokens', 'cachedReadTokens', 'cached_input_tokens'),
               'thinking_tokens': ('thinking_tokens', 'thinkingTokens', 'thoughtTokens'),
               'total_tokens': ('total_tokens', 'totalTokens')}
    result = {}
    for target, keys in aliases.items():
        for key in keys:
            if type(value.get(key)) is int and value[key] >= 0:
                result[target] = value[key]
                break
    return result if 'input_tokens' in result or 'output_tokens' in result else None


def parse_events(path, spec, workspace, *, require_native_quota_scan=False, require_controller=False):
    """Parse our recorded ACP stream with strict selected-model evidence."""
    events, errors, flags, models, usage_snapshots = [], [], [], set(), []
    invalid = 0
    for line in Path(path).read_text(errors='replace').splitlines():
        try:
            event = json.loads(line)
            if not isinstance(event, dict):
                raise ValueError('not an object')
            events.append(event)
        except ValueError:
            invalid += 1
    initial = [e for e in events if e.get('event') == 'init']
    mismatch = invalid > 0 or len(initial) != 1
    if initial:
        init = initial[0].get('init') or {}
        models.add(init.get('model', ''))
        mismatch |= (init.get('model') != spec['model'] or init.get('effort') != spec['effort']
                     or init.get('cwd') != str(Path(workspace).resolve()))
        try:
            verified_selection(initial[0].get('config') or {}, spec)
        except ValueError:
            mismatch = True
    results = []
    quota_evidence = []
    for event in events:
        if event.get('event') == 'delegation_flag':
            flags.append(event.get('reason'))
        if event.get('event') == 'config_update':
            try:
                verified_selection(event.get('config') or {}, spec)
            except ValueError:
                mismatch = True
        if event.get('event') == 'result':
            results.append(event.get('result') or {})
        rpc = event.get('rpc') or {}
        if event.get('direction') == 'server' and rpc.get('error'):
            errors.append(rpc['error'])
            encoded = json.dumps(rpc['error']).lower()
            matches = [code for code in QUOTA_CODES if code in encoded]
            if matches:
                quota_evidence.append({'source': 'native_acp_error', 'matched_codes': matches})
        if event.get('event') == 'runtime_error':
            errors.append(event.get('error'))
        # ACP reports cumulative usage when available. Never add step snapshots
        # and terminal counters together. Context-window `used` is not usage.
        candidates = [event.get('result', {}).get('usage'), rpc.get('result', {}).get('usage')]
        update = (rpc.get('params') or {}).get('update') or {}
        candidates += [update.get('usage')]
        for candidate in candidates:
            normalized = _usage(candidate)
            if normalized:
                usage_snapshots.append(normalized)
    controller, controller_quota = validate_controller(events, spec, workspace, Path(path).parent,
                                                      required=require_controller)
    terminal = (bool(results) and results[-1].get('stopReason') in ('end_turn', 'refusal')
                and (not controller['present'] or (controller['valid'] and controller['final_prompt_completed'])))
    budget = any(r.get('stopReason') in ('max_tokens', 'max_turn_requests') for r in results)
    mismatch |= not controller['valid']
    native_evidence, native_scan = native_quota_evidence(
        workspace, initial[0].get('session_id') if len(initial) == 1 else None, spec)
    quota_evidence.extend(native_evidence)
    quota_evidence.extend(controller_quota)
    return {'terminal_event': terminal, 'model_mismatch': bool(mismatch),
            'observed_models': sorted(models - {''}), 'initial_models': sorted(models - {''}),
            'actor_models': [], 'assistant_message_count': None,
            'missing_actor_model_evidence': True,
            'model_evidence_note': 'Native ACP selected model and effort; not independent server-side actor attestation.',
            'usage': usage_snapshots[-1:] if usage_snapshots else [],
            'usage_scope': 'Latest native cumulative usage when provided; otherwise unknown. Context-window usage is excluded.',
            'aggregate_usage_models': [], 'per_model_usage': [], 'auxiliary_models': [],
            'allowed_auxiliary_models': [], 'unrecognized_usage_models': [],
            'auxiliary_model_review_required': False, 'auxiliary_model_policy': 'no-deliberate-delegation',
            'subagent_stats': [], 'delegation_tool_calls': flags,
            'delegation_violation': bool(flags), 'delegation_review_required': bool(flags),
            'errors': errors, 'quota_blocked': bool(quota_evidence), 'quota_evidence': quota_evidence,
            'native_quota_scan': native_scan,
            'controller_lifecycle': controller,
            'native_evidence_error': (native_scan.get('error', 'Incomplete native quota evidence scan')
                                      if require_native_quota_scan and native_scan['status'] != 'complete' else None),
            'budget_exhausted': budget, 'refused': any(r.get('stopReason') == 'refusal' for r in results),
            'malformed_event_lines': invalid}


class AntigravityAgent(BaseAgent):
    def __init__(self, *args, effort=None, cli_pin=None, quota_halt_path=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.spec = resolve_model(self.model_name)
        if effort is not None and effort != self.spec['effort']:
            raise ValueError('Requested effort differs from the pinned Antigravity variant')
        self.effort = self.spec['effort']
        self.quota_halt_path = Path(quota_halt_path) if quota_halt_path is not None else DEFAULT_QUOTA_HALT_PATH
        self.cli_pin = cli_pin or cli_identity('agy')
        if self.cli_pin.get('provider') != 'agy':
            raise ValueError('Antigravity requires its own native CLI pin')

    @staticmethod
    def name():
        return 'cynthia-antigravity-native-acp'

    def version(self):
        return self.cli_pin['version']

    async def setup(self, environment):
        if not getattr(environment, 'workspace', None) or not getattr(environment, 'battle_session', None):
            raise RuntimeError('AntigravityAgent requires CynthiaEnvironment')
        if not Path('/usr/bin/sandbox-exec').exists():
            raise RuntimeError('AntigravityAgent requires the strict macOS Seatbelt boundary')

    async def run(self, instruction, environment, context: AgentContext):
        halt_path = getattr(self, 'quota_halt_path', DEFAULT_QUOTA_HALT_PATH)
        check_quota_halt(halt_path)
        workspace = Path(environment.workspace).resolve()
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        prepare_state(workspace)
        child_env = isolated_agent_env(workspace)
        pin_before = await asyncio.to_thread(validate_cli_pin, self.cli_pin)
        profile = self.logs_dir / 'isolation.sb'
        profile.write_text(strict_sandbox_profile(
            workspace, urlsplit(environment.admin.url).port,
            executable=Path(self.cli_pin['executable']),
            calculation_python=Path(child_env['CYNTHIA_PYTHON']), allow_keychain=True,
            allow_ephemeral_loopback_listener=True))
        prompt = instruction + '\n\nRead CONTROLS.md and team.json in the current workspace, then use battle.py and the live observations. Keep scratch files in this workspace.\n'
        (self.logs_dir / 'prompt.txt').write_text(prompt)
        cmd = ['/usr/bin/sandbox-exec', '-f', str(profile),
               *command(self.spec, workspace, self.cli_pin['executable'])]
        metadata = {'requested_model': self.spec['model'], 'effort': self.effort,
                    'cli_version': self.version(), 'cli_pin': self.cli_pin, 'command': cmd,
                    'authentication': 'existing native keychain login; API environment absent; useG1Credits false',
                    'context_policy': 'fresh-trial-local-client-state-no-resume-no-cross-trial-access',
                    'completion_policy': 'verified-game-win-or-loss-only; all other stops remain pending',
                    'continuation_policy': {'schema': CONTROLLER_SCHEMA,
                        'same_fresh_native_session': True, 'max_neutral_continuations': MAX_NEUTRAL_CONTINUATIONS,
                        'neutral_prompt_sha256': _digest(NEUTRAL_CONTINUATION)},
                    'calculation_python': calculation_python_identity(child_env['CYNTHIA_PYTHON']),
                    'cost_usd': None, 'model_evidence_note': 'Native client configuration only; no remote actor attestation.'}
        (self.logs_dir / 'invocation.json').write_text(json.dumps(metadata, indent=2) + '\n')
        events = self.logs_dir / 'events.jsonl'
        events.touch(exist_ok=False)
        cancelled, failure, process = False, None, None
        with (self.logs_dir / 'stderr.log').open('w') as stderr:
            try:
                check_quota_halt(halt_path)
                process = await asyncio.create_subprocess_exec(
                    *cmd, cwd=workspace, env=child_env, stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE, stderr=stderr, start_new_session=True,
                    limit=16 * 1024 * 1024)
                (self.logs_dir / 'pid.json').write_text(json.dumps({'pid': process.pid}) + '\n')
                client = ACPClient(process, self.logs_dir)
                await client.start(workspace, self.spec)
                await terminal_controller(client, prompt, environment, self.spec,
                                          workspace, self.logs_dir, halt_path)
            except asyncio.CancelledError:
                cancelled = True
                raise
            except (ACPError, ValueError, OSError, ConnectionError) as exc:
                failure = str(exc)
                with events.open('a') as output:
                    output.write(json.dumps({'event': 'runtime_error', 'error': failure}) + '\n')
            finally:
                cleanup_error = None
                if process and process.returncode is None:
                    try:
                        await stop_process(process)
                    except (OSError, ACPError) as exc:
                        cleanup_error = str(exc)
                        failure = failure or cleanup_error
                audit = parse_events(events, self.spec, workspace, require_native_quota_scan=True,
                                     require_controller=True)
                if audit['native_evidence_error']:
                    failure = failure or audit['native_evidence_error']
                if audit['quota_blocked']:
                    try:
                        audit['quota_halt_path'] = publish_quota_halt(halt_path, audit, workspace)
                    except OSError as exc:
                        audit['quota_halt_error'] = str(exc)
                        failure = failure or f'Cannot publish provider quota halt: {exc}'
                audit.update(return_code=process.returncode if process else None, cancelled=cancelled,
                             cli_pin_before=pin_before, calculation_python=metadata['calculation_python'])
                if cleanup_error:
                    audit['cleanup_error'] = cleanup_error
                try:
                    audit['cli_pin_after'] = await asyncio.to_thread(validate_cli_pin, self.cli_pin)
                except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
                    audit['cli_pin_error'] = str(exc)
                delegation = audit_agent_logs(self.logs_dir)
                delegation['native_delegation_flags'] = audit['delegation_tool_calls']
                delegation['requires_manual_review'] |= audit['delegation_violation']
                audit['delegation_review_required'] = delegation['requires_manual_review']
                (self.logs_dir / 'delegation-audit.json').write_text(json.dumps(delegation, indent=2) + '\n')
                environment.agent_audit = audit
                context.metadata = {**metadata, **audit}
                usage = audit['usage'][-1] if audit['usage'] else {}
                context.n_input_tokens = usage.get('input_tokens')
                context.n_output_tokens = usage.get('output_tokens')
                context.n_cache_tokens = usage.get('cache_read_tokens')
                normal_stop = audit['terminal_event'] or audit['budget_exhausted']
                environment.finalize_reason = ('timeout' if audit['budget_exhausted'] else 'agent_stopped') if normal_stop and not failure else 'runtime_error'
                if cancelled:
                    environment.framework_cancellation_pending = True
                    environment.agent_context = context
                    audit['cancellation_classification'] = 'pending_framework_exception'
                if audit['quota_blocked'] or audit['model_mismatch'] or audit['delegation_violation'] or audit.get('cli_pin_error'):
                    environment.finalize_reason = 'runtime_error'
                (self.logs_dir / 'runtime-audit.json').write_text(json.dumps(audit, indent=2) + '\n')
        # A provider exit cannot authorize replay of an already completed game.
        completed = False
        invalid_actor = audit['model_mismatch'] or audit['delegation_violation'] or audit.get('cli_pin_error')
        normal_stop = audit['terminal_event'] or audit['budget_exhausted']
        if not invalid_actor and (failure or audit['quota_blocked'] or not normal_stop):
            result = await asyncio.to_thread(environment.admin.request,
                '/admin/sessions/' + environment.battle_session['session_id'] + '/result')
            completed = (result.get('valid') is True and result.get('terminal') is True
                         and result.get('outcome') in ('win', 'loss'))
            if completed:
                audit['provider_failure_after_terminal'] = True
                context.metadata = {**(context.metadata or {}), **audit}
                (self.logs_dir / 'runtime-audit.json').write_text(json.dumps(audit, indent=2) + '\n')
        if invalid_actor or (not completed and (failure or audit['quota_blocked'] or not normal_stop)):
            raise RuntimeError('Antigravity native session did not complete cleanly; preserve evidence and inspect runtime-audit.json')
