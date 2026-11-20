"""HTTP and subprocess boundary tests with a deliberately controllable fake worker."""
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sys
import threading
import urllib.error
from urllib.request import Request, urlopen

import pytest

from cynthiabench.server import BattleHTTPServer, SessionManager, MAX_REQUEST_BYTES


FAKE_WORKER = '''
import argparse, json, sys, time
p=argparse.ArgumentParser()
for arg in ('team','seed','trial','output'):p.add_argument('--'+arg)
p.add_argument('--bad-ready',action='store_true')
a=p.parse_args()
if a.bad_ready:
 print('not JSON',flush=True);time.sleep(10);sys.exit()
frame=0;outcome='ongoing';valid=True;terminal=False
def observe():return {'frame':frame,'public_state':{'team':a.team},'terminal':terminal,'outcome':outcome,'private_seed':a.seed,'raw_memory':'SHOULD_NOT_REACH_AGENT'}
def result():return {'valid':valid,'terminal':terminal,'outcome':outcome,'progress_score':100 if outcome=='win' else 0,'team_id':a.team,'seed':int(a.seed),'trial_id':a.trial}
print(json.dumps(observe()),flush=True)
for line in sys.stdin:
 c=json.loads(line);op=c['op']
 if op=='action':
  action=c['action']
  if action.get('die'):sys.exit(7)
  if action.get('badjson'):print('BAD',flush=True);continue
  if action.get('sleep'):time.sleep(10)
  frame+=action.get('frames',1)
  if action.get('win'):outcome='win';terminal=True
  r=observe()
 elif op=='observe':r=observe()
 elif op=='result':r=result()
 elif op=='finalize':
  if c['reason']=='runtime_error':valid=False;outcome='invalid';terminal=False
  elif not terminal:outcome='timeout';terminal=True
  r=result()
 elif op=='close':
  print(json.dumps({'closed':True,'result':{**result(),'recording':{'frames':frame}}}),flush=True);break
 else:r={'error':'unknown'}
 print(json.dumps(r),flush=True)
'''


@pytest.fixture
def service(tmp_path):
    fake = tmp_path / 'worker.py'
    fake.write_text(FAKE_WORKER)
    manager = SessionManager(team_ids=['team-one', 'team-two'], output_root=tmp_path / 'runs',
        worker_command=[sys.executable, '-u', str(fake)], ready_timeout=2, request_timeout=1)
    server = BattleHTTPServer(('127.0.0.1', 0), manager)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield manager, server, tmp_path
    server.shutdown()
    server.server_close()
    manager.close_all()


def request(service, path, token=None, payload=None, raw=None):
    manager, server, _ = service
    headers = {'Content-Type': 'application/json'}
    if token:
        headers['Authorization'] = 'Bearer ' + token
    data = raw if raw is not None else (None if payload is None else json.dumps(payload).encode())
    req = Request(f'http://127.0.0.1:{server.server_port}' + path, data=data, headers=headers)
    try:
        with urlopen(req, timeout=5) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as exc:
        return exc.code, json.load(exc)


def create(service, trial='trial-1', team='team-one', seed=42):
    status, body = request(service, '/admin/sessions', service[0].admin_token,
                           {'team_id': team, 'seed': seed, 'trial_id': trial})
    assert status == 200, body
    return body


def test_authorization_public_filter_and_one_worker_per_trial(service):
    first = create(service)
    second = create(service, 'trial-2', 'team-two', 99)
    manager = service[0]
    assert request(service, '/health')[0] == 200
    assert request(service, '/admin/status')[0] == 401
    assert request(service, '/admin/status', first['agent_token'])[0] == 401
    status_code, host_status = request(service, '/admin/status', manager.admin_token)
    assert status_code == 200 and host_status['active_sessions'] == 2
    assert 'admin_token' not in json.dumps(host_status)
    assert request(service, '/v1/observe')[0] == 401
    assert request(service, '/v1/observe', manager.admin_token)[0] == 401
    admin_result = '/admin/sessions/' + first['session_id'] + '/result'
    assert request(service, admin_result, first['agent_token'])[0] == 401
    status, observation = request(service, '/v1/action', first['agent_token'], {'frames': 7})
    assert status == 200 and observation['frame'] == 7
    assert 'private_seed' not in observation and 'raw_memory' not in observation
    assert request(service, '/v1/observe', second['agent_token'])[1]['frame'] == 0
    assert manager.get_session(first['session_id']).process.pid != manager.get_session(second['session_id']).process.pid
    request(service, '/v1/action', first['agent_token'], {'win': True})
    result = request(service, admin_result, manager.admin_token)[1]
    assert result['outcome'] == 'win' and result['session_id'] == first['session_id']
    assert result['team_id'] == 'team-one' and result['seed'] == 42
    assert request(service, '/admin/sessions', manager.admin_token,
                   {'team_id': 'team-one', 'seed': 1, 'trial_id': 'trial-1'})[0] == 409
    close = '/admin/sessions/' + first['session_id'] + '/close'
    assert request(service, close, first['agent_token'], {})[0] == 401
    assert request(service, close, manager.admin_token, {})[1]['closed']
    assert request(service, '/v1/action', first['agent_token'], {'frames': 1})[0] == 410
    final = request(service, admin_result, manager.admin_token)[1]
    assert final['outcome'] == 'win'
    assert final['recording']['frames'] == 8
    persisted = json.loads((manager.get_session(first['session_id']).output / 'bridge-result.json').read_text())
    assert persisted == final


@pytest.mark.parametrize('field,value', [('team_id', '../team-one'), ('team_id', 'unknown'),
    ('trial_id', '../escape'), ('trial_id', '/tmp/file'), ('seed', True), ('seed', -1), ('seed', 2**32)])
def test_invalid_session_inputs_are_rejected(service, field, value):
    payload = {'team_id': 'team-one', 'seed': 42, 'trial_id': 'trial-1', field: value}
    assert request(service, '/admin/sessions', service[0].admin_token, payload)[0] == 400
    assert not service[0].sessions


def test_worker_protocol_failure_is_invalid_not_loss(service):
    first = create(service)
    assert request(service, '/v1/action', first['agent_token'], {'badjson': True})[0] == 503
    result = request(service, '/admin/sessions/' + first['session_id'] + '/result', service[0].admin_token)[1]
    assert result['valid'] is False and result['outcome'] == 'invalid'
    assert service[0].get_session(first['session_id']).process.poll() is not None


def test_worker_timeout_is_infrastructure_failure(service):
    first = create(service)
    assert request(service, '/v1/action', first['agent_token'], {'sleep': True})[0] == 503
    result = service[0].get_session(first['session_id']).result()
    assert result['valid'] is False and result['outcome'] == 'invalid'


def test_agent_budget_timeout_is_explicit_zero_not_invalid(service):
    first = create(service)
    route = '/admin/sessions/' + first['session_id'] + '/finalize'
    status, result = request(service, route, service[0].admin_token, {'reason': 'timeout'})
    assert status == 200
    assert result['valid'] and result['terminal'] and result['outcome'] == 'timeout'


def test_size_limit_and_malformed_request(service):
    first = create(service)
    assert request(service, '/v1/action', first['agent_token'], raw=b'{' * (MAX_REQUEST_BYTES + 1))[0] == 413
    assert request(service, '/v1/action', first['agent_token'], raw=b'not-json')[0] == 400
    assert request(service, '/v1/action', first['agent_token'], raw=b'[]')[0] == 400


def test_concurrent_actions_are_serialized_for_one_worker(service):
    first = create(service)
    with ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(pool.map(lambda _: request(service, '/v1/action', first['agent_token'], {'frames': 1}), range(8)))
    assert all(status == 200 for status, body in responses)
    assert sorted(body['frame'] for status, body in responses) == list(range(1, 9))
    assert request(service, '/v1/observe', first['agent_token'])[1]['frame'] == 8


def test_failed_readiness_does_not_create_session_or_valid_result(tmp_path):
    fake = tmp_path / 'worker.py'
    fake.write_text(FAKE_WORKER)
    manager = SessionManager(team_ids=['team-one'], output_root=tmp_path / 'runs',
        worker_command=[sys.executable, '-u', str(fake), '--bad-ready'], ready_timeout=1)
    with pytest.raises(Exception):
        manager.create({'team_id': 'team-one', 'seed': 42, 'trial_id': 'failed'})
    assert not manager.sessions and manager.pending == 0
    result = json.loads((tmp_path / 'runs/failed/bridge-result.json').read_text())
    assert result['valid'] is False


def test_terminal_game_survives_provider_error_finalization(service):
    first = create(service)
    request(service, '/v1/action', first['agent_token'], {'win': True})
    route = '/admin/sessions/' + first['session_id'] + '/finalize'
    status, result = request(service, route, service[0].admin_token, {'reason': 'runtime_error'})
    assert status == 200 and result['valid'] and result['outcome'] == 'win'
