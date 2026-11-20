"""Authenticated host bridge: one private JSONL emulator worker per Harbor trial.

Run with: PYTHONPATH=src .venv/bin/python -m cynthiabench.server
Administrator credentials are written only to the owner-readable connection file.
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import queue
import re
import secrets
import signal
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit
import uuid
from .server_preflight import configuration


ROOT = Path(__file__).resolve().parents[2]
MAX_REQUEST_BYTES = 32768
MAX_WORKER_LINE = 4 * 1024 * 1024
IDENTIFIER = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$')
PUBLIC_FIELDS = frozenset({'frame', 'screen_png_base64', 'public_state', 'terminal', 'outcome',
                           'controls', 'available_actions', 'phase', 'message', 'error'})


class BridgeError(RuntimeError):
    def __init__(self, message, status=500):
        super().__init__(message)
        self.status = status


def safe_identifier(value):
    return isinstance(value, str) and bool(IDENTIFIER.fullmatch(value)) and '..' not in value


def write_json(path, value, mode=None):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp')
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode or 0o600)
    if mode is not None:
        os.fchmod(descriptor, mode)
    with os.fdopen(descriptor, 'w') as file:
        json.dump(value, file, indent=2)
        file.write('\n')
        file.flush()
        os.fsync(file.fileno())
    temporary.replace(path)


def public_observation(response):
    observation = response.get('observation', response)
    if not isinstance(observation, dict):
        raise BridgeError('Worker returned an invalid observation')
    return {key: value for key, value in observation.items() if key in PUBLIC_FIELDS}


class WorkerSession:
    def __init__(self, *, session_id, team_id, seed, trial_id, output, worker_command,
                 ready_timeout=180, request_timeout=120):
        self.session_id, self.team_id, self.seed, self.trial_id = session_id, team_id, seed, trial_id
        self.output = Path(output)
        self.request_timeout = request_timeout
        self.lock = threading.RLock()
        self.responses = queue.Queue(maxsize=32)
        self.closed = False
        self.failed = False
        self.command_count = 0
        self.created_at = time.time()
        self.last_result = None
        self.output.mkdir(parents=True, exist_ok=False)
        self.stderr = (self.output / 'worker-stderr.log').open('wb')
        command = [*worker_command, '--team', team_id, '--seed', str(seed), '--trial', trial_id,
                   '--output', str(self.output)]
        env = os.environ.copy()
        env['PYTHONPATH'] = str(ROOT / 'src')
        for key in list(env):
            if key.startswith('CYNTHIA_ADMIN') or key in ('OPENAI_API_KEY', 'CODEX_API_KEY',
                'ANTHROPIC_API_KEY', 'GOOGLE_API_KEY', 'GEMINI_API_KEY', 'CLAUDE_CODE_OAUTH_TOKEN'):
                env.pop(key, None)
        self.process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=self.stderr, cwd=ROOT, env=env, start_new_session=True)
        write_json(self.output / 'server-session.json', {
            'session_id': session_id, 'trial_id': trial_id, 'team_id': team_id, 'seed': seed,
            'pid': self.process.pid, 'command': command, 'created_at': self.created_at,
        })
        self.reader = threading.Thread(target=self._read_responses, daemon=True,
                                       name='cynthia-worker-' + session_id)
        self.reader.start()
        try:
            ready = self._receive(ready_timeout)
            if ready.get('error'):
                raise BridgeError('Worker reported initialization failure', 503)
            self.initial_observation = public_observation(ready)
            if not self.initial_observation:
                raise BridgeError('Worker produced no initial observation', 503)
        except Exception:
            self._fail('Worker did not produce a valid ready observation')
            self.stderr.close()
            self.process.stdin.close()
            self.process.stdout.close()
            raise

    def _read_responses(self):
        try:
            while True:
                raw = self.process.stdout.readline(MAX_WORKER_LINE + 1)
                if not raw:
                    self.responses.put(BridgeError('Worker exited before responding', 503), timeout=1)
                    return
                if len(raw) > MAX_WORKER_LINE or not raw.endswith(b'\n'):
                    self.responses.put(BridgeError('Worker response exceeded JSONL limits', 503), timeout=1)
                    return
                try:
                    response = json.loads(raw)
                    if not isinstance(response, dict):
                        raise ValueError('not object')
                except (UnicodeDecodeError, ValueError):
                    self.responses.put(BridgeError('Worker emitted malformed JSON', 503), timeout=1)
                    return
                self.responses.put(response, timeout=1)
        except (OSError, queue.Full):
            return

    def _receive(self, timeout):
        try:
            response = self.responses.get(timeout=timeout)
        except queue.Empty:
            raise BridgeError('Worker response timed out', 504) from None
        if isinstance(response, Exception):
            raise response
        return response

    def _terminate(self):
        """Reap the owned worker even if it exits between poll and killpg.

        A missing process group is a normal shutdown race. Never allow that
        race to skip session cleanup or overwrite an already verified game.
        """
        for sig in (signal.SIGTERM, signal.SIGKILL):
            if self.process.poll() is not None:
                return True
            try:
                os.killpg(self.process.pid, sig)
            except ProcessLookupError:
                pass
            try:
                self.process.wait(timeout=3)
                return True
            except subprocess.TimeoutExpired:
                continue
        return self.process.poll() is not None

    def _record_error(self, reason):
        path = self.output / 'bridge-errors.json'
        records = json.loads(path.read_text()) if path.exists() else []
        records.append({'time': time.time(), 'reason': str(reason),
                        'terminal_result_preserved': bool(self.last_result and
                            self.last_result.get('valid') is True and self.last_result.get('terminal') is True)})
        write_json(path, records)

    def _fail(self, reason):
        self.failed = True
        try:
            if not self._terminate():
                self._record_error('Worker did not exit after SIGKILL')
        except OSError as exc:
            self._record_error('Worker termination failed: ' + str(exc))
        self._record_error(reason)
        # A transport/cleanup failure after a completed battle cannot create a
        # replacement opportunity. Keep game evidence immutable, record the
        # infrastructure defect separately, and let artifact review block release.
        if self.last_result and self.last_result.get('valid') is True and self.last_result.get('terminal') is True:
            return
        self.last_result = {'session_id': self.session_id, 'team_id': self.team_id, 'seed': self.seed,
            'trial_id': self.trial_id, 'valid': False, 'terminal': False, 'outcome': 'invalid',
            'infrastructure_error': reason}
        write_json(self.output / 'bridge-result.json', self.last_result)

    def request(self, operation, **parameters):
        with self.lock:
            if self.closed:
                raise BridgeError('Session is closed', 410)
            if self.failed:
                raise BridgeError('Session has an infrastructure failure', 503)
            if operation not in ('observe', 'action', 'result', 'finalize', 'close'):
                raise BridgeError('Unsupported worker operation', 400)
            command = {'op': operation, **parameters}
            self.command_count += 1
            if operation in ('action', 'finalize'):
                with (self.output / 'server-commands.jsonl').open('a') as log:
                    log.write(json.dumps({'index': self.command_count, 'time': time.time(), **command}) + '\n')
            try:
                self.process.stdin.write(json.dumps(command).encode() + b'\n')
                self.process.stdin.flush()
                response = self._receive(self.request_timeout)
            except (OSError, BridgeError) as exc:
                self._fail(str(exc))
                raise BridgeError('Emulator communication failed; trial marked invalid', 503) from None
            return response

    def result(self):
        with self.lock:
            if self.failed or self.closed:
                if self.last_result is None:
                    raise BridgeError('Closed session has no result', 410)
                return self.last_result
            return self._persist_result(self.request('result'))

    def _persist_result(self, response):
        response = response.get('result', response)
        if not isinstance(response, dict):
            self._fail('Worker result is not an object')
            return self.last_result
        for key, expected in (('team_id', self.team_id), ('seed', self.seed), ('trial_id', self.trial_id)):
            if key in response and response[key] != expected:
                self._fail('Worker result identity mismatch')
                return self.last_result
        self.last_result = {**response, 'session_id': self.session_id, 'team_id': self.team_id,
                            'seed': self.seed, 'trial_id': self.trial_id}
        write_json(self.output / 'bridge-result.json', self.last_result)
        return self.last_result

    def finalize(self, reason):
        if reason not in ('agent_stopped', 'timeout', 'runtime_error'):
            raise BridgeError('Invalid finalization reason', 400)
        with self.lock:
            if self.closed or self.failed:
                return self.result()
            # A provider quota/error after a verified terminal battle does not
            # undo the game. Never send a finalizer that could erase that result.
            current = self.result()
            if current.get('valid') is True and current.get('terminal') is True:
                return current
            self.request('finalize', reason=reason)
            return self.result()

    def close(self):
        with self.lock:
            if self.closed:
                return {'closed': True, 'session_id': self.session_id}
            try:
                if not self.failed:
                    self.result()
                    closed = self.request('close')
                    final = closed.get('result', closed)
                    if isinstance(final, dict) and 'outcome' in final:
                        self._persist_result(closed)
            except BridgeError:
                pass
            finally:
                try:
                    self.closed = self._terminate()
                    if not self.closed:
                        self.failed = True
                        self._record_error('Worker remains alive after close; admission slot remains occupied')
                except OSError as exc:
                    self.failed = True
                    self._record_error('Worker close failed: ' + str(exc))
                self.stderr.close()
                if self.process.stdin:
                    self.process.stdin.close()
                if self.process.stdout:
                    self.process.stdout.close()
            return {'closed': self.closed, 'session_id': self.session_id}


class SessionManager:
    def __init__(self, *, team_ids, output_root=ROOT / 'runs/engine', worker_command=None,
                 admin_token=None, max_sessions=4, ready_timeout=180, request_timeout=120,
                 startup_configuration=None, team_specs_path=None):
        self.team_ids = frozenset(team_ids)
        if not self.team_ids or not all(safe_identifier(value) for value in self.team_ids):
            raise ValueError('An explicit valid team-ID whitelist is required')
        self.output_root = Path(output_root)
        self.worker_command = worker_command or [sys.executable, '-u', '-m', 'cynthiabench.worker']
        self.admin_token = admin_token or secrets.token_urlsafe(48)
        self.max_sessions = max_sessions
        self.ready_timeout, self.request_timeout = ready_timeout, request_timeout
        self.lock = threading.RLock()
        self.sessions = {}
        self.tokens = {}
        self.pending = 0
        self.trials = set()
        self.startup_configuration = startup_configuration
        self.team_specs_path = team_specs_path
        self.started_at = time.time()

    def status(self):
        current = configuration(ROOT, self.team_specs_path) if self.startup_configuration is not None else None
        keys = ('team_ids', 'team_specs_sha256', 'task_manifest_sha256', 'server_code_sha256')
        matches = bool(current) and all(current[key] == self.startup_configuration.get(key) for key in keys)
        with self.lock:
            active = sum(session.process.poll() is None for session in self.sessions.values())
        return {'service': 'CynthiaBench', 'pid': os.getpid(), 'started_at': self.started_at,
                'active_sessions': active, 'pending_sessions': self.pending, 'max_sessions': self.max_sessions,
                'startup_configuration': self.startup_configuration, 'current_configuration': current,
                'configuration_matches_startup': matches}

    def is_admin(self, token):
        return bool(token) and hmac.compare_digest(token, self.admin_token)

    def agent_session(self, token):
        digest = hashlib.sha256(token.encode()).hexdigest()
        with self.lock:
            session_id = self.tokens.get(digest)
            if session_id is None:
                raise BridgeError('Unauthorized', 401)
            return self.sessions[session_id]

    def get_session(self, session_id):
        if not safe_identifier(session_id):
            raise BridgeError('Invalid session ID', 400)
        with self.lock:
            if session_id not in self.sessions:
                raise BridgeError('Unknown session', 404)
            return self.sessions[session_id]

    def create(self, request):
        team_id, seed, trial_id = request.get('team_id'), request.get('seed'), request.get('trial_id')
        if not safe_identifier(team_id) or team_id not in self.team_ids:
            raise BridgeError('Unknown or invalid team ID', 400)
        if not safe_identifier(trial_id):
            raise BridgeError('Invalid trial ID', 400)
        if isinstance(seed, bool) or not isinstance(seed, int) or not 0 <= seed <= 0xffffffff:
            raise BridgeError('Seed must be an unsigned 32-bit integer', 400)
        with self.lock:
            active = sum(s.process.poll() is None for s in self.sessions.values())
            if active + self.pending >= self.max_sessions:
                raise BridgeError('Active session limit reached', 429)
            output = self.output_root / trial_id
            if trial_id in self.trials or output.exists():
                raise BridgeError('Trial ID already exists; refusing to overwrite evidence', 409)
            self.pending += 1
            self.trials.add(trial_id)
        session_id = uuid.uuid4().hex
        token = secrets.token_urlsafe(48)
        try:
            session = WorkerSession(session_id=session_id, team_id=team_id, seed=seed,
                trial_id=trial_id, output=output, worker_command=self.worker_command,
                ready_timeout=self.ready_timeout, request_timeout=self.request_timeout)
            with self.lock:
                self.sessions[session_id] = session
                self.tokens[hashlib.sha256(token.encode()).hexdigest()] = session_id
            return {'session_id': session_id, 'agent_token': token}
        finally:
            with self.lock:
                self.pending -= 1

    def close_all(self):
        with self.lock:
            sessions = list(self.sessions.values())
        for session in sessions:
            session.close()


class BattleHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 32

    def __init__(self, address, manager):
        self.manager = manager
        super().__init__(address, BattleHandler)


class BattleHandler(BaseHTTPRequestHandler):
    server_version = 'CynthiaBench/1'

    def setup(self):
        super().setup()
        self.connection.settimeout(15)

    def log_message(self, format, *args):
        # Headers/tokens are deliberately never logged. Host diagnostics are in
        # each session's worker-stderr.log and server-commands.jsonl.
        return

    def _send(self, status, data):
        raw = json.dumps(data).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(raw)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(raw)

    def _body(self):
        if self.headers.get('Transfer-Encoding'):
            raise BridgeError('Chunked requests are unsupported', 400)
        try:
            length = int(self.headers.get('Content-Length', '0'))
        except ValueError:
            raise BridgeError('Invalid request length', 400) from None
        if length < 0 or length > MAX_REQUEST_BYTES:
            raise BridgeError('Request body too large', 413)
        try:
            body = json.loads(self.rfile.read(length) or b'{}')
        except (ValueError, UnicodeDecodeError):
            raise BridgeError('Invalid JSON body', 400) from None
        if not isinstance(body, dict):
            raise BridgeError('JSON body must be an object', 400)
        return body

    def do_GET(self):
        self._dispatch()

    def do_POST(self):
        self._dispatch()

    def _dispatch(self):
        try:
            path = urlsplit(self.path).path
            if path == '/health' and self.command == 'GET':
                self._send(200, {'ok': True, 'service': 'CynthiaBench'})
                return
            header = self.headers.get('Authorization', '')
            token = header[7:] if header.startswith('Bearer ') else ''
            manager = self.server.manager
            if path.startswith('/admin/'):
                if not manager.is_admin(token):
                    raise BridgeError('Unauthorized', 401)
                if path == '/admin/status' and self.command == 'GET':
                    self._send(200, manager.status())
                    return
                if path == '/admin/sessions' and self.command == 'POST':
                    response = manager.create(self._body())
                else:
                    match = re.fullmatch(r'/admin/sessions/([A-Za-z0-9_.-]+)/(result|finalize|close)', path)
                    if not match:
                        raise BridgeError('Unknown route', 404)
                    session = manager.get_session(match.group(1))
                    operation = match.group(2)
                    if operation == 'result' and self.command == 'GET':
                        response = session.result()
                    elif operation == 'finalize' and self.command == 'POST':
                        response = session.finalize(self._body().get('reason'))
                    elif operation == 'close' and self.command == 'POST':
                        self._body()
                        response = session.close()
                    else:
                        raise BridgeError('Method not allowed', 405)
            elif path in ('/v1/observe', '/v1/action'):
                session = manager.agent_session(token)
                if path == '/v1/observe' and self.command == 'GET':
                    response = public_observation(session.request('observe'))
                elif path == '/v1/action' and self.command == 'POST':
                    response = public_observation(session.request('action', action=self._body()))
                else:
                    raise BridgeError('Method not allowed', 405)
            else:
                raise BridgeError('Unknown route', 404)
            self._send(200, response)
        except BridgeError as exc:
            self._send(exc.status, {'error': str(exc)})
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception:
            self._send(500, {'error': 'Internal bridge error; no valid result inferred'})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=0)
    parser.add_argument('--admin-config', type=Path, default=ROOT / 'work/admin.json')
    parser.add_argument('--team-specs', type=Path, default=ROOT / 'teams/team-specs.json')
    parser.add_argument('--output-root', type=Path, default=ROOT / 'runs/engine')
    parser.add_argument('--max-sessions', type=int, default=4)
    args = parser.parse_args()
    startup = configuration(ROOT, args.team_specs)
    manager = SessionManager(team_ids=startup['team_ids'], output_root=args.output_root,
                             max_sessions=args.max_sessions, startup_configuration=startup,
                             team_specs_path=args.team_specs)
    server = BattleHTTPServer(('127.0.0.1', args.port), manager)
    url = f'http://127.0.0.1:{server.server_port}'
    write_json(args.admin_config, {'url': url, 'admin_token': manager.admin_token}, mode=0o600)
    print(json.dumps({'url': url, 'pid': os.getpid(), 'admin_config': str(args.admin_config)}), flush=True)
    def terminate_server(signum, frame):
        # shutdown must run off the serve_forever thread. This gives SIGTERM
        # a graceful path that stops all child process groups and persists results.
        threading.Thread(target=server.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, terminate_server)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        manager.close_all()


if __name__ == '__main__':
    main()
