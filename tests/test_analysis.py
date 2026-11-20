from copy import deepcopy
import json
from pathlib import Path

from cynthiabench.analysis import MODEL_IDS as SUPPORTED_MODELS, analyze, cluster_bootstrap, digest, validate_retry_launch
import pytest

# These regression fixtures exercise a four-system study; the registry may grow.
MODEL_IDS = SUPPORTED_MODELS[:4]


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + '\n')


def cohort(root):
    fixtures = []
    reviews = {'trial_reviews': {}, 'infrastructure_retry_reviews': {}}
    for team in range(10):
        for seed in range(3):
            name = f'team-{team}-seed{seed}'
            state = root / 'fixtures' / (name + '.state')
            state.parent.mkdir(exist_ok=True)
            state.write_bytes(bytes([team, seed]))
            fixtures.append({'name': name, 'team_id': f'team-{team}', 'seed': seed, 'replicate': seed + 1,
                'state_file': str(state.relative_to(root)), 'state_sha256': digest(state),
                'save_sha256': 'save', 'rom_sha256': 'rom', 'core_sha256': 'core', 'task_files': {}})
    manifest = {'phase': 'scored', 'models': MODEL_IDS, 'fixtures': fixtures, 'jobs': ['official'],
        'frozen_at': '2026-11-18T00:00:00+00:00', 'source_sha256': {}, 'task_sha256': {}}
    write(root / 'manifest.json', manifest)
    write(root / 'runs/harbor/official/protocol-snapshot.json', {
        'frozen_manifest_sha256': digest(root / 'manifest.json'), 'source_sha256': {}, 'task_sha256': {},
        'captured_at': '2026-11-18T00:01:00+00:00'})
    for model_index, model in enumerate(MODEL_IDS):
        for case_index, fixture in enumerate(fixtures):
            trial_name = f'model{model_index}-case{case_index}'
            make_attempt(root, trial_name, model, fixture, win=(model_index == 0), reviews=reviews)
    return manifest, reviews


def make_attempt(root, trial_name, model, fixture, win, reviews, started='2026-11-18T01:00:00Z'):
    trial = root / 'runs/harbor/official' / trial_name
    worker_id = trial_name + '__env'
    engine = root / 'runs/engine' / worker_id
    outcome = 'win' if win else 'loss'
    progress = 100 if win else 50
    rewards = {'win': int(win), 'score': 100 * int(win), 'progress': float(progress)}
    host = {'session_id': trial_name + '-session', 'trial_id': worker_id, 'team_id': fixture['team_id'],
        'seed': fixture['seed'], 'valid': True, 'terminal': True, 'outcome': outcome,
        'progress_score': progress, 'opponent_fainted': 6 if win else 0,
        'actions': 30, 'frames': 18000, 'model_wall_seconds': 600,
        'evidence': {'state_sha256': fixture['state_sha256'], 'terminal_state': {
            'opponent': [{'species': f'Pokemon-{i}', 'hp': 0 if win else 50, 'stats': {'hp': 100}} for i in range(6)]}}}
    write(trial / 'verifier/battle-result.json', host)
    video = engine / 'video/replay.mp4';video.parent.mkdir(parents=True, exist_ok=True);video.write_bytes(b'fixture replay')
    host['recording'] = {'status': 'complete', 'missing_video_frames': 0, 'replay': 'replay.mp4', 'replay_sha256': digest(video)}
    write(engine / 'bridge-result.json', host)
    write(engine / 'fixture.json', fixture)
    write(trial / 'verifier/session.json', {'session_id': host['session_id'], 'worker_trial_id': worker_id,
        'scenario': {'team_id': fixture['team_id'], 'seed': fixture['seed']}})
    write(trial / 'agent/runtime-audit.json', {'model_mismatch': False, 'budget_exhausted': False})
    write(trial / 'agent/delegation-audit.json', {'requires_manual_review': False})
    (trial / 'agent/events.jsonl').write_text('')
    write(trial / 'result.json', {'trial_name': trial_name, 'task_name': fixture['name'],
        'config': {'agent': {'model_name': model}}, 'started_at': started,
        'agent_result': {'cost_usd': .1 if model == MODEL_IDS[-1] else None},
        'verifier_result': {'rewards': rewards}, 'exception_info': None})
    reviews['trial_reviews'][trial_name] = {'reviewed': True, 'reviewer': 'test-fixture', 'protocol_compliant': True}
    return trial


def test_exact120_with_paired_cluster_intervals_and_no_development_mix(tmp_path):
    manifest, reviews = cohort(tmp_path)
    write(tmp_path / 'runs/harbor/pilot/irrelevant/result.json', {'this': 'must never be loaded'})
    result = analyze(tmp_path, manifest, draws=50, review_ledger=reviews, manifest_sha256=digest(tmp_path / 'manifest.json'))
    assert result['complete_valid_120_trial_report'], result['errors']
    assert result['model_summaries'][0]['wins'] == 30
    assert result['model_summaries'][0]['win_percentage'] == 100
    assert result['model_summaries'][1]['wins'] == 0
    assert len(result['closest_nonwins']) == 90
    assert result['uncertainty']['paired_differences'][0]['team_cluster_percentile_95'] == [100, 100]
    assert result['model_summaries'][-1]['cost_usd'] > 2.99


def test_native_helper_usage_requires_explicit_review_without_reopening_game(tmp_path):
    manifest,reviews=cohort(tmp_path)
    name='model2-case0';path=tmp_path/'runs/harbor/official'/name/'agent/runtime-audit.json'
    audit=json.loads(path.read_text());audit.update(auxiliary_model_review_required=True,auxiliary_models=['claude-haiku-5-5'])
    write(path,audit)
    result=analyze(tmp_path,manifest,draws=10,review_ledger=reviews,manifest_sha256=digest(tmp_path/'manifest.json'))
    row=next(r for r in result['failed_or_unreviewed_attempts'] if r['trial_name']==name)
    assert row['terminal_valid'] and any('auxiliary' in e for e in row['errors'])
    reviews['trial_reviews'][name].update(auxiliary_models_reviewed=True,approved_auxiliary_models=['claude-haiku-5-5'],auxiliary_model_review_note='Only documented built-in extraction; actor unchanged; no delegation.')
    result=analyze(tmp_path,manifest,draws=10,review_ledger=reviews,manifest_sha256=digest(tmp_path/'manifest.json'))
    assert result['complete_valid_120_trial_report']


def test_unreviewed_first_valid_loss_cannot_be_replaced_by_later_win(tmp_path):
    manifest, reviews = cohort(tmp_path)
    first = 'model1-case0'
    reviews['trial_reviews'].pop(first)
    make_attempt(tmp_path, 'later-win', MODEL_IDS[1], manifest['fixtures'][0], True, reviews,
                 started='2026-11-18T02:00:00Z')
    result = analyze(tmp_path, manifest, draws=20, review_ledger=reviews, manifest_sha256=digest(tmp_path / 'manifest.json'))
    assert not result['complete_valid_120_trial_report']
    assert result['duplicate_valid_cells'][0]['trials'] == [first, 'later-win']
    assert not any(r['trial_name'] == 'later-win' for r in result['accepted_first_valid'])
    assert result['model_summaries'][1]['valid_trials'] == 29
    assert result['model_summaries'][1]['win_percentage'] is None


def test_final_bridge_failure_or_changed_fixture_overrides_harbor_reward(tmp_path):
    manifest, reviews = cohort(tmp_path)
    host_path = tmp_path / 'runs/engine/model0-case0__env/bridge-result.json'
    host = json.loads(host_path.read_text());host['valid'] = False;host['outcome'] = 'invalid'
    write(host_path, host)
    (tmp_path / manifest['fixtures'][1]['state_file']).write_bytes(b'changed')
    result = analyze(tmp_path, manifest, draws=20, review_ledger=reviews, manifest_sha256=digest(tmp_path / 'manifest.json'))
    assert not result['complete_valid_120_trial_report']
    assert len(result['missing']) == 1
    assert any('fixture/task bytes changed' in error for error in result['errors'])
    assert result['model_summaries'][0]['wins'] == 29
    assert result['model_summaries'][0]['win_percentage'] is None
    assert result['uncertainty'] is None


def test_missing_video_cannot_turn_verified_loss_into_retry_eligibility(tmp_path):
    manifest, reviews = cohort(tmp_path)
    (tmp_path / 'runs/engine/model1-case0__env/video/replay.mp4').unlink()
    make_attempt(tmp_path, 'rerun-win', MODEL_IDS[1], manifest['fixtures'][0], True, reviews,
                 started='2026-11-18T02:00:00Z')
    reviews['infrastructure_retry_reviews']['model1-case0'] = {
        'infrastructure_failure': True, 'reviewer': 'test-fixture', 'reason': 'missing video'}
    result = analyze(tmp_path, manifest, draws=10, review_ledger=reviews, manifest_sha256=digest(tmp_path / 'manifest.json'))
    assert not result['complete_valid_120_trial_report']
    assert len(result['duplicate_valid_cells']) == 1
    assert not any(row['trial_name'] == 'rerun-win' for row in result['accepted_first_valid'])
    original = next(row for row in result['failed_or_unreviewed_attempts'] if row['trial_name'] == 'model1-case0')
    assert original['terminal_valid'] and original['outcome'] == 'loss'


def test_host_terminal_is_preserved_when_harbor_verifier_artifact_is_missing(tmp_path):
    manifest, reviews = cohort(tmp_path)
    (tmp_path / 'runs/harbor/official/model1-case0/verifier/battle-result.json').unlink()
    make_attempt(tmp_path, 'later-win', MODEL_IDS[1], manifest['fixtures'][0], True, reviews,
                 started='2026-11-18T02:00:00Z')
    result = analyze(tmp_path, manifest, draws=10, review_ledger=reviews, manifest_sha256=digest(tmp_path / 'manifest.json'))
    assert not result['complete_valid_120_trial_report']
    assert len(result['duplicate_valid_cells']) == 1
    original = next(r for r in result['failed_or_unreviewed_attempts'] if r['trial_name'] == 'model1-case0')
    assert original['terminal_valid'] and original['outcome'] == 'loss'


def test_prespecified99_cells_compare_four_models_only_on_common9(tmp_path):
    import shutil
    manifest, reviews = cohort(tmp_path)
    names = [case['name'] for case in manifest['fixtures']]
    common = names[:9]
    manifest['model_fixtures'] = {model: names if model != MODEL_IDS[-1] else common for model in MODEL_IDS}
    manifest['expected_trials'] = 99
    for index in range(9, 30):
        shutil.rmtree(tmp_path / 'runs/harbor/official' / f'model3-case{index}')
    result = analyze(tmp_path, manifest, draws=10, review_ledger=reviews, manifest_sha256=digest(tmp_path / 'manifest.json'))
    assert result['complete_valid_report'], result['errors']
    assert not result['complete_valid_120_trial_report']
    assert result['expected_trials'] == 99
    assert result['model_summaries'][-1]['expected_trials'] == 9
    assert result['model_summaries'][0]['expected_trials'] == 30
    assert len(result['common_fixtures']) == 9
    assert [(c['trials_per_model'], len(c['models'])) for c in result['matched_cohort_comparisons']] == [(30, 3), (9, 4)]
    assert result['uncertainty']['clusters'] == 3


def test_unplanned_gemini_cells_cannot_expand_declared_sample(tmp_path):
    manifest, reviews = cohort(tmp_path)
    names = [case['name'] for case in manifest['fixtures']]
    manifest['model_fixtures'] = {model: names if model != MODEL_IDS[-1] else names[:9] for model in MODEL_IDS}
    manifest['expected_trials'] = 99
    result = analyze(tmp_path, manifest, draws=10, review_ledger=reviews, manifest_sha256=digest(tmp_path / 'manifest.json'))
    assert not result['complete_valid_report']
    assert len(result['accepted_first_valid']) == 99
    assert any('unexpected/development' in error for error in result['errors'])


def test_current_native90_plan_never_requires_gemini(tmp_path):
    import shutil
    manifest, reviews = cohort(tmp_path)
    manifest['models'] = MODEL_IDS[:-1]
    manifest['expected_trials'] = 90
    for index in range(30):
        shutil.rmtree(tmp_path / 'runs/harbor/official' / f'model3-case{index}')
    result = analyze(tmp_path, manifest, draws=10, review_ledger=reviews, manifest_sha256=digest(tmp_path / 'manifest.json'))
    assert result['complete_valid_report'], result['errors']
    assert result['complete_valid_90_native_trial_report']
    assert not result['complete_valid_120_trial_report']
    assert len(result['model_summaries']) == 3 and len(result['accepted_first_valid']) == 90
    assert not result['missing']
    assert result['matched_cohort_comparisons'][0]['trials_per_model'] == 30


def test_unused_optional_retry_jobs_are_not_missing_required_jobs(tmp_path):
    manifest, reviews = cohort(tmp_path)
    manifest['optional_retry_jobs'] = [f'retry-{number:02d}' for number in range(1, 13)]
    result = analyze(tmp_path, manifest, draws=10, review_ledger=reviews, manifest_sha256=digest(tmp_path / 'manifest.json'))
    assert result['complete_valid_report'] and not result['used_retry_jobs']


def test_optional_retry_requires_preserved_invalid_original_and_prior_review(tmp_path):
    import shutil
    manifest, reviews = cohort(tmp_path)
    manifest['optional_retry_jobs'] = ['retry-01', 'retry-02']
    cell = {'model': MODEL_IDS[1], 'case': manifest['fixtures'][0]['name']}
    # A valid original loss cannot be reopened as an infrastructure retry.
    with pytest.raises(ValueError, match='terminal result'):
        validate_retry_launch(tmp_path, manifest, 'retry-01', [cell], reviews)
    for path in (tmp_path / 'runs/harbor/official/model1-case0/verifier/battle-result.json',
                 tmp_path / 'runs/engine/model1-case0__env/bridge-result.json'):
        host = json.loads(path.read_text());host.update(valid=False, terminal=False, outcome='invalid');write(path, host)
    with pytest.raises(ValueError, match='explicit infrastructure/non-game-stop review'):
        validate_retry_launch(tmp_path, manifest, 'retry-01', [cell], reviews)
    reviews['infrastructure_retry_reviews']['model1-case0'] = {
        'infrastructure_failure': True, 'reviewer': 'test-reviewer', 'reason': 'Fake engine crashed before terminal',
        'reviewed_at': '2026-11-18T01:30:00Z'}
    authorization = validate_retry_launch(tmp_path, manifest, 'retry-01', [cell], reviews)
    retry = make_attempt(tmp_path, 'authorized-rerun', MODEL_IDS[1], manifest['fixtures'][0], True, reviews,
                         started='2026-11-18T02:00:00Z')
    destination = tmp_path / 'runs/harbor/retry-01';destination.mkdir()
    shutil.move(str(retry), str(destination / retry.name))
    snapshot = json.loads((tmp_path / 'runs/harbor/official/protocol-snapshot.json').read_text())
    snapshot['retry_authorizations'] = authorization
    write(destination / 'protocol-snapshot.json', snapshot)
    result = analyze(tmp_path, manifest, draws=10, review_ledger=reviews, manifest_sha256=digest(tmp_path / 'manifest.json'))
    assert result['complete_valid_report'], result['errors']
    assert result['used_retry_jobs'] == ['retry-01']
    assert result['model_summaries'][1]['wins'] == 1
    with pytest.raises(ValueError, match='terminal result'):
        validate_retry_launch(tmp_path, manifest, 'retry-02', [cell], reviews)


def test_missing_runtime_audit_after_loss_blocks_retry_and_later_win(tmp_path):
    manifest, reviews = cohort(tmp_path)
    manifest['optional_retry_jobs'] = ['retry-01']
    (tmp_path / 'runs/harbor/official/model1-case0/agent/runtime-audit.json').unlink()
    reviews['infrastructure_retry_reviews']['model1-case0'] = {
        'infrastructure_failure': True, 'reviewer': 'test-reviewer', 'reason': 'Audit file missing',
        'reviewed_at': '2026-11-18T01:30:00Z'}
    cell = {'model': MODEL_IDS[1], 'case': manifest['fixtures'][0]['name']}
    with pytest.raises(ValueError, match='terminal result'):
        validate_retry_launch(tmp_path, manifest, 'retry-01', [cell], reviews)
    make_attempt(tmp_path, 'illicit-later-win', MODEL_IDS[1], manifest['fixtures'][0], True, reviews,
                 started='2026-11-18T02:00:00Z')
    result = analyze(tmp_path, manifest, draws=10, review_ledger=reviews, manifest_sha256=digest(tmp_path / 'manifest.json'))
    assert not result['complete_valid_report']
    assert result['duplicate_valid_cells'][0]['trials'] == ['model1-case0', 'illicit-later-win']
    assert not any(row['trial_name'] == 'illicit-later-win' for row in result['accepted_first_valid'])
    original = next(row for row in result['failed_or_unreviewed_attempts'] if row['trial_name'] == 'model1-case0')
    assert original['terminal_game_preserved'] and not original['terminal_valid']


def test_timeout_is_pending_and_can_receive_reviewed_fresh_replay(tmp_path):
    import shutil
    manifest, reviews = cohort(tmp_path)
    manifest['optional_retry_jobs'] = ['retry-01', 'retry-02']
    for path in (tmp_path / 'runs/harbor/official/model1-case0/verifier/battle-result.json',
                 tmp_path / 'runs/engine/model1-case0__env/bridge-result.json'):
        host = json.loads(path.read_text());host['outcome'] = 'timeout';write(path, host)
    reviews['infrastructure_retry_reviews']['model1-case0'] = {
        'non_game_stop': True, 'reviewer': 'test-reviewer', 'reason': 'Frame limit before a game ending',
        'reviewed_at': '2026-11-18T01:30:00Z'}
    result = analyze(tmp_path, manifest, draws=10, review_ledger=reviews,
                     manifest_sha256=digest(tmp_path / 'manifest.json'))
    assert not result['complete_valid_report']
    assert len(result['missing']) == 1
    assert result['model_summaries'][1]['valid_trials'] == 29
    authorization = validate_retry_launch(tmp_path, manifest, 'retry-01',
        [{'model': MODEL_IDS[1], 'case': manifest['fixtures'][0]['name']}], reviews)
    assert authorization[0]['review']['non_game_stop'] is True
    assert 'infrastructure_failure' not in authorization[0]['review']
    replay = make_attempt(tmp_path, 'reviewed-timeout-replay', MODEL_IDS[1], manifest['fixtures'][0], True, reviews,
                          started='2026-11-18T02:00:00Z')
    destination = tmp_path / 'runs/harbor/retry-01'
    destination.mkdir()
    shutil.move(str(replay), str(destination / replay.name))
    snapshot = json.loads((tmp_path / 'runs/harbor/official/protocol-snapshot.json').read_text())
    snapshot['retry_authorizations'] = authorization
    write(destination / 'protocol-snapshot.json', snapshot)
    complete = analyze(tmp_path, manifest, draws=10, review_ledger=reviews,
                       manifest_sha256=digest(tmp_path / 'manifest.json'))
    assert complete['complete_valid_report'], complete['errors']
    assert not complete['missing'] and not complete['duplicate_valid_cells']
    assert complete['used_retry_jobs'] == ['retry-01']
    assert complete['model_summaries'][1]['valid_trials'] == 30
    assert complete['model_summaries'][1]['wins'] == 1
    accepted = [row for row in complete['accepted_first_valid']
                if row['model'] == MODEL_IDS[1] and row['case'] == manifest['fixtures'][0]['name']]
    assert len(accepted) == 1 and accepted[0]['trial_name'] == 'reviewed-timeout-replay'
    original = next(row for row in complete['failed_or_unreviewed_attempts'] if row['trial_name'] == 'model1-case0')
    assert not original['terminal_valid'] and not original.get('terminal_game_preserved')
    with pytest.raises(ValueError, match='terminal result'):
        validate_retry_launch(tmp_path, manifest, 'retry-02',
            [{'model': MODEL_IDS[1], 'case': manifest['fixtures'][0]['name']}], reviews)


def test_genuine_loss_with_missing_hp_metadata_cannot_be_replayed(tmp_path):
    manifest, reviews = cohort(tmp_path)
    manifest['optional_retry_jobs'] = ['retry-01']
    for path in (tmp_path / 'runs/harbor/official/model1-case0/verifier/battle-result.json',
                 tmp_path / 'runs/engine/model1-case0__env/bridge-result.json'):
        host = json.loads(path.read_text())
        host['reason'] = 'game_terminal'
        host.pop('progress_score')
        host['evidence']['game_outcome_code'] = 2
        host['evidence']['terminal_state'] = {'terminal_confirmed':True,'outcome_code':2,'outcome':'loss'}
        write(path,host)
    reviews['infrastructure_retry_reviews']['model1-case0'] = {
        'infrastructure_failure':True,'reviewer':'test-reviewer','reason':'Missing HP metadata',
        'reviewed_at':'2026-11-18T01:30:00Z'}
    with pytest.raises(ValueError, match='terminal result'):
        validate_retry_launch(tmp_path, manifest, 'retry-01',
            [{'model':MODEL_IDS[1],'case':manifest['fixtures'][0]['name']}],reviews)
    incomplete = analyze(tmp_path, manifest, draws=10, review_ledger=reviews,
                         manifest_sha256=digest(tmp_path / 'manifest.json'))
    assert not incomplete['complete_valid_report']
    original = next(row for row in incomplete['failed_or_unreviewed_attempts'] if row['trial_name']=='model1-case0')
    assert original['terminal_game_preserved'] and not original['accepted']
