#!/usr/bin/env python3
"""Freeze an explicit scored cohort, or analyze only jobs named in that manifest."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from cynthiabench.analysis import freeze_manifest, analyze, digest
from cynthiabench.agent_config import effective_agent_configs, ALL_MODELS


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--freeze', action='store_true')
    parser.add_argument('--jobs', nargs='+')
    parser.add_argument('--retry-jobs', nargs='*', default=[], help='Predeclared optional infrastructure-retry jobs; unused names are not missing data')
    parser.add_argument('--concurrency', type=int, default=6, help='Frozen Job concurrency; current validated setting is6')
    parser.add_argument('--models', nargs='+', choices=list(ALL_MODELS), default=['sol', 'astra', 'opus'],
                        help='Explicit model aliases: sol, astra, opus, neon, carbon, flash.')
    parser.add_argument('--reviews', type=Path, help='Separate transcript/retry review ledger; never edit the frozen manifest')
    parser.add_argument('--output', type=Path, default=ROOT / 'reports/scored-analysis.json')
    parser.add_argument('--bootstrap-draws', type=int, default=10000)
    parser.add_argument('--model-fixtures', type=Path, help='JSON object exact model ID -> prespecified fixture names; default selected models ×30')
    args = parser.parse_args()
    if args.freeze:
        if args.manifest.exists():
            raise SystemExit('Refusing to overwrite a frozen scored manifest')
        cells = json.loads(args.model_fixtures.read_text()) if args.model_fixtures else None
        settings = effective_agent_configs(args.models)
        manifest = freeze_manifest(ROOT, args.jobs, cells, settings, [ALL_MODELS[model]['model'] for model in args.models],
                                   args.concurrency, args.retry_jobs)
        args.manifest.parent.mkdir(parents=True, exist_ok=True)
        args.manifest.write_text(json.dumps(manifest, indent=2) + '\n')
        print(f"Frozen {manifest['expected_trials']} explicit model/fixture cells and execution settings; keep reviews in a separate ledger.")
        return
    manifest = json.loads(args.manifest.read_text())
    reviews = json.loads(args.reviews.read_text()) if args.reviews else {}
    report = analyze(ROOT, manifest, args.bootstrap_draws, reviews, digest(args.manifest))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + '\n')
    for row in report['model_summaries']:
        print(f"{row['model']}: {row['wins']} wins / {row['valid_trials']} accepted of {row['expected_trials']} required; budget stops {row['budget_exhaustions']}")
    print('Complete:', report['complete_valid_report'], 'Missing:', len(report['missing']), 'Errors:', len(report['errors']))
    if not report['complete_valid_report']:
        raise SystemExit(2)


if __name__ == '__main__':
    main()
