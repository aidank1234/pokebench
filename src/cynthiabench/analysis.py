"""Strict, manifest-selected reporting. Development trials are never auto-discovered."""
from __future__ import annotations
from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import itertools
import json
import math
from pathlib import Path
import random
import statistics

from .harbor_env import validate_battle_result, validate_terminal_identity
from .native_agent import MODELS
from .provenance import collect_hashes
from .agent_config import effective_agent_configs, harbor_agent_args, ALL_MODELS
from .launch_order import POLICY as ORDER_POLICY, fixture_names, submission_order

MODEL_IDS = [spec['model'] for spec in ALL_MODELS.values()]
NATIVE_MODEL_IDS = list(MODEL_IDS)
HASH_KEYS = ('state_sha256', 'save_sha256', 'rom_sha256', 'core_sha256')


def digest(path):
    value = hashlib.sha256()
    with Path(path).open('rb') as file:
        for block in iter(lambda: file.read(1024 * 1024), b''):
            value.update(block)
    return value.hexdigest()


def read_json(path):
    return json.loads(Path(path).read_text())


def planned_cells(manifest):
    models = manifest['models']
    cases = {case['name'] for case in manifest['fixtures']}
    mapping = manifest.get('model_fixtures')
    if mapping is None:
        mapping = {model: sorted(cases) for model in models}
    if not isinstance(mapping, dict):
        raise ValueError('model_fixtures must be a mapping from model IDs to case names')
    if set(mapping) != set(models):
        raise ValueError('model_fixtures must explicitly cover every planned model')
    for model, names in mapping.items():
        if not isinstance(names, list) or not names or len(set(names)) != len(names) or not set(names) <= cases:
            raise ValueError('Invalid or duplicate fixture cells for ' + model)
    return {model: list(mapping[model]) for model in models}


def freeze_manifest(root, jobs, model_fixtures=None, agent_configs=None, models=None,
                    concurrency=6, optional_retry_jobs=None):
    root = Path(root)
    models = list(NATIVE_MODEL_IDS if models is None else models)
    if not models or len(set(models)) != len(models) or not set(models) <= set(MODEL_IDS):
        raise ValueError('Provide unique supported model IDs for the scored plan')
    if not jobs or any(Path(name).name != name for name in jobs):
        raise ValueError('Provide explicit Harbor job directory names')
    if len(set(jobs)) != len(jobs):
        raise ValueError('Duplicate job name')
    optional_retry_jobs = list(optional_retry_jobs or [])
    all_jobs = [*jobs, *optional_retry_jobs]
    if len(set(all_jobs)) != len(all_jobs) or any(Path(name).name != name or name in ('.', '..') for name in all_jobs):
        raise ValueError('Required and optional retry job names must be unique simple directory names')
    if not isinstance(concurrency, int) or not 1 <= concurrency <= 6:
        raise ValueError('Validated concurrency must be an integer from one through six')
    cases = read_json(root / 'provenance/task-manifest.json')
    if len(cases) != 30 or len({c['team_id'] for c in cases}) != 10:
        raise ValueError('Scored manifest requires thirty cases across ten teams')
    if len({c['name'] for c in cases}) != 30 or any(
        len({c['seed'] for c in cases if c['team_id'] == team}) != 3
        for team in {c['team_id'] for c in cases}):
        raise ValueError('Require thirty unique case names and three distinct seeds per team')
    fixtures = []
    for case in cases:
        from .paths import runtime_paths
        fixture = read_json(runtime_paths(root).fixtures / (case['name'] + '.json'))
        record = {key: case[key] for key in ('name', 'team_id', 'seed', 'replicate')}
        record.update({key: fixture[key] for key in HASH_KEYS})
        state = root / fixture['state_file']
        if digest(state) != fixture['state_sha256']:
            raise ValueError('Fixture bytes disagree with manifest: ' + case['name'])
        record['state_file'] = str(state.relative_to(root)) if state.is_relative_to(root) else str(state)
        fixtures.append(record)
    agent_configs = agent_configs or effective_agent_configs(models)
    manifest = {'schema_version': 2, 'phase': 'scored', 'frozen_at': datetime.now(timezone.utc).isoformat(),
            'models': models, 'jobs': jobs, 'optional_retry_jobs': optional_retry_jobs, 'fixtures': fixtures,
            'execution': {'n_concurrent_trials': concurrency},
            'model_fixtures': model_fixtures if model_fixtures is not None else {model: [f['name'] for f in fixtures] for model in models},
            'agent_configs': {model: agent_configs[model] for model in models},
            **collect_hashes(root),
            'selection_rule': 'First valid terminal result per model/fixture is immutable. Duplicate valid results block finalization; never select the best attempt.',
            'uncertainty_scope': 'Sensitivity to each explicitly matched curated team subset, not population intelligence. Small cluster counts give limited precision.'}
    mapping = planned_cells(manifest)
    if set(manifest['agent_configs']) != set(models):
        raise ValueError('Frozen agent settings must cover every planned model ID')
    manifest['expected_trials'] = sum(map(len, mapping.values()))
    manifest['common_fixtures'] = sorted(set.intersection(*(set(names) for names in mapping.values())))
    manifest['launch_order_policy'] = ORDER_POLICY
    manifest['fixture_order'] = fixture_names(fixtures)
    manifest['submission_order'] = submission_order(fixtures, models, mapping)
    return manifest


def has_preserved_terminal_game(root, row, expected=None):
    """A missing transcript/model audit must not reopen an existing game."""
    root = Path(root)
    trial = root / row['harbor_result']
    paths = [trial.parent / 'verifier/battle-result.json']
    session_path = trial.parent / 'verifier/session.json'
    session = {}
    engine_fixture = None
    if session_path.exists():
        session = read_json(session_path)
        worker_id = session.get('worker_trial_id')
        if worker_id:
            if Path(worker_id).name != worker_id or '..' in worker_id:
                raise ValueError('Invalid preserved worker identity')
            paths += [root / 'runs/engine' / worker_id / name for name in ('result.json', 'bridge-result.json')]
            fixture_path = root / 'runs/engine' / worker_id / 'fixture.json'
            if fixture_path.exists():
                engine_fixture = read_json(fixture_path)
    for path in paths:
        if path.exists():
            evidence = read_json(path)
            if (evidence.get('valid') is not True or evidence.get('terminal') is not True
                    or evidence.get('outcome') not in ('win', 'loss')):
                continue
            if expected:
                if any(evidence.get(key) != expected[key] for key in ('team_id', 'seed')):
                    continue
                if evidence.get('evidence', {}).get('state_sha256') != expected['state_sha256']:
                    continue
                if engine_fixture is not None and any(engine_fixture.get(key) != expected[key] for key in HASH_KEYS):
                    continue
            session_id = session.get('session_id') or evidence.get('session_id')
            try:
                validate_terminal_identity({**evidence, 'session_id': evidence.get('session_id', session_id)}, session_id)
            except (ValueError, KeyError, TypeError):
                continue
            return True
    return False


def validate_retry_launch(root, manifest, job, cells, reviews):
    """Authorize specific reruns before launch, without changing the freeze."""
    root = Path(root)
    if job not in manifest.get('optional_retry_jobs', []):
        raise ValueError('Retry job is not predeclared in the frozen manifest')
    if (root / 'runs/harbor' / job).exists():
        raise ValueError('Refusing to reuse a retry job directory or overwrite its evidence')
    fixtures = {case['name']: case for case in manifest['fixtures']}
    prior = []
    for name in [*manifest['jobs'], *manifest.get('optional_retry_jobs', [])]:
        if name == job:
            continue
        for path in (root / 'runs/harbor' / name).glob('*/result.json'):
            row = extract_attempt(root, path, fixtures, manifest['models'], {})
            prior.append(row)
    authorizations = []
    now = datetime.now(timezone.utc)
    for cell in cells:
        candidates = sorted([row for row in prior if row['model'] == cell['model'] and row['case'] == cell['case']],
                            key=lambda row: (row.get('started_at') or '', row['trial_name']))
        if any(row['terminal_valid'] or has_preserved_terminal_game(root, row, fixtures[cell['case']]) for row in candidates):
            raise ValueError('Known terminal result cannot be replayed: ' + str(cell))
        if not candidates:
            raise ValueError('Retry has no preserved invalid original: ' + str(cell))
        original = candidates[-1]
        review = reviews.get('infrastructure_retry_reviews', {}).get(original['trial_name'], {})
        if (not (review.get('infrastructure_failure') is True or review.get('non_game_stop') is True)
                or not review.get('reviewer') or not review.get('reason')):
            raise ValueError('Retry lacks explicit infrastructure/non-game-stop review: ' + original['trial_name'])
        try:
            approved = datetime.fromisoformat(review['reviewed_at'].replace('Z', '+00:00'))
            if approved.tzinfo is None or approved > now:
                raise ValueError('Invalid approval timestamp')
        except (KeyError, ValueError, TypeError):
            raise ValueError('Retry review needs a real prior timezone-aware reviewed_at timestamp') from None
        authorizations.append({**cell, 'original_trial': original['trial_name'],
                               'original_harbor_result': original['harbor_result'], 'review': review})
    return authorizations


def percentile(values, fraction):
    values = sorted(values)
    pos = (len(values) - 1) * fraction
    lo = int(pos)
    return values[lo] + (values[min(lo + 1, len(values) - 1)] - values[lo]) * (pos - lo)


def cluster_bootstrap(rows, models, draws=10000, seed=20261118):
    """Resample teams, retaining all matched seeds/models inside each team."""
    teams = sorted({row['team_id'] for row in rows})
    cells = {(model, team): statistics.mean(r['win'] for r in rows if r['model'] == model and r['team_id'] == team)
             for model in models for team in teams}
    rng = random.Random(seed)
    samples = {model: [] for model in models}
    for _ in range(draws):
        selected = rng.choices(teams, k=len(teams))
        for model in models:
            samples[model].append(100 * statistics.mean(cells[model, team] for team in selected))
    intervals = {model: [percentile(samples[model], .025), percentile(samples[model], .975)] for model in models}
    pairs = []
    for left, right in itertools.combinations(models, 2):
        differences = [a - b for a, b in zip(samples[left], samples[right])]
        point = 100 * statistics.mean(cells[left, team] - cells[right, team] for team in teams)
        pairs.append({'left': left, 'right': right, 'win_rate_difference_percentage_points': point,
                      'team_cluster_percentile_95': [percentile(differences, .025), percentile(differences, .975)]})
    return {'draws': draws, 'seed': seed, 'clusters': len(teams), 'model_intervals': intervals,
            'paired_differences': pairs,
            'interpretation': 'Curated-team-mix sensitivity only; not a general population confidence guarantee or model intelligence estimate.'}


def extract_attempt(root, path, fixtures, expected_models, reviews):
    trial = read_json(path)
    name = trial.get('trial_name', path.parent.name)
    model = trial.get('config', {}).get('agent', {}).get('model_name')
    case = trial.get('task_name')
    row = {'trial_name': name, 'model': model, 'case': case, 'harbor_result': str(path.relative_to(root)),
           'started_at': trial.get('started_at'), 'finished_at': trial.get('finished_at'),
           'accepted': False, 'terminal_valid': False, 'errors': [], 'exception_info': trial.get('exception_info'),
           'cost_usd': (trial.get('agent_result') or {}).get('cost_usd')}
    if case not in fixtures or model not in expected_models:
        row['errors'].append('Unexpected task/model: possible development run included')
        return row
    expected = fixtures[case]
    row.update(team_id=expected['team_id'], seed=expected['seed'])
    try:
        session = read_json(path.parent / 'verifier/session.json')
        session_id = session['session_id']
        if session.get('scenario') != {'team_id': expected['team_id'], 'seed': expected['seed']}:
            # Additional scenario metadata is legal; identity fields must match.
            if any(session.get('scenario', {}).get(k) != expected[k] for k in ('team_id', 'seed')):
                raise ValueError('Harbor scenario identity mismatch')
        # Worker trial_id is the environment session name, captured independently
        # by the host result. It need not equal Harbor's displayed trial name.
        verifier_path = path.parent / 'verifier/battle-result.json'
        verifier = read_json(verifier_path) if verifier_path.exists() else None
        worker_id = session.get('worker_trial_id') or (verifier or {})['trial_id']
        if Path(worker_id).name != worker_id or '..' in worker_id:
            raise ValueError('Invalid worker identity')
        engine = root / 'runs/engine' / worker_id
        if verifier is None:
            original = engine / 'result.json'
            recovered = read_json(original if original.exists() else engine / 'bridge-result.json')
            verifier = {**recovered, 'session_id': recovered.get('session_id', session_id)}
        audit = read_json(path.parent / 'agent/runtime-audit.json')
        if audit.get('model_mismatch') or audit.get('delegation_violation'):
            raise ValueError('Model identity or delegation policy mismatch')
        if any(verifier.get(k) != expected[k] for k in ('team_id', 'seed')):
            raise ValueError('Host case identity mismatch')
        rewards = validate_battle_result(verifier, session_id)
        fixture = read_json(engine / 'fixture.json')
        if any(fixture.get(key) != expected[key] for key in HASH_KEYS):
            raise ValueError('Engine fixture byte hashes differ from frozen manifest')
        if verifier.get('evidence', {}).get('state_sha256') != expected['state_sha256']:
            raise ValueError('Terminal evidence fixture hash mismatch')
        terminal = verifier['evidence']['terminal_state']
        opponents = terminal['opponent']
        if len(opponents) != 6 or any(not 0 <= p['hp'] <= p['stats']['hp'] for p in opponents):
            raise ValueError('Invalid terminal opponent HP evidence')
        progress = 100 * sum(1 - p['hp'] / p['stats']['hp'] for p in opponents) / 6
        if not math.isclose(progress, verifier['progress_score'], abs_tol=1e-8):
            raise ValueError('Progress does not match terminal remaining HP')
        fainted = sum(p['hp'] == 0 for p in opponents)
        if fainted != verifier['opponent_fainted']:
            raise ValueError('Faint count does not match terminal HP')
        # The game is independently verified before post-game artifacts. Missing
        # video or a recorder failure blocks release, but cannot erase a loss
        # and authorize a fresh battle attempt in that cell.
        row.update(terminal_valid=True, win=rewards['win'], outcome=verifier['outcome'],
                   progress_score=progress, opponent_fainted=fainted)
        if audit.get('native_evidence_error') or audit.get('cli_pin_error'):
            raise ValueError('Genuine game preserved; native transcript/CLI evidence needs review without replay')
        if audit.get('development_only'):
            raise ValueError('Development-only harness result is excluded from scored reporting')
        if not verifier_path.exists():
            raise ValueError('Verified host game preserved; missing Harbor verifier artifacts must be recovered without replaying battle')
        if (trial.get('verifier_result') or {}).get('rewards') != rewards:
            raise ValueError('Harbor rewards disagree with authoritative host result')
        host = read_json(engine / 'bridge-result.json')
        validate_battle_result(host, session_id)
        for key in ('session_id', 'team_id', 'seed', 'trial_id', 'valid', 'terminal', 'outcome', 'progress_score'):
            if verifier.get(key) != host.get(key):
                raise ValueError('Host terminal result changed after verification: ' + key)
        recording = host.get('recording', {})
        if recording.get('status') != 'complete' or recording.get('missing_video_frames') != 0:
            raise ValueError('Missing or incomplete replay recording')
        video = engine / 'video' / recording['replay']
        if video.parent != engine / 'video' or digest(video) != recording.get('replay_sha256'):
            raise ValueError('Replay hash mismatch')
        delegation = read_json(path.parent / 'agent/delegation-audit.json')
        review = reviews.get(name, {})
        if not review.get('reviewed') or not review.get('reviewer') or review.get('protocol_compliant') is not True:
            raise ValueError('Transcript protocol review is missing or not accepted')
        if delegation.get('requires_manual_review') and not review.get('delegation_flags_resolved'):
            raise ValueError('Unresolved potential model-delegation flags')
        if audit.get('auxiliary_model_review_required'):
            if (review.get('auxiliary_models_reviewed') is not True
                    or sorted(review.get('approved_auxiliary_models', [])) != sorted(audit.get('auxiliary_models', []))
                    or not review.get('auxiliary_model_review_note')):
                raise ValueError('Native auxiliary model usage requires explicit transcript review')
        agent = trial.get('agent_result') or {}
        if not isinstance(host.get('actions'), int) or not isinstance(host.get('model_wall_seconds'), (int, float)):
            raise ValueError('Missing action/time accounting')
        max_token_events = 0
        row.update(accepted=True, win=rewards['win'], outcome=host['outcome'], progress_score=progress,
                   actor_models=audit.get('actor_models', []), auxiliary_models=audit.get('auxiliary_models', []),
                   per_model_usage=audit.get('per_model_usage', []),
                   opponent_fainted=fainted, opponents_remaining=[{'species': p['species'], 'hp': p['hp'],
                     'max_hp': p['stats']['hp']} for p in opponents if p['hp'] > 0],
                   actions=host.get('actions'), emulated_frames=host.get('frames'),
                   wall_seconds=host.get('model_wall_seconds'),
                   cost_usd=agent.get('cost_usd') if audit.get('request_usage_complete') is not False else None,
                   known_metered_cost_usd=audit.get('cost_usd') if model == 'deepseek-flash' else None,
                   request_usage_complete=audit.get('request_usage_complete'),
                   budget_exhausted=bool(audit.get('budget_exhausted')),
                   max_output_token_events=max_token_events,
                   input_tokens=agent.get('n_input_tokens'), output_tokens=agent.get('n_output_tokens'),
                   fixture_hashes={key: fixture[key] for key in HASH_KEYS}, replay=str(video.relative_to(root)),
                   replay_sha256=recording['replay_sha256'], host_result=str((engine / 'bridge-result.json').relative_to(root)),
                   host_result_sha256=digest(engine / 'bridge-result.json'),
                   harness=trial.get('agent_info'), review=review)
    except (KeyError, TypeError, ValueError, OSError, ZeroDivisionError) as exc:
        row['errors'].append(str(exc))
    return row


def analyze(root, manifest, draws=10000, review_ledger=None, manifest_sha256=None):
    root = Path(root)
    review_ledger = review_ledger or {}
    errors = []
    fixtures = {c['name']: c for c in manifest['fixtures']}
    models = manifest['models']
    try:
        mapping = planned_cells(manifest)
    except ValueError as exc:
        errors.append(str(exc))
        mapping = {model: [] for model in models}
    expected_trials = sum(map(len, mapping.values()))
    if manifest.get('expected_trials', expected_trials) != expected_trials:
        errors.append('Expected trial count disagrees with explicit model/fixture cells')
    if (manifest.get('phase') != 'scored' or not models or len(set(models)) != len(models)
            or not set(models) <= set(MODEL_IDS) or len(fixtures) != 30):
        errors.append('Final report requires explicitly frozen supported models and thirty fixtures')
    if len({c['team_id'] for c in fixtures.values()}) != 10:
        errors.append('Expected exactly ten teams')
    for name, expected in manifest.get('source_sha256', {}).items():
        if not (root / name).is_file() or digest(root / name) != expected:
            errors.append('Frozen protocol file changed: ' + name)
    for case in fixtures.values():
        for name, expected in {case['state_file']: case['state_sha256']}.items():
            if not (root / name).is_file() or digest(root / name) != expected:
                errors.append('Frozen fixture/task bytes changed: ' + name)
    for name, expected in manifest.get('task_sha256', {}).items():
        if not (root / name).is_file() or digest(root / name) != expected:
            errors.append('Frozen task bytes changed: ' + name)
    attempts = []
    optional_jobs = manifest.get('optional_retry_jobs', [])
    for job in [*manifest['jobs'], *optional_jobs]:
        if Path(job).name != job:
            errors.append('Invalid explicit job name');continue
        directory = root / 'runs/harbor' / job
        if job in optional_jobs and not directory.exists():
            continue
        snapshot = None
        try:
            snapshot = read_json(directory / 'protocol-snapshot.json')
            if not manifest_sha256 or snapshot.get('frozen_manifest_sha256') != manifest_sha256:
                errors.append('Job lacks matching frozen-manifest provenance: ' + job)
            for name, expected in manifest.get('source_sha256', {}).items():
                if snapshot.get('source_sha256', {}).get(name) != expected:
                    errors.append('Job source snapshot mismatch: ' + job + '/' + name)
            for name, expected in manifest.get('task_sha256', {}).items():
                if snapshot.get('task_sha256', {}).get(name) != expected:
                    errors.append('Job task snapshot mismatch: ' + job + '/' + name)
            for model, config in snapshot.get('agent_configs', {}).items():
                if manifest.get('agent_configs') and manifest['agent_configs'].get(model) != config:
                    errors.append('Job agent settings mismatch: ' + job + '/' + model)
            if manifest.get('execution') and snapshot.get('execution') != manifest['execution']:
                errors.append('Job concurrency differs from frozen execution settings: ' + job)
        except (OSError, ValueError) as exc:
            errors.append('Missing/invalid job protocol snapshot: ' + job)
        paths = sorted(directory.glob('*/result.json'))
        if not paths:
            errors.append('No completed trial records in selected job: ' + job)
        for path in paths:
            row = extract_attempt(root, path, fixtures, models, review_ledger.get('trial_reviews', {}))
            if row['case'] in fixtures and row['model'] in models:
                row['terminal_game_preserved'] = has_preserved_terminal_game(root, row, fixtures[row['case']])
            row.update(job=job, optional_retry_job=job in optional_jobs)
            if job in optional_jobs:
                auth = [item for item in (snapshot or {}).get('retry_authorizations', [])
                        if item.get('model') == row['model'] and item.get('case') == row['case']]
                row['retry_authorization'] = auth[0] if len(auth) == 1 else None
                if len(auth) != 1:
                    row['accepted'] = False
                    row['errors'].append('Optional retry has no unique pre-launch authorization')
            if row['case'] not in mapping.get(row['model'], []):
                row['accepted'] = False
                row['errors'].append('Trial is outside the prespecified model/fixture cells')
            execution = manifest.get('agent_configs', {}).get(row['model'])
            if execution:
                if not snapshot or snapshot.get('agent_configs', {}).get(row['model']) != execution:
                    row['accepted'] = False
                    row['errors'].append('Missing matching pre-run agent configuration snapshot')
                actual = read_json(path).get('config', {}).get('agent', {})
                if any(actual.get(key) != value for key, value in harbor_agent_args(execution).items()):
                    row['accepted'] = False
                    row['errors'].append('Trial agent configuration differs from frozen execution settings')
                cli_pin = execution.get('kwargs', {}).get('cli_pin')
                if cli_pin:
                    try:
                        runtime = read_json(path.parent / 'agent/runtime-audit.json')
                        if (runtime.get('cli_pin_before') != cli_pin or runtime.get('cli_pin_after') != cli_pin
                                or runtime.get('cli_pin_error')):
                            raise ValueError('Executable identity was not preserved')
                    except (OSError, ValueError):
                        row['accepted'] = False
                        row['errors'].append('Native CLI path/hash/version audit does not match the frozen executable')
            if row.get('started_at') and datetime.fromisoformat(row['started_at'].replace('Z', '+00:00')) < datetime.fromisoformat(manifest['frozen_at']):
                row['accepted'] = False;row['terminal_valid'] = False
                row['errors'].append('Trial predates scored protocol freeze')
            if snapshot and row.get('started_at'):
                captured = snapshot.get('captured_at')
                if not captured or datetime.fromisoformat(captured.replace('Z', '+00:00')) > datetime.fromisoformat(row['started_at'].replace('Z', '+00:00')):
                    row['accepted'] = False
                    row['errors'].append('Job provenance was not captured before the trial')
            attempts.append(row)
    groups = defaultdict(list)
    for row in attempts:
        groups[row['model'], row['case']].append(row)
    accepted, missing, duplicates = [], [], []
    for model, case in ((model, case) for model in models for case in mapping[model]):
        rows = sorted(groups[model, case], key=lambda r: (r.get('started_at') or '', r['trial_name']))
        for index, row in enumerate(rows):
            if not row.get('optional_retry_job'):
                continue
            auth = row.get('retry_authorization') or {}
            prior = rows[:index]
            original = next((old for old in prior if old['trial_name'] == auth.get('original_trial')), None)
            review = review_ledger.get('infrastructure_retry_reviews', {}).get(auth.get('original_trial'), {})
            allowed = (original is not None and not original['terminal_valid']
                       and not any(old['terminal_valid'] or old.get('terminal_game_preserved') for old in prior)
                       and review == auth.get('review')
                       and (review.get('infrastructure_failure') is True or review.get('non_game_stop') is True)
                       and bool(review.get('reason')) and bool(review.get('reviewer')))
            try:
                approved = datetime.fromisoformat(review['reviewed_at'].replace('Z', '+00:00'))
                started = datetime.fromisoformat(row['started_at'].replace('Z', '+00:00'))
                allowed = allowed and approved.tzinfo is not None and approved < started
            except (KeyError, ValueError, TypeError):
                allowed = False
            if not allowed:
                row['accepted'] = False
                row['errors'].append('Retry is not backed by a prior reviewed invalid original')
                errors.append('Unauthorized optional retry: ' + row['trial_name'])
        valid = [r for r in rows if r['terminal_valid'] or r.get('terminal_game_preserved')]
        if not valid or not valid[0]['accepted']:
            missing.append({'model': model, 'case': case})
        else:
            accepted.append(valid[0])
        if len(valid) > 1:
            duplicates.append({'model': model, 'case': case, 'trials': [r['trial_name'] for r in valid]})
        for row in rows:
            if not row['terminal_valid'] and not row.get('terminal_game_preserved') and valid:
                if rows.index(row) > rows.index(valid[0]):
                    errors.append('Attempt launched after an immutable terminal result: ' + row['trial_name'])
                review = review_ledger.get('infrastructure_retry_reviews', {}).get(row['trial_name'], {})
                if (not (review.get('infrastructure_failure') is True or review.get('non_game_stop') is True)
                        or not review.get('reason') or not review.get('reviewer')):
                    errors.append('Unreviewed failed attempt preceding/alongside scored result: ' + row['trial_name'])
    if duplicates:
        errors.append('Duplicate valid results exist; first valid is immutable and no best-attempt selection is permitted')
    if any(r['case'] not in mapping.get(r['model'], []) for r in attempts):
        errors.append('Selected jobs contain unexpected/development trials')
    summaries = []
    for model in models:
        rows = [r for r in accepted if r['model'] == model]
        expected_model_trials = len(mapping[model])
        known_costs = [r['cost_usd'] for r in rows if r['cost_usd'] is not None]
        summaries.append({'model': model, 'valid_trials': len(rows), 'expected_trials': expected_model_trials,
                          'wins': sum(r['win'] for r in rows),
                          'win_percentage': 100 * sum(r['win'] for r in rows) / expected_model_trials
                              if expected_model_trials and len(rows) == expected_model_trials else None,
                          'outcomes': dict(Counter(r['outcome'] for r in rows)),
                          'budget_exhaustions': sum(r['budget_exhausted'] for r in rows),
                          'max_output_token_events': sum(r['max_output_token_events'] for r in rows),
                          'cost_usd': sum(known_costs) if len(known_costs) == len(rows) and rows else None,
                          'all_selected_attempts_known_cost_usd': sum(r['cost_usd'] for r in attempts
                              if r['model'] == model and r.get('cost_usd') is not None),
                          'median_actions': statistics.median([r['actions'] for r in rows]) if rows else None,
                          'median_wall_seconds': statistics.median([r['wall_seconds'] for r in rows]) if rows else None})
    complete = len(accepted) == expected_trials and not missing and not errors and not duplicates
    common = set.intersection(*(set(mapping[model]) for model in models)) if models else set()
    common_rows = [row for row in accepted if row['case'] in common]
    cohorts = []
    if complete:
        # Compare every system on identical fixtures; also retain the larger
        # native cohort when its prespecified cells are identical across models.
        cohort_sets = {tuple(sorted(common))} if common else set()
        cohort_sets.update(tuple(sorted(names)) for names in mapping.values())
        for names in sorted(cohort_sets, key=lambda names: (-len(names), names)):
            cohort_models = [model for model in models if set(names) <= set(mapping[model])]
            if len(cohort_models) < 2:
                continue
            rows = [row for row in accepted if row['model'] in cohort_models and row['case'] in names]
            cohorts.append({'fixtures': list(names), 'models': cohort_models,
                'trials_per_model': len(names), 'teams': sorted({row['team_id'] for row in rows}),
                'model_summaries': [{'model': model, 'wins': sum(row['win'] for row in rows if row['model'] == model),
                    'trials': len(names), 'win_percentage': 100 * sum(row['win'] for row in rows if row['model'] == model) / len(names)}
                    for model in cohort_models],
                'uncertainty': cluster_bootstrap(rows, cohort_models, draws)})
    return {'complete_valid_report': complete, 'complete_valid_120_trial_report': complete and expected_trials == 120,
            'complete_valid_90_native_trial_report': complete and expected_trials == 90 and set(models) == {s['model'] for s in MODELS.values()},
            'expected_trials': expected_trials, 'model_fixtures': mapping, 'common_fixtures': sorted(common),
            'matched_cohort_comparisons': cohorts, 'manifest_frozen_at': manifest['frozen_at'],
            'selected_jobs': manifest['jobs'], 'optional_retry_jobs': optional_jobs,
            'used_retry_jobs': sorted({r['job'] for r in attempts if r.get('optional_retry_job')}),
            'errors': errors, 'missing': missing, 'duplicate_valid_cells': duplicates,
            'model_summaries': summaries, 'accepted_first_valid': accepted,
            'failed_or_unreviewed_attempts': [r for r in attempts if not r['accepted']],
            'closest_nonwins': sorted([r for r in accepted if not r['win']],
                                     key=lambda r: (-r['progress_score'], r['model'], r['case'])),
            'uncertainty': cluster_bootstrap(common_rows, models, draws) if complete and common else None,
            'interpretation': 'Win counts describe fixed curated fixtures. Only genuine game wins/losses complete primary cells; non-game stops remain pending. '
                'HP depletion measures the final opponent HP state. Native harnesses and resource controls differ.'}
