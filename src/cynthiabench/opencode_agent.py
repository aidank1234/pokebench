"""Fresh OpenCode V1 native Flash sessions, with host-owned API accounting.

The host proxy owns the account credential and enforces the exact model/effort
and shared spending limit. Native tools and context compaction stay in OpenCode.
Native selection is configuration evidence, not independent model attestation.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import signal
import subprocess
import tempfile
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen

from harbor.agents.base import BaseAgent
from harbor.models.agent.context import AgentContext

from .delegation_audit import AI_COMMAND, audit_agent_logs
from .native_runtime import cli_identity, validate_cli_pin, calculation_python_identity
from .opencode_isolation import isolated_agent_env, strict_sandbox_profile, CALCULATION_PYTHON

MODEL = 'deepseek-flash'
NATIVE_MODEL = 'deepseek/deepseek-flash'
ADVERTISED_MODEL_VERSION = 'DeepSeek-V4.1-Flash-1017'
OUTPUT_TOKEN_MAX = 393216
DEFAULT_HALT_PATH = Path(__file__).resolve().parents[2] / 'work/provider-budget-halt.json'
FORBIDDEN_TOOLS = frozenset({'task', 'skill', 'plan_enter', 'plan_exit', 'question',
                           'experimental_image_generate', 'session', 'sessions', 'subagent'})
EXTRA_AI_COMMAND = re.compile(r'(?<![\w-])(?:opencode|agy)(?=\s|$)|api\.deepseek\.com', re.I)


def check_quota_halt(path=None):
    path = Path(path) if path is not None else DEFAULT_HALT_PATH
    if path.exists() or path.is_symlink():
        raise RuntimeError('Provider budget/quota halt is active: ' + str(path))


def publish_halt(path, reason):
    """Latch a host accounting/revocation failure without replacing evidence."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile('w', dir=path.parent, prefix='.provider-halt-', delete=False) as output:
            temporary = Path(output.name)
            json.dump({'reason': reason, 'source': 'opencode_host_adapter',
                       'detected_at': datetime.now(timezone.utc).isoformat()}, output)
            output.flush()
            os.fsync(output.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            pass
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)


def command(workspace, executable, prompt, title='pokebench-preflight'):
    """Build args; caller MUST send prompt verbatim over stdin, then close it.

    Pinned V1 quotes/escapes positional arguments that contain spaces. Stdin is
    its documented raw prompt transport and preserves the original instruction.
    """
    return [str(executable), 'run', '--pure', '--format', 'json', '--model', NATIVE_MODEL,
            '--variant', 'max', '--agent', 'build', '--title', title, '--auto', '--thinking',
            '--dir', str(Path(workspace).resolve())]


def prepare_state(workspace, base_url):
    """Pin V1's provider schema and refuse inherited conversations or settings."""
    root = Path(workspace).resolve() / '.opencode-state'
    if root.is_symlink() or (root.exists() and any(p.is_file() or p.is_symlink() for p in root.rglob('*'))):
        raise ValueError('OpenCode state must be fresh; no history or resume is allowed')
    disabled = {name: {'disable': True} for name in ('plan', 'general', 'explore', 'title', 'summary')}
    model = {'id': MODEL, 'name': ADVERTISED_MODEL_VERSION, 'attachment': True,
             'reasoning': True, 'tool_call': True, 'interleaved': {'field': 'reasoning_content'},
             'reasoning_options': [{'type': 'effort', 'values': ['max']}],
             'modalities': {'input': ['text', 'image'], 'output': ['text']},
             'limit': {'context': 1048576, 'output': 393216},
             'options': {'reasoningEffort': 'max', 'thinking': {'type': 'enabled'}},
             'variants': {'max': {'reasoningEffort': 'max', 'thinking': {'type': 'enabled'}}}}
    settings = {'$schema': 'https://opencode.ai/config.json', 'model': NATIVE_MODEL,
        'small_model': NATIVE_MODEL, 'default_agent': 'build', 'enabled_providers': ['deepseek'],
        'autoupdate': False, 'share': 'disabled', 'snapshot': False, 'plugin': [],
        'instructions': [], 'mcp': {}, 'lsp': False, 'formatter': False, 'subagent_depth': 0,
        'permission': {'*': 'allow', 'task': 'deny', 'skill': 'deny', 'question': 'deny',
                       'plan_enter': 'deny', 'plan_exit': 'deny', 'external_directory': 'deny'},
        'tools': {name: False for name in FORBIDDEN_TOOLS},
        'agent': {**disabled,
            'build': {'model': NATIVE_MODEL, 'variant': 'max', 'mode': 'primary',
                      'permission': {'task': 'deny', 'skill': 'deny'}},
            'compaction': {'model': NATIVE_MODEL, 'variant': 'max',
                           'options': {'reasoningEffort': 'max', 'thinking': {'type': 'enabled'}}}},
        'provider': {'deepseek': {'npm': '@ai-sdk/openai-compatible',
            'env': ['DEEPSEEK_API_KEY'], 'whitelist': [MODEL],
            'options': {'baseURL': base_url, 'apiKey': '{env:DEEPSEEK_API_KEY}'},
            'models': {MODEL: model}}}}
    destination = root / 'config/opencode/opencode.json'
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(settings, indent=2) + '\n')
    return settings


def prepare_client(workspace, model=MODEL, effort='max', *, proxy_url, proxy_token,
                   max_output_tokens=OUTPUT_TOKEN_MAX):
    """Shared smoke/scored setup; no inference or subprocess is performed."""
    if model not in ('flash', MODEL, NATIVE_MODEL) or effort != 'max':
        raise ValueError('Fresh native client requires exact Flash/max')
    if type(max_output_tokens) is not int or max_output_tokens != OUTPUT_TOKEN_MAX:
        raise ValueError('This separate experiment pins the native output limit to 393216')
    settings = prepare_state(workspace, proxy_url)
    env = isolated_agent_env(workspace, proxy_token, max_output_tokens=max_output_tokens)
    return {'settings': settings, 'env': env, 'model': MODEL, 'native_model': NATIVE_MODEL,
            'effort': 'max', 'max_output_tokens': max_output_tokens,
            'state_root': str(Path(workspace).resolve() / '.opencode-state')}


def proxy_request(config_path, path, body=None):
    """Host-only administrator request; no account/admin secret is logged."""
    config = json.loads(Path(config_path).read_text())
    parsed = urlsplit(config['url'])
    if parsed.scheme != 'http' or parsed.hostname not in ('127.0.0.1', 'localhost') or not parsed.port:
        raise ValueError('Model proxy administrator URL must be explicit loopback HTTP')
    if parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ('', '/'):
        raise ValueError('Invalid model proxy administrator URL')
    raw = json.dumps(body).encode() if body is not None else None
    request = Request(config['url'].rstrip('/') + path, data=raw,
        headers={'Authorization': 'Bearer ' + config['admin_token'], 'Content-Type': 'application/json'})
    with urlopen(request, timeout=30) as response:
        value = json.load(response)
    if not isinstance(value, dict):
        raise ValueError('Model proxy did not return a structured object')
    return value


def validate_registration(value, trial_id):
    parsed = urlsplit(value.get('base_url', ''))
    if (value.get('trial_id') != trial_id or value.get('model') != MODEL
            or value.get('reasoning_effort') != 'max' or value.get('max_tokens') != 393216
            or not isinstance(value.get('token'), str) or not value['token']
            or parsed.scheme != 'http' or parsed.hostname not in ('127.0.0.1', 'localhost')
            or not parsed.port or parsed.path != '/v1' or parsed.username or parsed.password
            or parsed.query or parsed.fragment):
        raise ValueError('Model proxy did not confirm scoped Flash/max registration')
    return parsed.port


def proxy_failure_partition_valid(status):
    names = ('hard_failures', 'unresolved_transient_failures', 'recovered_transient_failures')
    if not any(name in status for name in names):
        return True
    if any(not isinstance(status.get(name), list) for name in (*names, 'typed_failures')):
        return False
    combined = [failure for name in names for failure in status[name]]
    return sorted(json.dumps(value, sort_keys=True) for value in status['typed_failures']) == sorted(
        json.dumps(value, sort_keys=True) for value in combined)


def recovered_transport_evidence(status):
    """Verify explicit same-request recovery without erasing unknown liability.

    This accepts host-classified recovery only. It never scans actor prose or
    treats a later unrelated successful request as proof of transport recovery.
    """
    failures = status.get('typed_failures')
    recovered = status.get('recovered_transient_failures')
    if (not isinstance(failures, list) or not failures or not isinstance(recovered, list)
            or not recovered or status.get('hard_failures') != []
            or status.get('unresolved_transient_failures') != []):
        return False
    if sorted(map(lambda value: json.dumps(value, sort_keys=True), failures)) != sorted(
            map(lambda value: json.dumps(value, sort_keys=True), recovered)):
        return False
    usage = status.get('usage')
    if not isinstance(usage, list):
        return False
    successful = {item.get('request_id'): item for item in usage if isinstance(item, dict)}
    seen, liability = set(), 0.0
    for failure in recovered:
        if not isinstance(failure, dict):
            return False
        ident, semantic = failure.get('request_id'), failure.get('semantic_request_sha256')
        recovery = successful.get(failure.get('recovered_by_request_id'))
        if (not isinstance(ident, str) or not ident or ident in seen
                or failure.get('recoverable') is not True or failure.get('recovery_status') != 'recovered'
                or not isinstance(semantic, str) or not re.fullmatch(r'[0-9a-f]{64}', semantic)
                or not isinstance(recovery, dict) or recovery.get('request_id') == ident
                or recovery.get('semantic_request_sha256') != semantic
                or type(failure.get('consecutive_attempt_index')) is not int
                or not 1 <= failure['consecutive_attempt_index'] <= 5):
            return False
        try:
            recovered_at = datetime.fromisoformat(failure['recovered_at'].replace('Z', '+00:00'))
            value = float(failure['retained_liability_upper_usd'])
            if recovered_at.tzinfo is None or not math.isfinite(value) or value <= 0:
                return False
        except (ValueError, TypeError, KeyError, AttributeError):
            return False
        seen.add(ident)
        liability += value
    try:
        outstanding = float(status['outstanding_financial_liability_usd'])
        recovered_upper = float(status['recovered_transient_unknown_upper_usd'])
        if (not all(math.isfinite(value) for value in (outstanding, recovered_upper))
                or min(outstanding, recovered_upper) + 1e-12 < liability):
            return False
    except (ValueError, TypeError, KeyError):
        return False
    return (status.get('request_usage_complete') is False and status.get('usage_uncertainty') is True
            and status.get('liability_bounds_valid') is True)


def proxy_status_error(status, trial_id):
    if not isinstance(status, dict) or status.get('trial_id') != trial_id:
        return 'Missing or wrong-trial model proxy accounting'
    requested, completed, inflight = (status.get(k) for k in
        ('model_request_count', 'completed_request_count', 'inflight'))
    if any(type(n) is not int or n < 0 for n in (requested, completed, inflight)):
        return 'Invalid model proxy request counters'
    if requested < 1 or completed != requested or inflight:
        return 'Incomplete model proxy request accounting'
    if not proxy_failure_partition_valid(status):
        return 'Inconsistent model proxy failure classification'
    recovered = recovered_transport_evidence(status)
    if not isinstance(status.get('typed_failures'), list) or (status['typed_failures'] and not recovered):
        return 'Model proxy recorded typed provider failures'
    if ((status.get('usage_uncertainty') is not False and not recovered)
            or status.get('budget_halted') is not False):
        return 'Model proxy usage is uncertain or its budget is halted'
    if status.get('model_mismatch') is not False:
        return 'Model proxy cannot confirm exact response identity'
    if not isinstance(status.get('usage'), list) or not status['usage']:
        return 'Missing model proxy usage evidence'
    if recovered and requested != len(status['usage']) + len(status['recovered_transient_failures']):
        return 'Unreconciled recovered attempts and known model proxy usage'
    if status.get('request_usage_complete') is False and not recovered:
        return 'Incomplete model proxy token totals without verified transport recovery'
    return None


def proxy_usage(status):
    """Canonical provider totals; cached/reasoning counts are included subsets."""
    total, seen = {}, set()
    for item in status.get('usage', []):
        if not isinstance(item, dict):
            raise ValueError('Invalid raw provider usage record')
        ident, raw = item.get('request_id'), item.get('raw_provider_usage')
        if not isinstance(ident, str) or ident in seen or not isinstance(raw, dict):
            raise ValueError('Missing or duplicate raw provider request usage')
        seen.add(ident)
        prompt_details, completion_details = raw.get('prompt_tokens_details') or {}, raw.get('completion_tokens_details') or {}
        if not isinstance(prompt_details, dict) or not isinstance(completion_details, dict):
            raise ValueError('Invalid provider token detail records')
        cached = raw.get('prompt_cache_hit_tokens', prompt_details.get('cached_tokens', 0))
        reasoning = completion_details.get('reasoning_tokens', 0)
        values = {'input_tokens': raw.get('prompt_tokens'), 'output_tokens': raw.get('completion_tokens'),
                  'cache_read_tokens': cached, 'thinking_tokens': reasoning,
                  'total_tokens': raw.get('total_tokens')}
        if any(type(value) is not int or value < 0 for value in values.values()):
            raise ValueError('Invalid raw provider token accounting')
        if (values['input_tokens'] + values['output_tokens'] != values['total_tokens']
                or cached > values['input_tokens'] or reasoning > values['output_tokens']):
            raise ValueError('Inconsistent raw provider token accounting')
        for key, value in values.items():
            total[key] = total.get(key, 0) + value
    return [total] if total else []


def cancellation_transport_only(status, audit):
    """Evidence for a later *framework-confirmed* deadline, never a deadline claim.

    Financial uncertainty remains reserved. Genuine upstream errors,
    pre-cancellation failures, wrong actors and undocumented native history do
    not receive this exception. Operator cancellation stays an infrastructure
    interruption when Harbor resolves the pending framework classification.
    """
    if (not isinstance(status, dict) or status.get('cancellation_requested') is not True
            or status.get('pre_cancellation_provider_failure') is not False
            or status.get('pre_cancellation_infrastructure_failure') is not False
            or status.get('model_mismatch') is not False
            or audit.get('model_mismatch') or audit.get('delegation_violation')
            or audit.get('cli_pin_error') or audit.get('native_evidence_error')):
        return False
    if not proxy_failure_partition_valid(status):
        return False
    failures = status.get('typed_failures')
    if not isinstance(failures, list):
        return False
    recovered = status.get('recovered_transient_failures', [])
    if recovered:
        recovery_only = {**status, 'typed_failures': recovered,
                         'hard_failures': [], 'unresolved_transient_failures': []}
        if not recovered_transport_evidence(recovery_only):
            return False
    if any(not isinstance(f, dict) or (f not in recovered and f.get('cancellation_consequence') is not True) for f in failures):
        return False
    if (status.get('usage_uncertainty') and status.get('cancellation_usage_uncertainty') is not True
            and not (recovered and all(f in recovered for f in failures))):
        return False
    if any(not isinstance(e, dict) or e.get('name') != 'MessageAbortedError' for e in audit.get('errors', [])):
        return False
    return True


async def stop_process(process):
    if process.stdin:
        process.stdin.close()
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
    raise RuntimeError('OpenCode process did not exit after cleanup')


async def finish_cleanup(awaitable, on_cancel):
    """Finish bounded host cleanup before propagating its owner's cancellation.

    Shield a separate task, not just an individual wait: repeated cancellation
    must not interrupt export, process reaping, accounting or credential revoke.
    The caller records cancellation and re-raises it after all cleanup finishes.
    """
    task = asyncio.create_task(awaitable)
    while True:
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            on_cancel()
            if task.cancelled():
                raise RuntimeError('OpenCode host cleanup task was unexpectedly cancelled')


def _events(path):
    events, malformed = [], 0
    for line in Path(path).read_text(errors='replace').splitlines():
        try:
            event = json.loads(line)
            if not isinstance(event, dict):
                raise ValueError('Not an object')
            events.append(event)
        except ValueError:
            malformed += 1
    return events, malformed


def session_id_from_events(path):
    events, malformed = _events(path)
    ids = {event.get('sessionID') for event in events if event.get('sessionID')}
    if malformed or len(ids) != 1:
        raise ValueError('Native stream lacks a single fresh session identifier')
    value = ids.pop()
    if not isinstance(value, str) or not re.fullmatch(r'ses_[A-Za-z0-9_-]+', value):
        raise ValueError('Unsafe native session identifier')
    return value


def _tokens(value):
    result = {}
    for key, target in [('input', 'input_tokens'), ('output', 'output_tokens'),
                        ('reasoning', 'thinking_tokens'), ('total', 'total_tokens')]:
        if type(value.get(key)) in (int, float) and value[key] >= 0:
            result[target] = value[key]
    cache = value.get('cache') or {}
    if type(cache.get('read')) in (int, float) and cache['read'] >= 0:
        result['cache_read_tokens'] = cache['read']
    return result


def parse_events(path, export_path, workspace, prompt):
    """Use full native export for identity; never infer quota from actor prose."""
    events, malformed = _events(path)
    errors = [e.get('error') for e in events if e.get('type') == 'error']
    flags, models, usage, assistants, images = [], set(), [], [], 0
    user_count, real_user_count, unknown_continuations = 0, 0, []
    evidence_error, mismatch, session_id = None, bool(malformed), None
    try:
        session_id = session_id_from_events(path)
        export = json.loads(Path(export_path).read_text())
        info, messages = export['info'], export['messages']
        if info.get('id') != session_id or info.get('parentID') or info.get('directory') != str(Path(workspace).resolve()):
            mismatch = True
        if not isinstance(messages, list) or not messages:
            raise ValueError('Native export contains no messages')
        ids = set()
        first_user = None
        for message in messages:
            msg, parts = message['info'], message['parts']
            if not isinstance(msg.get('id'), str) or msg['id'] in ids or msg.get('sessionID') != session_id:
                raise ValueError('Duplicate or cross-session native message')
            ids.add(msg['id'])
            if msg.get('role') == 'user':
                user_count += 1
                real_text = [p for p in parts if p.get('type') == 'text' and not p.get('synthetic')]
                real_user_count += bool(real_text)
                if first_user is None:
                    first_user = message
                    selected = msg.get('model') or {}
                    mismatch |= (selected.get('providerID') != 'deepseek' or selected.get('modelID') != MODEL
                                 or selected.get('variant', msg.get('variant')) != 'max' or msg.get('agent') != 'build')
                elif not parts or any(p.get('type') != 'compaction' and not
                        (p.get('type') == 'text' and p.get('synthetic') is True) for p in parts):
                    unknown_continuations.append(msg['id'])
            elif msg.get('role') == 'assistant':
                assistants.append(msg)
                models.add(msg.get('modelID', ''))
                mismatch |= (msg.get('providerID') != 'deepseek' or msg.get('modelID') != MODEL
                             or msg.get('variant') != 'max'
                             or (msg.get('path') or {}).get('cwd') != str(Path(workspace).resolve()))
                if msg.get('agent') not in ('build', 'compaction'):
                    flags.append({'source': 'native_export', 'reason': 'unexpected_agent', 'agent': msg.get('agent')})
                if msg.get('error'):
                    errors.append(msg['error'])
                usage.append(_tokens(msg.get('tokens') or {}))
            else:
                raise ValueError('Unrecognized native message role')
            for part in parts:
                attachments = ([part] if part.get('type') == 'file' else []) + ((part.get('state') or {}).get('attachments') or [])
                images += sum(isinstance(a, dict) and str(a.get('mime', '')).startswith('image/') for a in attachments)
                if part.get('type') in ('subtask', 'agent'):
                    flags.append({'source': 'native_export', 'reason': 'delegation_part', 'type': part['type']})
                if part.get('type') == 'tool':
                    name = part.get('tool')
                    if name in FORBIDDEN_TOOLS or str(name).lower().startswith(('mcp', 'subagent')):
                        flags.append({'source': 'native_export', 'reason': 'prohibited_tool', 'tool': name})
                    raw = (part.get('state') or {}).get('input') or {}
                    cmd = raw.get('command') if isinstance(raw, dict) else None
                    if cmd and (AI_COMMAND.search(cmd) or EXTRA_AI_COMMAND.search(cmd)):
                        flags.append({'source': 'native_export', 'reason': 'possible_ai_command', 'command': cmd[:8000]})
        if first_user is None or not assistants:
            raise ValueError('Missing native user or actor evidence')
        first_text = [p.get('text', '') for p in first_user['parts'] if p.get('type') == 'text' and not p.get('synthetic')]
        mismatch |= first_text != [prompt]
        if real_user_count != 1 or unknown_continuations:
            raise ValueError('Native export has an additional or undocumented user continuation; review required')
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        evidence_error = str(exc)
    last = assistants[-1] if assistants else {}
    finish = last.get('finish')
    terminal = finish in ('stop', 'content-filter') and bool((last.get('time') or {}).get('completed'))
    budget = finish == 'length' and bool((last.get('time') or {}).get('completed'))
    normalized = {key: sum(item.get(key, 0) for item in usage) for key in set().union(*(item.keys() for item in usage))} if usage else {}
    return {'terminal_event': terminal, 'budget_exhausted': budget, 'model_mismatch': bool(mismatch),
        'session_id': session_id, 'native_evidence_error': evidence_error,
        'observed_models': sorted(models - {''}), 'initial_models': [MODEL] if assistants else [],
        'actor_models': sorted(models - {''}), 'assistant_message_count': len(assistants),
        'missing_actor_model_evidence': not bool(assistants),
        'model_evidence_note': 'Pinned OpenCode selected model and full native actor export, plus exact-model host proxy requests; not independent backend-weight attestation.',
        'usage': [normalized] if normalized else [], 'native_message_usage': usage,
        'usage_scope': 'Native-only counters: input excludes cache reads; output excludes reasoning. Native per-message counts are summed once, without event/step counters. The host adapter replaces scored context totals with inclusive raw provider counters.',
        'aggregate_usage_models': [], 'per_model_usage': [], 'auxiliary_models': [],
        'allowed_auxiliary_models': [], 'unrecognized_usage_models': [],
        'auxiliary_model_review_required': False, 'auxiliary_model_policy': 'native-same-model-compaction-only',
        'native_compaction_count': sum(a.get('agent') == 'compaction' for a in assistants),
        'native_image_attachment_count': images,
        'native_user_message_count': user_count, 'real_user_prompt_count': real_user_count,
        'native_continuation_review_required': bool(unknown_continuations),
        'undocumented_user_continuations': unknown_continuations,
        'subagent_stats': [], 'delegation_tool_calls': flags, 'delegation_violation': bool(flags),
        'delegation_review_required': bool(flags), 'errors': errors, 'quota_blocked': False,
        'malformed_event_lines': malformed}


class OpenCodeFlashAgent(BaseAgent):
    def __init__(self, *args, effort='max', cli_pin=None, proxy_admin_config_path=None,
                 quota_halt_path=None, max_output_tokens=OUTPUT_TOKEN_MAX, **kwargs):
        super().__init__(*args, **kwargs)
        if self.model_name not in ('flash', MODEL, NATIVE_MODEL) or effort != 'max':
            raise ValueError('This adapter requires exact DeepSeek Flash at max effort')
        if type(max_output_tokens) is not int or max_output_tokens != OUTPUT_TOKEN_MAX:
            raise ValueError('This separate experiment pins the native output limit to 393216')
        self.max_output_tokens = max_output_tokens
        self.cli_pin = cli_pin or cli_identity('opencode')
        if self.cli_pin.get('provider') != 'opencode' or self.cli_pin.get('version') != '1.19.6':
            raise ValueError('OpenCode Flash requires the pinned V1.19.6 CLI')
        self.proxy_admin_config_path = Path(proxy_admin_config_path) if proxy_admin_config_path else None
        self.quota_halt_path = Path(quota_halt_path) if quota_halt_path else DEFAULT_HALT_PATH

    @staticmethod
    def name():
        return 'cynthia-opencode-flash-native'

    def version(self):
        return self.cli_pin['version']

    async def setup(self, environment):
        if not getattr(environment, 'workspace', None) or not getattr(environment, 'battle_session', None):
            raise RuntimeError('OpenCodeFlashAgent requires CynthiaEnvironment')
        if not Path('/usr/bin/sandbox-exec').exists() or not self.proxy_admin_config_path:
            raise RuntimeError('OpenCode Flash requires strict macOS isolation and host proxy accounting')

    def _record_cancellation(self, environment, phase):
        state = self._cancellation
        state['count'] += 1
        if not state['requested']:
            process = getattr(self, '_native_process', None)
            state.update(requested=True, phase=phase,
                received_at=datetime.now(timezone.utc).isoformat(),
                primary_finished=bool(process and process.returncode is not None))
            if self._proxy_registered:
                state['marker_task'] = asyncio.create_task(self._mark_cancellation(environment))
        environment.framework_cancellation_pending = True

    async def _mark_cancellation(self, environment):
        state = self._cancellation
        try:
            marker = await asyncio.to_thread(proxy_request, self.proxy_admin_config_path,
                '/admin/trials/' + quote(environment.session_id, safe='') + '/cancel',
                {'reason': 'framework_cancellation_pending'})
            if marker.get('cancellation_requested') is not True:
                raise ValueError('Proxy did not confirm cancellation causality marker')
            state['marker'] = marker
        except (OSError, ValueError, KeyError) as exc:
            state['marker_error'] = str(exc)
            publish_halt(self.quota_halt_path, 'proxy_cancellation_accounting_unavailable')

    async def _await_cancellation_marker(self):
        task = self._cancellation.get('marker_task')
        if task is not None:
            await task

    def _apply_cancellation(self, environment, context, audit):
        state = self._cancellation
        if not state['requested']:
            return
        environment.framework_cancellation_pending = True
        environment.agent_context = context
        audit.update(cancelled=True, cancellation_classification='pending_framework_exception',
            cancellation_received_during=state['phase'],
            cancellation_received_at=state['received_at'],
            framework_cancellation_count=state['count'],
            primary_process_finished_before_cancellation=state['primary_finished'],
            proxy_cancellation_marker=state['marker'],
            proxy_cancellation_marker_error=state['marker_error'])
        audit['cancellation_transport_only'] = (not state['marker_error']
            and not audit.get('proxy_cancellation_status_error')
            and not audit.get('proxy_revocation_error')
            and cancellation_transport_only(
                audit.get('proxy_cancellation_status', audit.get('proxy_status')), audit))

    async def run(self, instruction, environment, context: AgentContext):
        self._proxy_registered = False
        self._native_process = None
        self._cancellation = {'requested': False, 'count': 0, 'phase': None,
                              'marker_task': None, 'marker': None, 'marker_error': None}
        try:
            return await self._run_native(instruction, environment, context)
        except asyncio.CancelledError:
            if not self._cancellation['requested']:
                self._record_cancellation(environment, 'native_run')
            raise
        finally:
            if self._proxy_registered:
                await finish_cleanup(self._revoke_proxy(environment, context),
                    lambda: self._record_cancellation(environment, 'credential_revocation'))
                if self._cancellation['requested']:
                    raise asyncio.CancelledError()

    async def _revoke_proxy(self, environment, context):
        await self._await_cancellation_marker()
        receipt, close_error = None, None
        try:
            receipt = await asyncio.to_thread(proxy_request, self.proxy_admin_config_path,
                '/admin/trials/' + quote(environment.session_id, safe='') + '/close', {})
            if receipt.get('trial_id') != environment.session_id or receipt.get('closed') is not True:
                raise ValueError('Proxy did not confirm native credential revocation')
        except (OSError, ValueError, KeyError) as exc:
            close_error = str(exc)
            publish_halt(self.quota_halt_path, 'proxy_credential_revocation_failed')
        await self._await_cancellation_marker()
        audit = getattr(environment, 'agent_audit', None) or {}
        if self._cancellation['requested'] and 'proxy_status' in audit:
            # Cancellation can arrive after the first accounting snapshot. Keep
            # that evidence and separately fetch the proxy's causal annotation.
            try:
                current = await asyncio.to_thread(proxy_request, self.proxy_admin_config_path,
                    '/admin/trials/' + quote(environment.session_id, safe=''))
                if current.get('trial_id') != environment.session_id:
                    raise ValueError('Proxy returned cancellation accounting for another trial')
                audit['proxy_cancellation_status'] = current
            except (OSError, ValueError, KeyError) as exc:
                audit['proxy_cancellation_status_error'] = str(exc)
        self._apply_cancellation(environment, context, audit)
        audit['proxy_close_receipt'] = receipt
        if close_error:
            audit['proxy_revocation_error'] = close_error
            audit['cancellation_transport_only'] = False
            environment.finalize_reason = 'runtime_error'
        environment.agent_audit = audit
        context.metadata = {**(context.metadata or {}), **audit}
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        (self.logs_dir / 'proxy-close.json').write_text(json.dumps(
            {'receipt': receipt, 'error': close_error}, indent=2) + '\n')
        (self.logs_dir / 'runtime-audit.json').write_text(json.dumps(audit, indent=2) + '\n')
        if close_error and not getattr(environment, 'framework_cancellation_pending', False):
            result = await asyncio.to_thread(environment.admin.request,
                '/admin/sessions/' + environment.battle_session['session_id'] + '/result')
            completed = result.get('valid') is True and result.get('terminal') is True and result.get('outcome') in ('win', 'loss')
            if not completed:
                raise RuntimeError('OpenCode proxy credential revocation failed; preserve evidence')
            audit['provider_failure_after_terminal'] = True
            context.metadata = {**context.metadata, **audit}
            (self.logs_dir / 'runtime-audit.json').write_text(json.dumps(audit, indent=2) + '\n')


    async def _run_native(self, instruction, environment, context: AgentContext):
        check_quota_halt(self.quota_halt_path)
        workspace = Path(environment.workspace).resolve()
        trial_id = environment.session_id
        registration = await finish_cleanup(asyncio.to_thread(proxy_request, self.proxy_admin_config_path,
            '/admin/trials', {'trial_id': trial_id}),
            lambda: self._record_cancellation(environment, 'proxy_registration'))
        self._proxy_registered = True
        if self._cancellation['requested']:
            self._cancellation['marker_task'] = asyncio.create_task(self._mark_cancellation(environment))
            raise asyncio.CancelledError()
        proxy_port = validate_registration(registration, trial_id)
        prepared = prepare_client(workspace, proxy_url=registration['base_url'], proxy_token=registration['token'],
                                  max_output_tokens=getattr(self, 'max_output_tokens', OUTPUT_TOKEN_MAX))
        child_env = prepared['env']
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        profile = self.logs_dir / 'isolation.sb'
        profile.write_text(strict_sandbox_profile(workspace, urlsplit(environment.admin.url).port,
            executable=Path(self.cli_pin['executable']), calculation_python=CALCULATION_PYTHON, proxy_port=proxy_port))
        prompt = instruction + '\n\nRead CONTROLS.md and team.json in the current workspace, then use battle.py and the live observations. Keep scratch files in this workspace.\n'
        (self.logs_dir / 'prompt.txt').write_text(prompt)
        (self.logs_dir / 'proxy-registration.json').write_text(json.dumps(
            {k: v for k, v in registration.items() if k != 'token'}, indent=2) + '\n')
        cmd = ['/usr/bin/sandbox-exec', '-f', str(profile),
               *command(workspace, self.cli_pin['executable'], prompt, 'pokebench-' + trial_id)]
        pin_before = await asyncio.to_thread(validate_cli_pin, self.cli_pin)
        metadata = {'requested_model': MODEL, 'native_model': NATIVE_MODEL, 'effort': 'max',
            'max_output_tokens': prepared['max_output_tokens'],
            'output_token_limit_source': 'Pinned native OpenCode OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX environment override',
            'advertised_model_version': ADVERTISED_MODEL_VERSION, 'cli_pin': self.cli_pin,
            'command': cmd, 'context_policy': 'fresh-trial-local-XDG-state-no-resume-native-same-model-compaction',
            'calculation_python': calculation_python_identity(str(CALCULATION_PYTHON)),
            'authentication': 'Per-trial proxy credential only; account key and admin credential never enter actor process',
            'cost_usd': None}
        (self.logs_dir / 'invocation.json').write_text(json.dumps(metadata, indent=2) + '\n')
        events, export = self.logs_dir / 'events.jsonl', self.logs_dir / 'native-session.json'
        process, cancelled, failure, status = None, False, None, None
        export_error = None
        with events.open('xb') as output, (self.logs_dir / 'stderr.log').open('wb') as stderr:
            try:
                check_quota_halt(self.quota_halt_path)
                process = await asyncio.create_subprocess_exec(*cmd, cwd=workspace, env=child_env,
                    stdin=asyncio.subprocess.PIPE, stdout=output, stderr=stderr, start_new_session=True)
                self._native_process = process
                (self.logs_dir / 'pid.json').write_text(json.dumps({'pid': process.pid}) + '\n')
                process.stdin.write(prompt.encode())
                await process.stdin.drain()
                process.stdin.close()
                await process.wait()
            except asyncio.CancelledError:
                cancelled = True
                self._record_cancellation(environment, 'primary_process')
            except (OSError, ValueError, RuntimeError) as exc:
                failure = str(exc)
            finally:
                async def finalize_native():
                    nonlocal failure, status, audit, export_error
                    await self._await_cancellation_marker()
                    if process and process.returncode is None:
                        try:
                            await stop_process(process)
                        except (OSError, RuntimeError) as exc:
                            failure = failure or str(exc)
                    output.flush()
                    try:
                        session_id = session_id_from_events(events)
                        with export.open('xb') as exported, (self.logs_dir / 'export-stderr.log').open('wb') as export_stderr:
                            exporter = await asyncio.create_subprocess_exec('/usr/bin/sandbox-exec', '-f', str(profile),
                                self.cli_pin['executable'], 'export', session_id, '--pure', cwd=workspace, env=child_env,
                                stdin=asyncio.subprocess.DEVNULL, stdout=exported, stderr=export_stderr, start_new_session=True)
                            try:
                                await asyncio.wait_for(exporter.wait(), timeout=30)
                            finally:
                                if exporter.returncode is None:
                                    await stop_process(exporter)
                            if exporter.returncode != 0:
                                raise RuntimeError('Native session export failed')
                    except (OSError, ValueError, RuntimeError, asyncio.TimeoutError) as exc:
                        export_error = str(exc)
                    await self._await_cancellation_marker()
                    audit = parse_events(events, export, workspace, prompt)
                    if export_error:
                        audit['native_evidence_error'] = export_error
                    failure = failure or audit['native_evidence_error']
                    try:
                        status = await asyncio.to_thread(proxy_request, self.proxy_admin_config_path,
                            '/admin/trials/' + quote(trial_id, safe=''))
                        audit['proxy_accounting_error'] = proxy_status_error(status, trial_id)
                        audit['proxy_status'] = status
                        audit['model_mismatch'] |= status.get('model_mismatch') is True
                        audit['quota_blocked'] = status.get('budget_halted') is True
                        audit['native_exclusive_usage'] = audit['usage']
                        audit['metered_request_usage'] = proxy_usage(status)
                        audit['request_usage_complete'] = (status.get('request_usage_complete')
                            if 'request_usage_complete' in status else status.get('usage_uncertainty') is False)
                        audit['recovered_transport_evidence'] = recovered_transport_evidence(status)
                        audit['usage'] = audit['metered_request_usage'] if audit['request_usage_complete'] is True else []
                        audit['usage_scope'] = ('Host proxy raw provider totals, one count per unique metered request: input includes cached tokens; output includes reasoning tokens. Cache/thinking counters are subsets. Native exclusive counts are retained separately and never added. '
                            'When any attempt is unmetered, scored context token totals are null; metered_request_usage is a known subset, not total study usage. Unknown financial liability remains fully reserved.')
                        (self.logs_dir / 'proxy-status.json').write_text(json.dumps(status, indent=2) + '\n')
                        failure = failure or audit['proxy_accounting_error']
                    except (OSError, ValueError, KeyError) as exc:
                        audit['proxy_accounting_error'] = 'Host proxy accounting unavailable: ' + str(exc)
                        failure = failure or audit['proxy_accounting_error']
                    audit.update(return_code=process.returncode if process else None, cancelled=cancelled,
                                 cli_pin_before=pin_before, calculation_python=metadata['calculation_python'])
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
                    context.n_input_tokens, context.n_output_tokens = usage.get('input_tokens'), usage.get('output_tokens')
                    context.n_cache_tokens = usage.get('cache_read_tokens')
                    normal = audit['terminal_event'] or audit['budget_exhausted']
                    environment.finalize_reason = ('timeout' if audit['budget_exhausted'] else 'agent_stopped') if normal and not failure else 'runtime_error'
                    if audit['errors'] or audit['quota_blocked'] or audit['model_mismatch'] or audit['delegation_violation'] or audit.get('cli_pin_error') or audit['return_code'] != 0:
                        environment.finalize_reason = 'runtime_error'
                    if self._cancellation['requested']:
                        self._apply_cancellation(environment, context, audit)
                    context.cost_usd = None
                    if status is not None:
                        try:
                            cost = float(status['estimated_cost_usd'])
                            if not math.isfinite(cost) or cost < 0:
                                raise ValueError('Invalid proxy calculated cost')
                            context.cost_usd = cost
                        except (ValueError, TypeError, KeyError):
                            audit['proxy_cost_evidence_error'] = 'Proxy published-rate cost is missing or invalid'
                            environment.finalize_reason = 'runtime_error'
                            failure = failure or audit['proxy_cost_evidence_error']
                    audit['cost_usd_basis'] = 'Host proxy known metered published-rate USD; not an invoice or measured account charge, and not total cost when any usage is unknown. Usage uncertainty and full reserved upper bounds are recorded separately.'
                    audit['cost_usd'] = context.cost_usd
                    context.metadata = {**metadata, **audit}
                    self._apply_cancellation(environment, context, audit)
                    context.metadata = {**metadata, **audit}
                    (self.logs_dir / 'runtime-audit.json').write_text(json.dumps(audit, indent=2) + '\n')
                audit = None
                await finish_cleanup(finalize_native(),
                    lambda: self._record_cancellation(environment, 'native_finalization'))
        if self._cancellation['requested']:
            raise asyncio.CancelledError()
        invalid_actor = audit['model_mismatch'] or audit['delegation_violation'] or audit.get('cli_pin_error')
        problem = failure or audit['errors'] or audit['quota_blocked'] or audit['return_code'] != 0 or not (audit['terminal_event'] or audit['budget_exhausted'])
        completed = False
        if not invalid_actor and problem:
            result = await asyncio.to_thread(environment.admin.request,
                '/admin/sessions/' + environment.battle_session['session_id'] + '/result')
            completed = result.get('valid') is True and result.get('terminal') is True and result.get('outcome') in ('win', 'loss')
            if completed:
                audit['provider_failure_after_terminal'] = True
                context.metadata = {**context.metadata, **audit}
                (self.logs_dir / 'runtime-audit.json').write_text(json.dumps(audit, indent=2) + '\n')
        if invalid_actor or (problem and not completed):
            raise RuntimeError('OpenCode native session did not complete cleanly; preserve evidence and inspect runtime-audit.json')
