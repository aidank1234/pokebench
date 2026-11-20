"""Task generation guardrails; every write stays in pytest's temporary directory."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import pytest

SCRIPT=Path(__file__).resolve().parents[1]/'scripts/build_harbor_tasks.py'


def build(tmp_path, ids, *flags):
    specs=tmp_path/'specs.json'
    specs.write_text(json.dumps({'teams':[{'id':name,'party':[],'bag':{},'battle_style':'set'} for name in ids]}))
    process=subprocess.run([sys.executable,str(SCRIPT),'--specs',str(specs),'--output',str(tmp_path/'tasks'),
        '--manifest',str(tmp_path/'manifest.json'),*flags],capture_output=True,text=True)
    return process


def test_official_defaults_preserve_ten_by_three_seed_rule_and_scenario_shape(tmp_path):
    process=build(tmp_path,[f'human-{i:02}' for i in range(1,11)])
    assert process.returncode==0,process.stderr
    manifest=json.loads((tmp_path/'manifest.json').read_text())
    assert len(manifest)==30
    for row in manifest:
        expected=int.from_bytes(hashlib.sha256(f'CynthiaBench-v1:{row["team_id"]}:{row["replicate"]}'.encode()).digest()[:4],'little')
        assert row['seed']==expected
        scenario=json.loads((Path(row['path'])/'environment/scenario.json').read_text())
        assert set(scenario)=={'team_id','replicate','seed','seed_scheme'}
    assert manifest[0]['seed']==686932978


def test_three_calibrations_can_share_original_first_rng_without_reusing_team_ids(tmp_path):
    ids=['cal-101','cal-102','cal-103']
    process=build(tmp_path,ids,'--development','--replicates','1','--seed-reference','human-01')
    assert process.returncode==0,process.stderr
    manifest=json.loads((tmp_path/'manifest.json').read_text())
    assert [row['name'] for row in manifest]==[name+'-seed1' for name in ids]
    assert {row['seed'] for row in manifest}=={686932978}
    assert all(row['phase']=='development' and row['seed_reference']=='human-01' for row in manifest)
    assert all('seed_reference' in row['seed_scheme'] for row in manifest)


def test_development_flags_cannot_relax_official_generation(tmp_path):
    for flags in (('--replicates','1'),('--seed-reference','human-01'),('--development','--replicates','4'),
                  ('--development','--seed-reference','../private')):
        run=tmp_path/str(len(list(tmp_path.iterdir())));run.mkdir()
        assert build(run,['cal-101','cal-102','cal-103'],*flags).returncode!=0
        assert not (run/'manifest.json').exists() and not (run/'tasks').exists()
    run=tmp_path/'too-few-official';run.mkdir()
    assert build(run,['cal-101','cal-102','cal-103']).returncode!=0
    assert not (run/'manifest.json').exists()


def test_development_default_replicates_remain_three_and_ids_must_be_unique(tmp_path):
    process=build(tmp_path,['cal-101'],'--development')
    assert process.returncode==0
    manifest=json.loads((tmp_path/'manifest.json').read_text())
    assert [row['replicate'] for row in manifest]==[1,2,3]
    assert len({row['seed'] for row in manifest})==3
    duplicate=tmp_path/'duplicate';duplicate.mkdir()
    assert build(duplicate,['cal-101','cal-101'],'--development').returncode!=0
    assert not (duplicate/'tasks').exists()


def test_three_calibration_cases_cannot_be_frozen_as_an_official_cohort(tmp_path):
    from cynthiabench.analysis import freeze_manifest
    assert build(tmp_path,['cal-101','cal-102','cal-103'],'--development','--replicates','1','--seed-reference','human-01').returncode==0
    (tmp_path/'provenance').mkdir()
    (tmp_path/'provenance/task-manifest.json').write_bytes((tmp_path/'manifest.json').read_bytes())
    with pytest.raises(ValueError,match='thirty cases across ten teams'):
        freeze_manifest(tmp_path,['must-not-launch'])
