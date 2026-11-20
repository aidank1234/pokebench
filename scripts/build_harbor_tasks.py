#!/usr/bin/env python3
"""Create official 30-case tasks, or explicitly marked development calibration tasks."""
import hashlib
import json
import argparse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INSTRUCTION = '''# Defeat Cynthia

Win the champion battle in original Pokemon Diamond with the supplied team and
bag. The environment starts at Cynthia. You have one continuous attempt. All
Pokemon have their default species names. Do not reset, edit saves or memory,
alter the starting loadout externally, use cheats, or seek private benchmark files.
Normal in-battle level-ups and move-learning choices are permitted.

Use all provided public information, your tools, online references, and calculations
to make strategic decisions. Do not deliberately invoke other AI services, another
model CLI, or additional agent instances. Built-in public web tools and their
documented internal extraction/background helpers are allowed; Agent/subagent
delegation and asking another model to play the battle remain prohibited.
You may choose any legal in-battle action available
under the given inventory and battle style. Read team.json and CONTROLS.md first.
Use the live screenshot and permitted observation data to follow the actual game.
Continue until a verified terminal outcome. A strategy explanation alone does not
complete the task. You have 3600 seconds of agent wall-clock time.

The primary outcome is binary: winning earns 100; loss, draw, or noncompletion earns 0.
The secondary progress score is reported separately and does not change the win
score. The verifier uses host-owned game evidence, never your final text.
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--specs', type=Path, default=ROOT / 'teams/team-specs.json')
    parser.add_argument('--output', type=Path, default=ROOT / 'tasks')
    parser.add_argument('--manifest', type=Path, default=ROOT / 'provenance/task-manifest.json')
    parser.add_argument('--development', action='store_true',
                        help='Allow a non-ten-team calibration set; these tasks are not the scored cohort')
    parser.add_argument('--replicates', type=int, choices=(1, 2, 3), default=3,
                        help='Replicates per team; official generation always requires three')
    parser.add_argument('--seed-reference',
                        help='Development only: substitute this ID for team_id in the existing seed derivation')
    args = parser.parse_args()
    if not args.development and args.replicates != 3:
        parser.error('--replicates other than 3 requires --development')
    if args.seed_reference is not None and not args.development:
        parser.error('--seed-reference requires --development')
    if args.seed_reference is not None and (not args.seed_reference or '/' in args.seed_reference or '..' in args.seed_reference):
        parser.error('--seed-reference must be a safe nonempty team ID')
    source = args.specs
    data = json.loads(source.read_text())
    teams = data['teams'] if isinstance(data, dict) else data
    if not teams:
        raise ValueError('At least one team is required')
    if not args.development and len(teams) != 10:
        raise ValueError('Exactly ten teams are required')
    team_ids = [team.get('id', team.get('team_id')) for team in teams]
    if any(not isinstance(team_id, str) or not team_id or '/' in team_id or '..' in team_id for team_id in team_ids):
        raise ValueError('Each team must have a safe id')
    if len(set(team_ids)) != len(team_ids):
        raise ValueError('Team IDs must be unique')
    manifest = []
    for team in teams:
        team_id = team.get('id', team.get('team_id'))
        seed_reference = args.seed_reference if args.seed_reference is not None else team_id
        for replicate in range(1, args.replicates + 1):
            seed = int.from_bytes(hashlib.sha256(f'CynthiaBench-v1:{seed_reference}:{replicate}'.encode()).digest()[:4], 'little')
            name = f'{team_id}-seed{replicate}'
            path = args.output / name
            for directory in ('environment', 'tests'):
                (path / directory).mkdir(parents=True, exist_ok=True)
            scenario = {'team_id': team_id, 'replicate': replicate, 'seed': seed,
                        'seed_scheme': 'sha256(CynthiaBench-v1:team_id:replicate) first4 little-endian'}
            if args.development:
                scenario.update(phase='development', seed_reference=seed_reference)
                if args.seed_reference is not None:
                    scenario['seed_scheme'] = 'sha256(CynthiaBench-v1:seed_reference:replicate) first4 little-endian'
            (path / 'environment/scenario.json').write_text(json.dumps(scenario, indent=2) + '\n')
            # Loadout facts are public. Designer strategy and difficulty hypotheses
            # remain private so this is a decision test, not a supplied walk-through.
            public_team = {key: team[key] for key in ('id', 'name', 'battle_style', 'bag') if key in team}
            public_team['party'] = [{key: value for key, value in pokemon.items()
                                    if key in ('species', 'level', 'nature', 'gender', 'ability', 'moves',
                                               'held_item', 'ivs', 'evs', 'friendship', 'nickname')}
                                   for pokemon in team['party']]
            (path / 'environment/team.json').write_text(json.dumps(public_team, indent=2) + '\n')
            (path / 'instruction.md').write_text(INSTRUCTION)
            (path / 'task.toml').write_text('''schema_version = "1.4"
[metadata]
benchmark = "PokeBench"
category = "game-strategy"
execution_os = "macOS"
[agent]
timeout_sec = 3600
network_mode = "public"
[verifier]
timeout_sec = 120
[environment]
build_timeout_sec = 180
network_mode = "public"
''')
            (path / 'tests/test.sh').write_text('#!/bin/sh\n# This benchmark uses the host-side custom verifier.\necho "Use cynthiabench.harbor_env:CynthiaVerifier; default verifier is unsupported" >&2\nexit 1\n')
            manifest.append({'name': name, 'path': str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else str(path), **scenario})
    args.manifest.parent.mkdir(parents=True, exist_ok=True)
    args.manifest.write_text(json.dumps(manifest, indent=2) + '\n')
    mode = 'development calibration' if args.development else 'official candidate'
    print(f'Created {len(manifest)} Harbor task fixtures ({len(teams)} teams × {args.replicates} seeds; {mode}).')


if __name__ == '__main__':
    main()
