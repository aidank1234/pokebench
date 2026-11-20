"""Byte-level execution provenance; never includes credentials or asset contents."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import platform
import sys
from .agent_config import effective_agent_configs
from .native_runtime import calculation_python_identity
from .paths import runtime_paths

ROOT = Path(__file__).resolve().parents[2]


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def collect_hashes(root=ROOT):
    """One canonical source/task collector shared by freeze and execution."""
    root = Path(root)
    sources = [*sorted((root / 'src/cynthiabench').glob('*.py')),
               *[path for path in (runtime_paths(root).data / name for name in ('gen4-diamond.json', 'cynthia-rom.json')) if path.is_file()]]
    sources += [root / name for name in (
        'scripts/run_harbor.py', 'scripts/build_harbor_tasks.py', 'scripts/analyze_harbor.py',
        'teams/team-specs.json', 'requirements.lock.txt', 'pyproject.toml',
        'config/local.toml', 'docs/METHODOLOGY.md') if (root / name).is_file()]
    tasks = sorted(p for p in (root / 'tasks').rglob('*')
                   if p.is_file() and p.suffix in ('.json', '.toml', '.md', '.sh'))
    return {'source_sha256': {(str(p.relative_to(root)) if p.is_relative_to(root) else str(p)): sha256(p) for p in sources},
            'task_sha256': {(str(p.relative_to(root)) if p.is_relative_to(root) else str(p)): sha256(p) for p in tasks}}


def snapshot(freeze_manifest=None, agent_configs=None, requested_cells=None, concurrency=6):
    if agent_configs is None:
        selected_models = (json.loads(Path(freeze_manifest).read_text())['models'] if freeze_manifest
                           else None)
        defaults = effective_agent_configs(selected_models)
        selected_models = selected_models or list(defaults)
        agent_configs = {model: defaults[model] for model in selected_models}
    result = {
        'captured_at': datetime.now(timezone.utc).isoformat(),
        **collect_hashes(),
        'frozen_manifest_sha256': sha256(freeze_manifest) if freeze_manifest else None,
        'frozen_manifest_path': str(Path(freeze_manifest).resolve()) if freeze_manifest else None,
        'python': sys.version, 'platform': platform.platform(),
        'calculation_python': calculation_python_identity(),
        'agent_configs': agent_configs,
        'requested_cells': requested_cells,
        'execution': {'n_concurrent_trials': concurrency},
    }
    return result


def validate_freeze(result, path):
    """Official executions must match the immutable plan before any agent starts."""
    frozen = json.loads(Path(path).read_text())
    for category in ('source_sha256', 'task_sha256'):
        expected = frozen.get(category)
        if not isinstance(expected, dict) or not expected:
            raise ValueError(f'Frozen manifest lacks {category}')
        mismatches = [name for name, digest in expected.items()
                      if result[category].get(name) != digest]
        if mismatches:
            raise ValueError(f'Execution differs from frozen {category}: {mismatches}')
    settings = frozen.get('agent_configs')
    if not isinstance(settings, dict) or not settings:
        raise ValueError('Frozen manifest lacks explicit agent configurations')
    for model, config in result.get('agent_configs', {}).items():
        if settings.get(model) != config:
            raise ValueError('Execution differs from frozen agent configuration: ' + model)
    if result.get('execution') != frozen.get('execution'):
        raise ValueError('Execution concurrency differs from frozen settings')
    if result.get('requested_cells') is not None:
        mapping = frozen.get('model_fixtures') or {model: [f['name'] for f in frozen['fixtures']] for model in frozen['models']}
        for cell in result['requested_cells']:
            if cell['case'] not in mapping.get(cell['model'], []):
                raise ValueError('Requested trial is outside frozen model/fixture cells: ' + str(cell))
        if frozen.get('submission_order') is not None:
            selected = {(cell['model'], cell['case']) for cell in result['requested_cells']}
            expected_order = [cell for cell in frozen['submission_order'] if (cell['model'], cell['case']) in selected]
            if result['requested_cells'] != expected_order:
                raise ValueError('Requested launch order differs from frozen task/model submission order')
