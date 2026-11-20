"""Build immutable first-decision checkpoints from edited saves and explicit RNGs.

Runs outside every tested agent. No trial may call this program or change a seed.
"""
from pathlib import Path
import argparse,hashlib,json,sys,time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from cynthiabench.emulator import Emulator
from cynthiabench.paths import runtime_paths
PATHS=runtime_paths()
from cynthiabench.controls import ui_kind
from cynthiabench.battle_memory import BattleMemory
from cynthiabench.rng import locate_lcrng,read_lcrng


def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def build(team,save_dir=None,output=None,task_manifest=None):
    source=Path(save_dir or PATHS.saves).resolve()/f'{team}.sav'
    work=ROOT/'work/fixture-build'/team
    e=Emulator(PATHS.core,
               PATHS.rom,source,work)
    e.step(480)
    for _ in range(4):e.press('A',after=300)
    e.press('START',after=180)
    for _ in range(50):
        e.press('A',after=120)
        if ui_kind(e.image())=='command':break
    else:raise RuntimeError(f'{team}: no first command menu')
    e.step(30)
    ram=e.ram();state=BattleMemory().observe(ram)
    if not state or not state['needs_input'] or state['menu']!='command':
        raise RuntimeError(f'{team}: did not reach validated first command')
    if any(p['hp']!=p['stats']['hp'] for p in state['player']+state['opponent']):
        raise RuntimeError(f'{team}: non-full starting HP')
    expected=json.loads(source.with_suffix('.audit.json').read_text())
    # A full decode-level audit makes accidental booting of the donor save visible.
    expected_party=expected['party'] if 'party' in expected else expected['decoded']['party']
    if [p['pid'] for p in state['player']]!=[p['pid'] for p in expected_party]:
        raise RuntimeError(f'{team}: loaded party differs from edited save')
    locator=locate_lcrng(ram)
    base=work/'first-command.state';e.save_state(base)
    e.image().save(work/'first-command.png')
    (work/'first-command.ram').write_bytes(ram)
    out=Path(output or PATHS.fixtures).resolve();out.mkdir(parents=True,exist_ok=True)
    tasks=json.loads(Path(task_manifest or ROOT/'provenance/task-manifest.json').read_text())
    results=[]
    for task in tasks:
        if task['team_id']!=team:continue
        e.load_state(base)
        e.initialize_u32(locator['state_address'],task['seed'])
        seeded=e.ram()
        if seeded[:locator['state_offset']]!=ram[:locator['state_offset']] or seeded[locator['state_offset']+4:]!=ram[locator['state_offset']+4:]:
            raise RuntimeError('Seeding changed unrelated main RAM')
        if read_lcrng(seeded)!=task['seed']:raise RuntimeError('Seed write failed')
        filename=out/(task['name']+'.state');e.save_state(filename)
        manifest={**task,'state_file':str(filename.relative_to(ROOT)) if filename.is_relative_to(ROOT) else str(filename),
          'source_save_file':str(source.relative_to(ROOT)) if source.is_relative_to(ROOT) else str(source),
          'state_sha256':sha(filename),'save_sha256':sha(source),
          'rom_sha256':sha(PATHS.rom),
          'core_sha256':sha(PATHS.core),
          'seed_method':'Set the source-verified global battle LCRNG at first command before any agent choice; the rest of main RAM is identical across a team’s replicas.',
          'rng_locator':locator,'bootstrap_frames':e.frame,'initial_state':state}
        filename.with_suffix('.json').write_text(json.dumps(manifest,indent=2)+'\n')
        results.append(manifest['state_file'])
    e.close()
    return results


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--team',required=True)
    parser.add_argument('--save-dir',type=Path)
    parser.add_argument('--output',type=Path)
    parser.add_argument('--task-manifest',type=Path)
    args=parser.parse_args()
    print(json.dumps({'built':build(args.team,args.save_dir,args.output,args.task_manifest)}),flush=True)
