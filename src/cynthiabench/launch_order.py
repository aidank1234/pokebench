"""Deterministic seed-round ordering using public Harbor task list order."""
import json
from pathlib import Path

POLICY = 'replicate-then-stable-team-id; task-major-model-minor-v1'


def fixture_names(fixtures):
    return [case['name'] for case in sorted(fixtures,
        key=lambda case: (case['replicate'], case['team_id'], case['name']))]


def order_task_paths(root, paths):
    fixtures = json.loads((Path(root) / 'provenance/task-manifest.json').read_text())
    rank = {name: index for index, name in enumerate(fixture_names(fixtures))}
    if any(path.name not in rank for path in paths) or len({path.name for path in paths}) != len(paths):
        raise ValueError('Every selected task must be unique and present in the current fixture manifest')
    return sorted(paths, key=lambda path: rank[path.name])


def submission_order(fixtures, models, mapping):
    return [{'case': name, 'model': model} for name in fixture_names(fixtures)
            for model in models if name in mapping[model]]
