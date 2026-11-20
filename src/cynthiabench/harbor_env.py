"""Harbor custom sandbox and host-side verifier for the Cynthia battle service."""
from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path, PurePosixPath
import re
import shlex
import shutil
from urllib.request import Request, urlopen
from urllib.parse import urlsplit

from harbor.environments.base import BaseEnvironment, ExecResult
from harbor.environments.capabilities import EnvironmentCapabilities
from harbor.models.verifier.result import VerifierResult
from harbor.verifier.base import BaseVerifier

from .native_agent import ROOT, clean_env, sandbox_profile


CONTROLS = '''# Battle controls

The supplied CYNTHIA_PYTHON environment variable names your local Python interpreter.
Use `"$CYNTHIA_PYTHON"` as shown for battle commands and for Python calculations.

Run `"$CYNTHIA_PYTHON" battle.py observe` to save observation.json and observation.png.
Use your image-viewing tool to inspect observation.png. Images show both DS screens.

`"$CYNTHIA_PYTHON" battle.py press A --frames 1` presses a DS button. Valid buttons include
A, B, X, Y, UP, DOWN, LEFT, RIGHT, L, R, START, SELECT. Frame limits are enforced
by the service. `"$CYNTHIA_PYTHON" battle.py press --frames 120` advances without a button.
`"$CYNTHIA_PYTHON" battle.py touch 120 80 --frames 1` touches the lower DS screen (256×192).
`"$CYNTHIA_PYTHON" battle.py act '<JSON object>'` sends an action documented by the service.
Every action refreshes the observation files. Full prior observations and screenshots
are retained under observation_history/; archived_screenshot names the image file.
Re-observe after an uncertain action.
The common engine caps are 1,200 action requests and 180,000 advanced game frames
(about 50 minutes of game time), independently of the wall-clock budget;
ordinary observations do not advance game frames.

## Decision-level actions

Use the legal high-level actions advertised in available_actions. They operate the
game's menus and wait through animations; they do not choose strategy for you:

* `"$CYNTHIA_PYTHON" battle.py act '{"type":"move","slot":1}'` chooses move slot 1 (slots 1–4).
* `"$CYNTHIA_PYTHON" battle.py act '{"type":"switch","slot":2}'` switches to party slot 2 (slots 1–6).
* `"$CYNTHIA_PYTHON" battle.py act '{"type":"shift","accept":false}'` declines an offered free switch in Shift mode. Use `{"type":"shift","accept":true,"slot":2}` to accept and choose original party slot 2.
* `"$CYNTHIA_PYTHON" battle.py act '{"type":"acknowledge"}'` advances an informational level-up stats screen. It cannot accept or decline a move.
* `"$CYNTHIA_PYTHON" battle.py act '{"type":"learn_move","accept":false}'` declines a newly offered move. Use `{"type":"learn_move","accept":true,"slot":2}` to replace exactly existing move slot 2; HM moves cannot be forgotten in this menu.
* `"$CYNTHIA_PYTHON" battle.py act '{"type":"item","item":"Hyper Potion","target":2}'` uses the named item on party slot 2. Supported medicine names are Hyper Potion, Full Heal, and Revive, subject to this team's actual bag and valid game targets.
* `"$CYNTHIA_PYTHON" battle.py act '{"type":"advance"}'` advances animations/dialogue to the next input without selecting a tactical action.

Always check the returned observation. An action can fail if the current menu,
Pokemon, PP, item count, or target makes it unavailable. Raw buttons/touch remain
legal alternatives. Treat live available_actions as the implemented interface.

Public state includes your team's own HP, stats, gender, status, moves/PP and bag;
the active opponent's species, gender, level, visible HP-bar pixels and status;
and observed faints. Named status, stat stages, volatile conditions, an embargoed
flag and field conditions describe publicly observable effects. During a Shift
offer, opponent_active is null and shift_offer gives only the announced incoming
species; its other details become visible after it enters. recent_battle_events retains
the latest 20 announced/observed events; battle_event_count is the total observed count.
These memory-derived observations expose announced moves and visible effects,
never future choices, hidden exact opponent stats, or RNG state.

Party slot numbers always refer to ORIGINAL team.json order, even when the game's
party menu reorders Pokemon. Use those original slots for switch/item targets.
Normal in-battle experience gains and move-learning choices are permitted.
level_up and learning_offer identify the visible level-up/learning prompts. Inspect
the offered move and choose explicitly; the helper does not select a move for you.

You may inspect team.json, research public sources, and use calculations/scripts.
Do not deliberately invoke other AI services, another model CLI, or additional
agent instances. Built-in public web tools are allowed, including their documented
internal extraction/background helpers; this does not authorize Agent/subagent
delegation or asking another model to play the battle. Public references and
ordinary calculation tools are allowed.
This is original Diamond, not Platinum or Brilliant Diamond. You get one battle,
with no reset or game editing. Win by defeating all six of Cynthia's Pokemon.
Do not treat a local text file or your final message as proof of victory: the host
verifier observes the actual game. Stop only after the battle's terminal outcome.
'''


class AdminClient:
    def __init__(self, path):
        config = json.loads(Path(path).read_text())
        self.url = config['url'].rstrip('/')
        self.token = config['admin_token']

    def request(self, path, payload=None):
        req = Request(self.url + path, data=None if payload is None else json.dumps(payload).encode(),
                      headers={'Authorization': 'Bearer ' + self.token, 'Content-Type': 'application/json'})
        with urlopen(req, timeout=180) as response:
            return json.load(response)


class CynthiaEnvironment(BaseEnvironment):
    """A macOS process sandbox with a remote, host-owned emulator.

    The task's Harbor POSIX/Linux path convention is virtualized into sandbox_root;
    execution is explicitly macOS, not a Linux container. No ROM or RAM is mounted.
    """
    def __init__(self, *args, admin_config_path=None, quota_halt_path=None, **kwargs):
        self.admin_config_path = Path(admin_config_path or ROOT / 'work/admin.json')
        self.quota_halt_path = Path(quota_halt_path) if quota_halt_path is not None else None
        self.agent_audit = None
        self.battle_session = None
        super().__init__(*args, **kwargs)
        self.sandbox_root = ROOT / 'work/harbor-sandboxes' / self.session_id
        self.workspace = self.sandbox_root / 'workspace'

    @staticmethod
    def type():
        return 'cynthia-macos-rpc'

    @property
    def capabilities(self):
        return EnvironmentCapabilities()

    def _validate_definition(self):
        if not (self.environment_dir / 'scenario.json').exists():
            raise FileNotFoundError('Cynthia task requires environment/scenario.json')

    def _path(self, path):
        path = PurePosixPath(str(path))
        if '..' in path.parts:
            raise ValueError('Parent traversal in environment path')
        result = (self.sandbox_root / str(path).lstrip('/')).resolve()
        if not result.is_relative_to(self.sandbox_root.resolve()):
            raise ValueError('Environment path escapes sandbox')
        return result

    async def start(self, force_build=False):
        if self.quota_halt_path is not None:
            from .budget_control import check_quota_halt
            check_quota_halt(self.quota_halt_path)
        self.workspace.mkdir(parents=True, exist_ok=False)
        for d in ('logs/agent', 'logs/verifier', 'logs/artifacts', 'tests', 'solution'):
            (self.sandbox_root / d).mkdir(parents=True, exist_ok=True)
        self.scenario = json.loads((self.environment_dir / 'scenario.json').read_text())
        self.admin = AdminClient(self.admin_config_path)
        self.battle_session = await asyncio.to_thread(self.admin.request, '/admin/sessions', {
            'team_id': self.scenario['team_id'], 'seed': self.scenario['seed'], 'trial_id': self.session_id,
        })
        session = {'url': self.admin.url, 'session_id': self.battle_session['session_id'],
                   'agent_token': self.battle_session['agent_token']}
        (self.workspace / 'battle-session.json').write_text(json.dumps(session) + '\n')
        shutil.copy2(Path(__file__).with_name('battle_client.py'), self.workspace / 'battle.py')
        (self.workspace / 'CONTROLS.md').write_text(CONTROLS)
        public_team = self.environment_dir / 'team.json'
        if public_team.exists():
            shutil.copy2(public_team, self.workspace / 'team.json')
        from .battle_client import request, persist_observation
        observation = await asyncio.to_thread(request, session, '/v1/observe')
        persist_observation(observation, self.workspace)
        # Non-secret provenance; administrator token never enters any task directory.
        self.trial_paths.verifier_dir.mkdir(parents=True, exist_ok=True)
        (self.trial_paths.verifier_dir / 'session.json').write_text(json.dumps({
            'session_id': session['session_id'], 'scenario': self.scenario,
            'worker_trial_id': self.session_id,
            'host_execution_os': 'macOS', 'sandbox_type': self.type(),
        }, indent=2) + '\n')

    async def stop(self, delete=True):
        if self.battle_session:
            self.resolve_framework_cancellation()
            if getattr(self, 'finalize_reason', None):
                await asyncio.to_thread(self.admin.request,
                    '/admin/sessions/' + self.battle_session['session_id'] + '/finalize',
                    {'reason': self.finalize_reason})
            await asyncio.to_thread(self.admin.request,
                '/admin/sessions/' + self.battle_session['session_id'] + '/close', {})
        # Keep per-trial workspace for audit. delete=True closes the emulator;
        # archived workspace remains inaccessible to all subsequent agents.

    def resolve_framework_cancellation(self):
        if not getattr(self, 'framework_cancellation_pending', False):
            return
        from .framework_signals import cancellation_classification
        reason, exception = cancellation_classification()
        audit = self.agent_audit or {}
        if audit.get('model_mismatch') or audit.get('cli_pin_error') or audit.get('delegation_violation'):
            reason = 'runtime_error'
        elif audit.get('quota_blocked'):
            if reason == 'timeout' and audit.get('cancellation_transport_only') is True:
                audit['financial_halt_after_confirmed_deadline'] = True
            else:
                reason = 'runtime_error'
        self.finalize_reason = reason
        audit.update(cancellation_classification=reason, framework_exception_type=exception)
        self.agent_audit = audit
        context = getattr(self, 'agent_context', None)
        if context is not None:
            context.metadata = {**(context.metadata or {}), **audit}
        path = self.trial_paths.agent_dir / 'runtime-audit.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(audit, indent=2) + '\n')
        self.framework_cancellation_pending = False

    async def upload_file(self, source_path, target_path):
        target = self._path(target_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_path, target)

    async def upload_dir(self, source_dir, target_dir):
        shutil.copytree(source_dir, self._path(target_dir), dirs_exist_ok=True)

    async def download_file(self, source_path, target_path):
        target = Path(target_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(self._path(source_path), target)

    async def download_dir(self, source_dir, target_dir):
        source = self._path(source_dir)
        Path(target_dir).mkdir(parents=True, exist_ok=True)
        if source.exists():
            shutil.copytree(source, target_dir, dirs_exist_ok=True)

    async def is_dir(self, path, user=None):
        return self._path(path).is_dir()

    async def is_file(self, path, user=None):
        return self._path(path).is_file()

    async def exec(self, command, cwd=None, env=None, timeout_sec=None, user=None):
        """Execute Harbor's sandbox housekeeping with virtual POSIX path mapping."""
        self._resolve_user(user)
        for prefix in ('/logs', '/tests', '/solution', '/workspace', '/runtime'):
            command = re.sub(r'(?<![A-Za-z0-9_:/])' + re.escape(prefix) + r'(?=/|\s|[\x27\x22;]|$)',
                             str(self._path(prefix)), command)
        work = self._path(cwd) if cwd else self.workspace
        work.mkdir(parents=True, exist_ok=True)
        profile = self.sandbox_root / 'harbor-exec.sb'
        profile.write_text(sandbox_profile(self.sandbox_root, urlsplit(self.admin.url).port,
                                          block_ai_clients=getattr(self, 'block_ai_clients', False)))
        child_env = clean_env(self.workspace)
        child_env.update(self._merge_env(env) or {})
        process = await asyncio.create_subprocess_exec('/usr/bin/sandbox-exec', '-f', str(profile),
            '/bin/sh', '-c', command, cwd=work, env=child_env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            out, err = await asyncio.wait_for(process.communicate(), timeout=timeout_sec)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            raise
        return ExecResult(stdout=out.decode(errors='replace'), stderr=err.decode(errors='replace'),
                          return_code=process.returncode)


def validate_terminal_identity(result, expected_session):
    """Confirm the host's game ending independently of reward/HP metadata."""
    if result.get('session_id') != expected_session:
        raise ValueError('Result session does not match the running trial')
    if result.get('valid') is not True:
        raise ValueError('Battle result is invalid or lacks explicit validation')
    if result.get('terminal') is not True or result.get('outcome') not in ('win', 'loss'):
        raise ValueError('Battle has no genuine win/loss; non-game stops remain pending')
    # Older fixtures omit these detailed fields. If supplied, none may
    # contradict the declared game ending: lifecycle termination is insufficient.
    for field in ('reason', 'termination_reason'):
        if field in result and result[field] != 'game_terminal':
            raise ValueError('Result reason contradicts a genuine game ending')
    evidence = result.get('evidence', {})
    if not isinstance(evidence, dict):
        raise ValueError('Invalid host evidence object')
    terminal = evidence.get('terminal_state', {})
    if not isinstance(terminal, dict):
        raise ValueError('Invalid terminal state object')
    expected_code = 1 if result['outcome'] == 'win' else 2
    for record, field in ((evidence, 'game_outcome_code'), (terminal, 'outcome_code')):
        if field in record and (type(record[field]) is not int or record[field] != expected_code):
            raise ValueError('Game outcome code contradicts the declared result')
    if 'terminal_confirmed' in terminal and terminal['terminal_confirmed'] is not True:
        raise ValueError('Game ending is not confirmed by terminal state')
    if 'outcome' in terminal and terminal['outcome'] != result['outcome']:
        raise ValueError('Terminal state outcome contradicts the declared result')


def validate_battle_result(result, expected_session):
    """Grade only a confirmed host ending with complete scoring metadata."""
    validate_terminal_identity(result, expected_session)
    progress = result.get('progress_score')
    if isinstance(progress, bool) or not isinstance(progress, (int, float)) or not 0 <= progress <= 100:
        raise ValueError('Missing or invalid host progress_score')
    if result['outcome'] == 'win' and progress != 100:
        raise ValueError('A win must have complete opponent defeat progress')
    return {'win': int(result['outcome'] == 'win'), 'score': 100 * int(result['outcome'] == 'win'),
            'progress': float(progress)}


class CynthiaVerifier(BaseVerifier):
    async def verify(self):
        env = self.environment
        session_id = env.battle_session['session_id']
        env.resolve_framework_cancellation()
        if getattr(env, 'finalize_reason', None):
            await asyncio.to_thread(env.admin.request, '/admin/sessions/' + session_id + '/finalize',
                                    {'reason': env.finalize_reason})
        result = await asyncio.to_thread(env.admin.request, '/admin/sessions/' + session_id + '/result')
        destination = self.trial_paths.verifier_dir
        destination.mkdir(parents=True, exist_ok=True)
        (destination / 'battle-result.json').write_text(json.dumps(result, indent=2) + '\n')
        if env.agent_audit and env.agent_audit.get('delegation_review_required'):
            raise ValueError('Possible model delegation flagged; manually review before scoring')
        if env.agent_audit and (env.agent_audit.get('model_mismatch') or env.agent_audit.get('cli_pin_error')):
            raise ValueError('Infrastructure/model validity failure; not a scored battle loss')
        rewards = validate_battle_result(result, session_id)
        (destination / 'reward.json').write_text(json.dumps(rewards, indent=2) + '\n')
        return VerifierResult(rewards=rewards)
