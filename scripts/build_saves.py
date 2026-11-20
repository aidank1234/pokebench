#!/usr/bin/env python3
"""Build and audit all team save fixtures without changing the running emulator."""
from pathlib import Path
import argparse,hashlib,json,sys
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from cynthiabench.paths import runtime_paths
PATHS=runtime_paths()
from cynthiabench.save_editor import GameData,build_save

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--specs',type=Path,default=ROOT/'teams/team-specs.json')
    p.add_argument('--base',type=Path,default=PATHS.private/'base.sav')
    p.add_argument('--output',type=Path,default=PATHS.saves)
    p.add_argument('--team',help='Build just one team ID')
    args=p.parse_args();game=GameData();doc=json.loads(args.specs.read_text());base=args.base.read_bytes()
    args.output.mkdir(parents=True,exist_ok=True)
    teams=[t for t in doc['teams'] if args.team is None or t['id']==args.team]
    if not teams:raise SystemExit('No matching teams')
    manifest={'status':'binary validated; gameplay and balance require independent testing',
      'specs_sha256':hashlib.sha256(args.specs.read_bytes()).hexdigest(),
      'base_sha256':hashlib.sha256(base).hexdigest(),'rom_sha256':game.data['rom_sha256'],
      'teams':[]}
    for team in teams:
        data,audit=build_save(base,team,game)
        path=args.output/(team['id']+'.sav');path.write_bytes(data)
        (args.output/(team['id']+'.audit.json')).write_text(json.dumps(audit,indent=2)+'\n')
        manifest['teams'].append({'id':team['id'],'file':str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else str(path),'sha256':audit['sha256']})
        print(team['id'],audit['sha256'],', '.join(f"{x['name']} Lv{x['level']}" for x in audit['party']))
    (args.output/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
if __name__=='__main__':main()
