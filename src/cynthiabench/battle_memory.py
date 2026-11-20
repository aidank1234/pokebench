"""Host-only, fail-closed observation of original Diamond's live battle memory.

Offsets are from pret/pokediamond overlay 11 and verified on the supplied Europe
Rev 5 game. This module only reads bytes. It must not be offered as an agent tool:
opponent HP/stats and unobserved party information are evaluator information.

BattleSetup.party is a stale pre-battle copy until cleanup. BattleSystem parties
are live; the BattleContext active monster contains the latest HP while a damage
animation/controller synchronization is in flight. See docs/HARBOR.md.
"""
from __future__ import annotations

from copy import deepcopy
from functools import lru_cache
import json
from pathlib import Path
import struct
from typing import Any

from .save_editor import GameData, decode_party_memory, u16, u32

ROOT = Path(__file__).resolve().parents[2]
from .paths import runtime_paths
RAM_BASE = 0x02000000
RAM_SIZE = 0x400000
SYSTEM_SIZE = 0x2444
CONTEXT_SIZE = 0x314C
PARTY_BYTES = 8 + 236 * 6
OUTCOMES = {0: 'in_progress', 1: 'win', 2: 'loss', 3: 'draw',
            4: 'caught', 5: 'player_fled', 6: 'opponent_fled'}
EXPECTED_STATS = [
    {'hp':150,'atk':135,'def':155,'spa':121,'spd':155,'spe':72},
    {'hp':160,'atk':117,'def':89,'spa':173,'spd':134,'spe':131},
    {'hp':221,'atk':109,'def':104,'spa':133,'spd':133,'spe':69},
    {'hp':180,'atk':145,'def':112,'spa':168,'spd':112,'spe':150},
    {'hp':211,'atk':99,'def':123,'spa':149,'spd':199,'spe':112},
    {'hp':238,'atk':176,'def':150,'spa':130,'spd':137,'spe':174},
]
EXPECTED_PIDS = [0xFECC78,0x357978,0x399E78,0x80DE78,0x77D78,0x80DE78]


class MemoryValidationError(ValueError):
    """Untrusted/ambiguous/transient memory; never convert this into a loss/win."""


@lru_cache(maxsize=1)
def _game():
    return GameData()


@lru_cache(maxsize=1)
def _opponent():
    return json.loads((runtime_paths().data/'cynthia-rom.json').read_text())['party']


def _offset(ram: bytes, address: int, size: int) -> int:
    off = address - RAM_BASE
    if address % 4 or off < 0 or off + size > len(ram):
        raise MemoryValidationError(f'Invalid main-RAM pointer 0x{address:08x}, size {size}')
    return off


def _party(ram: bytes, address: int) -> list[dict]:
    off = _offset(ram,address,PARTY_BYTES)
    try:
        d = decode_party_memory(ram[off:off+PARTY_BYTES],_game())
    except (ValueError,IndexError,KeyError,struct.error) as exc:
        raise MemoryValidationError(f'Invalid party at 0x{address:08x}: {exc}') from exc
    if d['capacity'] != 6 or d['count'] != 6:
        raise MemoryValidationError('The benchmark requires complete six-member parties')
    for p in d['party']:
        if not p['checksum_valid'] or p.get('checksum_pending'):
            raise MemoryValidationError('Pokemon checksum is unverified; retry after engine synchronization')
        if p['is_egg'] or not 1 <= p['species_id'] <= 493 or not 1 <= p['level'] <= 100:
            raise MemoryValidationError('Invalid battle Pokemon identity')
        if not 0 <= p['hp'] <= p['stats']['hp'] <= 714:
            raise MemoryValidationError('Impossible party HP')
    return d['party']


def _check_enemy(party: list[dict]):
    for p,expected,stats,pid in zip(party,_opponent(),EXPECTED_STATS,EXPECTED_PIDS):
        if (p['species_id'] != expected['species_id'] or p['level'] != expected['level']
                or p['move_ids'] != expected['move_ids'] or p['stats'] != stats
                or p['pid'] != pid or any(v != 30 for v in p['ivs'].values())
                or any(p['evs'].values())):
            raise MemoryValidationError('Live enemy party does not match original trainer267')
        if p['held_item_id'] not in (0,expected['held_item_id']):
            raise MemoryValidationError('Unexpected enemy held item')


def _public_volatile_conditions(ram: bytes, mon_offset: int) -> dict[str,bool]:
    """Publicly announced effects, with every hidden turn counter discarded.

    These masks are traced through original Diamond's move-effect scripts,
    side-effect lookup table and BattleMon getter/handlers. See
    docs/HARBOR.md. The raw words stay host-private.
    """
    volatile=u32(ram,mon_offset+0x70)
    effects=u32(ram,mon_offset+0x80)
    counters=u32(ram,mon_offset+0x88)
    return {'confusion':bool(volatile&0x7),
            'taunt':bool(counters&0x700),
            'leech_seed':bool(effects&0x4),
            'aqua_ring':bool(effects&0x1000000),
            'drowsy':bool(effects&0x1800),
            'recharging':bool(volatile&0x400000),
            'encore':bool(counters&0x38),
            'disable':bool(counters&0x7)}


def _candidates(ram: bytes):
    """Search structural identity, never a hardcoded heap address or HP value."""
    needle = struct.pack('<H',267)
    at = 0
    while True:
        hit = ram.find(needle,at)
        if hit < 0:
            return
        at = hit + 1
        off = hit - 0xA2
        if off < 0 or off % 4 or off + SYSTEM_SIZE > len(ram):
            continue
        if (u32(ram,off+0x2C) == 1 and u32(ram,off+0x44) == 2
                and list(struct.unpack_from('<4H',ram,off+0xA0)) == [0,267,0,0]):
            yield RAM_BASE + off


def _find_setup(ram: bytes,player: list[dict],opponent: list[dict]) -> int:
    hits=[];needle=struct.pack('<4I',0,267,0,0);at=0
    while True:
        hit=ram.find(needle,at)
        if hit<0:break
        at=hit+1;off=hit-0x18
        if off<0 or off%4 or off+0x190>len(ram) or u32(ram,off)!=1:continue
        try:
            p=_party(ram,u32(ram,off+4));e=_party(ram,u32(ram,off+8))
            if [m['pid'] for m in p]!=[m['pid'] for m in player]:continue
            if [m['pid'] for m in e]!=[m['pid'] for m in opponent]:continue
            _check_enemy(e)
            hits.append(RAM_BASE+off)
        except MemoryValidationError:continue
    if len(hits)!=1:
        raise MemoryValidationError(f'Expected exactly one matching BattleSetup, found {len(hits)}')
    return hits[0]


def _read_system(ram: bytes,address: int,setup_address: int|None=None) -> dict[str,Any]:
    bs=_offset(ram,address,SYSTEM_SIZE)
    if (u32(ram,bs+0x2C)!=1 or u32(ram,bs+0x44)!=2
            or list(struct.unpack_from('<4H',ram,bs+0xA0))!=[0,267,0,0]):
        raise MemoryValidationError('Bound battle structure no longer has Cynthia battle identity')
    context_address=u32(ram,bs+0x30)
    ctx=_offset(ram,context_address,CONTEXT_SIZE)
    player_address=u32(ram,bs+0x68);enemy_address=u32(ram,bs+0x6C)
    if len({address,context_address,player_address,enemy_address})!=4:
        raise MemoryValidationError('Aliased battle structures')
    player=_party(ram,player_address);opponent=_party(ram,enemy_address)
    _check_enemy(opponent)
    if setup_address is None:
        setup_address=_find_setup(ram,player,opponent)
    else:
        setup=_offset(ram,setup_address,0x190)
        if u32(ram,setup)!=1 or list(struct.unpack_from('<4I',ram,setup+0x18))!=[0,267,0,0]:
            raise MemoryValidationError('Bound BattleSetup identity was lost')
    phase=u32(ram,ctx+8)
    if phase>43:
        raise MemoryValidationError(f'Invalid BattleContext dispatch phase {phase}')
    # UI party slots are reordered when switching: display position -> original
    # party index. ov11_0224B290 swaps the incoming member with display position0;
    # both the bag and party emitters copy this map into their UI commands.
    player_party_order=list(ram[ctx+0x312C:ctx+0x3132])
    if sorted(player_party_order)!=list(range(6)):
        raise MemoryValidationError('Invalid player party display-order permutation')
    # These conditions are announced by the game. Decode only presence, not
    # duration bits (notably Trick Room packs its remaining turns into 0x70000).
    field_flags=u32(ram,ctx+0x180)
    weathers=[name for name,mask in [('rain',0x3),('sandstorm',0xC),
                                    ('sun',0x30),('hail',0xC0)] if field_flags&mask]
    if len(weathers)>1:
        raise MemoryValidationError('Conflicting active weather flags')
    public_field_conditions={'weather':weathers[0] if weathers else None,
                             'trick_room':bool(field_flags&0x70000)}
    for side,name in enumerate(('player','opponent')):
        flags=u32(ram,ctx+0x1BC+4*side)
        layers=u32(ram,ctx+0x1C8+8*side)
        toxic_layers=(layers>>2)&3
        if flags&0x400 and toxic_layers>2:
            raise MemoryValidationError('Invalid active Toxic Spikes layer count')
        public_field_conditions[name]={'reflect':bool(flags&1),'light_screen':bool(flags&2),
            'stealth_rock':bool(flags&0x80),'spikes_layers':layers&3 if flags&4 else 0,
            'toxic_spikes_layers':toxic_layers if flags&0x400 else 0}
    active_indexes=list(ram[ctx+0x219C:ctx+0x219E])
    active=[]
    for side,party in enumerate((player,opponent)):
        slot=active_indexes[side]
        if slot>=6:
            raise MemoryValidationError('Invalid active party slot')
        off=ctx+0x2D40+0xC0*side
        species=u16(ram,off);hp=u32(ram,off+0x4C);max_hp=u32(ram,off+0x50)
        pid=u32(ram,off+0x68);level=ram[off+0x34]
        mon=party[slot]
        if (species != mon['species_id'] or pid != mon['pid']
                or max_hp != mon['stats']['hp'] or not 0<=hp<=max_hp):
            raise MemoryValidationError('Active battle monster and party identity/HP disagree; retry after transition')
        status=u32(ram,off+0x6C)
        stages=list(ram[off+0x18:off+0x20])
        if any(v>12 for v in stages):
            raise MemoryValidationError('Invalid stat stage')
        # Active HP is authoritative during damage animation before the party sync.
        mon['party_hp']=mon['hp'];mon['hp']=hp
        mon['party_status']=mon['status'];mon['status']=status
        active.append({'side':side,'party_index':slot,'species_id':species,
                       'species':mon['species'],'hp':hp,'max_hp':max_hp,'level':level,
                       'status':status,'stat_stages_raw':stages,
                       'public_stat_stages':dict(zip(
                           ('attack','defense','speed','special_attack','special_defense','accuracy','evasion'),
                           (v-6 for v in stages[1:]))),
                       'volatile_status':u32(ram,off+0x70),
                       'public_volatile_conditions':_public_volatile_conditions(ram,off),
                       # Public restriction, never expose the hidden turns left.
                       # Bag emitter ov11_0225573C checks these same three bits.
                       'embargoed':bool((u32(ram,off+0x8C)>>19)&7),
                       'move_ids':list(struct.unpack_from('<4H',ram,off+0x0C)),
                       'pp':list(ram[off+0x2C:off+0x30]),
                       'held_item_id':u16(ram,off+0x78),'ability_id':ram[off+0x27]})
    raw_outcome=ram[bs+0x241C];code=raw_outcome&0x3F
    if code not in OUTCOMES:
        raise MemoryValidationError(f'Unknown battle outcome {raw_outcome}')
    if raw_outcome&0x80:
        raise MemoryValidationError('Unknown outcome flag bit')
    alive_player=sum(m['hp']>0 for m in player)
    alive_enemy=sum(m['hp']>0 for m in opponent)
    terminal=raw_outcome in (1,2,3)
    if terminal:
        if code==1 and (alive_enemy!=0 or alive_player==0):
            raise MemoryValidationError('Victory flag contradicts live party HP')
        if code==2 and alive_player!=0:
            raise MemoryValidationError('Loss flag contradicts live player HP')
        if code==3 and (alive_player!=0 or alive_enemy!=0):
            raise MemoryValidationError('Draw flag contradicts party HP')
    if code in (4,5,6):
        raise MemoryValidationError('Unexpected caught/fled outcome in Cynthia benchmark')
    substates=list(ram[ctx:ctx+4]);response=ram[ctx+0x2300]
    menu={1:'command',3:'move',5:'target',7:'bag',9:'party'}.get(substates[0]) if phase==5 else None
    script_position=None;script_opcode=None;forced_replacement=False
    experience_task_state=None;public_learning_offer=None;public_level_up=None
    if phase==21:
        script_position=u32(ram,ctx+0xB4)
        # The script buffer occupies 0x2700..0x2D3F, before battle-mon records.
        if script_position>=0x640//4:
            raise MemoryValidationError('Invalid battle script position')
        script_opcode=u32(ram,ctx+0x2700+4*script_position)
        if script_opcode==44 and (u32(ram,ctx+0x13C)&1):
            # ov11_02233BC0 waits for the player's forced replacement response.
            # U-turn uses this same source wait with a living active battler.
            # Require a living reserve, not a fainted active monster. The script
            # and replacement-request flag are the evidence a choice is needed.
            if alive_player>int(active[0]['hp']>0):
                menu='forced_party';forced_replacement=True
        elif script_opcode==176:
            menu='shift_prompt'
        elif script_opcode==178:
            menu='shift_party'
        elif script_opcode==41 and u32(ram,ctx+0x178):
            # Script41 waits for experience task ov11_0223A79C to finish. Its
            # choice dialogs are separate from ordinary battle command input.
            task=_offset(ram,u32(ram,ctx+0x178),0x58)
            if u32(ram,task)!=address or u32(ram,task+4)!=context_address:
                raise MemoryValidationError('Experience task is not bound to this battle')
            experience_task_state=u32(ram,task+0x28)
            if experience_task_state>38:raise MemoryValidationError('Invalid experience task state')
            if experience_task_state in (11,13,21,23,34):
                # +0x48 is only the scan start. Replicate the original eligibility
                # scan; it skips nonparticipants without an Exp.Share held effect.
                learner=u32(ram,task+0x48)
                participation=u32(ram,ctx+0xA4+4*((u32(ram,ctx+0x74)>>1)&1))
                while learner<6:
                    item=player[learner]['held_item_id']
                    if participation&(1<<learner) or _game().data['items'][item]['held_effect']==50:break
                    learner+=1
                if learner>=6:raise MemoryValidationError('Experience input has no eligible learner')
                if experience_task_state in (11,13):
                    menu='level_up_stats'
                    public_level_up={'party_slot':learner+1,'species':player[learner]['species'],
                                     'level':player[learner]['level']}
                else:
                    move=u32(ram,task+0x40)
                    if not 1<=move<len(_game().data['moves']):raise MemoryValidationError('Invalid offered learning move')
                    menu={21:'learn_move_prompt',23:'learn_move_slots',34:'learn_move_confirm_stop'}[experience_task_state]
                    public_learning_offer={'party_slot':learner+1,'species':player[learner]['species'],
                                           'move':_game().data['moves'][move]['name']}
    # This is an input-protocol classification, not proof the UI has finished animating.
    needs_input=menu is not None and (response==0 or menu=='level_up_stats') and raw_outcome==0
    # Subscript231 prints trainer message835 naming the incoming Pokemon before
    # the Shift-style yes/no offer. The incoming BattleMon is already loaded at
    # this point, but only the announced species (not its level/HP) is public.
    public_shift_offer=None
    if menu in ('shift_prompt','shift_party'):
        words=struct.unpack_from('<400I',ram,ctx+0x2700)
        start=script_position-(10 if menu=='shift_prompt' else 18)
        announcement=(18,835,58,6,6,6,3,14,175,4)
        if start>=0 and words[start:start+10]==announcement:
            public_shift_offer={'species':active[1]['species']}
    # Set by the "used MOVE" public-message emitter, not by AI move selection.
    # The stateful observer below only publishes a new event on the rising edge.
    message_flag=bool(u32(ram,ctx+0x213C)&1)
    message=None
    if message_flag:
        side=u32(ram,ctx+0x64);move_id=u32(ram,ctx+0x3044)
        if side in (0,1) and 1<=move_id<len(_game().data['moves']):
            message={'kind':'move_announced','side':'player' if side==0 else 'opponent',
                     'species':active[side]['species'],'move':_game().data['moves'][move_id]['name'],
                     'move_id':move_id,
                     'meaning':'The game announced this move. It may miss, fail, or have no effect.'}
    return {'validation_status':'valid','battle_system_address':address,
            'battle_setup_address':setup_address,'context_address':context_address,
            'player_party_address':player_address,'opponent_party_address':enemy_address,
            'trainer_id':267,'battle_flags':1,'phase':phase,'input_substates':substates,
            'input_response':response,'menu':menu,'needs_input':needs_input,
            'script_position':script_position,'script_opcode':script_opcode,
            'forced_replacement':forced_replacement,
            'public_shift_offer':public_shift_offer,
            'experience_task_state':experience_task_state,
            'public_learning_offer':public_learning_offer,'public_level_up':public_level_up,
            '_attack_message_flag':message_flag,'_attack_message':message,
            'outcome_raw':raw_outcome,'outcome_code':code,'outcome':OUTCOMES[code],
            'terminal_confirmed':terminal,'player':player,'opponent':opponent,
            'active':active,'active_party_indexes':active_indexes,
            'player_party_order':player_party_order,
            'public_field_conditions':public_field_conditions,
            'player_alive':alive_player,'opponent_alive':alive_enemy,
            'opponent_knocked_out':6-alive_enemy,
            'opponent_hp_fraction_remaining':sum(m['hp']/m['stats']['hp'] for m in opponent)/6,
            'progress_percent':100*sum(1-m['hp']/m['stats']['hp'] for m in opponent)/6,
            'progress_definition':'Current opponent HP depletion averaged equally over six original party members. Not cumulative damage and not win probability.'}


def read_state(ram: bytes) -> dict[str,Any]|None:
    """Discover once. None means no validated live battle, not a model loss."""
    return BattleMemory().observe(ram)


class BattleMemory:
    """One instance per attempt. Bind once; never switch to stale/freed structures.

    After a confirmed outcome, retain the exact snapshot and stop reading RAM.
    A caller reloading a checkpoint must construct a fresh instance.
    """
    def __init__(self):
        self.address: int|None=None
        self.setup_address: int|None=None
        self.terminal_snapshot: dict|None=None
        self._last_attack_message_flag: bool|None=None
        self._public_event_count=0

    def observe(self,ram: bytes) -> dict[str,Any]|None:
        if self.terminal_snapshot is not None:
            return deepcopy(self.terminal_snapshot)
        if len(ram)!=RAM_SIZE:
            raise MemoryValidationError(f'Expected 4MiB main RAM, got {len(ram)}')
        if self.address is not None:
            state=_read_system(ram,self.address,self.setup_address)
        else:
            valid=[];errors=[]
            for addr in _candidates(ram):
                try:valid.append(_read_system(ram,addr))
                except MemoryValidationError as exc:errors.append(str(exc))
            if len(valid)>1:
                raise MemoryValidationError('Multiple valid Cynthia BattleSystems; refuse ambiguous score')
            if not valid:
                if errors:raise MemoryValidationError('; '.join(errors[:3]))
                return None
            state=valid[0]
            self.address=state['battle_system_address'];self.setup_address=state['battle_setup_address']
        flag=state.pop('_attack_message_flag');message=state.pop('_attack_message')
        events=[]
        # On first binding establish a baseline; never invent prior move history
        # from a mid-animation snapshot. One instance is sampled every game frame.
        if self._last_attack_message_flag is False and flag and message is not None:
            self._public_event_count+=1
            events=[dict(message,event_id=self._public_event_count)]
        self._last_attack_message_flag=flag
        state['new_public_events']=events
        state['public_event_count']=self._public_event_count
        if state['terminal_confirmed']:
            self.terminal_snapshot=deepcopy(state)
        return state
