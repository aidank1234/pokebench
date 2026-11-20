"""Actor identity and native helper accounting; never calls a model provider."""
from copy import deepcopy
import json
from pathlib import Path
import pytest

from cynthiabench.native_agent import ROOT, NativeSubscriptionAgent, parse_events, resolve_model

MODEL='claude-opus-5-5'
HELPER='claude-haiku-5-5'


def events():
    return [
        {'type':'system','subtype':'init','model':MODEL},
        {'type':'assistant','parent_tool_use_id':None,
         'message':{'model':MODEL,'content':[{'type':'tool_use','id':'fetch','name':'WebFetch','input':{'url':'https://example.com'}}]}},
        {'type':'result','subtype':'success','is_error':False,
         'modelUsage':{MODEL:{'inputTokens':63161},HELPER:{'inputTokens':47060}},
         'subagent_stats':{'spawned':0,'requested':{'foreground':0,'background':0,'unset':0}},'usage':{}}]


def parse(tmp_path, data):
    path=tmp_path/'events.jsonl';path.write_text(''.join(json.dumps(e)+'\n' for e in data))
    return parse_events(path,resolve_model('opus'))


def test_documented_helper_preserves_actor_but_requires_review(tmp_path):
    audit=parse(tmp_path,events())
    assert not audit['model_mismatch']
    assert audit['actor_models']==[MODEL] and audit['auxiliary_models']==[HELPER]
    assert audit['auxiliary_model_review_required'] and audit['per_model_usage']==[events()[-1]['modelUsage']]


@pytest.mark.parametrize('change',['alternate_actor','unknown_usage','agent_tool','spawned_subagent','missing_actor'])
def test_actor_fallback_unknown_usage_and_deliberate_delegation_still_fail(tmp_path,change):
    data=events()
    if change=='alternate_actor':data[1]['message']['model']=HELPER
    elif change=='unknown_usage':data[-1]['modelUsage']['claude-sonnet-5-5']={}
    elif change=='agent_tool':data[1]['message']['content'][0]['name']='Agent'
    elif change=='spawned_subagent':data[-1]['subagent_stats']['spawned']=1
    elif change=='missing_actor':data[1]['message'].pop('model')
    assert parse(tmp_path,data)['model_mismatch']


def test_recorded_opus_events_run_through_actual_native_adapter_and_harbor(tmp_path,monkeypatch):
    from harbor.models.trial.config import AgentConfig
    from test_harbor_integration import run_harbor_fixture
    import cynthiabench.native_agent as native
    recorded=ROOT/'runs/harbor/dev-human01-native-v1/human-01-seed1__2PS4k2o/agent/events.jsonl'
    if not recorded.exists():pytest.skip('Preserved local development transcript is unavailable')
    actual=parse_events(recorded,resolve_model('opus'))
    assert actual['assistant_message_count']==120 and actual['actor_models']==[MODEL]
    assert actual['auxiliary_models']==[HELPER] and not actual['model_mismatch']
    original_setup=NativeSubscriptionAgent.setup
    async def setup(self,environment):
        await original_setup(self,environment)
        (environment.workspace/'recorded-events.jsonl').write_bytes(recorded.read_bytes())
        (environment.workspace/'replay.py').write_text('''import sys,json,urllib.request
from pathlib import Path
sys.stdin.read()
s=json.loads(Path('battle-session.json').read_text())
r=urllib.request.Request(s['url']+'/v1/action',data=b'{"buttons":["A"],"frames":1}',headers={'Authorization':'Bearer '+s['agent_token'],'Content-Type':'application/json'})
with urllib.request.urlopen(r) as response:response.read()
sys.stdout.write(Path('recorded-events.jsonl').read_text())
''')
    monkeypatch.setattr(NativeSubscriptionAgent,'setup',setup)
    monkeypatch.setattr(native,'command',lambda spec,workspace,effort,executable:['/usr/bin/python3',str(workspace/'replay.py')])
    run_harbor_fixture(tmp_path,[AgentConfig(import_path='cynthiabench.native_agent:NativeSubscriptionAgent',model_name=MODEL,skip_model_parsing=True)])
    audit=json.loads(next((tmp_path/'jobs/harbor-smoke').glob('*/agent/runtime-audit.json')).read_text())
    assert audit['auxiliary_model_review_required'] and not audit['model_mismatch']
    invocation=json.loads(next((tmp_path/'jobs/harbor-smoke').glob('*/agent/invocation.json')).read_text())
    assert audit['calculation_python']==invocation['calculation_python']
    assert len(audit['calculation_python']['sha256'])==64
