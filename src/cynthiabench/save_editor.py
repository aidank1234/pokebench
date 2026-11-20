"""Strict, offline editor for CynthiaBench's raw Pokemon Diamond save fixtures.

Battle data is read from the supplied ROM via data/gen4-diamond.json. See
docs/HARBOR.md for source references, limits and validation evidence.
This creates synthetic battle fixtures, not provenance-legitimate encounter files.
"""
from __future__ import annotations

import binascii
import hashlib
import json
import re
import struct
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
from .paths import runtime_paths
GENERAL_SIZE = 0xC100
STORAGE_SIZE = 0x121E0
HALF_SIZE = 0x40000
SAVE_SIZE = 0x80000
PARTY_OFFSET = 0x98
PARTY_SIZE = 236
STAT_KEYS = ('hp', 'atk', 'def', 'spe', 'spa', 'spd')
# Canonical block -> physical block for each personality shuffle value.
BLOCK_ORDERS = (
    (0,1,2,3),(0,1,3,2),(0,2,1,3),(0,3,1,2),(0,2,3,1),(0,3,2,1),
    (1,0,2,3),(1,0,3,2),(2,0,1,3),(3,0,1,2),(2,0,3,1),(3,0,2,1),
    (1,2,0,3),(1,3,0,2),(2,1,0,3),(3,1,0,2),(2,3,0,1),(3,2,0,1),
    (1,2,3,0),(1,3,2,0),(2,1,3,0),(3,1,2,0),(2,3,1,0),(3,2,1,0),
)
# Pocket number in the game's item data: (absolute general-block offset, capacity).
POCKETS = {0:(0x624,165),1:(0xB40,40),2:(0xCE0,15),3:(0x980,100),
           4:(0xBE0,64),5:(0xB10,12),6:(0xD1C,30),7:(0x8B8,50)}
BAG_END = 0xD98  # Includes registered-item u32 after the 30 battle-item slots.


def u16(data, offset=0): return struct.unpack_from('<H',data,offset)[0]
def u32(data, offset=0): return struct.unpack_from('<I',data,offset)[0]
def put16(data,offset,value): struct.pack_into('<H',data,offset,value)
def put32(data,offset,value): struct.pack_into('<I',data,offset,value)


def normalize_name(name: str) -> str:
    return re.sub(r'[^a-z0-9]', '', name.lower().replace('♀','f').replace('♂','m'))


def newer(a: int, b: int) -> int:
    """PKHeX SAV4BlockDetection wrap/uninitialized-aware comparison."""
    if a == 0xffffffff and b != 0xfffffffe: return -1
    if b == 0xffffffff and a != 0xfffffffe: return 1
    return (a>b)-(a<b)


def active_offset(save: bytes, start: int=0, size: int=GENERAL_SIZE) -> int:
    if len(save) != SAVE_SIZE:
        raise ValueError(f'Expected raw 512 KiB save; got {len(save)} bytes')
    footer=start+size-20
    major=newer(u32(save,footer),u32(save,footer+HALF_SIZE))
    minor=newer(u32(save,footer+4),u32(save,footer+HALF_SIZE+4))
    return start+(HALF_SIZE if (major or minor)<0 else 0)


def checksum_valid(section: bytes) -> bool:
    return len(section)>=20 and binascii.crc_hqx(section[:-20],0xffff)==u16(section,len(section)-2)


def crypt_words(data: bytes, seed: int) -> bytes:
    if len(data)%2: raise ValueError('Cipher data must contain whole u16 words')
    result=bytearray(data)
    for p in range(0,len(result),2):
        seed=(seed*0x41C64E6D+0x6073)&0xffffffff
        put16(result,p,u16(result,p)^(seed>>16))
    return bytes(result)


def decrypt_pk4(raw: bytes, validate: bool=True) -> bytes:
    if len(raw) not in (136,PARTY_SIZE): raise ValueError('PK4 must be 136 or 236 bytes')
    if u16(raw,4)!=0: raise ValueError('On-disk PK4 has nonzero encryption/error flags')
    pid=u32(raw);checksum=u16(raw,6)
    shuffled=crypt_words(raw[8:136],checksum)
    order=BLOCK_ORDERS[((pid>>13)&31)%24]
    canonical=b''.join(shuffled[i*32:(i+1)*32] for i in order)
    data=raw[:8]+canonical+crypt_words(raw[136:],pid)
    if validate and sum(struct.unpack('<64H',canonical))&0xffff != checksum:
        raise ValueError('Pokemon checksum mismatch')
    return data


def encrypt_pk4(plain: bytes) -> bytes:
    if len(plain) not in (136,PARTY_SIZE): raise ValueError('PK4 must be 136 or 236 bytes')
    data=bytearray(plain)
    # On disk neither party nor box encryption lock is held, nor checksum-fail bit.
    put16(data,4,0)
    checksum=sum(struct.unpack('<64H',data[8:136]))&0xffff
    put16(data,6,checksum)
    order=BLOCK_ORDERS[((u32(data)>>13)&31)%24]
    shuffled=bytearray(128)
    for canonical,physical in enumerate(order):
        shuffled[physical*32:(physical+1)*32]=data[8+canonical*32:8+(canonical+1)*32]
    return bytes(data[:8])+crypt_words(shuffled,checksum)+crypt_words(data[136:],u32(data))


class GameData:
    def __init__(self,path: Path|str|None=None):
        self.data=json.loads(Path(path or runtime_paths().data/'gen4-diamond.json').read_text())
        self.lookup={key:{normalize_name(row['name']):row for row in self.data[key]}
                     for key in ('species','moves','items')}
        # Modern spelling changed while the numeric move identifier stayed fixed.
        self.lookup['moves']['faintattack']=self.lookup['moves']['feintattack']
        self.abilities={normalize_name(v):i for i,v in enumerate(self.data['abilities'])}
        self.natures={normalize_name(v):i for i,v in enumerate(self.data['natures'])}
        self.char_to_code=self.data['charmap']
        self.code_to_char={v:k for k,v in self.char_to_code.items()}

    def resolve(self,category: str,value: str|int) -> dict:
        try:
            row=self.data[category][value] if isinstance(value,int) else self.lookup[category][normalize_name(value)]
        except (KeyError,IndexError,TypeError):
            raise ValueError(f'Unknown Diamond {category}: {value!r}') from None
        if row['id']==0 or (category=='items' and not row['valid']):
            raise ValueError(f'Invalid or empty {category}: {value!r}')
        return row

    def encode_string(self,text: str,capacity: int) -> bytes:
        if len(text)>=capacity: raise ValueError(f'Name too long: {text!r}')
        result=bytearray(capacity*2)
        for i,c in enumerate(text):
            if c not in self.char_to_code: raise ValueError(f'Unsupported Gen4 glyph: {c!r}')
            put16(result,i*2,self.char_to_code[c])
        put16(result,len(text)*2,0xffff)
        return bytes(result)

    def decode_string(self,data: bytes) -> str:
        result=[]
        for p in range(0,len(data),2):
            c=u16(data,p)
            if c in (0,0xffff): break
            result.append(self.code_to_char.get(c,f'<{c:04x}>'))
        return ''.join(result)

    def stats(self,species: dict,level: int,ivs: dict,evs: dict,nature: int) -> dict:
        values={}
        for i,key in enumerate(STAT_KEYS):
            v=((species['base_stats'][key]*2+ivs[key]+evs[key]//4)*level)//100
            if key=='hp':v=1 if species['id']==292 else v+level+10
            else:
                v+=5
                if nature//5 != nature%5:
                    if i-1==nature//5:v=v*110//100
                    elif i-1==nature%5:v=v*90//100
            values[key]=v
        return values


def choose_pid(spec: dict,species: dict,ability_id: int,nature: int,otid: int,identity: str) -> int:
    start=int.from_bytes(hashlib.sha256(identity.encode()).digest()[:4],'little')
    second=species['abilities'][1] not in (0,species['abilities'][0]) and ability_id==species['abilities'][1]
    threshold=species['gender_threshold']
    requested=spec.get('gender')
    if requested not in (None,'male','female','genderless'):raise ValueError('Invalid gender')
    if requested=='female' and threshold in (0,255):raise ValueError('Species cannot be female')
    if requested=='male' and threshold in (254,255):raise ValueError('Species cannot be male')
    if requested=='genderless' and threshold!=255:raise ValueError('Species is not genderless')
    for n in range(100000):
        pid=(start+n)&0xffffffff
        gender=2 if threshold==255 else (1 if threshold==254 or (pid&255)<threshold else 0)
        if pid%25!=nature or (pid&1)!=int(second):continue
        if requested is not None and gender!={'male':0,'female':1,'genderless':2}[requested]:continue
        if ((otid&65535)^(otid>>16)^(pid&65535)^(pid>>16))<8:continue
        return pid
    raise ValueError('Could not construct matching PID')


def create_pokemon(spec: dict,trainer: bytes,identity: str,game: GameData) -> bytes:
    if spec.get('nickname') not in (None,''):raise ValueError('CynthiaBench forbids nicknames')
    species=game.resolve('species',spec['species'])
    level=spec['level']
    if type(level) is not int or not 1<=level<=100:raise ValueError('Level must be 1..100')
    if len(spec['moves'])!=4 or len(set(map(str,spec['moves'])))!=4:raise ValueError('Four distinct moves required')
    moves=[game.resolve('moves',m) for m in spec['moves']]
    try:
        ability=game.abilities[normalize_name(spec['ability'])]
        nature=game.natures[normalize_name(spec['nature'])]
    except KeyError as exc:raise ValueError(f'Unknown ability/nature {exc}') from None
    if ability==0 or ability not in species['abilities']:raise ValueError('Ability is not a Diamond species ability')
    ivs=spec['ivs'];evs=spec['evs']
    if set(ivs)!=set(STAT_KEYS) or set(evs)!=set(STAT_KEYS):raise ValueError('All six IV and EV values required')
    if any(type(v) is not int or not 0<=v<=31 for v in ivs.values()):raise ValueError('IV must be 0..31')
    if any(type(v) is not int or not 0<=v<=255 for v in evs.values()) or sum(evs.values())>510:raise ValueError('Invalid EV allocation')
    held=0 if spec.get('held_item') is None else game.resolve('items',spec['held_item'])['id']
    friendship=spec.get('friendship',255)
    if type(friendship) is not int or not 0<=friendship<=255:raise ValueError('Friendship must be 0..255')
    otid=u32(trainer,0x10)
    pid=choose_pid(spec,species,ability,nature,otid,identity)
    data=bytearray(PARTY_SIZE)
    put32(data,0,pid)
    put16(data,8,species['id']);put16(data,10,held);put32(data,12,otid)
    put32(data,16,game.data['growth'][species['growth_group']][level])
    data[0x14]=friendship;data[0x15]=ability;data[0x17]=2
    data[0x18:0x1e]=bytes(evs[k] for k in STAT_KEYS)
    struct.pack_into('<4H',data,0x28,*(m['id'] for m in moves))
    data[0x30:0x34]=bytes(m['pp'] for m in moves)
    # No PP Ups, eggs, nickname bit, status, mail, seals, ribbons or Pokerus.
    put32(data,0x38,sum(ivs[k]<<(i*5) for i,k in enumerate(STAT_KEYS)))
    threshold=species['gender_threshold']
    gender=2 if threshold==255 else (1 if threshold==254 or (pid&255)<threshold else 0)
    data[0x40]=gender<<1
    default_name=species['name'].upper().replace('’',"'")
    data[0x48:0x5e]=game.encode_string(default_name,11)
    data[0x5f]=10  # Diamond origin game.
    data[0x68:0x78]=trainer[:16]
    data[0x7b:0x7e]=bytes((26,11,18))
    # Synthetic fixture metadata, not a claim of a legal wild encounter.
    put16(data,0x80,0);data[0x83]=4
    data[0x84]=level|((trainer[0x18]&1)<<7)
    data[0x8c]=level
    stats=game.stats(species,level,ivs,evs,nature)
    put16(data,0x8e,stats['hp'])
    struct.pack_into('<6H',data,0x90,*(stats[k] for k in STAT_KEYS))
    return encrypt_pk4(data)


def _decode_canonical(data: bytes,game: GameData) -> dict:
    species_id=u16(data,8);item=u16(data,10);pid=u32(data)
    packed=u32(data,0x38)
    moves=list(struct.unpack_from('<4H',data,0x28))
    result={'species_id':species_id,'species':game.data['species'][species_id]['name'],
      'pid':pid,'nature':game.data['natures'][pid%25],
      'ability':game.data['abilities'][data[0x15]],'ability_id':data[0x15],
      'held_item':game.data['items'][item]['name'] if item else None,'held_item_id':item,
      'name':game.decode_string(data[0x48:0x5e]),'is_nicknamed':bool(packed>>31),
      'is_egg':bool((packed>>30)&1),'gender':('male','female','genderless')[(data[0x40]>>1)&3],
      'form':data[0x40]>>3,'moves':[game.data['moves'][i]['name'] for i in moves],
      'move_ids':moves,'pp':list(data[0x30:0x34]),'pp_ups':list(data[0x34:0x38]),
      'ivs':{k:(packed>>(5*i))&31 for i,k in enumerate(STAT_KEYS)},
      'evs':dict(zip(STAT_KEYS,data[0x18:0x1e])),
      'experience':u32(data,16),'friendship':data[0x14],
      'original_trainer':game.decode_string(data[0x68:0x78]),'ot_id':u32(data,12),
      'checksum_valid':True}
    if len(data)==PARTY_SIZE:
        result.update(level=data[0x8c],hp=u16(data,0x8e),
                      stats=dict(zip(STAT_KEYS,struct.unpack_from('<6H',data,0x90))),
                      status=u32(data,0x88))
    return result


def decode_pokemon(raw: bytes,game: GameData|None=None) -> dict:
    """Decode an encrypted, checksum-valid on-disk PK4; never guesses locks."""
    return _decode_canonical(decrypt_pk4(raw),game or GameData())


def decode_pokemon_memory(raw: bytes,game: GameData|None=None) -> dict:
    """Decode a live 236-byte Pokemon respecting the game's lock flags.

    AcquireMonLock decrypts both regions but leaves physical block order intact.
    AcquireBoxMonLock decrypts only the four box blocks. Their checksum can be
    stale while a lock is held: Release*Lock refreshes it before encryption.
    A partial/torn snapshot can still fail validation; callers should retry at a
    stable decision boundary and corroborate identity against the fixture.
    """
    if len(raw)!=PARTY_SIZE:raise ValueError('RAM Pokemon must be 236 bytes')
    game=game or GameData();flags=u16(raw,4)
    if flags&~3:raise ValueError('RAM Pokemon has checksum-fail or reserved flags')
    party_lock=bool(flags&1);box_lock=bool(flags&2)
    if party_lock and not box_lock:raise ValueError('Impossible/torn party-lock state')
    pid=u32(raw);check=u16(raw,6)
    physical=raw[8:136] if box_lock else crypt_words(raw[8:136],check)
    valid=(sum(struct.unpack('<64H',physical))&0xffff)==check
    if not valid and not box_lock:raise ValueError('RAM Pokemon checksum mismatch')
    order=BLOCK_ORDERS[((pid>>13)&31)%24]
    canonical=b''.join(physical[i*32:(i+1)*32] for i in order)
    tail=raw[136:] if party_lock else crypt_words(raw[136:],pid)
    plain=raw[:8]+canonical+tail
    species=u16(plain,8);held=u16(plain,10);ability=plain[0x15]
    if not 1<=species<=493 or held>=len(game.data['items']) or ability>=len(game.data['abilities']):
        raise ValueError('Invalid RAM Pokemon identity fields')
    if any(m>=len(game.data['moves']) for m in struct.unpack_from('<4H',plain,0x28)):
        raise ValueError('Invalid RAM move IDs')
    if ((plain[0x40]>>1)&3)==3:raise ValueError('Invalid RAM gender')
    if not 1<=plain[0x8c]<=100 or not 1<=u16(plain,0x90)<=714 or u16(plain,0x8e)>u16(plain,0x90):
        raise ValueError('Invalid RAM level/HP; snapshot may be transient')
    result=_decode_canonical(plain,game)
    result.update(memory_flags=flags,party_lock=party_lock,box_lock=box_lock,
                  checksum_valid=valid,checksum_pending=box_lock and not valid)
    return result


def decode_party_memory(raw: bytes,game: GameData|None=None) -> dict:
    """Decode the Diamond Party struct: maxCount, curCount, six Pokemon slots."""
    if len(raw)!=8+6*PARTY_SIZE:raise ValueError('RAM Party must be 1424 bytes')
    maximum,count=struct.unpack_from('<II',raw)
    if not 1<=maximum<=6 or not 0<=count<=maximum:raise ValueError('Invalid RAM party count')
    game=game or GameData()
    return {'capacity':maximum,'count':count,
      'party':[decode_pokemon_memory(raw[8+i*PARTY_SIZE:8+(i+1)*PARTY_SIZE],game) for i in range(count)]}


def decode_bag_memory(raw: bytes,game: GameData|None=None,strict: bool=True) -> dict:
    """Read the 0x774-byte live Bag; includes all eight pockets and registered item.

    This never mutates inventory. A quantity snapshot does not authorize an item
    action: the controller must still enforce the frozen scenario and consume
    the item via the normal game menu. strict=False is for the irregular donor.
    """
    if len(raw)!=0x774:raise ValueError('RAM Bag must be1908 bytes')
    game=game or GameData();items=[];quantities={};warnings=[]
    for pocket,(absolute,capacity) in POCKETS.items():
        for slot in range(capacity):
            item,qty=struct.unpack_from('<HH',raw,absolute-0x624+slot*4)
            if not item:
                if qty:raise ValueError('RAM bag has quantity for empty item')
                continue
            if item>=len(game.data['items']) or not 0<=qty<=999:raise ValueError('Invalid RAM bag item/quantity')
            row=game.data['items'][item];name=row['name']
            if row['pocket']!=pocket or name in quantities:
                message=f'Irregular RAM bag entry {name} in pocket{pocket}'
                if strict:raise ValueError(message)
                warnings.append(message)
            items.append({'id':item,'name':name,'quantity':qty,'pocket':pocket,'slot':slot})
            quantities[name]=quantities.get(name,0)+qty
    return {'items':items,'quantities':quantities,'registered_item_id':u32(raw,0x770),'warnings':warnings}


def inspect_save(raw: bytes,game: GameData|None=None,strict_bag: bool=True) -> dict:
    game=game or GameData()
    go=active_offset(raw);so=active_offset(raw,GENERAL_SIZE,STORAGE_SIZE)
    general=raw[go:go+GENERAL_SIZE];storage=raw[so:so+STORAGE_SIZE]
    if u32(general,GENERAL_SIZE-8)!=0x20060623:raise ValueError('Save is not Diamond/Pearl')
    if not checksum_valid(general) or not checksum_valid(storage):raise ValueError('Active general/storage save checksum invalid')
    count=u32(general,0x94)
    if not 0<=count<=6:raise ValueError('Invalid party size')
    bag={};bag_warnings=[]
    for pocket,(offset,capacity) in POCKETS.items():
        for i in range(capacity):
            item,qty=struct.unpack_from('<HH',general,offset+i*4)
            if item:
                if item>=len(game.data['items']):raise ValueError('Unknown bag item')
                name=game.data['items'][item]['name']
                if name in bag:
                    if strict_bag:raise ValueError('Duplicate bag item')
                    bag_warnings.append(f'Duplicate donor item {name} in pocket {pocket}')
                if game.data['items'][item]['pocket']!=pocket:
                    if strict_bag:raise ValueError('Item stored in wrong pocket')
                    bag_warnings.append(f'Donor item {name} stored in wrong pocket {pocket}')
                bag[name]=qty
            elif qty:raise ValueError('Quantity for empty bag slot')
    options=u16(general,0x60)
    return {'sha256':hashlib.sha256(raw).hexdigest(),'general_offset':go,'storage_offset':so,
      'general_checksum_valid':True,'storage_checksum_valid':True,
      'party':[decode_pokemon(general[PARTY_OFFSET+i*PARTY_SIZE:PARTY_OFFSET+(i+1)*PARTY_SIZE],game) for i in range(count)],
      'bag':bag,'bag_warnings':bag_warnings,'battle_style':'set' if options&64 else 'shift',
      'battle_animations':not bool(options&128),'text_speed':options&15,
      'map':u32(general,0x1238),'x':u32(general,0x1240),'z':u32(general,0x1244)}


def build_save(base: bytes,team: dict,game: GameData|None=None) -> tuple[bytes,dict]:
    game=game or GameData()
    before=inspect_save(base,game,strict_bag=False)
    if len(team['party'])!=6:raise ValueError('Benchmark teams must have six Pokemon')
    battle_style=team.get('battle_style','set')
    if battle_style not in ('set','shift'):raise ValueError('Battle style must be set or shift')
    offset=before['general_offset'];g=bytearray(base[offset:offset+GENERAL_SIZE])
    trainer=bytes(g[0x64:0x84])
    put32(g,0x90,6);put32(g,0x94,6)
    for i,spec in enumerate(team['party']):
        pk=create_pokemon(spec,trainer,f"{team['id']}/{i}/{spec['species']}",game)
        g[PARTY_OFFSET+i*PARTY_SIZE:PARTY_OFFSET+(i+1)*PARTY_SIZE]=pk
    # Remove every donor item, including reserved battle slots and registered item.
    g[0x624:BAG_END]=bytes(BAG_END-0x624)
    pockets={k:[] for k in POCKETS}
    for name,count in team['bag'].items():
        if type(count) is not int or not 1<=count<=999:raise ValueError(f'Invalid quantity for {name}')
        item=game.resolve('items',name);pocket=item['pocket']
        if pocket==7 or (pocket==3 and count>99):raise ValueError('Unsupported benchmark bag contents')
        if item['id'] in [x[0] for x in pockets[pocket]]:raise ValueError('Duplicate bag item alias')
        pockets[pocket].append((item['id'],count))
    for pocket,rows in pockets.items():
        start,capacity=POCKETS[pocket]
        if len(rows)>capacity:raise ValueError('Pocket overflow')
        for i,(item,count) in enumerate(sorted(rows)):struct.pack_into('<HH',g,start+i*4,item,count)
    # Fast text (2), explicit battle style, battle animations on (0).
    # Original Diamond Options_Init sets battleStyle=0 (Shift).
    options=(u16(g,0x60)&~0xcf)|2|(0x40 if battle_style=='set' else 0)
    put16(g,0x60,options)
    put16(g,GENERAL_SIZE-2,binascii.crc_hqx(g[:-20],0xffff))
    output=bytearray(base);output[offset:offset+GENERAL_SIZE]=g
    result=inspect_save(bytes(output),game)
    if (result['map'],result['x'],result['z'])!=(185,8,18):raise ValueError('Base is not the validated Cynthia-room start')
    # Only authorized party, bag, options and CRC ranges may have changed.
    allowed=((0x60,0x62),(0x90,0x620),(0x624,BAG_END),(GENERAL_SIZE-2,GENERAL_SIZE))
    for i,(old,new) in enumerate(zip(base,output)):
        if old!=new and not any(offset+a<=i<offset+b for a,b in allowed):
            raise AssertionError(f'Unexpected save modification at {i:#x}')
    result['team_id']=team['id'];result['source_save_sha256']=before['sha256']
    result['gameplay_validated']=False
    return bytes(output),result
