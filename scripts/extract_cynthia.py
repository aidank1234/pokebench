#!/usr/bin/env python3
"""Extract Cynthia's actual trainer record from the supplied ROM (trainer267)."""
import hashlib,json,struct
from pathlib import Path
from extract_gen4_data import ROOT,PATHS,rom_files,narc_entries

def main():
    rom=(PATHS.rom).read_bytes();files=rom_files(rom)
    game=json.loads((PATHS.data/'gen4-diamond.json').read_text())
    paths={k:f'poketool/trainer/{k}.narc' for k in ['trdata','trpoke']}
    td=narc_entries(files[paths['trdata']])[267]
    tp=narc_entries(files[paths['trpoke']])[267]
    kind,cls,unknown,count=td[:4]
    if kind!=3 or count!=6 or len(tp)!=96:raise ValueError('Unexpected Cynthia trainer format')
    out={'trainer_id':267,'rom_sha256':hashlib.sha256(rom).hexdigest(),
      'trainer_type':kind,'trainer_class':cls,'unknown2':unknown,'party_count':count,
      'items':[game['items'][i]['name'] for i in struct.unpack_from('<4H',td,4)],
      'ai_flags':struct.unpack_from('<I',td,12)[0],
      'double_battle':struct.unpack_from('<I',td,16)[0],
      'trdata_sha256':hashlib.sha256(td).hexdigest(),'trpoke_sha256':hashlib.sha256(tp).hexdigest(),
      'party':[]}
    for slot in range(count):
        difficulty,level,species,item,*moves=struct.unpack_from('<8H',tp,slot*16)
        out['party'].append({'species':game['species'][species]['name'],'species_id':species,
          'level':level,'difficulty':difficulty,'derived_iv':difficulty*31//255,
          'held_item':game['items'][item]['name'] if item else None,'held_item_id':item,
          'moves':[game['moves'][m]['name'] for m in moves],'move_ids':moves})
    path=PATHS.data/'cynthia-rom.json';path.write_text(json.dumps(out,indent=2)+'\n');print(path)
if __name__=='__main__':main()
