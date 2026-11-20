"""Fail-closed host configuration check before starting paid/subscribed agents."""
import hashlib
import json
from pathlib import Path


def file_hash(path):
    path = Path(path)
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def recording_evidence(root):
    root = Path(root)
    names = ('reports/recording-validation.json', 'reports/recording-game-verification.json')
    evidence, checks = [], []
    records = []
    for name in names:
        path = root / name
        try:
            record = json.loads(path.read_text())
        except (OSError, ValueError):
            record = {}
        records.append(record)
        evidence.append({'path': name, 'sha256': file_hash(path), 'status': record.get('status')})
    for name, expected in records[0].get('files_sha256', {}).items():
        actual = file_hash(root / name)
        checks.append({'path': name, 'sha256': actual, 'matches_validated_source': actual == expected})
    game_checks = records[1].get('checks', {})
    ready = (all(record.get('status') == 'PASS' for record in records) and bool(checks)
             and all(check['matches_validated_source'] for check in checks)
             and bool(game_checks) and all(value is True for value in game_checks.values()))
    return {'ready': ready, 'evidence': evidence, 'source_checks': checks,
            'actual_game_capture_checks_pass': bool(game_checks) and all(value is True for value in game_checks.values()),
            'scope': 'Prior recording validation evidence, not a new recording or a scored game.'}


def configuration(root, team_specs=None):
    root = Path(root)
    team_specs = Path(team_specs or root / 'teams/team-specs.json')
    specs = json.loads(team_specs.read_text())
    teams = specs['teams'] if isinstance(specs, dict) else specs
    return {'team_ids': sorted(team['id'] for team in teams),
            'team_specs_sha256': file_hash(team_specs),
            'task_manifest_sha256': file_hash(root / 'provenance/task-manifest.json'),
            'server_code_sha256': {name: file_hash(root / name) for name in (
                'src/cynthiabench/server.py', 'src/cynthiabench/server_preflight.py')},
            'recording_preflight': recording_evidence(root)}


def validate_status(root, status, task_paths):
    root = Path(root)
    startup = status.get('startup_configuration')
    current = configuration(root)
    if not isinstance(startup, dict):
        raise ValueError('Server lacks startup configuration provenance; restart the updated host bridge')
    for key in ('team_ids', 'team_specs_sha256', 'task_manifest_sha256', 'server_code_sha256'):
        if startup.get(key) != current[key] or status.get('current_configuration', {}).get(key) != current[key]:
            raise ValueError('Stale host bridge configuration: ' + key + '; restart before launching agents')
    if status.get('configuration_matches_startup') is not True:
        raise ValueError('Host bridge reports configuration changed after startup')
    if not current['recording_preflight']['ready']:
        raise ValueError('Recording validation evidence is missing or stale')
    if status.get('current_configuration', {}).get('recording_preflight') != current['recording_preflight']:
        raise ValueError('Host recording preflight evidence disagrees with local evidence')
    cases = {case['name']: case for case in json.loads((root / 'provenance/task-manifest.json').read_text())}
    for task in task_paths:
        task = Path(task)
        scenario = json.loads((task / 'environment/scenario.json').read_text())
        expected = cases.get(task.name)
        if not expected or any(expected.get(key) != scenario.get(key) for key in ('team_id', 'seed')):
            raise ValueError('Task scenario disagrees with current task manifest: ' + task.name)
        if scenario['team_id'] not in startup['team_ids']:
            raise ValueError('Task team absent from running host whitelist: ' + task.name)
    return {'validated': True, 'status': status, 'requested_task_count': len(task_paths)}
