"""Subscription-authenticated native CLI adapter for Harbor 0.26.

The outer macOS Seatbelt boundary is required. Do not run the command builder
without it. Authentication stays in the vendors' existing login stores.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
from urllib.parse import urlsplit
from .paths import runtime_paths
from .native_runtime import (cli_identity, validate_cli_pin, selected_calculation_python,
                             calculation_python_identity)

from harbor.agents.base import BaseAgent
from harbor.models.agent.context import AgentContext


ROOT = Path(__file__).resolve().parents[2]
MODELS = {
    'sol': {'provider': 'codex', 'model': 'gpt-6.1-sol'},
    'astra': {'provider': 'codex', 'model': 'gpt-6-astra'},
    'opus': {'provider': 'claude', 'model': 'claude-opus-5-5'},
}
AUXILIARY_POLICY = 'native-built-in-helper-v1'
CLAUDE_AUXILIARY_MODELS = ('claude-haiku-5-5',)


def resolve_model(model):
    for alias, spec in MODELS.items():
        if model in (alias, spec['model']):
            return dict(spec)
    raise ValueError(f'Unsupported or unpinned model: {model}')


def clean_env(workspace: Path):
    env = os.environ.copy()
    for key in list(env):
        if key in {'OPENAI_API_KEY', 'CODEX_API_KEY', 'ANTHROPIC_API_KEY',
                   'ANTHROPIC_AUTH_TOKEN', 'CLAUDE_CODE_OAUTH_TOKEN',
                   'ANTHROPIC_BASE_URL', 'OPENAI_BASE_URL', 'CLAUDECODE',
                   'GOOGLE_API_KEY', 'GEMINI_API_KEY', 'POKEBENCH_CONFIG',
                   'CLAUDE_CODE_EFFORT_LEVEL', 'ANTHROPIC_MODEL', 'PYTHONPATH'} or key.startswith((
                       'CLAUDE_CODE_USE_', 'ANTHROPIC_DEFAULT_', 'CYNTHIA_ADMIN')):
            env.pop(key, None)
    env.update(NO_COLOR='1', CLAUDE_CODE_DISABLE_AUTO_MEMORY='1',
               CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC='1', DISABLE_AUTOUPDATER='1')
    temp = workspace / '.tmp'
    temp.mkdir(exist_ok=True)
    for key in ('TMPDIR', 'TMP', 'TEMP', 'CLAUDE_CODE_TMPDIR'):
        env[key] = str(temp)
    # zsh uses TMPPREFIX, independently of TMPDIR, for here-documents.
    env['TMPPREFIX'] = str(temp / 'zsh')
    env['CYNTHIA_PYTHON'] = selected_calculation_python()
    return env


def sandbox_profile(workspace: Path, allowed_loopback_port=None, block_ai_clients=False):
    """Block private evaluation data and peer workspaces; leave public web enabled.

    This profile is not a network-isolation claim. The battle server independently
    authenticates every operation and never exposes mutation/admin methods to the
    per-trial agent bearer token.
    """
    workspace = workspace.resolve()
    home = Path.home()
    paths = runtime_paths()
    protected = [ROOT, paths.private, paths.rom, paths.data, paths.core, home / '.codex/sessions',
                 home / '.codex/archived_sessions', home / '.claude/projects',
                 home / '.config/cynthiabench',
                 home / 'Library/Application Support/CynthiaBench']
    shared_temps = {Path('/private/tmp'), Path(tempfile.gettempdir()).resolve()}
    if os.environ.get('TMPDIR'):
        shared_temps.add(Path(os.environ['TMPDIR']).resolve())
    lines = ['(version 1)', '(allow default)']
    lines += [
        '(deny process-info* (require-not (target same-sandbox)))',
        '(deny signal (require-not (target same-sandbox)))',
        '(deny mach-priv-task-port (require-not (target same-sandbox)))',
        '(deny appleevent-send)', '(deny lsopen)', '(deny job-creation)',
        '(deny file-link)',
        # In particular, do not let shell tools control Docker/OrbStack or a
        # browser via a host Unix socket, bypassing filesystem restrictions.
        '(deny network-outbound (remote unix-socket (path-regex #"^/")))',
        # macOS DNS uses this system socket. It is not an arbitrary application
        # control socket; keep hostname resolution for public model/web traffic.
        '(allow network-outbound (remote unix-socket (literal "/private/var/run/mDNSResponder")))',
    ]
    if allowed_loopback_port is not None:
        if not isinstance(allowed_loopback_port, int) or not 1 <= allowed_loopback_port <= 65535:
            raise ValueError('Invalid allowed battle service port')
        lines += ['(deny network-outbound (remote ip "localhost:*"))',
                  f'(allow network-outbound (remote ip "localhost:{allowed_loopback_port}"))']
    else:
        lines.append('(deny network-outbound (remote ip "localhost:*"))')
    for path in [*protected, *sorted(shared_temps)]:
        path = path.resolve()
        for operation in ('file-read-data', 'file-write*'):
            lines.append(f'(deny {operation} (require-all (subpath {json.dumps(str(path))}) '
                         f'(require-not (subpath {json.dumps(str(workspace))}))))')
    # Native login access remains available, but global history is not task input.
    # Codex's SQLite/log paths are separately redirected into the current workspace.
    for relative in ('.codex/sqlite', '.codex/memories', '.codex/log', '.codex/history.jsonl',
                     '.claude/history.jsonl', '.claude/debug', '.claude/todos', '.claude/tasks',
                     '.claude/plans', '.claude/file-history', '.claude/session-env'):
        lines.append(f'(deny file-read-data (subpath {json.dumps(str(home / relative))}))')
    prefix = str(home / '.codex').replace('.', '[.]')
    for pattern in (prefix + '/[^/]*[.]sqlite[^/]*$', prefix + '/[^/]*global-state[^/]*$'):
        lines.append(f'(deny file-read-data (regex #{json.dumps("^" + pattern)}))')
    if block_ai_clients:
        for provider in ('codex', 'claude', 'gemini'):
            binary = shutil.which(provider)
            if binary:
                lines.append(f'(deny process-exec (literal {json.dumps(str(Path(binary).resolve()))}))')
        for relative in ('.codex', '.claude', '.claude.json'):
            lines.append(f'(deny file-read-data (subpath {json.dumps(str(home / relative))}))')
    return '\n'.join(lines) + '\n'


def command(spec, workspace, effort='max', executable=None):
    if spec['provider'] == 'codex':
        cmd = [executable or shutil.which('codex'), 'exec', '--ignore-user-config', '--ephemeral',
               '--skip-git-repo-check', '--json', '--color', 'never', '-C', str(workspace),
               '-m', spec['model'], '-c', f'model_reasoning_effort="{effort}"',
               '-c', 'approval_policy="never"', '-c', 'web_search="live"',
               '-c', 'project_doc_max_bytes=0', '-c', 'features.multi_agent=false',
               '-c', 'features.memories=false', '-s', 'danger-full-access',
               '-c', 'sqlite_home=' + json.dumps(str(workspace / '.codex-state')),
               '-c', 'log_dir=' + json.dumps(str(workspace / '.codex-log')),
               '-c', 'history.persistence="none"',
               '-o', str(workspace / 'last_response.txt')]
        for feature in ('apps', 'plugins', 'browser_use', 'computer_use', 'image_generation',
                        'hooks', 'shell_snapshot', 'remote_plugin'):
            cmd += ['-c', f'features.{feature}=false']
        cmd += ['-c', 'features.skip_host_skill_discovery=true', '-']
        return cmd
    settings = {'switchModelsOnFlag': False, 'autoMemoryEnabled': False,
                'permissions': {'defaultMode': 'acceptEdits'}, 'sandbox': {'enabled': False}}
    public_tools = 'Read,Write,Edit,Glob,Grep,Bash,WebSearch,WebFetch'
    return [executable or shutil.which('claude'), '-p', '--model', spec['model'], '--effort', effort,
            '--output-format', 'stream-json', '--verbose', '--safe-mode',
            '--setting-sources', '', '--strict-mcp-config', '--mcp-config', '{"mcpServers":{}}',
            '--no-session-persistence', '--permission-mode', 'acceptEdits',
            '--permission-prompts', 'none', '--no-chrome', '--settings', json.dumps(settings),
            '--tools', public_tools, '--allowedTools', public_tools]


def parse_events(path, spec, allowed_auxiliary_models=None):
    events = []
    for line in path.read_text(errors='replace').splitlines():
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    initial_models, actor_models, usage_models = set(), set(), set()
    allowed = set(CLAUDE_AUXILIARY_MODELS if spec['provider'] == 'claude' else ()) if allowed_auxiliary_models is None else set(allowed_auxiliary_models)
    if not allowed <= set(CLAUDE_AUXILIARY_MODELS) or (allowed and spec['provider'] != 'claude'):
        raise ValueError('Only documented Claude native helper IDs can be permitted')
    per_model_usage, subagent_stats, delegation_tools = [], [], []
    assistant_count = missing_actor_ids = child_messages = 0
    usage = []
    for event in events:
        if event.get('type') == 'system' and event.get('model'):
            initial_models.add(event['model'])
        if event.get('type') == 'assistant':
            assistant_count += 1
            message = event.get('message') or {}
            if message.get('model'):
                actor_models.add(message['model'])
            else:
                missing_actor_ids += 1
            child_messages += bool(event.get('parent_tool_use_id'))
            for part in message.get('content', []):
                if isinstance(part, dict) and part.get('type') == 'tool_use' and part.get('name') in ('Agent', 'Task'):
                    delegation_tools.append(part['name'])
        if event.get('type') == 'turn.completed':
            usage.append(event.get('usage', {}))
        if event.get('type') == 'result':
            usage_models.update(event.get('modelUsage', {}))
            per_model_usage.append(event.get('modelUsage', {}))
            if event.get('subagent_stats') is not None:
                subagent_stats.append(event['subagent_stats'])
            usage.append(event.get('usage', {}))
    terminal = any(e.get('type') == 'turn.completed' for e in events) if spec['provider'] == 'codex' else any(
        e.get('type') == 'result' and e.get('subtype') == 'success' and not e.get('is_error') for e in events)
    matches = lambda model: model == spec['model'] or model.startswith(spec['model'] + '-')
    auxiliary = {m for m in usage_models if not matches(m)}
    unrecognized = auxiliary - allowed
    delegation = bool(delegation_tools or child_messages or any(
        s.get('spawned', 0) or any(s.get('requested', {}).values()) for s in subagent_stats))
    missing_actor_evidence = spec['provider'] == 'claude' and (not assistant_count or bool(missing_actor_ids))
    # A deadline before the first completed message can still be a valid
    # noncompletion. A successful Claude result must contain actor evidence.
    missing_required_actor = spec['provider'] == 'claude' and (bool(missing_actor_ids) or terminal and not assistant_count)
    mismatch = bool(any(not matches(m) for m in initial_models | actor_models) or unrecognized or missing_required_actor or delegation)
    return {'terminal_event': terminal, 'observed_models': sorted(initial_models | actor_models | usage_models),
            'model_mismatch': mismatch, 'usage': usage,
            'initial_models': sorted(initial_models), 'actor_models': sorted(actor_models),
            'assistant_message_count': assistant_count, 'missing_actor_model_evidence': missing_actor_evidence,
            'aggregate_usage_models': sorted(usage_models), 'per_model_usage': per_model_usage,
            'auxiliary_models': sorted(auxiliary), 'allowed_auxiliary_models': sorted(allowed),
            'unrecognized_usage_models': sorted(unrecognized),
            'auxiliary_model_review_required': bool(auxiliary), 'auxiliary_model_policy': AUXILIARY_POLICY,
            'auxiliary_attribution_note': 'Aggregate helper usage is recorded; individual internal request attribution is not available.',
            'subagent_stats': subagent_stats, 'delegation_tool_calls': delegation_tools,
            'child_assistant_messages': child_messages, 'delegation_violation': delegation,
            'errors': [e for e in events if e.get('type') in ('error', 'turn.failed') or e.get('is_error')]}


def detect_quota(events_path, stderr_path):
    """Only provider error envelopes are authoritative; tool/web prose is not."""
    phrases = ('usage limit reached', 'hit your limit', 'insufficient_quota',
               'weekly limit reached', 'credit_balance_exhausted',
               'exceeded your current quota', 'usage_limit_reached')
    evidence = []
    for line in events_path.read_text(errors='replace').splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        kind = event.get('type')
        payloads = []
        if kind in ('error', 'turn.failed'):
            payloads = [event.get(key) for key in ('error', 'code', 'message') if key in event]
        elif kind == 'result' and (event.get('is_error') is True or str(event.get('subtype', '')).startswith('error')):
            # Never scan the result's arbitrary assistant prose; only its
            # structured failure fields. A successful result isn't a quota error.
            payloads = [event.get(key) for key in ('error', 'errors', 'error_code', 'code') if key in event]
        for payload in payloads:
            rendered = json.dumps(payload).lower()
            found = [phrase for phrase in phrases if phrase in rendered]
            if found:
                evidence.append({'source': 'provider_error_event', 'event_type': kind, 'matched_codes': found})
    # CLI stderr isn't tool stdout. Require a standalone provider-style error,
    # not a generic percentage warning, rate-limit status, or quoted prose.
    for line in stderr_path.read_text(errors='replace').splitlines():
        value = line.strip().lower()
        if value.startswith(('warning', 'warn:', 'rate_limit_event')) or any(q in value for q in ('"', '“', '”', '`')):
            continue
        prefixes = ('error:', 'error ', 'usage limit reached', 'usage_limit_reached',
                    "you've hit your limit", 'you have hit your limit', 'insufficient_quota',
                    'weekly limit reached', 'credit_balance_exhausted')
        if value.startswith(prefixes):
            found = [phrase for phrase in phrases if phrase in value]
            if found:
                evidence.append({'source': 'provider_stderr', 'matched_codes': found})
    return {'quota_blocked': bool(evidence), 'quota_evidence': evidence}


class NativeSubscriptionAgent(BaseAgent):
    def __init__(self, *args, effort='max', cli_pin=None, allowed_auxiliary_models=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.spec = resolve_model(self.model_name)
        self.effort = effort
        self.allowed_auxiliary_models = list(CLAUDE_AUXILIARY_MODELS if self.spec['provider'] == 'claude' else ()) if allowed_auxiliary_models is None else list(allowed_auxiliary_models)
        self.cli_pin = cli_pin or cli_identity(self.spec['provider'])
        if self.cli_pin['provider'] != self.spec['provider']:
            raise ValueError('Native CLI pin belongs to the wrong provider')
        self._version = self.cli_pin['version']

    @staticmethod
    def name():
        return 'cynthia-native-subscription'

    def version(self):
        return self._version

    async def setup(self, environment):
        if not hasattr(environment, 'workspace') or not hasattr(environment, 'battle_session'):
            raise RuntimeError('NativeSubscriptionAgent requires CynthiaEnvironment')
        if not Path('/usr/bin/sandbox-exec').exists():
            raise RuntimeError('This adapter requires macOS sandbox-exec; no unisolated fallback')

    async def run(self, instruction, environment, context: AgentContext):
        workspace = environment.workspace
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        pin_before = await asyncio.to_thread(validate_cli_pin, self.cli_pin)
        profile = self.logs_dir / 'isolation.sb'
        profile.write_text(sandbox_profile(workspace, urlsplit(environment.admin.url).port))
        prompt = (instruction + '\n\nYour current working directory is ' + str(workspace) +
                  '. The battle is already loaded. Read CONTROLS.md and team.json, then use battle.py. '
                  'View screenshot files to inspect the game. You can research publicly available sources '
                  'and calculate strategy using local scripts. Keep scratch work here or in .tmp. '
                  'Play this one battle to its terminal win or loss; do not stop after only proposing a plan. '
                  'You cannot reset, edit the game, access emulator memory, or inspect other trials.\n')
        (self.logs_dir / 'prompt.txt').write_text(prompt)
        cmd = ['/usr/bin/sandbox-exec', '-f', str(profile),
               *command(self.spec, workspace, self.effort, self.cli_pin['executable'])]
        child_env = clean_env(workspace)
        python_identity = calculation_python_identity(child_env['CYNTHIA_PYTHON'])
        metadata = {'requested_model': self.spec['model'], 'effort': self.effort,
                    'cli_version': self._version, 'cli_pin': self.cli_pin, 'command': cmd,
                    'calculation_python': python_identity,
                    'auxiliary_model_policy': AUXILIARY_POLICY, 'allowed_auxiliary_models': self.allowed_auxiliary_models,
                    'authentication': 'existing native subscription login; API environment removed',
                    'model_evidence_note': 'CLI requested model; Codex JSON may not attest resolved server model',
                    'cost_usd': None}
        (self.logs_dir / 'invocation.json').write_text(json.dumps(metadata, indent=2) + '\n')
        events_path = self.logs_dir / 'events.jsonl'
        with events_path.open('w') as stdout, (self.logs_dir / 'stderr.log').open('w') as stderr:
            process = await asyncio.create_subprocess_exec(*cmd, cwd=workspace, env=child_env,
                         stdin=asyncio.subprocess.PIPE, stdout=stdout, stderr=stderr, start_new_session=True)
            (self.logs_dir / 'pid.json').write_text(json.dumps({'pid': process.pid}))
            cancelled = False
            try:
                await process.communicate(prompt.encode())
            except asyncio.CancelledError:
                cancelled = True
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                    await asyncio.wait_for(process.wait(), timeout=5)
                except (ProcessLookupError, asyncio.TimeoutError):
                    if process.returncode is None:
                        os.killpg(process.pid, signal.SIGKILL)
                        await process.wait()
                raise
            finally:
                audit = parse_events(events_path, self.spec, self.allowed_auxiliary_models)
                audit.update(return_code=process.returncode, cancelled=cancelled)
                audit['calculation_python'] = python_identity
                audit['cli_pin_before'] = pin_before
                try:
                    audit['cli_pin_after'] = await asyncio.to_thread(validate_cli_pin, self.cli_pin)
                except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
                    audit['cli_pin_error'] = str(exc)
                audit.update(detect_quota(events_path, self.logs_dir / 'stderr.log'))
                from .delegation_audit import audit_agent_logs
                delegation = audit_agent_logs(self.logs_dir)
                (self.logs_dir / 'delegation-audit.json').write_text(json.dumps(delegation, indent=2) + '\n')
                audit['delegation_review_required'] = delegation['requires_manual_review']
                environment.agent_audit = audit
                (self.logs_dir / 'runtime-audit.json').write_text(json.dumps(audit, indent=2) + '\n')
                context.metadata = {**metadata, **audit}
                context.n_input_tokens = sum(x.get('input_tokens', 0) for x in audit['usage'])
                context.n_output_tokens = sum(x.get('output_tokens', 0) for x in audit['usage'])
                context.n_cache_tokens = sum(x.get('cached_input_tokens', x.get('cache_read_input_tokens', 0)) for x in audit['usage'])
                # Finalize incomplete play explicitly. A completed game outcome
                # must remain immutable regardless of the supplied reason.
                reason = 'runtime_error' if cancelled else ('agent_stopped' if process.returncode == 0 and audit['terminal_event'] else 'runtime_error')
                if cancelled:
                    environment.framework_cancellation_pending = True
                    environment.agent_context = context
                    audit['cancellation_classification'] = 'pending_framework_exception'
                if audit['quota_blocked'] or audit['model_mismatch'] or audit.get('cli_pin_error'):
                    reason = 'runtime_error'
                environment.finalize_reason = reason
        # If the vendor fails only after the battle ended, preserve that game
        # result. A model mismatch remains invalid regardless of the outcome.
        completed_battle = False
        if not audit['model_mismatch'] and (audit['quota_blocked'] or process.returncode != 0 or not audit['terminal_event']):
            result = await asyncio.to_thread(environment.admin.request,
                '/admin/sessions/' + environment.battle_session['session_id'] + '/result')
            completed_battle = result.get('valid') is True and result.get('terminal') is True
            if completed_battle:
                audit['provider_failure_after_terminal'] = True
                context.metadata = {**(context.metadata or {}), 'provider_failure_after_terminal': True}
                (self.logs_dir / 'runtime-audit.json').write_text(json.dumps(audit, indent=2) + '\n')
        if audit['quota_blocked'] and not completed_battle:
            raise RuntimeError('Subscription quota blocked; preserve this trial, do not count as a loss')
        if audit['model_mismatch'] or audit.get('cli_pin_error') or (not completed_battle and (process.returncode != 0 or not audit['terminal_event'])):
            raise RuntimeError('Native CLI did not complete cleanly; inspect runtime-audit.json')
