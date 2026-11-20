"""Offline tests: real local HTTP transport with fake upstream replies only."""
import concurrent.futures
from decimal import Decimal
import importlib.util
import io
import hashlib
import http.client as http_client
import json
from pathlib import Path
import socket
import threading
import time
import urllib.error
import urllib.request

import pytest

from cynthiabench import deepseek_proxy as p


def zero_carry(tmp_path):
    path=tmp_path/'offline-zero-carry.json'
    path.write_text(json.dumps({'schema_version':1,'currency':'USD','study_cap_usd':'50',
        'prior_known_metered_usd':'0','prior_unknown_upper_usd':'0',
        'prior_committed_upper_usd':'0','development_only':True}))
    path.chmod(0o444)
    return p.load_carry_ledger(path,allow_zero_fixture=True)


def production_carry(tmp_path,known='0.1',unknown='0.2'):
    known,unknown=Decimal(known),Decimal(unknown);upper=known+unknown
    status=tmp_path/'old-trial-status.json';audit=tmp_path/'old-provider.jsonl'
    status.write_text(json.dumps({'trial_id':'old-closed-trial','closed':True,'inflight':0,
        'model_request_count':2,'completed_request_count':2,'estimated_cost_usd':str(known),
        'estimated_cost_upper_usd':str(upper)}))
    events=[{'event':'model_request_started','request_id':'known'},
        {'event':'model_request_finished','request_id':'known','usage':{'prompt_tokens':1},
         'usage_uncertainty':False,'cost_usd_upper_bound':str(known)},
        {'event':'model_request_started','request_id':'unknown'},
        {'event':'model_request_finished','request_id':'unknown','usage':None,
         'usage_uncertainty':True,'cost_usd_upper_bound':str(unknown)}]
    audit.write_text(''.join(json.dumps(e)+'\n' for e in events))
    path=tmp_path/'carry.json';path.write_text(json.dumps({'schema_version':1,'currency':'USD',
        'study_cap_usd':'50','prior_known_metered_usd':str(known),'prior_unknown_upper_usd':str(unknown),
        'prior_committed_upper_usd':str(upper),'prior_provider_audit':str(audit),
        'prior_trial_statuses':[str(status)],'evidence_sha256':{
            str(f):hashlib.sha256(f.read_bytes()).hexdigest() for f in (status,audit)}}))
    path.chmod(0o444)
    return path


class Response(io.BytesIO):
    def __init__(self, data, status=200, content_type='application/json', chunk_size=None):
        super().__init__(data)
        self.status, self.content_type, self.chunk_size = status, content_type, chunk_size

    def getheader(self, name, default=None):
        return self.content_type if name.lower() == 'content-type' else default

    def read1(self, n=-1):
        return self.read(min(n, self.chunk_size) if self.chunk_size else n)


class FakeUpstream:
    def __init__(self):
        self.replies = []
        self.calls = []
        self.headers = []

    def request(self, method, path, body=None, *, headers=None):
        self.calls.append((method, path, json.loads(body) if body else None))
        self.headers.append(headers)
        assert self.replies, 'No real provider access is possible in this fixture'
        reply = self.replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        return reply


def balance(amount='50.00', currency='USD'):
    return {'is_available': True, 'balance_infos': [
        {'currency': currency, 'total_balance': amount,
         'granted_balance': '0', 'topped_up_balance': amount}]}


@pytest.fixture
def proxy(tmp_path):
    state = p.ProxyState(tmp_path / 'provider', tmp_path / 'halt.json',
                         admin_token='admin-secret-test', provider_secret='provider-secret-test',
                         carry=zero_carry(tmp_path))
    state.set_balance(balance())
    upstream = FakeUpstream()
    server = p.make_server(state, upstream)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield state, upstream, server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def http(state, path, token, data=None):
    request = urllib.request.Request(state.url + path,
        data=json.dumps(data).encode() if data is not None else None,
        headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as response:
        return response.code, response.read()


def register(state, name='trial-1'):
    status, raw = http(state, '/admin/trials', state.admin_token, {'trial_id': name})
    assert status == 201, raw
    return json.loads(raw)['token']


def body(**updates):
    return {'model': p.MODEL, 'reasoning_effort': 'max',
            'messages': [{'role': 'user', 'content': 'offline fixture'}], **updates}


USAGE = {'prompt_tokens': 100, 'completion_tokens': 30, 'total_tokens': 130,
         'prompt_cache_hit_tokens': 80, 'prompt_cache_miss_tokens': 20,
         'completion_tokens_details': {'reasoning_tokens': 12}}


def test_cost_uses_cache_split_and_does_not_double_count_reasoning():
    assert p.price(USAGE) == Decimal('0.00004248')
    assert p.price(USAGE, peak=False) == Decimal('0.00002124')
    assert p.price({'prompt_tokens': p.MAX_INPUT_BOUND,
                    'completion_tokens': p.MAX_OUTPUT}) == Decimal('0.786432')


@pytest.mark.parametrize('currency,amount', [('CNY', '50'), ('USD', 'NaN'), ('USD', '-1')])
def test_balance_currency_and_invalid_amount_fail_closed(tmp_path, currency, amount):
    state = p.ProxyState(tmp_path / 'provider', tmp_path / 'halt.json',carry=zero_carry(tmp_path))
    with pytest.raises(p.GuardError, match='verified USD'):
        state.set_balance(balance(amount, currency))
    assert state.halt_path.exists() and state.halt_reason == 'balance_currency_unverified'


def test_streaming_raw_usage_large_native_output_cap_and_no_prose_quota(proxy):
    state, upstream, server = proxy
    token = register(state)
    chunks = [
        {'model': p.MODEL, 'choices': [{'delta': {'content': 'An article says Individual quota reached.'}}]},
        {'model': p.MODEL, 'choices': [], 'usage': USAGE},
    ]
    stream = b''.join(b'data: ' + p.encoded(x) + b'\r\n\r\n' for x in chunks) + b'data: [DONE]\r\n\r\n'
    upstream.replies.append(Response(stream, content_type='text/event-stream', chunk_size=7))
    payload = body(stream=True, max_tokens=131072)
    payload['messages'][0]['content'] = [{'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,AAAA'}}]
    status, raw = http(state, '/v1/chat/completions', token, payload)
    assert status == 200 and raw == stream
    assert upstream.calls[0][2]['max_tokens'] == 131072
    assert upstream.calls[0][2]['stream_options']['include_usage'] is True
    result = state.status('trial-1')
    assert result['model_request_count'] == result['completed_request_count'] == 1
    assert result['inflight'] == 0 and not result['typed_failures'] and not result['usage_uncertainty']
    assert result['usage'][0]['raw_provider_usage'] == USAGE
    assert not state.halt_path.exists()
    logs = (state.root / 'audit.jsonl').read_text()
    assert '"image_input_present": true' in logs
    for secret in (token, state.admin_token, 'provider-secret-test'):
        assert secret not in logs
    assert http(state, '/admin/status', token)[0] == 401


def test_missing_native_output_cap_reserved_at_model_max_without_body_mutation(proxy):
    state, upstream, server = proxy
    token = register(state)
    upstream.replies.append(Response(p.encoded({'model': p.MODEL, 'choices': [], 'usage': USAGE})))
    assert http(state, '/chat/completions', token, body())[0] == 200
    assert 'max_tokens' not in upstream.calls[0][2]
    events = [json.loads(x) for x in (state.root / 'audit.jsonl').read_text().splitlines()]
    request = next(x for x in events if x['event'] == 'model_request_started')
    assert request['reserved_output_tokens'] == p.MAX_OUTPUT and request['forwarded_max_tokens'] is None


@pytest.mark.parametrize('changes', [
    {'model': 'deepseek-v4-pro'}, {'reasoning_effort': 'high'},
    {'thinking': {'type': 'disabled'}}, {'max_tokens': p.MAX_OUTPUT + 1},
    {'tools': [{'type': 'function', 'function': {'name': 'task'}}]},
])
def test_unapproved_model_effort_delegation_or_output_bound_never_forward(proxy, changes):
    state, upstream, server = proxy
    token = register(state)
    assert http(state, '/v1/chat/completions', token, body(**changes))[0] == 400
    assert not upstream.calls and state.status('trial-1')['model_request_count'] == 0


def test_models_endpoint_exposes_only_exact_flash(proxy):
    state, upstream, server = proxy
    token = register(state)
    upstream.replies.append(Response(p.encoded({'data': [{'id': p.MODEL}, {'id': 'deepseek-v4-pro'}]})))
    status, raw = http(state, '/v1/models', token)
    assert status == 200 and json.loads(raw)['data'] == [{'id': p.MODEL}]
    assert state.status('trial-1')['model_request_count'] == 0


@pytest.mark.parametrize('code,label', [(401, 'provider_request_error'),
    (402, 'insufficient_balance'), (429, 'rate_or_concurrency_limit')])
def test_typed_http_failures_halt_and_do_not_become_clean_native_stops(proxy, code, label):
    state, upstream, server = proxy
    token = register(state)
    upstream.replies.append(Response(p.encoded({'error': {'code': 'provider_error',
        'message': 'provider-secret-test deliberately echoed by fixture'}}), status=code))
    status, raw = http(state, '/v1/chat/completions', token, body())
    assert status == code and b'provider-secret-test' not in raw
    result = state.status('trial-1')
    assert result['typed_failures'][0]['source'] == 'provider_http'
    assert result['typed_failures'][0]['http_status'] == code
    assert result['typed_failures'][0]['classification'] == label
    assert result['usage_uncertainty'] and result['budget_halted']
    assert state.reserved == 0 and state.committed_upper == Decimal('0.786432')
    assert state.halt_path.exists()
    assert http(state, '/v1/chat/completions', token, body())[0] == 503
    assert len(upstream.calls) == 1
    for file in state.root.rglob('*'):
        if file.is_file():
            assert 'provider-secret-test' not in file.read_text()


@pytest.mark.parametrize('missing', ['usage', 'done'])
def test_streaming_missing_terminal_metering_fails_closed(proxy, missing):
    state, upstream, server = proxy
    token = register(state)
    chunk = {'model': p.MODEL, 'choices': []}
    if missing != 'usage':
        chunk['usage'] = USAGE
    stream = b'data: ' + p.encoded(chunk) + b'\n\n'
    if missing != 'done':
        stream += b'data: [DONE]\n\n'
    upstream.replies.append(Response(stream, content_type='text/event-stream'))
    assert http(state, '/v1/chat/completions', token, body(stream=True))[0] == 200
    assert state.status('trial-1')['usage_uncertainty'] and state.halt_path.exists()
    assert state.committed_upper == Decimal('0.786432')


def test_typed_sse_error_with_http_success_is_not_assistant_prose(proxy):
    state, upstream, server = proxy
    token = register(state)
    stream = b'data: {"error":{"code":"insufficient_balance"}}\n\ndata: [DONE]\n\n'
    upstream.replies.append(Response(stream, content_type='text/event-stream'))
    assert http(state, '/v1/chat/completions', token, body(stream=True))[0] == 200
    result = state.status('trial-1')
    assert result['typed_failures'][0]['source'] == 'provider_sse' and result['budget_halted']


def test_concurrent_reservations_are_atomic_and_two_request_limit_holds(proxy):
    state, upstream, server = proxy
    for i in range(10):
        state.register('trial-' + str(i))
    gate = threading.Barrier(10)

    def begin(i):
        gate.wait(timeout=5)
        try:
            return state.begin('trial-' + str(i), body())[0]
        except p.GuardError as exc:
            assert exc.code == 'proxy_concurrency_limit'
            return None

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        accepted = [x for x in executor.map(begin, range(10)) if x]
    assert len(accepted) == 2 and len(state.inflight) == 2
    assert state.reserved == Decimal('1.572864')
    for ident in accepted:
        state.finish(ident, usage=USAGE, response_models=[p.MODEL])
    assert not state.inflight and state.reserved == 0 and not state.halt_path.exists()


def test_reserve_floor_warns_and_stops_before_upstream_spend(proxy):
    state, upstream, server = proxy
    token = register(state)
    state.set_balance(balance('2.70'))
    assert http(state, '/v1/chat/completions', token, body())[0] == 402
    assert not upstream.calls and state.warned and state.halt_reason == 'budget_reservation_denied'
    assert state.committed_upper == state.reserved == 0
    assert http(state, '/admin/trials', state.admin_token, {'trial_id': 'later'})[0] == 503


def test_missing_or_wrong_response_identity_and_bad_usage_are_fail_closed(proxy):
    state, upstream, server = proxy
    token = register(state)
    upstream.replies.append(Response(p.encoded({'model': 'another-model', 'usage': USAGE})))
    assert http(state, '/v1/chat/completions', token, body())[0] == 200
    result = state.status('trial-1')
    assert result['usage_uncertainty'] and result['typed_failures'][0]['code'] == 'model_mismatch'
    assert state.halt_path.exists()


def test_duplicate_trial_id_forbids_context_reuse(proxy):
    state, upstream, server = proxy
    register(state)
    assert http(state, '/admin/trials', state.admin_token, {'trial_id': 'trial-1'})[0] == 409
    assert not upstream.calls


def test_host_close_revokes_late_auth_but_retains_inflight_and_final_metering(proxy):
    state, upstream, server = proxy
    token = register(state)
    ident, raw = state.begin('trial-1', body())
    status, payload = http(state, '/admin/trials/trial-1/close', state.admin_token, {})
    assert status == 200 and json.loads(payload)['closed'] is True
    assert json.loads(payload)['inflight'] == 1
    assert http(state, '/v1/models', token)[0] == 401
    assert not upstream.calls
    with pytest.raises(p.GuardError, match='revoked'):
        state.begin('trial-1', body())
    state.finish(ident, usage=USAGE, response_models=[p.MODEL])
    status, payload = http(state, '/admin/trials/trial-1', state.admin_token)
    final = json.loads(payload)
    assert status == 200 and final['closed'] is True and final['inflight'] == 0
    assert final['completed_request_count'] == 1 and len(final['usage']) == 1
    assert Decimal(final['estimated_cost_upper_usd']) > 0
    assert http(state, '/admin/trials/trial-1/close', state.admin_token, {})[0] == 200


def test_cancellation_marker_distinguishes_financial_uncertainty_from_provider_failure(proxy):
    state, upstream, server = proxy
    register(state)
    ident, raw = state.begin('trial-1', body())
    status, payload = http(state, '/admin/trials/trial-1/cancel', state.admin_token,
                           {'reason': 'framework_cancellation_pending'})
    marked = json.loads(payload)
    assert status == 200 and marked['cancellation_requested']
    assert not marked['pre_cancellation_provider_failure'] and not marked['pre_cancellation_infrastructure_failure']
    state.finish(ident, uncertain=True)  # no output/usage after native cancellation
    final = state.status('trial-1')
    assert final['cancellation_usage_uncertainty'] and not final['typed_failures']
    assert not final['model_mismatch']
    assert final['halt_reason'] is None and not state.halt_path.exists()
    assert final['usage_uncertainty'] and state.committed_upper == Decimal('0.786432')
    # Marker alone is not an assertion that Harbor's actual exception was a deadline.
    assert final['cancellation_reason'] == 'framework_cancellation_pending'


def test_prior_provider_quota_failure_cannot_be_erased_by_later_cancel_marker(proxy):
    state, upstream, server = proxy
    token = register(state)
    upstream.replies.append(Response(p.encoded({'error': {'code': 'InsufficientBalance'}}), status=402))
    assert http(state, '/v1/chat/completions', token, body())[0] == 402
    status, payload = http(state, '/admin/trials/trial-1/cancel', state.admin_token,
                           {'reason': 'framework_cancellation_pending'})
    row = json.loads(payload)
    assert status == 200 and row['pre_cancellation_provider_failure']
    assert row['pre_cancellation_infrastructure_failure'] and row['typed_failures'][0]['http_status'] == 402


def test_actual_downstream_disconnect_after_host_marker_retains_cancellation_causality(proxy):
    state, upstream, server = proxy
    token = register(state)
    release = threading.Event()

    class GateResponse(Response):
        def read1(self, n=-1):
            assert release.wait(5)
            return super().read1(n)

    large = p.encoded({'model': p.MODEL, 'choices': [{'delta': {'content': 'X' * 512000}}]})
    upstream.replies.append(GateResponse(b'data: ' + large + b'\n\n',
                                       content_type='text/event-stream', chunk_size=8192))
    client = socket.create_connection(('127.0.0.1', server.server_port), timeout=5)
    raw = p.encoded(body(stream=True))
    client.sendall((f'POST /v1/chat/completions HTTP/1.0\r\nAuthorization: Bearer {token}\r\n'
                    f'Content-Length: {len(raw)}\r\n\r\n').encode() + raw)
    assert b'200' in client.recv(1024)
    assert http(state, '/admin/trials/trial-1/cancel', state.admin_token,
                {'reason': 'framework_cancellation_pending'})[0] == 200
    client.shutdown(socket.SHUT_RDWR)
    client.close()
    release.set()
    for _ in range(500):
        if state.status('trial-1')['inflight'] == 0:
            break
        time.sleep(.01)
    final = state.status('trial-1')
    assert final['inflight'] == 0 and final['cancellation_usage_uncertainty']
    assert not final['pre_cancellation_provider_failure']
    assert final['typed_failures'][0]['source'] == 'downstream_client_disconnect'
    assert final['typed_failures'][0]['cancellation_consequence'] is True
    assert not final['budget_halted'] and state.committed_upper == Decimal('0.786432')
    assert state.register('later-valid-trial')['trial_id']=='later-valid-trial'


def test_validated_carry_is_required_and_production_rejects_zero_fixture(tmp_path):
    with pytest.raises(ValueError,match='carry ledger'):
        p.ProxyState(tmp_path/'provider',tmp_path/'halt')
    fixture=zero_carry(tmp_path)
    with pytest.raises(ValueError,match='forbidden'):
        p.load_carry_ledger(fixture.path)


def test_production_carry_preserves_global_cap_and_reports_old_and_new_separately(tmp_path):
    ledger=p.load_carry_ledger(production_carry(tmp_path))
    state=p.ProxyState(tmp_path/'provider',tmp_path/'halt',carry=ledger)
    assert state.committed_upper==Decimal('.3') and state.prior_unknown_upper==Decimal('.2')
    state.set_balance(balance('49.85'))
    assert state.effective_balance()==Decimal('49.85')  # actual prior debit is not subtracted twice
    upstream=FakeUpstream();server=p.make_server(state,upstream)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        token=register(state,'new-trial')
        upstream.replies.append(Response(p.encoded({'model':p.MODEL,'usage':USAGE})))
        assert http(state,'/v1/chat/completions',token,body())[0]==200
        status,raw=http(state,'/admin/status',state.admin_token);record=json.loads(raw)
        assert status==200 and record['study_cap_usd']=='50'
        assert Decimal(record['prior_known_metered_usd'])==Decimal('.1')
        assert Decimal(record['prior_unknown_upper_usd'])==Decimal('.2')
        assert Decimal(record['current_cohort_known_metered_usd'])>0
        assert Decimal(record['study_committed_upper_usd'])==Decimal('.3')+Decimal(record['current_cohort_committed_upper_usd'])
        assert record['carry_ledger_sha256']==ledger.sha256
        before=state.committed_upper;state.set_balance(balance('50'))
        assert state.prior_unknown_upper==Decimal('.2') and state.committed_upper==before
    finally:
        server.shutdown();server.server_close();thread.join(timeout=2)


@pytest.mark.parametrize('change',['currency','total','evidence','mutable','missing'])
def test_carry_rejects_malformed_or_hash_drift_before_any_provider_access(tmp_path,change):
    path=production_carry(tmp_path)
    if change=='missing':
        path.unlink()
    elif change=='evidence':
        (tmp_path/'old-provider.jsonl').write_text('changed historical proof')
    elif change=='mutable':
        path.chmod(0o644)
    else:
        d=json.loads(path.read_text());d['currency' if change=='currency' else 'prior_committed_upper_usd']='CNY' if change=='currency' else '0.01'
        path.chmod(0o644);path.write_text(json.dumps(d));path.chmod(0o444)
    with pytest.raises((ValueError,OSError)):
        p.load_carry_ledger(path)


def test_new_proxy_cannot_reset_existing_accounting_root(tmp_path):
    carry=zero_carry(tmp_path)
    state=p.ProxyState(tmp_path/'provider',tmp_path/'halt',carry=carry)
    with pytest.raises(ValueError,match='fresh'):
        p.ProxyState(tmp_path/'provider',tmp_path/'halt',carry=carry)


def test_expected_cancel_liabilities_cannot_admit_past_original_usd50_cap(tmp_path):
    state=p.ProxyState(tmp_path/'provider',tmp_path/'halt',carry=p.load_carry_ledger(production_carry(tmp_path)))
    state.url='http://127.0.0.1:12345';state.set_balance(balance('50'))
    completed=0
    for n in range(100):
        try:
            state.register('trial-'+str(n));ident,_=state.begin('trial-'+str(n),body())
        except p.GuardError as error:
            assert error.code in ('budget_reservation_denied','budget_reserve_reached')
            break
        state.mark_cancelled('trial-'+str(n))
        state.finish(ident,uncertain=True)
        state.close_trial('trial-'+str(n))
        completed+=1
        assert not state.halt_path.exists() and state.committed_upper+state.reserved<=48
        # Even a displayed account balance that has not yet settled may not
        # release prior or current unmetered reservations.
        state.set_balance(balance('50'))
    assert completed==60 and state.halt_path.exists()
    assert state.prior_unknown_upper==Decimal('.2')
    assert state.committed_upper==Decimal('.3')+Decimal('.786432')*completed
    assert state.committed_upper<50 and state.reserved==0


def test_prior_provider_failure_prevents_expected_cancel_exception(proxy):
    state,upstream,server=proxy;register(state)
    first,_=state.begin('trial-1',body());second,_=state.begin('trial-1',body())
    state.finish(first,failure={'source':'provider_http','http_status':402})
    state.mark_cancelled('trial-1')
    state.finish(second,uncertain=True,failure={'source':'downstream_client_disconnect'})
    final=state.status('trial-1')
    assert final['pre_cancellation_provider_failure'] and final['pre_cancellation_infrastructure_failure']
    assert final['budget_halted'] and not final['cancellation_usage_uncertainty']
    assert 'cancellation_consequence' not in final['typed_failures'][-1]


def successful_response():
    return Response(p.encoded({'model': p.MODEL, 'choices': [], 'usage': USAGE}))


@pytest.mark.parametrize('transient', [500, 502, 503, 'RemoteDisconnected', 'TimeoutError'])
def test_native_same_request_retry_recovers_without_refunding_unknown_liability(proxy, transient, tmp_path):
    state, upstream, server = proxy
    token = register(state)
    if isinstance(transient, int):
        upstream.replies.append(Response(p.encoded({'error': {'code': 'temporary'}}), status=transient))
        expected = transient
    else:
        upstream.replies.append(http_client.RemoteDisconnected('no status') if transient == 'RemoteDisconnected'
                                else TimeoutError('connect timed out'))
        expected = 502
    payload = body(stream=False, max_tokens=p.MAX_OUTPUT)
    assert http(state, '/v1/chat/completions', token, payload)[0] == expected
    pending = state.status('trial-1')
    assert not pending['budget_halted'] and not state.halt_path.exists()
    assert pending['hard_failures'] == pending['recovered_transient_failures'] == []
    failure = pending['unresolved_transient_failures'][0]
    assert failure['consecutive_attempt_index'] == 1 and failure['recoverable'] is True
    assert Decimal(failure['retained_liability_upper_usd']) == Decimal('0.786432')
    assert state.committed_upper == Decimal('0.786432')
    # Object order changes are semantically identical. No request fields are removed.
    upstream.replies.append(successful_response())
    assert http(state, '/v1/chat/completions', token, dict(reversed(list(payload.items()))))[0] == 200
    recovered = state.close_trial('trial-1')
    assert not recovered['budget_halted'] and recovered['usage_uncertainty']
    assert recovered['request_usage_complete'] is False and recovered['liability_bounds_valid'] is True
    assert recovered['hard_failures'] == recovered['unresolved_transient_failures'] == []
    assert recovered['typed_failures'] == recovered['recovered_transient_failures']
    failure = recovered['recovered_transient_failures'][0]
    assert failure['recovery_status'] == 'recovered'
    assert failure['recovered_by_request_id'] == recovered['usage'][0]['request_id']
    assert failure['semantic_request_sha256'] == recovered['usage'][0]['semantic_request_sha256']
    assert Decimal(recovered['outstanding_financial_liability_usd']) == Decimal('0.786432')
    assert Decimal(recovered['recovered_transient_unknown_upper_usd']) == Decimal('0.786432')
    assert state.committed_upper == Decimal('0.786432') + Decimal(recovered['estimated_cost_usd'])
    # Exercise the real adapter acceptance contract on actual proxy evidence.
    from cynthiabench.opencode_agent import proxy_status_error, recovered_transport_evidence
    assert recovered_transport_evidence(recovered)
    assert proxy_status_error(recovered, 'trial-1') is None
    if transient == 'RemoteDisconnected':
        (tmp_path / 'real-proxy-recovered-status.json').write_text(json.dumps(recovered, indent=2))


def test_six_same_body_transients_exhaust_native_five_retries_and_halt(proxy):
    state, upstream, server = proxy
    token = register(state)
    for attempt in range(1, 7):
        upstream.replies.append(Response(p.encoded({'error': {'code': 'unavailable'}}), status=503))
        assert http(state, '/chat/completions', token, body())[0] == 503
        row = state.status('trial-1')
        assert row['unresolved_transient_failures'][-1]['consecutive_attempt_index'] == attempt
        assert row['budget_halted'] is (attempt == 6)
    assert state.halt_reason == 'provider_transient_retry_exhausted'
    assert state.committed_upper == 6 * Decimal('0.786432')
    assert not row['recovered_transient_failures'] and not row['hard_failures']
    assert http(state, '/chat/completions', token, body())[0] == 503
    assert len(upstream.calls) == 6


def test_unresolved_retry_cannot_be_replaced_by_different_context_or_other_trial(proxy):
    state, upstream, server = proxy
    first = register(state)
    second = register(state, 'unrelated-trial')
    upstream.replies.append(Response(p.encoded({'error': {}}), status=502))
    assert http(state, '/chat/completions', first, body())[0] == 502
    upstream.replies.append(successful_response())
    assert http(state, '/chat/completions', second, body())[0] == 200
    assert state.status('trial-1')['unresolved_transient_failures']
    changed = body(messages=[{'role': 'user', 'content': 'different context'}])
    assert http(state, '/chat/completions', first, changed)[0] == 409
    row = state.status('trial-1')
    assert row['budget_halted'] and row['hard_failures'][-1]['code'] == 'unresolved_request_changed'
    assert not row['recovered_transient_failures'] and len(upstream.calls) == 2


def test_only_before_header_transport_is_retryable_and_model_or_bounds_errors_stay_fatal(proxy):
    state, upstream, server = proxy
    register(state)
    ident, _ = state.begin('trial-1', body())
    state.finish(ident, failure={'source': 'proxy_transport', 'code': 'RemoteDisconnected',
                                'transport_phase': 'stream_started'}, uncertain=True)
    row = state.status('trial-1')
    assert row['budget_halted'] and row['hard_failures'] and not row['unresolved_transient_failures']


def test_recovered_financial_uncertainty_does_not_falsify_later_framework_deadline(proxy):
    state, upstream, server = proxy
    token = register(state)
    upstream.replies.extend([Response(p.encoded({'error': {}}), status=502), successful_response()])
    assert http(state, '/chat/completions', token, body())[0] == 502
    assert http(state, '/chat/completions', token, body())[0] == 200
    ident, _ = state.begin('trial-1', body(messages=[{'role':'user','content':'next request'}]))
    marker = state.mark_cancelled('trial-1')
    assert not marker['pre_cancellation_provider_failure']
    assert not marker['pre_cancellation_infrastructure_failure']
    state.finish(ident, failure={'source':'downstream_client_disconnect','code':'BrokenPipeError'}, uncertain=True)
    row = state.status('trial-1')
    assert not row['budget_halted'] and row['cancellation_usage_uncertainty']
    assert len(row['recovered_transient_failures']) == len(row['hard_failures']) == 1
    assert row['hard_failures'][0]['cancellation_consequence'] is True
    from cynthiabench.opencode_agent import cancellation_transport_only, proxy_failure_partition_valid
    assert proxy_failure_partition_valid(row)
    assert cancellation_transport_only(row, {'errors': []})
    assert Decimal(row['outstanding_financial_liability_usd']) == 2 * Decimal('0.786432')


def chain_carry(tmp_path):
    child_dir = tmp_path / 'child'; child_dir.mkdir()
    child = production_carry(child_dir)
    local_dir = tmp_path / 'local'; local_dir.mkdir()
    local = production_carry(local_dir, known='0.4', unknown='0.5')
    record = json.loads(local.read_text())
    inherited = {'event':'prior_budget_carried','currency':'USD',
        'carry_ledger_sha256':hashlib.sha256(child.read_bytes()).hexdigest(),
        'prior_known_metered_usd':'0.1','prior_unknown_upper_usd':'0.2','prior_committed_upper_usd':'0.3'}
    audit = Path(record['prior_provider_audit'])
    audit.write_text(json.dumps(inherited)+'\n'+audit.read_text())
    record['evidence_sha256'][str(audit)] = hashlib.sha256(audit.read_bytes()).hexdigest()
    record.update(schema_version=2,
        previous_carry_ledger={'path':str(child),'sha256':hashlib.sha256(child.read_bytes()).hexdigest()},
        local_known_metered_usd='0.4',local_unknown_upper_usd='0.5',local_committed_upper_usd='0.9',
        prior_known_metered_usd='0.5',prior_unknown_upper_usd='0.7',prior_committed_upper_usd='1.2')
    record['evidence_sha256'][str(child)] = hashlib.sha256(child.read_bytes()).hexdigest()
    local.chmod(0o644);local.write_text(json.dumps(record));local.chmod(0o444)
    return local, child


def test_recursive_carry_validates_real_generation_rows_and_inherited_liability(tmp_path):
    ledger, child = chain_carry(tmp_path)
    validated = p.load_carry_ledger(ledger)
    assert validated.known_metered == Decimal('0.5')
    assert validated.unknown_upper == Decimal('0.7')
    assert validated.committed_upper == Decimal('1.2')
    state = p.ProxyState(tmp_path/'new-provider',tmp_path/'halt',carry=validated)
    state.set_balance(balance('49.90'))
    assert state.committed_upper == Decimal('1.2') and state.current_committed_upper == 0
    # A fresh account snapshot cannot forgive inherited unknown liabilities.
    state.set_balance(balance('50'))
    assert state.committed_upper == Decimal('1.2')


@pytest.mark.parametrize('mutation', ['omitted_child','wrong_inherited_audit','aggregate_reset','child_raw_drift'])
def test_chain_carry_rejects_omitted_previous_or_current_generation_liability(tmp_path, mutation):
    ledger, child = chain_carry(tmp_path)
    record = json.loads(ledger.read_text())
    if mutation == 'omitted_child':
        del record['previous_carry_ledger']
    elif mutation == 'aggregate_reset':
        record.update(prior_known_metered_usd='0.4',prior_unknown_upper_usd='0.5',prior_committed_upper_usd='0.9')
    elif mutation == 'wrong_inherited_audit':
        audit = Path(record['prior_provider_audit'])
        text = audit.read_text().replace('"prior_committed_upper_usd": "0.3"','"prior_committed_upper_usd": "0"')
        audit.write_text(text)
        record['evidence_sha256'][str(audit)] = hashlib.sha256(audit.read_bytes()).hexdigest()
    else:
        child_record = json.loads(child.read_text())
        Path(child_record['prior_trial_statuses'][0]).write_text('{}')
    ledger.chmod(0o644);ledger.write_text(json.dumps(record));ledger.chmod(0o444)
    with pytest.raises(ValueError):
        p.load_carry_ledger(ledger)


def test_recovered_transients_retain_full_upper_liability_and_cannot_cross_usd50_cap(tmp_path):
    state = p.ProxyState(tmp_path/'provider',tmp_path/'halt',
                         carry=p.load_carry_ledger(production_carry(tmp_path)))
    state.url='http://127.0.0.1:1'
    state.set_balance(balance())
    completed=0
    for n in range(100):
        trial='recovered-'+str(n)
        try:
            state.register(trial)
            first,_=state.begin(trial,body())
            state.finish(first,failure={'source':'proxy_transport','code':'RemoteDisconnected',
                'transport_phase':'before_headers'},uncertain=True)
            second,_=state.begin(trial,body())
        except p.GuardError as error:
            assert error.code=='budget_reservation_denied'
            break
        state.finish(second,usage=USAGE,response_models={p.MODEL})
        row=state.close_trial(trial)
        completed+=1
        assert not row['budget_halted']
        assert Decimal(row['recovered_transient_unknown_upper_usd']) == Decimal('0.786432')
        # Fresh rounded metadata cannot erase already conservatively committed unknowns.
        state.set_balance(balance())
    assert completed==59
    assert state.committed_upper > Decimal('47.48')
    assert state.halt_reason=='budget_reservation_denied'
    assert state.committed_upper + state.reserved < Decimal('48')


def test_reported_balance_admission_preserves_unknowns_without_blocking_prepaid_funds(tmp_path):
    state = p.ProxyState(tmp_path/'provider', tmp_path/'halt.json',
        carry=zero_carry(tmp_path), balance_admission='reported-balance')
    state.url = 'http://127.0.0.1:1'
    state.set_balance(balance('49.41'))
    for n in range(70):
        trial = 'recovered-'+str(n)
        state.register(trial)
        first, _ = state.begin(trial, body())
        state.finish(first, failure={'source':'proxy_transport', 'code':'RemoteDisconnected',
            'transport_phase':'response_headers'}, uncertain=True)
        second, _ = state.begin(trial, body())
        state.finish(second, usage=USAGE, response_models={p.MODEL})
        row = state.close_trial(trial)
        assert not row['budget_halted']
        assert row['request_usage_complete'] is False
        assert Decimal(row['recovered_transient_unknown_upper_usd']) == Decimal('0.786432')
    assert state.committed_upper > 50
    assert state.effective_balance() == Decimal('49.41')-state.current_known_metered
    assert not state.halt_path.exists() and not state.warned
    # A real balance decline controls admission, without clearing the evidence.
    upper_before = state.committed_upper
    state.set_balance(balance('2.50'))
    with pytest.raises(p.GuardError, match='reservation'):
        state.begin(state.register('declined')['trial_id'], body())
    assert state.halt_reason == 'budget_reservation_denied'
    assert state.committed_upper == upper_before


def test_native_transport_header_allowlist_reaches_upstream_without_proxy_credentials(proxy):
    state, upstream, server = proxy
    token = register(state)
    upstream.replies.append(successful_response())
    request = urllib.request.Request(state.url+'/chat/completions', data=p.encoded(body()), headers={
        'Authorization':'Bearer '+token, 'Content-Type':'application/json',
        'User-Agent':'opencode/1.19.6 fixture', 'Accept':'*/*',
        'x-opencode-session-id':'ses_fixture', 'x-session-affinity':'ses_fixture',
        'x-session-id':'ses_fixture', 'Accept-Encoding':'gzip,br',
        'Cookie':'private-cookie', 'X-Unrelated-Secret':'private-header'})
    with urllib.request.urlopen(request, timeout=5) as response:
        assert response.status == 200
    headers = {k.lower():v for k,v in upstream.headers[-1].items()}
    assert set(headers) == p.NATIVE_HEADERS
    assert headers['user-agent'] == 'opencode/1.19.6 fixture'
    assert headers['x-session-affinity'] == 'ses_fixture'
    audit = (state.root/'audit.jsonl').read_text()
    assert token not in audit and 'private-cookie' not in audit and 'private-header' not in audit


@pytest.mark.parametrize('failure_phase', ['connect','request_send','response_headers'])
def test_upstream_failures_preserve_precise_phase_and_close_connection(tmp_path, monkeypatch, failure_phase):
    key = tmp_path/'key'; key.write_text('offline-provider-key')
    class Connection:
        sock = None
        closed = False
        def __init__(self,*args,**kwargs): pass
        def connect(self):
            if failure_phase == 'connect': raise ConnectionRefusedError('fixture')
        def request(self,*args,**kwargs):
            if failure_phase == 'request_send': raise BrokenPipeError('fixture')
        def getresponse(self): raise http_client.RemoteDisconnected('fixture')
        def close(self): self.closed=True
    connection = Connection()
    monkeypatch.setattr(p.http.client,'HTTPSConnection',lambda *a,**k:connection)
    with pytest.raises(OSError) as caught:
        p.HTTPSUpstream(key).request('POST','/chat/completions',b'{}')
    assert caught.value.proxy_transport_phase == failure_phase
    assert connection.closed


@pytest.mark.parametrize('has_response', [True,False])
def test_early_http_rejection_survives_broken_upload_and_read_is_bounded(tmp_path, monkeypatch, has_response):
    key = tmp_path/'key'; key.write_text('offline-provider-key')
    class Socket:
        def __init__(self): self.timeouts=[]
        def settimeout(self,value): self.timeouts.append(value)
    class Connection:
        closed=False
        def __init__(self): self.sock=Socket();self.sent_headers=None
        def connect(self): pass
        def request(self,*args,**kwargs):
            self.sent_headers=kwargs['headers'];raise BrokenPipeError('original upload failure')
        def getresponse(self):
            if not has_response: raise TimeoutError('bounded read expired')
            return Response(b'{"error":{"code":"request_rejected"}}',status=413)
        def close(self):self.closed=True
    connection=Connection()
    monkeypatch.setattr(p.http.client,'HTTPSConnection',lambda *a,**k:connection)
    upstream=p.HTTPSUpstream(key)
    headers={'User-Agent':'opencode/1.19.6','Authorization':'untrusted',
        'Host':'untrusted','Content-Length':'1','Accept-Encoding':'gzip','Cookie':'untrusted'}
    if has_response:
        response=upstream.request('POST','/chat/completions',b'{}',headers=headers)
        assert response.status==413
        assert response._proxy_transport_metadata['upload_error_with_http_response']=='BrokenPipeError'
        p.close_response(response)
    else:
        with pytest.raises(BrokenPipeError,match='original upload failure') as caught:
            upstream.request('POST','/chat/completions',b'{}',headers=headers)
        assert caught.value.proxy_transport_phase=='request_send'
    assert connection.sock.timeouts==[3,120] and connection.closed
    assert connection.sent_headers=={'User-Agent':'opencode/1.19.6',
        'Authorization':'Bearer offline-provider-key','Accept-Encoding':'identity','Content-Type':'application/json'}
