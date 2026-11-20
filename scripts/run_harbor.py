#!/usr/bin/env python3
"""Run real Harbor Jobs with native subscription agents and host battle grading."""
import argparse
import asyncio
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from harbor.job import Job
from harbor.models.job.config import JobConfig, RetryConfig
from harbor.models.trial.config import AgentConfig, EnvironmentConfig, TaskConfig, VerifierConfig
from cynthiabench.provenance import snapshot, validate_freeze
from cynthiabench.agent_config import ALL_MODELS, effective_agent_configs, harbor_agent_args
from cynthiabench.harbor_env import AdminClient
from cynthiabench.server_preflight import validate_status
from cynthiabench.framework_signals import install_framework_signals
from cynthiabench.launch_order import order_task_paths, POLICY as ORDER_POLICY
from cynthiabench.analysis import validate_retry_launch
from cynthiabench.budget_control import check_quota_halt
from cynthiabench.agent_config import QUOTA_HALT_PATH
from cynthiabench.quota_control import run_with_quota_halt, ProviderQuotaHalt

def agent_config(model, settings=None):
    settings = settings or effective_agent_configs([model])
    return AgentConfig(**harbor_agent_args(settings[ALL_MODELS[model]['model']]))


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--job', required=True)
    parser.add_argument('--models', nargs='+', choices=list(ALL_MODELS), default=['sol', 'astra', 'opus'],
                        help='Native systems: sol, astra, opus, neon, carbon or flash.')
    parser.add_argument('--tasks', nargs='*', help='Explicit task directory names; omitted means full 30-fixture evaluation')
    parser.add_argument('--concurrency', type=int, default=6)
    parser.add_argument('--admin-config', default=str(ROOT / 'work/admin.json'))
    parser.add_argument('--freeze-manifest', type=Path,
                        help='Immutable plan required for official runs and official infrastructure retries')
    parser.add_argument('--retry-reviews', type=Path, help='Required before a predeclared optional retry job launches')
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    if not args.dry_run:
        check_quota_halt(QUOTA_HALT_PATH)
    paths = [ROOT / 'tasks' / name for name in args.tasks] if args.tasks else sorted((ROOT / 'tasks').glob('*/task.toml'))
    paths = [p.parent if p.name == 'task.toml' else p for p in paths]
    if not paths:
        raise SystemExit('No task fixtures. Run scripts/build_harbor_tasks.py after saving ten team specs.')
    paths = order_task_paths(ROOT, paths)
    if not args.tasks and not args.dry_run:
        gates = ROOT / 'reports/release-gates.json'
        if not gates.exists() or json.loads(gates.read_text()).get('ready_for_scored_run') is not True:
            raise SystemExit('Full scored run is gated on reports/release-gates.json ready_for_scored_run=true.')
        if len(paths) != 30:
            raise SystemExit('Full fixture run requires 30 task fixtures.')
        if not args.freeze_manifest:
            raise SystemExit('Full scored run requires --freeze-manifest PATH.')
    settings = effective_agent_configs(args.models)
    selected_settings = {ALL_MODELS[model]['model']: settings[ALL_MODELS[model]['model']] for model in args.models}
    cells = [{'model': ALL_MODELS[model]['model'], 'case': path.name} for path in paths for model in args.models]
    provenance = snapshot(args.freeze_manifest, selected_settings, cells, args.concurrency)
    provenance.update(launch_order_policy=ORDER_POLICY, submission_order=cells,
                      actual_trial_start_log='trial-start-order.jsonl')
    if args.freeze_manifest:
        validate_freeze(provenance, args.freeze_manifest)
        frozen = json.loads(args.freeze_manifest.read_text())
        if args.job not in [*frozen['jobs'], *frozen.get('optional_retry_jobs', [])]:
            raise SystemExit('Official job name is not in the frozen plan.')
        if not args.dry_run and (ROOT / 'runs/harbor' / args.job).exists():
            raise SystemExit('Frozen job directory already exists; preserve it and use a predeclared new retry job with review.')
        if args.job in frozen.get('optional_retry_jobs', []) and not args.dry_run:
            if not args.retry_reviews:
                raise SystemExit('Optional retry jobs require --retry-reviews with explicit prior infrastructure approvals.')
            reviews = json.loads(args.retry_reviews.read_text())
            provenance['retry_authorizations'] = validate_retry_launch(ROOT, frozen, args.job, cells, reviews)
    if not args.dry_run:
        try:
            server_status = await asyncio.to_thread(AdminClient(args.admin_config).request, '/admin/status')
            provenance['engine_preflight'] = validate_status(ROOT, server_status, paths)
            if server_status.get('max_sessions', 0) < args.concurrency:
                raise ValueError('Host session capacity is below requested concurrency; restart with sufficient --max-sessions')
        except Exception as exc:
            raise SystemExit('Host preflight failed before any agent launched: ' + str(exc)) from None
    config = JobConfig(job_name=args.job, jobs_dir=ROOT / 'runs/harbor', n_attempts=1,
        n_concurrent_trials=args.concurrency, retry=RetryConfig(max_retries=0),
        environment=EnvironmentConfig(import_path='cynthiabench.harbor_env:CynthiaEnvironment',
                                     kwargs={'admin_config_path': args.admin_config,
                                             'quota_halt_path': str(QUOTA_HALT_PATH)}),
        verifier=VerifierConfig(import_path='cynthiabench.harbor_env:CynthiaVerifier'),
        agents=[agent_config(model, settings) for model in args.models],
        tasks=[TaskConfig(path=path) for path in paths])
    if args.dry_run:
        print(config.model_dump_json(indent=2))
        return
    job = await Job.create(config)
    install_framework_signals(job, ROOT / 'runs/harbor' / args.job / 'trial-start-order.jsonl')
    destination = ROOT / 'runs/harbor' / args.job / 'protocol-snapshot.json'
    destination.write_text(json.dumps(provenance, indent=2) + '\n')
    try:
        result = await run_with_quota_halt(job, QUOTA_HALT_PATH,
            ROOT / 'runs/harbor' / args.job / 'quota-controller-stop.json')
    except ProviderQuotaHalt as exc:
        raise SystemExit(str(exc)) from None
    print(result.model_dump_json(indent=2))


if __name__ == '__main__':
    asyncio.run(main())
