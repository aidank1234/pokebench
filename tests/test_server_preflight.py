from copy import deepcopy
import json
from pathlib import Path
import pytest

from cynthiabench.server_preflight import configuration, validate_status
from cynthiabench.provenance import ROOT


def test_configuration_and_recording_evidence_are_required(tmp_path):
    current = configuration(tmp_path)
    assert current['recording_preflight']['ready']
    status = {'startup_configuration': current, 'current_configuration': current, 'configuration_matches_startup': True}
    case = 'mock-seed1'
    result = validate_status(tmp_path, status, [tmp_path / 'tasks' / case])
    assert result['validated'] and result['requested_task_count'] == 1


@pytest.mark.parametrize('field', ['team_specs_sha256', 'task_manifest_sha256', 'team_ids', 'server_code_sha256'])
def test_stale_running_server_rejected_before_agents(field, tmp_path):
    current = configuration(tmp_path)
    startup = deepcopy(current);startup[field] = 'stale'
    with pytest.raises(ValueError, match='Stale host bridge'):
        validate_status(tmp_path, {'startup_configuration': startup, 'current_configuration': current,
                              'configuration_matches_startup': False}, [])


def test_legacy_server_without_status_provenance_fails_closed(tmp_path):
    with pytest.raises(ValueError, match='lacks startup'):
        validate_status(tmp_path, {'service': 'CynthiaBench'}, [])


@pytest.fixture(autouse=True)
def temporary_configuration(tmp_path):
    # Synthetic evidence exists only inside this test's temporary directory.
    import hashlib
    for folder in ('teams', 'provenance', 'reports', 'tasks/mock-seed1/environment', 'src/cynthiabench'):
        (tmp_path / folder).mkdir(parents=True, exist_ok=True)
    (tmp_path / 'teams/team-specs.json').write_text(json.dumps({'teams':[{'id':'mock'}]}))
    case={'name':'mock-seed1','team_id':'mock','seed':42}
    (tmp_path / 'provenance/task-manifest.json').write_text(json.dumps([case]))
    (tmp_path / 'tasks/mock-seed1/environment/scenario.json').write_text(json.dumps(case))
    source=tmp_path/'src/cynthiabench/recording.py';source.write_text('# synthetic fixture')
    (tmp_path/'reports/recording-validation.json').write_text(json.dumps({'status':'PASS', 'files_sha256':{'src/cynthiabench/recording.py':hashlib.sha256(source.read_bytes()).hexdigest()}}))
    (tmp_path/'reports/recording-game-verification.json').write_text(json.dumps({'status':'PASS','checks':{'synthetic_only':True}}))


def test_missing_recording_evidence_rejected(tmp_path):
    (tmp_path/'reports/recording-game-verification.json').unlink()
    current=configuration(tmp_path)
    with pytest.raises(ValueError, match='Recording validation evidence'):
        validate_status(tmp_path, {'startup_configuration':current,'current_configuration':current,'configuration_matches_startup':True}, [])
