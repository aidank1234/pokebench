"""Host-owned, Flash-only DeepSeek transport and shared study spending guard.

This process owns the provider credential. Native OpenCode receives only a
per-trial proxy token. It is not an agent, tool runner, or benchmark verifier.
"""
from __future__ import annotations

import argparse
import copy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import signal
import tempfile
import threading
import time
from typing import NamedTuple
from urllib.parse import unquote, urlsplit

MODEL = 'deepseek-flash'
EFFORT = 'max'
MAX_OUTPUT = 393_216
MAX_INPUT_BOUND = 1_048_576
MAX_BODY = 48 * 1024 * 1024
RATES = {'hit': Decimal('0.006'), 'miss': Decimal('0.30'), 'output': Decimal('1.20')}
MILLION = Decimal(1_000_000)
DELEGATION = frozenset({'task', 'agent', 'subagent', 'delegate', 'delegate_task',
                         'invoke_subagent', 'manage_subagents', 'define_subagent'})
NATIVE_HEADERS = frozenset({'user-agent', 'accept', 'x-opencode-session-id',
                            'x-session-affinity', 'x-session-id'})


class CarryLedger(NamedTuple):
    path: str
    sha256: str
    known_metered: Decimal
    unknown_upper: Decimal
    committed_upper: Decimal
    development_only: bool


def new_study_ledger(output_root):
    """Record an explicit new study before touching the provider credential.

    Existing output/accounting directories are never reused. Continued studies
    must instead supply their reviewed carry ledger.
    """
    root = Path(output_root).absolute()
    if root.exists() or root.is_symlink():
        raise ValueError('A new study requires a previously unused output directory')
    root.parent.mkdir(parents=True, exist_ok=True)
    path = root.with_name(root.name + '-initial-ledger.json')
    record = {'schema_version': 1, 'kind': 'new-independent-study', 'currency': 'USD',
              'study_cap_usd': '50', 'prior_known_metered_usd': '0',
              'prior_unknown_upper_usd': '0', 'prior_committed_upper_usd': '0',
              'created_at': datetime.now(timezone.utc).isoformat()}
    raw = (json.dumps(record, indent=2) + '\n').encode()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
        os.fchmod(stream.fileno(), 0o400)
    return CarryLedger(str(path), hashlib.sha256(raw).hexdigest(),
                       Decimal(0), Decimal(0), Decimal(0), False)


def load_carry_ledger(path, *, allow_zero_fixture=False, _ancestors=()):
    """Validate immutable previous liabilities before any credential/API access.

    A balance snapshot never releases an unmetered prior reservation. Production
    startup requires the pinned old request log and all closed trial statuses.
    Zero ledgers are available only through an explicit offline-test argument.
    """
    path = Path(path).absolute()
    if str(path) in _ancestors or len(_ancestors) >= 12:
        raise ValueError('Carry ledger chain is cyclic or too deep')
    if path.is_symlink() or path.resolve() != path or not path.is_file():
        raise ValueError('Carry ledger is absent or redirected')
    if path.stat().st_mode & 0o222:
        raise ValueError('Carry ledger must be a read-only immutable record')
    raw = path.read_bytes()
    record = json.loads(raw)
    if (not isinstance(record, dict) or record.get('schema_version') not in (1, 2)
            or record.get('currency') != 'USD' or record.get('study_cap_usd') != '50'):
        raise ValueError('Carry ledger must preserve the original USD50 study cap')

    def amount(key):
        value = record.get(key)
        if not isinstance(value, str):
            raise ValueError('Carry amounts must be exact decimal strings')
        parsed = Decimal(value)
        if not parsed.is_finite() or parsed < 0:
            raise ValueError('Invalid carry amount')
        return parsed

    known, unknown, upper = (amount(key) for key in
        ('prior_known_metered_usd', 'prior_unknown_upper_usd', 'prior_committed_upper_usd'))
    if known + unknown != upper:
        raise ValueError('Carry totals are inconsistent')
    development = record.get('development_only') is True
    if development:
        if record['schema_version'] != 1 or not allow_zero_fixture or any((known, unknown, upper)):
            raise ValueError('Development zero carry is forbidden for production startup')
    else:
        evidence = record.get('evidence_sha256')
        statuses = record.get('prior_trial_statuses')
        audit = record.get('prior_provider_audit')
        if not isinstance(evidence, dict) or not evidence or not isinstance(statuses, list) or not statuses or not isinstance(audit, str):
            raise ValueError('Carry ledger lacks pinned old log/status evidence')
        for name, expected in evidence.items():
            source = Path(name)
            if (not source.is_absolute() or source.is_symlink() or not source.is_file()
                    or not isinstance(expected, str) or not re.fullmatch('[a-f0-9]{64}', expected)):
                raise ValueError('Invalid carry evidence commitment')
            with source.open('rb') as stream:
                actual = hashlib.file_digest(stream, 'sha256').hexdigest()
            if actual != expected:
                raise ValueError('Carry evidence hash drift: ' + name)
        if audit not in evidence or any(name not in evidence for name in statuses):
            raise ValueError('Carry log/status files are not committed')
        previous = None
        local_known, local_unknown, local_upper = known, unknown, upper
        if record['schema_version'] == 2:
            link = record.get('previous_carry_ledger')
            if (not isinstance(link, dict) or not isinstance(link.get('path'), str)
                    or link['path'] not in evidence or link.get('sha256') != evidence[link['path']]):
                raise ValueError('Carry chain lacks a committed previous ledger')
            previous = load_carry_ledger(link['path'], _ancestors=(*_ancestors, str(path)))
            if previous.development_only or previous.sha256 != link['sha256']:
                raise ValueError('Previous carry ledger identity is invalid')
            local_known, local_unknown, local_upper = (amount(key) for key in
                ('local_known_metered_usd', 'local_unknown_upper_usd', 'local_committed_upper_usd'))
            if (local_known + local_unknown != local_upper
                    or previous.known_metered + local_known != known
                    or previous.unknown_upper + local_unknown != unknown
                    or previous.committed_upper + local_upper != upper):
                raise ValueError('Carry chain totals omit or duplicate previous liabilities')
        rows = [json.loads(Path(name).read_text()) for name in statuses]
        if len({r.get('trial_id') for r in rows}) != len(rows) or any(
                r.get('closed') is not True or r.get('inflight') != 0
                or r.get('completed_request_count') != r.get('model_request_count') for r in rows):
            raise ValueError('Prior trial scopes are not completely closed/accounted')
        row_known = sum((Decimal(r['estimated_cost_usd']) for r in rows), Decimal(0))
        row_upper = sum((Decimal(r['estimated_cost_upper_usd']) for r in rows), Decimal(0))
        if row_known != local_known or row_upper != local_upper:
            raise ValueError('Carry amounts disagree with closed old scopes')
        events = [json.loads(line) for line in Path(audit).read_text().splitlines()]
        if previous is not None:
            carried = [e for e in events if e.get('event') == 'prior_budget_carried']
            if (len(carried) != 1 or carried[0].get('currency') != 'USD'
                    or carried[0].get('carry_ledger_sha256') != previous.sha256
                    or Decimal(carried[0].get('prior_known_metered_usd', 'NaN')) != previous.known_metered
                    or Decimal(carried[0].get('prior_unknown_upper_usd', 'NaN')) != previous.unknown_upper
                    or Decimal(carried[0].get('prior_committed_upper_usd', 'NaN')) != previous.committed_upper):
                raise ValueError('Real previous provider log does not confirm its inherited carry')
        starts = [e for e in events if e.get('event') == 'model_request_started']
        ends = [e for e in events if e.get('event') == 'model_request_finished']
        if (len({e['request_id'] for e in starts}) != len(starts)
                or len({e['request_id'] for e in ends}) != len(ends)
                or {e['request_id'] for e in starts} != {e['request_id'] for e in ends}):
            raise ValueError('Prior provider requests have incomplete or duplicate accounting')
        if sum((Decimal(e['cost_usd_upper_bound']) for e in ends), Decimal(0)) != local_upper:
            raise ValueError('Carry amount disagrees with old provider request log')
        audit_known = sum((Decimal(e['cost_usd_upper_bound']) for e in ends
                           if e.get('usage') is not None and e.get('usage_uncertainty') is False), Decimal(0))
        if audit_known != local_known:
            raise ValueError('Metered carry amount disagrees with old typed usage')
    if path.read_bytes() != raw:
        raise ValueError('Carry ledger changed while being verified')
    return CarryLedger(str(path), hashlib.sha256(raw).hexdigest(), known, unknown, upper, development)


def utcnow():
    return datetime.now(timezone.utc)


def encoded(value):
    return json.dumps(value, separators=(',', ':'), ensure_ascii=False).encode()


def price(usage, *, peak=True):
    """Account for completion tokens once; reasoning is already included."""
    if not isinstance(usage, dict):
        raise ValueError('Provider usage must be an object')
    prompt, completion = usage.get('prompt_tokens'), usage.get('completion_tokens')
    if type(prompt) is not int or type(completion) is not int or min(prompt, completion) < 0:
        raise ValueError('Missing or invalid provider token usage')
    cached = usage.get('prompt_cache_hit_tokens',
                       (usage.get('prompt_tokens_details') or {}).get('cached_tokens', 0))
    if type(cached) is not int or not 0 <= cached <= prompt:
        raise ValueError('Invalid provider cache token usage')
    miss = usage.get('prompt_cache_miss_tokens', prompt - cached)
    if type(miss) is not int or miss < 0 or miss + cached != prompt:
        raise ValueError('Inconsistent provider cache token usage')
    divisor = MILLION if peak else MILLION * 2
    return (cached * RATES['hit'] + miss * RATES['miss'] + completion * RATES['output']) / divisor


def overlaps_peak(start, end):
    """Use peak if any part of a request touches a weekday peak window.

    Chinese holidays are deliberately treated as weekdays, a conservative bound.
    """
    day = start.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    while day <= end:
        if day.weekday() < 5:
            for lo, hi in ((1, 4), (6, 10)):
                if start < day + timedelta(hours=hi) and end >= day + timedelta(hours=lo):
                    return True
        day += timedelta(days=1)
    return False


class GuardError(Exception):
    def __init__(self, status, code, message):
        self.status, self.code, self.message = status, code, message
        super().__init__(message)


class DownstreamDisconnect(OSError):
    """The native client's socket disappeared, distinct from provider failure."""


class SSEUsage:
    """Read only typed SSE envelopes, never classify assistant prose as errors."""
    def __init__(self):
        self.pending = b''
        self.lines = []
        self.usage = None
        self.models = set()
        self.done = False
        self.errors = []
        self.invalid = False

    def feed(self, chunk):
        self.pending += chunk
        if len(self.pending) > 8 * 1024 * 1024:
            raise ValueError('Oversized upstream SSE line')
        while b'\n' in self.pending:
            line, self.pending = self.pending.split(b'\n', 1)
            line = line.rstrip(b'\r')
            if not line:
                self.event()
            elif line.startswith(b'data:'):
                self.lines.append(line[5:].lstrip())

    def event(self):
        if not self.lines:
            return
        raw = b'\n'.join(self.lines)
        self.lines.clear()
        if raw == b'[DONE]':
            self.done = True
            return
        try:
            event = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            self.invalid = True
            return
        if not isinstance(event, dict):
            self.invalid = True
            return
        if isinstance(event.get('model'), str):
            self.models.add(event['model'])
        if isinstance(event.get('usage'), dict):
            self.usage = event['usage']
        if isinstance(event.get('error'), dict):
            self.errors.append(event['error'])

    def finish(self):
        if self.pending:
            self.feed(b'\n')
        self.event()


class HTTPSUpstream:
    """The only component that reads and sends the real provider API key."""
    def __init__(self, key_file):
        self.key = Path(key_file).read_text().strip()
        if not self.key:
            raise ValueError('Empty provider credential')

    def request(self, method, path, body=None, *, headers=None):
        connection = http.client.HTTPSConnection('api.deepseek.com', timeout=120)
        forwarded = {}
        for name, value in (headers or {}).items():
            if name.lower() in NATIVE_HEADERS:
                if not isinstance(value, str) or len(value) > 2048 or '\r' in value or '\n' in value:
                    raise ValueError('Invalid native transport header')
                forwarded[name] = value
        forwarded.update({'Authorization': 'Bearer ' + self.key, 'Accept-Encoding': 'identity'})
        if body is not None:
            forwarded['Content-Type'] = 'application/json'
        phase = 'connect'
        upload_error = None
        try:
            connection.connect()
            phase = 'request_send'
            try:
                connection.request(method, path, body=body, headers=forwarded)
            except (BrokenPipeError, ConnectionResetError) as exc:
                # A server may reject an upload before consuming the complete body.
                # Preserve a real HTTP response rather than hiding it as an EOF.
                upload_error = exc
                if connection.sock is None:
                    raise
                upload_socket = connection.sock
                upload_socket.settimeout(3)
                try:
                    response = connection.getresponse()
                except (OSError, http.client.HTTPException):
                    raise upload_error
                finally:
                    try:
                        upload_socket.settimeout(120)
                    except OSError:
                        pass
            else:
                phase = 'response_headers'
                response = connection.getresponse()
        except BaseException as exc:
            exc.proxy_transport_phase = phase
            connection.close()
            raise
        response._proxy_connection = connection
        response._proxy_transport_metadata = {
            'native_header_names': sorted(name.lower() for name in forwarded if name.lower() in NATIVE_HEADERS),
            'upload_error_with_http_response': type(upload_error).__name__ if upload_error else None}
        return response


def close_response(response):
    try:
        response.close()
    finally:
        connection = getattr(response, '_proxy_connection', None)
        if connection:
            connection.close()


class ProxyState:
    def __init__(self, output_root, halt_path, *, admin_token=None, provider_secret='',
                 cap_usd='50', reserve_usd='2', warning_usd='10', max_concurrent=2,
                 carry=None, balance_admission='worst-case'):
        if not isinstance(carry, CarryLedger):
            raise ValueError('A validated explicit carry ledger is required')
        self.root, self.halt_path = Path(output_root), Path(halt_path)
        if self.root.exists() and any(self.root.iterdir()):
            raise ValueError('Provider output root must be fresh; accounting cannot be reset')
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / 'trialstatus').mkdir(exist_ok=True)
        self.admin_token = admin_token or secrets.token_urlsafe(36)
        self.secrets = [x for x in (self.admin_token, provider_secret) if x]
        self.cap, self.reserve_floor, self.warning = map(Decimal, (cap_usd, reserve_usd, warning_usd))
        if not (Decimal(0) < self.reserve_floor < self.warning < self.cap <= Decimal(50)):
            raise ValueError('Invalid USD study limits')
        if type(max_concurrent) is not int or not 1 <= max_concurrent <= 2:
            raise ValueError('At most two concurrent model requests are permitted')
        self.max_concurrent = max_concurrent
        if balance_admission not in ('worst-case', 'reported-balance'):
            raise ValueError('Unknown balance admission policy')
        self.balance_admission = balance_admission
        self.carry = carry
        self.prior_known_metered = carry.known_metered
        self.prior_unknown_upper = carry.unknown_upper
        self.prior_committed_upper = carry.committed_upper
        self.current_known_metered = Decimal(0)
        self.current_committed_upper = Decimal(0)
        self.lock = threading.RLock()
        self.trials = {}
        self.tokens = {}
        self.inflight = {}
        self.committed_upper = self.prior_committed_upper
        self.reserved = Decimal(0)
        self.balance = None
        self.initial_balance = None
        self.balance_at_commit = Decimal(0)
        self.balance_at_known = Decimal(0)
        self.balance_time = None
        self.balance_poll_error = None
        self.halt_reason = None
        self.warned = False
        self.url = None
        self.audit('prior_budget_carried', currency='USD', study_cap_usd=str(self.cap),
            balance_admission=self.balance_admission,
            carry_ledger_sha256=carry.sha256, prior_known_metered_usd=str(self.prior_known_metered),
            prior_unknown_upper_usd=str(self.prior_unknown_upper),
            prior_committed_upper_usd=str(self.prior_committed_upper))

    def redact(self, value):
        # No request headers are logged. Also redact accidentally echoed keys.
        text = json.dumps(value, ensure_ascii=False)
        for secret in self.secrets:
            text = text.replace(secret, '[REDACTED]')
        return json.loads(text)

    def audit(self, event, **data):
        with self.lock:
            with (self.root / 'audit.jsonl').open('a') as out:
                out.write(json.dumps(self.redact({'event': event, 'at': utcnow().isoformat(), **data})) + '\n')

    def persist(self, trial):
        path = self.root / 'trialstatus' / (hashlib.sha256(trial.encode()).hexdigest() + '.json')
        record = self.status(trial)
        with tempfile.NamedTemporaryFile('w', dir=path.parent, delete=False) as out:
            temp = Path(out.name)
            json.dump(self.redact(record), out, indent=2)
        os.replace(temp, path)

    def halt(self, reason, **details):
        with self.lock:
            if self.halt_reason is not None:
                return
            self.halt_reason = reason
            record = self.redact({'schema_version': 1, 'provider': 'deepseek',
                'halted': True, 'reason': reason, 'at': utcnow().isoformat(), **details})
            self.halt_path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile('w', dir=self.halt_path.parent, delete=False) as out:
                temp = Path(out.name)
                json.dump(record, out, indent=2)
                out.flush()
                os.fsync(out.fileno())
            try:
                os.link(temp, self.halt_path)
            except FileExistsError:
                pass
            finally:
                temp.unlink(missing_ok=True)
            self.audit('study_halted', **record)

    def effective_balance(self):
        if self.balance is None:
            return None
        if self.balance_admission == 'reported-balance':
            return self.balance - max(Decimal(0), self.current_known_metered - self.balance_at_known) - self.reserved
        return self.balance - max(Decimal(0), self.committed_upper - self.balance_at_commit) - self.reserved

    def admission_remaining(self):
        if self.balance_admission == 'reported-balance':
            return self.effective_balance()
        return min(self.cap - self.committed_upper - self.reserved, self.effective_balance())

    def set_balance(self, data):
        with self.lock:
            infos = data.get('balance_infos') if isinstance(data, dict) else None
            usd = [i for i in infos if isinstance(i, dict) and i.get('currency') == 'USD'] if isinstance(infos, list) else []
            try:
                amount = Decimal(usd[0]['total_balance']) if len(usd) == 1 else None
                if amount is None or not amount.is_finite() or amount < 0:
                    raise ValueError('USD balance is absent or invalid')
            except (KeyError, ValueError, ArithmeticError, TypeError):
                self.halt('balance_currency_unverified')
                raise GuardError(503, 'balance_currency_unverified', 'A verified USD account balance is required') from None
            self.balance, self.balance_at_commit = amount, self.committed_upper
            self.balance_at_known = self.current_known_metered
            self.initial_balance = amount if self.initial_balance is None else self.initial_balance
            self.balance_time = time.monotonic()
            self.balance_poll_error = None
            self.audit('balance_snapshot', balance_infos=data['balance_infos'],
                       is_available=data.get('is_available'), study_committed_upper_usd=str(self.committed_upper))
            if data.get('is_available') is not True or amount <= self.reserve_floor:
                self.halt('insufficient_balance_or_reserve', balance_usd=str(amount))
            self.check_warning()

    def check_warning(self):
        remaining = self.admission_remaining() if self.balance is not None else self.cap
        if remaining <= self.warning and not self.warned:
            self.warned = True
            self.audit('low_balance_warning', remaining_usd_lower_bound=str(remaining),
                       warning_threshold_usd=str(self.warning))

    def can_continue(self):
        if self.halt_reason or self.halt_path.exists() or self.halt_path.is_symlink():
            raise GuardError(503, 'study_halted', self.halt_reason or 'Persistent study halt exists')
        if self.balance_time is None or time.monotonic() - self.balance_time > 60:
            self.halt('balance_monitor_unavailable')
            raise GuardError(503, 'balance_monitor_unavailable', 'USD balance verification is stale')
        remaining = self.admission_remaining()
        if remaining <= self.reserve_floor:
            self.halt('budget_reserve_reached', remaining_usd_lower_bound=str(remaining))
            raise GuardError(402, 'budget_reserve_reached', 'Study spending reserve reached')

    def register(self, trial):
        if not isinstance(trial, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,200}', trial):
            raise GuardError(400, 'invalid_trial', 'A safe unique trial_id is required')
        with self.lock:
            self.can_continue()
            if trial in self.trials:
                raise GuardError(409, 'duplicate_trial', 'Trial already registered; session reuse is forbidden')
            token = secrets.token_urlsafe(36)
            self.secrets.append(token)
            self.tokens[hashlib.sha256(token.encode()).digest()] = trial
            self.trials[trial] = {'trial_id': trial, 'model_request_count': 0,
                'completed_request_count': 0, 'inflight': 0, 'typed_failures': [],
                'usage_uncertainty': False, 'usage': [], 'estimated_cost_usd': '0',
                'estimated_cost_upper_usd': '0', 'closed': False,
                'cancellation_requested': False, 'cancellation_requested_at': None,
                'pre_cancellation_provider_failure': False,
                'pre_cancellation_infrastructure_failure': False,
                'cancellation_usage_uncertainty': False, 'model_mismatch': False,
                'liability_bounds_valid': True}
            self.persist(trial)
            self.audit('trial_registered', trial_id=trial)
            return {'trial_id': trial, 'token': token, 'base_url': self.url + '/v1',
                    'model': MODEL, 'reasoning_effort': EFFORT, 'max_tokens': MAX_OUTPUT}

    def authenticate(self, token):
        with self.lock:
            trial = self.tokens.get(hashlib.sha256(token.encode()).digest())
            if trial is None or self.trials[trial]['closed']:
                raise GuardError(401, 'invalid_proxy_token', 'Unknown or closed trial credential')
            return trial

    def status(self, trial):
        with self.lock:
            if trial not in self.trials:
                raise GuardError(404, 'unknown_trial', 'Trial is not registered')
            row = copy.deepcopy(self.trials[trial])
            row['recovered_transient_failures'] = [f for f in row['typed_failures']
                if f.get('recoverable') is True and f.get('recovery_status') == 'recovered']
            row['unresolved_transient_failures'] = [f for f in row['typed_failures']
                if f.get('recoverable') is True and f.get('recovery_status') == 'unresolved']
            row['hard_failures'] = [f for f in row['typed_failures'] if f.get('recoverable') is not True]
            row['request_usage_complete'] = not row['usage_uncertainty']
            row['outstanding_financial_liability_usd'] = str(
                Decimal(row['estimated_cost_upper_usd']) - Decimal(row['estimated_cost_usd']))
            row['recovered_transient_unknown_upper_usd'] = str(sum(
                (Decimal(f['retained_liability_upper_usd']) for f in row['recovered_transient_failures']), Decimal(0)))
            row.update(budget_halted=bool(self.halt_reason or self.halt_path.exists() or self.halt_path.is_symlink()),
                       halt_reason=self.halt_reason, currency='USD', model=MODEL,
                       reasoning_effort=EFFORT, budget_carry_ledger_sha256=self.carry.sha256)
            return row

    def mark_cancelled(self, trial, reason='framework_cancellation_pending'):
        """Host marker records causality, without asserting a time deadline."""
        with self.lock:
            if trial not in self.trials:
                raise GuardError(404, 'unknown_trial', 'Trial is not registered')
            row = self.trials[trial]
            if not row['cancellation_requested']:
                row['cancellation_requested'] = True
                row['cancellation_requested_at'] = utcnow().isoformat()
                row['cancellation_reason'] = reason
                active = [f for f in row['typed_failures'] if f.get('recovery_status') != 'recovered']
                row['pre_cancellation_provider_failure'] = any(
                    f.get('source', '').startswith('provider_') for f in active)
                # A recovered request still holds its full financial liability,
                # but no longer represents an active infrastructure failure.
                recovered_unknown = sum((Decimal(f['retained_liability_upper_usd'])
                    for f in row['typed_failures'] if f.get('recovery_status') == 'recovered'), Decimal(0))
                unknown = Decimal(row['estimated_cost_upper_usd']) - Decimal(row['estimated_cost_usd'])
                row['pre_cancellation_infrastructure_failure'] = bool(
                    active or not row['liability_bounds_valid'] or unknown != recovered_unknown)
                self.audit('native_cancellation_marked', trial_id=trial, reason=reason,
                    pre_cancellation_provider_failure=row['pre_cancellation_provider_failure'],
                    pre_cancellation_infrastructure_failure=row['pre_cancellation_infrastructure_failure'],
                    inflight=row['inflight'])
                self.persist(trial)
            return self.status(trial)

    def close_trial(self, trial):
        """Revoke the trial credential without erasing pending billing evidence."""
        with self.lock:
            if trial not in self.trials:
                raise GuardError(404, 'unknown_trial', 'Trial is not registered')
            self.trials[trial]['closed'] = True
            self.tokens = {token: owner for token, owner in self.tokens.items() if owner != trial}
            self.audit('trial_closed', trial_id=trial, inflight=self.trials[trial]['inflight'])
            self.persist(trial)
            return self.status(trial)

    def reject(self, trial, error):
        """A locally rejected native request cannot look like a clean actor stop."""
        with self.lock:
            failure = {'source': 'proxy_guard', 'http_status': error.status,
                       'code': error.code, 'upstream_forwarded': False}
            if self.trials[trial]['cancellation_requested'] and error.code == 'trial_cancelled':
                failure['cancellation_consequence'] = True
                self.audit('cancelled_request_prevented', trial_id=trial, **failure)
                return
            self.trials[trial]['typed_failures'].append(failure)
            self.halt('proxy_request_rejected', trial_id=trial, failure=failure)
            self.audit('request_rejected', trial_id=trial, **failure)
            self.persist(trial)

    def begin(self, trial, body):
        if not isinstance(body, dict) or body.get('model') != MODEL:
            raise GuardError(400, 'model_not_allowed', 'Only deepseek-flash is permitted')
        thinking = body.get('thinking')
        if thinking is not None and not isinstance(thinking, dict):
            raise GuardError(400, 'invalid_thinking', 'thinking must be an object')
        if body.get('reasoning_effort') != EFFORT or (thinking or {}).get('type') == 'disabled':
            raise GuardError(400, 'effort_not_allowed', 'reasoning_effort=max with thinking enabled is required')
        for tool in body.get('tools', []):
            if isinstance(tool, dict) and (tool.get('function') or {}).get('name', '').lower() in DELEGATION:
                raise GuardError(400, 'delegation_not_allowed', 'Delegation tools are forbidden')
        declared_maximum = body.get('max_tokens')
        maximum = MAX_OUTPUT if declared_maximum is None else declared_maximum
        if type(maximum) is not int or not 1 <= maximum <= MAX_OUTPUT:
            raise GuardError(400, 'invalid_max_tokens', 'max_tokens exceeds the advertised model bounds')
        forwarded = copy.deepcopy(body)
        if forwarded.get('stream'):
            forwarded['stream_options'] = {**(forwarded.get('stream_options') or {}), 'include_usage': True}
        semantic = hashlib.sha256(json.dumps(forwarded, sort_keys=True,
            separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode()).hexdigest()
        reservation = price({'prompt_tokens': MAX_INPUT_BOUND, 'completion_tokens': maximum})
        with self.lock:
            if self.trials[trial]['closed']:
                raise GuardError(401, 'trial_closed', 'Trial credential has been revoked')
            if self.trials[trial]['cancellation_requested']:
                raise GuardError(409, 'trial_cancelled', 'Host cancellation prevents new model requests')
            pending = [f for f in self.trials[trial]['typed_failures']
                if f.get('recoverable') is True and f.get('recovery_status') == 'unresolved']
            if any(f['semantic_request_sha256'] != semantic for f in pending):
                raise GuardError(409, 'unresolved_request_changed',
                    'An unresolved transient request must be retried with the same semantic body')
            self.can_continue()
            if len(self.inflight) >= self.max_concurrent:
                raise GuardError(429, 'proxy_concurrency_limit', 'At most two concurrent model requests are allowed')
            remaining = self.admission_remaining()
            if remaining - reservation < self.reserve_floor:
                self.halt('budget_reservation_denied', required_usd=str(reservation), remaining_usd_lower_bound=str(remaining))
                raise GuardError(402, 'budget_reservation_denied', 'Insufficient remaining study budget for request reservation')
            ident = secrets.token_hex(16)
            self.reserved += reservation
            self.inflight[ident] = {'trial': trial, 'reservation': reservation,
                                   'started': utcnow(), 'max_tokens': maximum,
                                   'semantic_request_sha256': semantic,
                                   'consecutive_attempt_index': len(pending) + 1}
            row = self.trials[trial]
            row['model_request_count'] += 1
            row['inflight'] += 1
            raw = encoded(forwarded)
            self.audit('model_request_started', trial_id=trial, request_id=ident,
                model=MODEL, reasoning_effort=EFFORT, requested_max_tokens=declared_maximum,
                forwarded_max_tokens=forwarded.get('max_tokens'), reserved_output_tokens=maximum,
                request_sha256=hashlib.sha256(raw).hexdigest(),
                request_body_bytes=len(raw),
                semantic_request_sha256=semantic,
                consecutive_attempt_index=len(pending) + 1,
                image_input_present='"image_url"' in raw.decode(),
                reserved_usd=str(reservation), stream=bool(forwarded.get('stream')))
            self.persist(trial)
            self.check_warning()
            return ident, raw

    def finish(self, ident, *, usage=None, failure=None, uncertain=False, response_models=()):
        with self.lock:
            request = self.inflight.pop(ident)
            trial = request['trial']; row = self.trials[trial]
            semantic = request['semantic_request_sha256']
            wrong_model = any(m != MODEL for m in response_models)
            row['model_mismatch'] |= wrong_model
            missing_identity = (not response_models and not (row['cancellation_requested']
                                and (uncertain or usage is None)))
            if not failure and (wrong_model or missing_identity):
                failure = {'source': 'provider_response', 'code': 'model_mismatch', 'observed_models': list(response_models)}
                uncertain = True
            if usage is not None and not isinstance(usage, dict):
                failure = failure or {'source': 'provider_usage', 'code': 'invalid_usage'}
                usage, uncertain = None, True
            if usage is None:
                uncertain = True
            cancellation_consequence = (row['cancellation_requested'] and uncertain
                and not row['pre_cancellation_provider_failure']
                and not row['pre_cancellation_infrastructure_failure'] and not row['model_mismatch']
                and (failure is None or failure.get('source') == 'downstream_client_disconnect'))
            if cancellation_consequence:
                row['cancellation_usage_uncertainty'] = True
                if failure:
                    failure = {**failure, 'cancellation_consequence': True}
            cost = request['reservation']
            if usage is not None and not uncertain:
                try:
                    if usage.get('prompt_tokens', 0) > MAX_INPUT_BOUND or usage.get('completion_tokens', 0) > request['max_tokens']:
                        raise ValueError('Provider usage exceeds reserved token bounds')
                    cost = price(usage, peak=overlaps_peak(request['started'], utcnow()))
                    if cost > request['reservation']:
                        raise ValueError('Provider charge exceeds request reservation')
                except (ValueError, TypeError, ArithmeticError) as exc:
                    uncertain = True
                    row['liability_bounds_valid'] = False
                    failure = failure or {'source': 'provider_usage', 'code': 'invalid_usage', 'message': str(exc)}
                    cost = request['reservation']
            if wrong_model or (failure and failure.get('source') == 'provider_usage'):
                row['liability_bounds_valid'] = False
            # A failed attempt can have unknown processing. Its full reservation
            # stays charged even after a later same-request retry succeeds.
            self.reserved -= request['reservation']
            self.committed_upper += cost
            self.current_committed_upper += cost
            row['inflight'] -= 1
            row['completed_request_count'] += 1
            row['usage_uncertainty'] |= uncertain
            row['estimated_cost_upper_usd'] = str(Decimal(row['estimated_cost_upper_usd']) + cost)
            clean = (not failure and not uncertain and usage is not None
                     and set(response_models) == {MODEL} and not row['cancellation_requested'])
            if usage is not None:
                row['usage'].append({'request_id': ident, 'raw_provider_usage': usage,
                    'semantic_request_sha256': semantic, 'usage_complete': not uncertain,
                    'cost_usd_upper_bound': str(cost)})
                if not uncertain:
                    row['estimated_cost_usd'] = str(Decimal(row['estimated_cost_usd']) + cost)
                    self.current_known_metered += cost
            recoverable = bool(failure and not row['cancellation_requested'] and not wrong_model
                and row['liability_bounds_valid'] and usage is None and uncertain and (
                    (failure.get('source') == 'provider_http'
                     and type(failure.get('http_status')) is int and 500 <= failure['http_status'] < 600)
                    or (failure.get('source') == 'proxy_transport'
                        and failure.get('transport_phase') in {'before_headers', 'connect', 'request_send', 'response_headers'}
                        and failure.get('code') in {'RemoteDisconnected', 'ConnectionResetError',
                            'ConnectionAbortedError', 'TimeoutError', 'ConnectionRefusedError',
                            'SSLEOFError', 'SSLError', 'gaierror', 'OSError', 'BrokenPipeError'})))
            if failure:
                failure = self.redact({**failure, 'request_id': ident,
                    'semantic_request_sha256': semantic})
                if recoverable:
                    failure.update(recoverable=True, recovery_status='unresolved',
                        consecutive_attempt_index=request['consecutive_attempt_index'],
                        retained_liability_upper_usd=str(cost))
                row['typed_failures'].append(failure)
            if clean:
                recovered_at = utcnow().isoformat()
                for previous in row['typed_failures']:
                    if (previous.get('recoverable') is True and previous.get('recovery_status') == 'unresolved'
                            and previous['semantic_request_sha256'] == semantic):
                        previous.update(recovery_status='recovered', recovered_by_request_id=ident,
                                        recovered_at=recovered_at)
                        self.audit('transient_request_recovered', trial_id=trial,
                            request_id=previous['request_id'], semantic_request_sha256=semantic,
                            recovered_by_request_id=ident, recovered_at=recovered_at,
                            retained_liability_upper_usd=previous['retained_liability_upper_usd'])
            if recoverable:
                self.audit('transient_failure_retry_available', trial_id=trial, **failure,
                    global_halt_required=request['consecutive_attempt_index'] >= 6)
                if request['consecutive_attempt_index'] >= 6:
                    self.halt('provider_transient_retry_exhausted', trial_id=trial, failure=failure)
            elif (uncertain or failure) and not cancellation_consequence:
                self.halt('provider_or_transport_failure' if failure else 'usage_uncertainty',
                          trial_id=trial, failure=failure, usage_uncertainty=uncertain)
            elif cancellation_consequence:
                self.audit('expected_cancellation_liability_retained', trial_id=trial,
                    request_id=ident, liability_upper_usd=str(cost),
                    global_halt_required=False, study_committed_upper_usd=str(self.committed_upper))
            self.audit('model_request_finished', trial_id=trial, request_id=ident,
                semantic_request_sha256=semantic, usage=usage, failure=failure, usage_uncertainty=uncertain,
                cost_usd_upper_bound=str(cost), response_models=list(response_models),
                study_committed_upper_usd=str(self.committed_upper))
            self.persist(trial)
            self.check_warning()


def make_server(state, upstream, host='127.0.0.1', port=0):
    if host != '127.0.0.1':
        raise ValueError('Proxy must bind IPv4 loopback only')

    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.0'

        def log_message(self, *args):
            pass

        def reply(self, status, data, *, registration=False):
            # The authenticated host registration response intentionally carries
            # the newly issued scoped token. It is never written to an audit.
            raw = encoded(data if registration else state.redact(data))
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(raw)))
            self.send_header('Connection', 'close')
            self.end_headers()
            self.wfile.write(raw)

        def token(self):
            header = self.headers.get('Authorization', '')
            return header[7:] if header.startswith('Bearer ') else ''

        def body(self):
            try:
                length = int(self.headers.get('Content-Length', 0))
            except ValueError:
                raise GuardError(400, 'invalid_length', 'Content-Length required') from None
            if not 0 < length <= MAX_BODY or self.headers.get('Transfer-Encoding'):
                raise GuardError(413, 'body_not_allowed', 'Bounded Content-Length required')
            try:
                return json.loads(self.rfile.read(length))
            except (ValueError, UnicodeDecodeError):
                raise GuardError(400, 'invalid_json', 'Valid JSON required') from None

        def do_GET(self):
            self.route('GET')

        def do_POST(self):
            self.route('POST')

        def route(self, method):
            ident = None
            trial = None
            transport_phase = 'before_headers'
            try:
                path = urlsplit(self.path).path
                if path.startswith('/admin/'):
                    if not secrets.compare_digest(self.token(), state.admin_token):
                        raise GuardError(401, 'invalid_admin_token', 'Host administrator credential required')
                    if method == 'POST' and path == '/admin/trials':
                        return self.reply(201, state.register(self.body().get('trial_id')), registration=True)
                    if method == 'POST' and path.startswith('/admin/trials/') and path.endswith('/cancel'):
                        trial_id = unquote(path[len('/admin/trials/'):-len('/cancel')])
                        return self.reply(200, state.mark_cancelled(trial_id,
                            self.body().get('reason', 'framework_cancellation_pending')))
                    if method == 'POST' and path.startswith('/admin/trials/') and path.endswith('/close'):
                        trial_id = unquote(path[len('/admin/trials/'):-len('/close')])
                        return self.reply(200, state.close_trial(trial_id))
                    if method == 'GET' and path.startswith('/admin/trials/'):
                        return self.reply(200, state.status(unquote(path[len('/admin/trials/'):])) )
                    if method == 'GET' and path == '/admin/status':
                        with state.lock:
                            return self.reply(200, {'currency': 'USD', 'balance_usd': str(state.balance),
                                'initial_balance_usd': str(state.initial_balance),
                                'account_balance_change_usd': str(state.initial_balance - state.balance) if state.balance is not None else None,
                                'study_cap_usd': str(state.cap),
                                'balance_admission': state.balance_admission,
                                'admission_remaining_usd': str(state.admission_remaining()),
                                'reserve_floor_usd': str(state.reserve_floor),
                                'carry_ledger_sha256': state.carry.sha256,
                                'prior_known_metered_usd': str(state.prior_known_metered),
                                'prior_unknown_upper_usd': str(state.prior_unknown_upper),
                                'prior_committed_upper_usd': str(state.prior_committed_upper),
                                'current_cohort_known_metered_usd': str(state.current_known_metered),
                                'current_cohort_committed_upper_usd': str(state.current_committed_upper),
                                'current_cohort_unknown_upper_usd': str(state.current_committed_upper - state.current_known_metered),
                                'study_committed_upper_usd': str(state.committed_upper),
                                'reserved_usd': str(state.reserved), 'inflight': len(state.inflight),
                                'budget_halted': bool(state.halt_reason), 'halt_reason': state.halt_reason,
                                'low_balance_warning': state.warned, 'trial_count': len(state.trials)})
                    raise GuardError(404, 'unknown_admin_route', 'Unknown host control route')
                trial = state.authenticate(self.token())
                if method == 'GET' and path in ('/models', '/v1/models'):
                    response = upstream.request('GET', '/models')
                    try:
                        raw = response.read(2 * 1024 * 1024 + 1)
                        data = json.loads(raw)
                        if response.status != 200 or not isinstance(data, dict):
                            raise GuardError(502, 'models_metadata_failed', 'Upstream model metadata failed')
                        filtered = [m for m in data.get('data', []) if m.get('id') == MODEL]
                        if len(filtered) != 1:
                            raise GuardError(502, 'flash_model_unavailable', 'Exact Flash model metadata is unavailable')
                        return self.reply(200, {'object': 'list', 'data': filtered})
                    finally:
                        close_response(response)
                if method != 'POST' or path not in ('/chat/completions', '/v1/chat/completions'):
                    raise GuardError(404, 'route_not_allowed', 'Only Flash Chat Completions and model metadata are allowed')
                ident, raw = state.begin(trial, self.body())
                response = upstream.request('POST', '/chat/completions', raw,
                    headers={name: value for name, value in self.headers.items() if name.lower() in NATIVE_HEADERS})
                state.audit('upstream_http_response_received', trial_id=trial, request_id=ident,
                    http_status=response.status,
                    transport_metadata=getattr(response, '_proxy_transport_metadata', {}))
                transport_phase = 'response_headers_received'
                failure, usage, uncertain, models = None, None, False, set()
                try:
                    if response.status != 200:
                        error_raw = response.read(1024 * 1024)
                        try:
                            error_data = json.loads(error_raw)
                        except (ValueError, UnicodeDecodeError):
                            error_data = {'unparsed_error_sha256': hashlib.sha256(error_raw).hexdigest()}
                        failure = {'source': 'provider_http', 'http_status': response.status,
                                   'error': error_data, 'classification': {402: 'insufficient_balance',
                                   429: 'rate_or_concurrency_limit'}.get(response.status,
                                   'provider_service_error' if response.status >= 500 else 'provider_request_error')}
                        state.finish(ident, failure=failure)
                        ident = None
                        return self.reply(response.status, error_data)
                    content_type = response.getheader('Content-Type', '')
                    if 'text/event-stream' in content_type:
                        parser = SSEUsage()
                        transport_phase = 'stream_started'
                        self.send_response(200)
                        self.send_header('Content-Type', 'text/event-stream')
                        self.send_header('Cache-Control', 'no-cache')
                        self.send_header('Connection', 'close')
                        self.end_headers()
                        reader = getattr(response, 'read1', response.read)
                        while chunk := reader(64 * 1024):
                            parser.feed(chunk)
                            try:
                                self.wfile.write(chunk)
                                self.wfile.flush()
                            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError) as exc:
                                raise DownstreamDisconnect(type(exc).__name__) from exc
                        parser.finish()
                        usage, models = parser.usage, parser.models
                        uncertain = parser.invalid or not parser.done or usage is None
                        if parser.errors:
                            failure = {'source': 'provider_sse', 'errors': parser.errors}
                    else:
                        raw_response = response.read(16 * 1024 * 1024 + 1)
                        if len(raw_response) > 16 * 1024 * 1024:
                            raise ValueError('Oversized provider response')
                        data = json.loads(raw_response)
                        if not isinstance(data, dict):
                            raise ValueError('Invalid provider completion envelope')
                        usage = data.get('usage')
                        models = {data['model']} if isinstance(data.get('model'), str) else set()
                        if isinstance(data.get('error'), dict):
                            failure = {'source': 'provider_json', 'error': data['error']}
                        state.finish(ident, usage=usage, failure=failure, response_models=models)
                        ident = None
                        return self.reply(200, data)
                    state.finish(ident, usage=usage, failure=failure, uncertain=uncertain, response_models=models)
                    ident = None
                finally:
                    close_response(response)
            except GuardError as exc:
                if ident is not None:
                    state.finish(ident, failure={'source': 'proxy_guard', 'code': exc.code}, uncertain=True)
                elif trial is not None:
                    state.reject(trial, exc)
                self.reply(exc.status, {'error': {'type': 'study_proxy_error', 'code': exc.code, 'message': exc.message}})
            except (OSError, ValueError, TypeError, http.client.HTTPException) as exc:
                if ident is not None:
                    state.finish(ident, failure={'source': 'downstream_client_disconnect' if isinstance(exc, DownstreamDisconnect) else 'proxy_transport', 'code': type(exc).__name__,
                                               'message': str(exc), 'transport_phase': getattr(exc, 'proxy_transport_phase', transport_phase)}, uncertain=True)
                try:
                    self.reply(502, {'error': {'type': 'study_proxy_error', 'code': 'transport_failure'}})
                except OSError:
                    pass

    server = ThreadingHTTPServer((host, port), Handler)
    state.url = f'http://127.0.0.1:{server.server_port}'
    return server


def refresh_balance(state, upstream):
    response = upstream.request('GET', '/user/balance')
    try:
        raw = response.read(1024 * 1024)
        if response.status != 200:
            raise ValueError('Balance metadata request failed with HTTP ' + str(response.status))
        state.set_balance(json.loads(raw))
    finally:
        close_response(response)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--key-file', type=Path, required=True)
    parser.add_argument('--admin-config', type=Path, required=True)
    parser.add_argument('--halt-path', type=Path, required=True)
    parser.add_argument('--output-root', type=Path, required=True)
    budget = parser.add_mutually_exclusive_group(required=True)
    budget.add_argument('--carry-ledger', type=Path, help='Reviewed prior study liabilities')
    budget.add_argument('--new-study', action='store_true', help='Start independent accounting in a new output directory')
    parser.add_argument('--balance-admission', choices=('worst-case', 'reported-balance'), required=True)
    parser.add_argument('--port', type=int, default=0)
    args = parser.parse_args()
    carry = new_study_ledger(args.output_root) if args.new_study else load_carry_ledger(args.carry_ledger)
    if args.admin_config.exists() or args.admin_config.is_symlink():
        raise SystemExit('Refusing to replace existing proxy administrator configuration')
    if args.halt_path.exists() or args.halt_path.is_symlink():
        raise SystemExit('Persistent provider/budget halt exists')
    upstream = HTTPSUpstream(args.key_file)
    state = ProxyState(args.output_root, args.halt_path, provider_secret=upstream.key, carry=carry,
                       balance_admission=args.balance_admission)
    refresh_balance(state, upstream)
    state.can_continue()
    server = make_server(state, upstream, port=args.port)
    args.admin_config.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(args.admin_config, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as out:
        json.dump({'url': state.url, 'admin_token': state.admin_token,
                   'carry_ledger_sha256': carry.sha256}, out)
        out.write('\n')
    stopping = threading.Event()

    def monitor():
        while not stopping.wait(10):
            try:
                refresh_balance(state, upstream)
            except (OSError, ValueError, TypeError, http.client.HTTPException, GuardError) as exc:
                state.balance_poll_error = type(exc).__name__
                state.audit('balance_poll_failed', error_type=type(exc).__name__)
                if state.balance_time is None or time.monotonic() - state.balance_time > 60:
                    state.halt('balance_monitor_unavailable')

    watcher = threading.Thread(target=monitor, daemon=True)
    watcher.start()

    def stop(signum, frame):
        stopping.set()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    print(json.dumps({'ready': True, 'url': state.url, 'model': MODEL,
                      'admin_config': str(args.admin_config)}), flush=True)
    try:
        server.serve_forever(poll_interval=.1)
    finally:
        stopping.set()
        server.server_close()


if __name__ == '__main__':
    main()
