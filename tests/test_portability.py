"""Portable configuration and public task consistency without private assets."""
import hashlib
import json
from pathlib import Path
import pytest
from cynthiabench.paths import runtime_paths
from cynthiabench.provenance import collect_hashes, validate_freeze
from cynthiabench.analysis import freeze_manifest, NATIVE_MODEL_IDS


def test_relative_paths_use_checkout_and_external_paths_are_supported(tmp_path, monkeypatch):
    root=tmp_path/'checkout';(root/'config').mkdir(parents=True)
    private=tmp_path/'private'
    (root/'config/local.toml').write_text('[paths]\nprivate = "../private"\nrom = "../private/game.nds"\ndata = "generated"\ncore = "../engine/core.dylib"\n')
    monkeypatch.delenv('POKEBENCH_CONFIG',raising=False)
    paths=runtime_paths(root)
    assert paths.private == private and paths.data == root/'generated'
    assert paths.fixtures == private/'fixtures'
    assert paths.rom == private/'game.nds'
    (root/'config/local.toml').write_text('[paths]\nunknown = "wrong"\n')
    with pytest.raises(ValueError):runtime_paths(root)


def test_public_tasks_match_authoritative_teams_and_preserved_seed_rule():
    root=Path(__file__).resolve().parents[1]
    teams={t['id']:t for t in json.loads((root/'teams/team-specs.json').read_text())['teams']}
    cases=json.loads((root/'provenance/task-manifest.json').read_text())
    assert len(cases)==30 and len(teams)==10
    for case in cases:
        assert not Path(case['path']).is_absolute()
        expected=int.from_bytes(hashlib.sha256(f"CynthiaBench-v1:{case['team_id']}:{case['replicate']}".encode()).digest()[:4],'little')
        assert case['seed']==expected
        team=json.loads((root/case['path']/'environment/team.json').read_text())
        original=teams[case['team_id']]
        keys={'species','level','nature','gender','ability','moves','held_item','ivs','evs','friendship','nickname'}
        assert team['party']==[{k:v for k,v in p.items() if k in keys} for p in original['party']]
        assert team['bag']==original['bag']
        assert set(team)<= {'id','name','battle_style','bag','party'}


def test_freeze_roundtrip_uses_same_portable_hash_collector(tmp_path, monkeypatch):
    monkeypatch.delenv('POKEBENCH_CONFIG',raising=False)
    (tmp_path/'provenance').mkdir()
    fixtures=tmp_path/'assets/private/fixtures';fixtures.mkdir(parents=True)
    (tmp_path/'src/cynthiabench').mkdir(parents=True)
    (tmp_path/'src/cynthiabench/example.py').write_text('# synthetic source')
    cases=[]
    for team in range(10):
        for replicate in range(1,4):
            name=f'team-{team}-seed{replicate}'
            task=tmp_path/'tasks'/name;task.mkdir(parents=True)
            (task/'instruction.md').write_text('synthetic task')
            case={'name':name,'team_id':f'team-{team}','seed':replicate,'replicate':replicate}
            cases.append(case)
            state=fixtures/(name+'.state');state.write_bytes(bytes([team,replicate]))
            fixture={**case,'state_file':str(state.relative_to(tmp_path)),
                     'state_sha256':hashlib.sha256(state.read_bytes()).hexdigest(),
                     'save_sha256':'save','rom_sha256':'rom','core_sha256':'core'}
            (fixtures/(name+'.json')).write_text(json.dumps(fixture))
    (tmp_path/'provenance/task-manifest.json').write_text(json.dumps(cases))
    settings={model:{'import_path':'synthetic','kwargs':{}} for model in NATIVE_MODEL_IDS}
    frozen=freeze_manifest(tmp_path,['official'],agent_configs=settings)
    path=tmp_path/'freeze.json';path.write_text(json.dumps(frozen))
    snapshot={**collect_hashes(tmp_path),'agent_configs':settings,'execution':{'n_concurrent_trials':6}}
    validate_freeze(snapshot,path)
    assert frozen['expected_trials']==180
    (tmp_path/'src/cynthiabench/example.py').write_text('# changed')
    with pytest.raises(ValueError,match='source_sha256'):
        validate_freeze({**snapshot,**collect_hashes(tmp_path)},path)
