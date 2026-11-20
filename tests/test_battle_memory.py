"""Regression tests use two real local RAM captures plus adversarial mutations.

Synthetic terminal mutations test verifier logic only; they are NOT evidence that
an emulator battle won/lost. Full actual-engine terminal tests are required too.
"""
from pathlib import Path
import struct
import pytest
from cynthiabench.battle_memory import BattleMemory,MemoryValidationError,read_state,RAM_BASE
from cynthiabench.save_editor import decrypt_pk4,encrypt_pk4,put16

ROOT=Path(__file__).resolve().parents[1]

@pytest.fixture
def initial():
    p=ROOT/'assets/private/validation/first-command.ram'
    if not p.exists():pytest.skip('Local proprietary game RAM fixture is not redistributed')
    return p.read_bytes()


def test_live_initial_battle_is_not_a_win(initial):
    s=read_state(initial)
    assert s['trainer_id']==267 and s['opponent_alive']==6
    assert s['outcome_code']==0 and not s['terminal_confirmed']
    assert s['menu']=='command' and s['needs_input']
    assert s['progress_percent']==0


def test_reads_live_party_not_stale_battle_setup(initial):
    p=ROOT/'assets/private/validation/after-spiritomb.ram'
    if not p.exists():pytest.skip('Local game RAM fixture missing')
    s=read_state(p.read_bytes())
    assert s['opponent'][0]['hp']==0
    assert s['opponent_alive']==5
    assert s['progress_percent']==pytest.approx(100/6)
    assert not s['terminal_confirmed']


def test_no_structure_is_not_loss():
    assert read_state(bytes(0x400000)) is None


def test_wrong_ram_length_fails():
    with pytest.raises(MemoryValidationError):read_state(bytes(64))


def test_bogus_win_flag_cannot_override_live_hp(initial):
    state=read_state(initial);b=bytearray(initial)
    b[state['battle_system_address']-RAM_BASE+0x241c]=1
    with pytest.raises(MemoryValidationError,match='Victory flag contradicts'):
        read_state(bytes(b))


def test_checksum_corruption_cannot_score(initial):
    state=read_state(initial);b=bytearray(initial)
    b[state['opponent_party_address']-RAM_BASE+8+8]^=1
    with pytest.raises(MemoryValidationError):read_state(bytes(b))


def test_ambiguous_structures_fail_closed(initial):
    state=read_state(initial);b=bytearray(initial)
    off=state['battle_system_address']-RAM_BASE
    b[0x100000:0x100000+0x2444]=b[off:off+0x2444]
    with pytest.raises(MemoryValidationError,match='Multiple valid'):
        read_state(bytes(b))


def test_bound_address_cannot_fall_back_to_another_copy(initial):
    monitor=BattleMemory();state=monitor.observe(initial);b=bytearray(initial)
    off=state['battle_system_address']-RAM_BASE
    b[0x100000:0x100000+0x2444]=b[off:off+0x2444]
    struct.pack_into('<I',b,off+0x2c,0)
    with pytest.raises(MemoryValidationError,match='Bound battle structure'):
        monitor.observe(bytes(b))


def _zero_side(b,state,side):
    ptr=state['player_party_address' if side==0 else 'opponent_party_address']-RAM_BASE
    for i in range(6):
        off=ptr+8+i*236
        plain=bytearray(decrypt_pk4(bytes(b[off:off+236])))
        put16(plain,142,0)
        b[off:off+236]=encrypt_pk4(bytes(plain))
    struct.pack_into('<I',b,state['context_address']-RAM_BASE+0x2d8c+0xc0*side,0)


def test_synthetic_win_requires_flag_and_hp_and_is_latched(initial):
    b=bytearray(initial);state=read_state(initial);_zero_side(b,state,1)
    # Six fainted enemies alone is not enough before an outcome event.
    waiting=read_state(bytes(b));assert not waiting['terminal_confirmed']
    b[state['battle_system_address']-RAM_BASE+0x241c]=1
    monitor=BattleMemory();s=monitor.observe(bytes(b))
    assert s['terminal_confirmed'] and s['outcome']=='win' and s['progress_percent']==100
    # Do not reinterpret freed memory after a valid terminal snapshot.
    assert monitor.observe(bytes(0x400000))==s


def test_synthetic_loss_and_draw_do_not_earn_win(initial):
    b=bytearray(initial);state=read_state(initial);_zero_side(b,state,0)
    b[state['battle_system_address']-RAM_BASE+0x241c]=2
    s=read_state(bytes(b));assert s['terminal_confirmed'] and s['outcome']=='loss'
    _zero_side(b,state,1);b[state['battle_system_address']-RAM_BASE+0x241c]=3
    s=read_state(bytes(b));assert s['terminal_confirmed'] and s['outcome']=='draw'
    assert s['progress_percent']==100 and s['outcome_code']!=1


def test_progress_tracks_current_hp_and_can_reverse_after_healing(initial):
    state=read_state(initial);b=bytearray(initial)
    # Active battle HP changes before controller writes back to Party.
    struct.pack_into('<I',b,state['context_address']-RAM_BASE+0x2d8c+0xc0,75)
    s=read_state(bytes(b));assert s['opponent'][0]['party_hp']==150
    assert s['opponent'][0]['hp']==75 and s['progress_percent']==pytest.approx(100/12)
    healed=read_state(initial);assert healed['progress_percent']==0


def test_source_script_opcode_numbers_include_named_functions():
    # CalcExperienceGain is a named entry among ov11_* labels: omitting it shifts
    # every later opcode and could auto-advance across an actual decision.
    path=ROOT/'vendor/pokediamond/arm9/overlays/11/asm/ov11_02232264.s'
    if not path.exists():pytest.skip('Source checkout unavailable')
    table=path.read_text().split('ov11_0225DDE4: ;')[1].split('.global')[0]
    names=[]
    for line in table.splitlines():
        if '.word ' in line:names.extend(s.strip() for s in line.split('.word ')[1].split(','))
    assert names[44]=='ov11_02233BC0'
    assert names[40]=='ov11_02233A78'
    assert names[41]=='ov11_02233AB8'
    assert names[176]=='ov11_022396D8'
    assert names[178]=='ov11_02239748'


def test_forced_replacement_requires_wait_script_and_living_reserve(initial):
    s=read_state(initial);b=bytearray(initial);ctx=s['context_address']-RAM_BASE
    struct.pack_into('<I',b,ctx+8,21)
    struct.pack_into('<I',b,ctx+0xB4,0)
    struct.pack_into('<I',b,ctx+0x2700,44)
    struct.pack_into('<I',b,ctx+0x13C,1)
    # U-turn requests a replacement while the active Pokemon is still alive.
    assert read_state(bytes(b))['forced_replacement']
    struct.pack_into('<I',b,ctx+0x2D8C,0)
    state=read_state(bytes(b))
    assert state['forced_replacement'] and state['menu']=='forced_party' and state['needs_input']
    b[ctx+0x2300]=2
    assert not read_state(bytes(b))['needs_input']
    b[ctx+0x2300]=0
    struct.pack_into('<I',b,ctx+0x2700,43)
    assert not read_state(bytes(b))['forced_replacement']


def test_actual_donor_shift_prompt_is_recognized(initial):
    path=ROOT/'assets/private/validation/after-spiritomb.ram'
    if not path.exists():pytest.skip('Local game RAM fixture missing')
    state=read_state(path.read_bytes())
    assert state['script_opcode']==176 and state['menu']=='shift_prompt'


def test_shift_offer_exposes_only_announced_species_and_requires_message_prefix():
    path=ROOT/'assets/private/validation/actual-shift-offer.ram'
    if not path.exists():pytest.skip('Local engineered game RAM fixture missing')
    raw=path.read_bytes();state=read_state(raw)
    assert state['public_shift_offer']=={'species':'Roserade'}
    b=bytearray(raw);ctx=state['context_address']-RAM_BASE
    # An unrelated yes/no script using the same opcode must not reveal a reserve.
    struct.pack_into('<I',b,ctx+0x2700+4*(state['script_position']-9),0)
    assert read_state(bytes(b))['public_shift_offer'] is None


def test_actual_engine_win_has_consistent_terminal_evidence():
    path=ROOT/'assets/private/validation/actual-win.ram'
    if not path.exists():pytest.skip('Local game RAM fixture missing')
    state=read_state(path.read_bytes())
    assert state['outcome_raw']==1 and state['terminal_confirmed']
    assert state['outcome']=='win' and state['opponent_alive']==0
    assert state['player_alive']>0 and state['progress_percent']==100


def test_actual_engine_forced_replacement_protocol():
    path=ROOT/'assets/private/validation/actual-forced-replacement.ram'
    if not path.exists():pytest.skip('Local game RAM fixture missing')
    state=read_state(path.read_bytes())
    assert state['script_opcode']==44 and state['forced_replacement']
    assert state['active'][0]['hp']==0 and state['player_alive']==5
    assert state['needs_input'] and not state['terminal_confirmed']


def test_actual_engine_loss_cannot_earn_win():
    path=ROOT/'assets/private/validation/actual-loss.ram'
    if not path.exists():pytest.skip('Local game RAM fixture missing')
    state=read_state(path.read_bytes())
    assert state['outcome_raw']==2 and state['terminal_confirmed']
    assert state['outcome']=='loss' and state['player_alive']==0
    assert state['opponent_alive']==6 and state['progress_percent']==0


def test_actual_uturn_requests_living_replacement():
    path=ROOT/'assets/private/validation/actual-uturn-replacement.ram'
    if not path.exists():pytest.skip('Local game RAM fixture missing')
    state=read_state(path.read_bytes())
    assert state['script_opcode']==44 and state['forced_replacement']
    assert state['active'][0]['hp']>0 and state['menu']=='forced_party'
    assert state['needs_input']
    assert state['player_party_order']==[1,0,2,3,4,5]
    assert state['active'][0]['embargoed']


def test_party_display_order_is_validated_and_not_assumed(initial):
    state=read_state(initial)
    assert state['player_party_order']==[0,1,2,3,4,5]
    b=bytearray(initial);ctx=state['context_address']-RAM_BASE
    b[ctx+0x312C:ctx+0x3132]=bytes([5,0,1,2,3,4])
    assert read_state(bytes(b))['player_party_order']==[5,0,1,2,3,4]
    b[ctx+0x312C]=0
    with pytest.raises(MemoryValidationError,match='display-order permutation'):
        read_state(bytes(b))


def test_embargo_exposes_presence_not_remaining_duration(initial):
    state=read_state(initial);b=bytearray(initial);ctx=state['context_address']-RAM_BASE
    assert not state['active'][0]['embargoed']
    for remaining in (1,2,5):
        struct.pack_into('<I',b,ctx+0x2DCC,remaining<<19)
        assert read_state(bytes(b))['active'][0]['embargoed'] is True
    # Neighboring volatile fields must not be mistaken for Embargo.
    struct.pack_into('<I',b,ctx+0x2DCC,(1<<18)|(1<<22))
    assert read_state(bytes(b))['active'][0]['embargoed'] is False


@pytest.mark.parametrize('name,relative,mask',[
    ('confusion',0x70,0x7),('taunt',0x88,0x700),
    ('leech_seed',0x80,0x4),('aqua_ring',0x80,0x1000000),
    ('drowsy',0x80,0x1800),('recharging',0x70,0x400000),
    ('encore',0x88,0x38),('disable',0x88,0x7),
])
def test_public_volatile_masks_drop_hidden_counters_and_neighboring_flags(initial,name,relative,mask):
    # Synthetic decoder tests: this is not evidence that a game move succeeded.
    state=read_state(initial);ctx=state['context_address']-RAM_BASE
    assert not any(state['active'][0]['public_volatile_conditions'].values())
    for side in (0,1):
        mon=ctx+0x2D40+0xC0*side
        for bits in (mask, mask&-mask):
            b=bytearray(initial);struct.pack_into('<I',b,mon+relative,bits)
            conditions=read_state(bytes(b))['active'][side]['public_volatile_conditions']
            assert conditions[name] is True
            assert all(type(v) is bool for v in conditions.values())
            assert all(not v for k,v in conditions.items() if k!=name)
        # Other effects sharing this word cannot turn this effect on.
        b=bytearray(initial);struct.pack_into('<I',b,mon+relative,0xFFFFFFFF^mask)
        assert read_state(bytes(b))['active'][side]['public_volatile_conditions'][name] is False


def test_diamond_effect_script_source_chain():
    """Pin the original-game evidence, independently of Python decoder masks."""
    base=ROOT/'vendor/pokediamond'
    p=base/'arm9/overlays/11/asm/ov11_02242B78.s'
    if not p.exists():pytest.skip('Source checkout unavailable')
    source=p.read_text()
    table=source.split('ov11_0225E4C8:')[1].split('.section')[0]
    raw=[]
    for line in table.splitlines():
        if '.byte ' in line:raw.extend(int(n.strip(),0) for n in line.split('.byte ')[1].split(','))
    lookup=struct.unpack('<'+'I'*(len(raw)//4),bytes(raw))
    def script(kind,num):
        data=(base/f'files/battle/skill/{kind}/narc_{num:04}.bin').read_bytes()
        return list(struct.unpack('<'+'I'*(len(data)//4),data))
    # effect ID -> side-effect pointer -> subscript is not an identity mapping.
    for effect,pointer,sub in [(49,7,37),(80,29,64),(84,72,68),
                               (90,74,73),(175,109,132),(187,113,141)]:
        assert script('be_seq',effect)[3]&0x7FFFFF==pointer
        assert lookup[pointer]==sub
    def contains(words,part):
        return any(words[i:i+len(part)]==part for i in range(len(words)-len(part)+1))
    assert contains(script('sub_seq',37),[33,4,7,53,7]) # confusion mask check
    assert script('sub_seq',64)[:5]==[52,10,1,53,0x400000] # recharge OR
    assert contains(script('sub_seq',68),[52,10,2,59,4]) # Leech Seed OR
    assert contains(script('be_seq',251),[52,10,1,59,0x1000000]) # Aqua Ring OR
    assert contains(script('sub_seq',132),[58,7,2,64,9]) # Taunt: set param64
    assert script('sub_seq',73)[:2]==[92,9] # Encore native command
    assert contains(script('sub_seq',141),[134,23]) # Yawn native command
    native=(base/'arm9/overlays/11/asm/ov11_02232264.s').read_text()
    encore=native.split('ov11_02235D38: ;')[1].split('thumb_func_start')[0]
    assert '=0x00002DC8' in encore and 'mov r0, #0x38' in encore
    yawn=native.split('ov11_02237FCC: ;')[1].split('thumb_func_start')[0]
    assert 'mov r0, #0xb7' in yawn and 'lsl r0, r0, #6' in yawn
    assert 'lsl r5, r5, #5' in yawn and 'lsl r1, r1, #0xc' in yawn


@pytest.mark.parametrize('condition',['confusion','taunt','leech_seed','drowsy','encore'])
def test_actual_engine_applied_volatile_condition(condition):
    path=ROOT/'assets/private/validation'/f'actual-{condition}.ram'
    if not path.exists():pytest.skip('Local game RAM fixture is not redistributed')
    state=read_state(path.read_bytes())
    assert state['menu']=='command' and state['needs_input']
    assert state['active'][1]['species']=='Spiritomb'
    assert state['active'][1]['public_volatile_conditions'][condition] is True
    assert not any(state['active'][0]['public_volatile_conditions'].values())


@pytest.mark.parametrize('condition',['aqua_ring','recharging'])
def test_actual_engineered_control_applied_volatile_condition(condition):
    path=ROOT/'assets/private/validation'/f'actual-{condition}.ram'
    if not path.exists():pytest.skip('Local engineered game RAM fixture is not redistributed')
    state=read_state(path.read_bytes())
    assert state['active'][0]['public_volatile_conditions'][condition] is True


def test_actual_fullheal_cures_confusion_without_persistent_status():
    folder=ROOT/'assets/private/validation'
    paths=[folder/f'actual-fullheal-confusion-{stage}.ram' for stage in ('before','after')]
    if not all(path.exists() for path in paths):pytest.skip('Local engineered game RAM fixtures are not redistributed')
    before,after=(read_state(path.read_bytes()) for path in paths)
    assert before['active'][0]['status']==after['active'][0]['status']==0
    assert before['active'][0]['public_volatile_conditions']['confusion'] is True
    assert after['active'][0]['public_volatile_conditions']['confusion'] is False
    assert before['active'][0]['species']==after['active'][0]['species']=='Vaporeon'
    assert before['menu']==after['menu']=='command'


@pytest.mark.parametrize('capture,menu,task_state',[
    ('stats1','level_up_stats',11),('stats2','level_up_stats',13),
    ('offer','learn_move_prompt',21),('slots','learn_move_slots',23),
    ('confirm-stop','learn_move_confirm_stop',34),
])
def test_actual_learning_waits_are_decisions_not_animation(capture,menu,task_state):
    path=ROOT/'assets/private/validation'/f'actual-learning-{capture}.ram'
    if not path.exists():pytest.skip('Local engineered game RAM fixture is not redistributed')
    state=read_state(path.read_bytes())
    assert state['menu']==menu and state['needs_input']
    assert state['experience_task_state']==task_state
    if task_state in (11,13):
        assert state['public_level_up']=={'party_slot':2,'species':'Lucario','level':37}
        assert state['public_learning_offer'] is None
    else:
        assert state['public_learning_offer']=={'party_slot':2,'species':'Lucario','move':'Aura Sphere'}
        assert state['public_level_up'] is None


def test_learning_scan_start_is_not_assumed_to_be_actual_learner():
    path=ROOT/'assets/private/validation/actual-learning-offer.ram'
    if not path.exists():pytest.skip('Local engineered game RAM fixture is not redistributed')
    raw=path.read_bytes();state=read_state(raw);b=bytearray(raw)
    ctx=state['context_address']-RAM_BASE;task=struct.unpack_from('<I',b,ctx+0x178)[0]-RAM_BASE
    # Synthetic regression: start before an ineligible mon, leaving Lucario's
    # real Exp.Share as the only eligibility. +0x48 is not directly a party slot.
    struct.pack_into('<I',b,task+0x48,0)
    struct.pack_into('<I',b,ctx+0xA4,0)
    assert read_state(bytes(b))['public_learning_offer']['party_slot']==2
    # An unannounced task animation does not publish an offer from its private ID.
    struct.pack_into('<I',b,task+0x28,19)
    assert read_state(bytes(b))['public_learning_offer'] is None
    struct.pack_into('<I',b,task+4,0)
    with pytest.raises(MemoryValidationError,match='not bound'):
        read_state(bytes(b))


def test_public_field_conditions_have_no_hidden_timers(initial):
    state=read_state(initial);b=bytearray(initial);ctx=state['context_address']-RAM_BASE
    assert state['public_field_conditions']=={
        'weather':None,'trick_room':False,
        'player':{'reflect':False,'light_screen':False,'stealth_rock':False,'spikes_layers':0,'toxic_spikes_layers':0},
        'opponent':{'reflect':False,'light_screen':False,'stealth_rock':False,'spikes_layers':0,'toxic_spikes_layers':0}}
    for duration in (1,3,5):
        struct.pack_into('<I',b,ctx+0x180,(duration<<16)|1)
        struct.pack_into('<2I',b,ctx+0x1BC,1,2)
        assert read_state(bytes(b))['public_field_conditions']=={
            'weather':'rain','trick_room':True,
            'player':{'reflect':True,'light_screen':False,'stealth_rock':False,'spikes_layers':0,'toxic_spikes_layers':0},
            'opponent':{'reflect':False,'light_screen':True,'stealth_rock':False,'spikes_layers':0,'toxic_spikes_layers':0}}


def test_actual_hail_is_publicly_observable():
    path=ROOT/'assets/private/validation/actual-hail.ram'
    if not path.exists():pytest.skip('Local game RAM fixture missing')
    state=read_state(path.read_bytes())
    assert state['active'][0]['species']=='Abomasnow'
    assert state['public_field_conditions']['weather']=='hail'


def test_executed_move_events_do_not_reveal_unannounced_selected_move():
    initial=ROOT/'assets/private/validation/public-move-initial.ram'
    announced=ROOT/'assets/private/validation/actual-opponent-move.ram'
    if not initial.exists() or not announced.exists():pytest.skip('Local game RAM fixtures missing')
    monitor=BattleMemory();assert monitor.observe(initial.read_bytes())['new_public_events']==[]
    raw=announced.read_bytes();b=bytearray(raw);ctx=read_state(raw)['context_address']-RAM_BASE
    struct.pack_into('<I',b,ctx+0x213C,struct.unpack_from('<I',b,ctx+0x213C)[0]&~1)
    unannounced=monitor.observe(bytes(b))
    assert unannounced['new_public_events']==[] and unannounced['public_event_count']==0
    assert not any(k.startswith('_attack_message') for k in unannounced)
    events=monitor.observe(raw)['new_public_events']
    assert len(events)==1 and events[0]['side']=='opponent'
    assert events[0]['species']=='Garchomp' and events[0]['move']=='Dragon Rush'
    assert monitor.observe(raw)['new_public_events']==[]
    # A changed current-move field without a new announcement edge is not public.
    changed=bytearray(raw);struct.pack_into('<I',changed,ctx+0x3044,57)
    assert monitor.observe(bytes(changed))['new_public_events']==[]
    monitor.observe(bytes(b))
    assert monitor.observe(raw)['new_public_events'][0]['event_id']==2


def test_named_public_stage_mapping(initial):
    state=read_state(initial);b=bytearray(initial);ctx=state['context_address']-RAM_BASE
    b[ctx+0x2D58:ctx+0x2D60]=bytes([6,5,7,8,9,4,3,2])
    named=read_state(bytes(b))['active'][0]['public_stat_stages']
    assert named=={'attack':-1,'defense':1,'speed':2,'special_attack':3,
                   'special_defense':-2,'accuracy':-3,'evasion':-4}
