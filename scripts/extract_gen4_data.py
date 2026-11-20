#!/usr/bin/env python3
"""Extract battle-relevant data from the user supplied Diamond ROM.

English labels/charmap are supplied by pinned PKHeX/pret upstream source references;
all numeric species stats, experience curves, moves and item pockets come from
this exact ROM. Run from any directory; no emulator state is changed.
"""
from pathlib import Path
import hashlib, json, re, struct, subprocess, sys
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from cynthiabench.paths import runtime_paths
PATHS=runtime_paths()

def u32(b,p): return struct.unpack_from('<I',b,p)[0]
def rom_files(rom):
    fnt = rom[u32(rom,0x40):u32(rom,0x40)+u32(rom,0x44)]
    fat = rom[u32(rom,0x48):u32(rom,0x48)+u32(rom,0x4c)]
    def walk(index, prefix=''):
        cursor, file_id = struct.unpack_from('<IH',fnt,(index&0xfff)*8)
        while fnt[cursor]:
            size=fnt[cursor];cursor+=1
            name=fnt[cursor:cursor+(size&127)].decode();cursor+=size&127
            if size&128:
                child=struct.unpack_from('<H',fnt,cursor)[0];cursor+=2
                yield from walk(child,prefix+name+'/')
            else:
                a,b=struct.unpack_from('<II',fat,file_id*8)
                yield prefix+name, rom[a:b]
                file_id+=1
    return dict(walk(0xf000))

def narc_entries(b):
    assert b[:4] == b'NARC'
    chunks={};cursor=16
    while cursor<len(b):
        size=u32(b,cursor+4);chunks[b[cursor:cursor+4]]=b[cursor:cursor+size];cursor+=size
    fat=chunks[b'BTAF'];data=chunks[b'GMIF'][8:]
    count=struct.unpack_from('<H',fat,8)[0]
    return [data[a:z] for a,z in [struct.unpack_from('<II',fat,12+8*i) for i in range(count)]]

def build():
    rom=(PATHS.rom).read_bytes();files=rom_files(rom)
    pk=ROOT/'vendor/PKHeX/PKHeX.Core/Resources/text'
    labels={key:(pk/path).read_text(encoding='utf-8-sig').splitlines() for key,path in {
        'species':'other/en/text_Species_en.txt','moves':'other/en/text_Moves_en.txt',
        'abilities':'other/en/text_Abilities_en.txt','items':'items/text_Items_en.txt',
        'natures':'other/en/text_Natures_en.txt'}.items()}
    resources={
        'personal':'poketool/personal/personal.narc',
        'growth':'poketool/personal/growtbl.narc',
        'moves':'poketool/waza/waza_tbl.narc',
        'items':'itemtool/itemdata/item_data.narc',
    }
    arrays={k:narc_entries(files[v]) for k,v in resources.items()}
    species=[]
    for i,b in enumerate(arrays['personal'][:494]):
        species.append({'id':i,'name':labels['species'][i],
          'base_stats':dict(zip(['hp','atk','def','spe','spa','spd'],b[:6])),
          'types':list(b[6:8]),'gender_threshold':b[0x10],'friendship':b[0x12],
          'growth_group':b[0x13],'abilities':list(b[0x16:0x18])})
    moves=[]
    for i,b in enumerate(arrays['moves'][:468]):
        moves.append({'id':i,'name':labels['moves'][i],'effect':struct.unpack_from('<H',b)[0],
          'category':b[2],'power':b[3],'type':b[4],'accuracy':b[5],'pp':b[6],
          'effect_chance':b[7],'priority':struct.unpack_from('<b',b,10)[0]})
    mapping_source=(ROOT/'vendor/pokediamond/arm9/src/itemtool.c').read_text()
    mapping_body=mapping_source.split('static const u16 sItemIndexMappings[][4] = {',1)[1].split('};',1)[0]
    mapping=[int(m[0]) for m in re.findall(r'\{\s*(\d+),\s*(\d+),\s*(\d+),\s*(\d+)\s*\}',mapping_body)]
    assert len(mapping)==465
    items=[]
    for i,index in enumerate(mapping):
        b=arrays['items'][index]
        items.append({'id':i,'name':labels['items'][i],
          'resource_index':index,'valid':i==0 or index>0,
          'pocket':(struct.unpack_from('<H',b,8)[0]>>7)&15,
          'held_effect':b[2],'held_parameter':b[3]})
    for i in range(420,428):items[i]['name']=f'HM{i-419:02d}'
    charmap={}
    for line in (ROOT/'vendor/pokediamond/charmap.txt').read_text().splitlines():
        m=re.match(r'^([0-9A-F]{4})=(.)$',line)
        if m: charmap[m[2]]=int(m[1],16)
    data={'format_version':1,'rom_sha256':hashlib.sha256(rom).hexdigest(),
      'source_commits':{name:subprocess.check_output(['git','-C',str(ROOT/'vendor'/name),'rev-parse','HEAD'],text=True).strip() for name in ['PKHeX','pokediamond']},
      'resource_sha256':{k:hashlib.sha256(files[v]).hexdigest() for k,v in resources.items()},
      'species':species,'moves':moves,'items':items,'abilities':labels['abilities'][:124],
      'natures':labels['natures'],'growth': [list(struct.unpack('<'+str(len(b)//4)+'I',b)) for b in arrays['growth']],
      'charmap':charmap}
    PATHS.data.mkdir(parents=True,exist_ok=True)
    out=PATHS.data/'gen4-diamond.json';out.write_text(json.dumps(data,indent=2,ensure_ascii=False)+'\n')
    print(out);print({k:len(v) for k,v in arrays.items()})
    print('Hyper Potion',items[25]);print('Gengar',species[94]);print('Shadow Ball',moves[247])
if __name__=='__main__':build()
