"""Private emulator process. Only the authenticated server speaks this JSONL API.

Public actions cannot load states, set RNG, read addresses or write game memory.
"""
from __future__ import annotations

import argparse
import base64
from copy import deepcopy
import ctypes
import hashlib
import json
import math
import os
from pathlib import Path
import struct
import sys
import time

from .battle_memory import BattleMemory, MemoryValidationError
from .emulator import Emulator, BUTTONS
from .controls import party_ui_kind, wait_for_party_ui, party_touch_position
from .rng import read_lcrng
from .save_editor import GameData, decode_bag_memory, normalize_name

ROOT=Path(__file__).resolve().parents[2]
from .paths import runtime_paths
PATHS=runtime_paths()
CORE=PATHS.core
MAX_ACTIONS=1200
MAX_FRAMES=180000


def digest(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def public_status(raw):
    """Visible condition only; never reveal hidden sleep/toxic counters."""
    if raw&7:return 'sleep'
    for mask,name in ((0x80,'badly_poisoned'),(8,'poison'),(16,'burn'),
                      (32,'freeze'),(64,'paralysis')):
        if raw&mask:return name
    return None


class BattleSession:
    def __init__(self,team,seed,trial,output,record=True,fixture=None):
        self.team,self.seed,self.trial=team,seed,trial
        self.output=Path(output);self.output.mkdir(parents=True,exist_ok=True)
        self.game=GameData()
        self.engine=BattleMemory()
        self.state=None
        self.result_value=None
        self.actions=0
        self.invalid_frames=0
        self.last_error=None
        self.recorder=None
        self.closed=False
        self.started=time.time()
        self.event_file=(self.output/'events.jsonl').open('x',buffering=1)
        self.signature=None
        self.replay_metadata=None
        self.public_events=[]
        if fixture is None:
            task=next(t for t in json.loads((ROOT/'provenance/task-manifest.json').read_text())
                      if t['team_id']==team and t['seed']==seed)
            fixture=PATHS.fixtures/(task['name']+'.json')
        self.manifest=json.loads(Path(fixture).read_text())
        if self.manifest['team_id']!=team or self.manifest['seed']!=seed:
            raise ValueError('Fixture identity mismatch')
        state_path=ROOT/self.manifest['state_file']
        save_path=(ROOT/self.manifest['source_save_file'] if 'source_save_file' in self.manifest
                   else PATHS.saves/f'{team}.sav')
        for path,key in [(state_path,'state_sha256'),(save_path,'save_sha256'),
                         (CORE,'core_sha256'),(PATHS.rom,'rom_sha256')]:
            if digest(path)!=self.manifest[key]:raise ValueError(f'Fixture integrity mismatch: {key}')
        self.e=Emulator(CORE,PATHS.rom,save_path,self.output/'runtime')
        self.e.load_state(state_path)
        if read_lcrng(self.e.ram())!=seed:raise ValueError('Initial game RNG does not match fixture')
        self._sample()
        if not self.state or self.state['menu']!='command' or not self.state['needs_input']:
            raise ValueError('Fixture does not start at a validated battle decision')
        self.initial_player_ids=[p['pid'] for p in self.state['player']]
        if record:
            from .recording import Recorder
            self.recorder=Recorder(self.output/'video')
            self.e.on_audio=self.recorder.audio
        self.e.on_frame=self._on_frame
        # libretro refreshes its framebuffer on the first frame after loading.
        # Every system receives this same one-frame transition from a fixture.
        self.e.step(1)
        self._event('start',{'fixture_sha256':digest(state_path),'seed':seed,
                            'post_refresh_rng':read_lcrng(self.e.ram()),
                            'initial_state':self.state})
        (self.output/'initial.png').write_bytes(self.e.png())
        (self.output/'fixture.json').write_text(json.dumps(self.manifest,indent=2)+'\n')

    def _event(self,kind,payload):
        self.event_file.write(json.dumps({'event':kind,'frame':getattr(self,'e',None).frame if hasattr(self,'e') else 0,
                                         'wall_time':time.time(),**payload})+'\n')

    def _sample(self):
        ram=self.e.ram()
        state=self.engine.observe(ram)
        if state is None:raise MemoryValidationError('No identified live Cynthia battle')
        if hasattr(self,'initial_player_ids') and [p['pid'] for p in state['player']]!=self.initial_player_ids:
            raise MemoryValidationError('Player party identity changed')
        # Source: overlay11 BattleSystem+0x58 owns its copied live Bag.
        bs=state['battle_system_address']-0x02000000
        bag_pointer=struct.unpack_from('<I',ram,bs+0x58)[0]
        bag_offset=bag_pointer-0x02000000
        if not 0<=bag_offset<len(ram)-1908:raise MemoryValidationError('Invalid live bag pointer')
        try:
            decoded_bag=decode_bag_memory(ram[bag_offset:bag_offset+1908],self.game)
        except ValueError as exc:raise MemoryValidationError(str(exc)) from exc
        state['bag']=decoded_bag['quantities']
        state['bag_items']=decoded_bag['items']
        for event in state.get('new_public_events',[]):
            public_event={**event,'frame':self.e.frame}
            self.public_events.append(public_event)
            self._event('public_battle_event',{'public_event':public_event})
        self.state=state
        self.invalid_frames=0
        self.last_error=None

    def _on_frame(self,e):
        if self.recorder:self.recorder.frame(e)
        if self.result_value:return
        try:
            self._sample()
            s=self.state
            signature=(s['outcome_raw'],s['phase'],tuple(s['input_substates']),s['input_response'],
                       tuple((p['hp'],p['status']) for p in s['player']+s['opponent']),
                       tuple(s['active_party_indexes']),tuple(sorted(s['bag'].items())))
            if signature!=self.signature:
                self.signature=signature
                self._event('state',{'state':s})
            if s['terminal_confirmed']:
                self._finish(s['outcome'],True,'game_terminal')
        except MemoryValidationError as exc:
            self.invalid_frames+=1
            self.last_error=str(exc)
            if self.invalid_frames==1:self._event('memory_transition',{'message':str(exc)})
            if self.invalid_frames>3000:
                self._finish('invalid',False,'persistent_memory_validation_failure')
        if not self.result_value and e.frame>=MAX_FRAMES:
            self._finish('timeout',not bool(self.invalid_frames),'emulated_frame_limit')

    def _finish(self,outcome,valid,reason):
        if self.result_value:return
        self.e.halted=True
        state=deepcopy(self.state)
        game_complete = valid and reason == 'game_terminal' and outcome in ('win', 'loss')
        self.result_value={'team_id':self.team,'seed':self.seed,'trial_id':self.trial,
          'outcome':outcome,'terminal':True,'valid':valid,'reason':reason,
          'win':(outcome=='win') if game_complete else None,
          'score':(100 if outcome=='win' else 0) if game_complete else None,
          'progress_score':state['progress_percent'] if state else None,
          'opponent_fainted':state['opponent_knocked_out'] if state else None,
          'frames':self.e.frame,'actions':self.actions,'last_validation_error':self.last_error,
          'evidence':{'terminal_state':state,'state_sha256':self.manifest['state_sha256'],
                      'game_outcome_code':state['outcome_code'] if state else None,
                      'event_log':'events.jsonl','terminal_image':'terminal.png'},
          'model_wall_seconds':time.time()-self.started}
        (self.output/'terminal.png').write_bytes(self.e.png())
        (self.output/'terminal.ram').write_bytes(self.e.ram())
        self._event('result',{'result':self.result_value})
        self._persist_result()

    def _persist_result(self):
        if self.result_value:
            (self.output/'result.json').write_text(json.dumps(self.result_value,indent=2)+'\n')

    def _public_state(self):
        s=self.state
        if not s:return {'status':'initializing'}
        active=s['active'][0]
        player=[]
        for i,p in enumerate(s['player']):
            is_active=i==active['party_index']
            pp=active['pp'] if is_active else p['pp']
            move_names=[self.game.data['moves'][mid]['name'] for mid in active['move_ids']] if is_active else p['moves']
            held_id=active['held_item_id'] if is_active else p['held_item_id']
            ability_id=active['ability_id'] if is_active else p['ability_id']
            player.append({'slot':i+1,'species':p['species'],'name':p['name'],'level':p['level'],
              'gender':p['gender'],
              'hp':p['hp'],'max_hp':p['stats']['hp'],'status':public_status(p['status']),'stats':p['stats'],
              'active':is_active,'ability':self.game.data['abilities'][ability_id],
              'held_item':self.game.data['items'][held_id]['name'] if held_id else None,
              'stat_stages':active.get('public_stat_stages') if is_active else None,
              'embargoed':active.get('embargoed',False) if is_active else False,
              'volatile_conditions':active.get('public_volatile_conditions',{}) if is_active else None,
              'moves':[{'slot':n+1,'name':name,'pp':pp[n]} for n,name in enumerate(move_names)]})
        foe=s['active'][1]
        shift_offer=s.get('public_shift_offer')
        return {'menu':s['menu'],'needs_input':s['needs_input'],
                'transitioning':bool(self.invalid_frames),'player':player,'bag':s['bag'],
                'field_conditions':s.get('public_field_conditions',{}),
                'shift_offer':shift_offer,
                'level_up':s.get('public_level_up'),
                'learning_offer':s.get('public_learning_offer'),
                'opponent_active':None if shift_offer else {'species':foe['species'],'level':foe['level'],
                    'gender':s['opponent'][foe['party_index']]['gender'],
                    'hp_bar_pixels':max(1,48*foe['hp']//foe['max_hp']) if foe['hp'] else 0,
                    'hp_bar_total':48,'status':public_status(foe['status']),
                    'embargoed':foe.get('embargoed',False),
                    'volatile_conditions':foe.get('public_volatile_conditions',{}),
                    'stat_stages':foe.get('public_stat_stages')},
                'recent_battle_events':self.public_events[-20:],
                'battle_event_count':len(self.public_events),
                'observed_fainted_opponents':[p['species'] for p in s['opponent'] if p['hp']==0]}

    def observe(self):
        return {'frame':self.e.frame,'screen_png_base64':base64.b64encode(self.e.png()).decode(),
                'public_state':self._public_state(),'terminal':self.result_value is not None,
                'outcome':self.result_value['outcome'] if self.result_value else 'ongoing',
                'available_actions':{'move':'{"type":"move","slot":1..4}',
                  'switch':'{"type":"switch","slot":1..6}',
                  'shift':'At a free-switch offer: {"type":"shift","accept":false} or {"type":"shift","accept":true,"slot":1..6}',
                  'acknowledge':'At level-up stats only: {"type":"acknowledge"} advances one informational screen',
                  'learn_move':'At a learning offer: {"type":"learn_move","accept":false} or {"type":"learn_move","accept":true,"slot":1..4} replaces that existing move',
                  'item':'{"type":"item","item":"Hyper Potion"|"Full Heal"|"Revive","target":1..6}',
                  'advance':'{"type":"advance"} runs animations without choosing a strategy',
                  'raw':'{"buttons":["A"],"frames":1..120} or {"touch":{"x":0..255,"y":0..191},"frames":1..120}'}}

    def _touch(self,x,y,after=20):
        self.e.step(3,touch=(x,y));self.e.step(after)

    def _party_slot(self,slot):
        x,y=party_touch_position(slot,self.state['player_party_order'])
        self._touch(x,y,after=60)

    def _advance(self,limit=3000):
        for i in range(limit):
            if self.result_value:return
            self.e.step(1)
            if i>20 and not self.invalid_frames and self.state['needs_input']:
                # Let the input screen finish drawing, without pressing anything.
                self.e.step(20)
                return

    def action(self,action):
        if self.result_value:return self.observe()
        if not isinstance(action,dict):raise ValueError('Action must be an object')
        # Invalid choices are counted too. Do not let another request advance
        # the game after earlier rejected choices used the action allowance.
        if self.actions>=MAX_ACTIONS:
            self._finish('timeout',not bool(self.invalid_frames),'action_limit')
            return self.observe()
        self.actions+=1
        self._event('action',{'action':action})
        kind=action.get('type')
        if kind=='advance':
            self._advance()
        elif kind=='move':
            slot=action.get('slot')
            if type(slot)!=int or not 1<=slot<=4:raise ValueError('Move slot must be 1..4')
            if self.invalid_frames or self.state['menu'] not in ('command','move') or not self.state['needs_input']:
                raise ValueError('Move choices are accepted only at the command or move menu')
            if self.state['active'][0]['pp'][slot-1]<=0:raise ValueError('Selected move has no PP')
            if self.state['menu']=='command':self._touch(128,80,after=25)
            if self.state['menu']!='move':raise ValueError('Move menu has not opened; observe/advance first')
            x,y=[(64,48),(192,48),(64,114),(192,114)][slot-1]
            self._touch(x,y,after=25)
            self._advance()
        elif kind=='switch':
            slot=action.get('slot')
            if type(slot)!=int or not 1<=slot<=6:raise ValueError('Party slot must be 1..6')
            if self.invalid_frames or not self.state['needs_input'] or self.state['menu'] not in ('command','party','forced_party'):
                raise ValueError('Switch choices require the command or party/replacement menu')
            if self.state['player'][slot-1]['hp']<=0:raise ValueError('Cannot switch to a fainted Pokemon')
            if self.state['active_party_indexes'][0]==slot-1:raise ValueError('That Pokemon is already active')
            target_pid=self.state['player'][slot-1]['pid']
            if self.state['menu']=='command':self._touch(215,170,after=90)
            # A forced-replacement request appears in RAM before its fade-in
            # finishes. Touching during that interval can select the wrong card.
            # Also recover a selection panel opened by an earlier raw action.
            if party_ui_kind(self.e.image())=='shift':self.e.press('B',after=20)
            wait_for_party_ui(self.e,'party_grid')
            self._party_slot(slot)
            wait_for_party_ui(self.e,'shift')
            self._touch(128,90,after=90)  # The explicit SHIFT button, no strategy choice.
            self._advance()
            if (self.state['active_party_indexes'][0]!=slot-1 or
                    self.state['player'][self.state['active_party_indexes'][0]]['pid']!=target_pid):
                raise RuntimeError('Switch control did not activate the explicitly requested Pokemon')
        elif kind=='shift':
            accept=action.get('accept');slot=action.get('slot')
            if type(accept) is not bool:raise ValueError('Shift accept must explicitly be true or false')
            if (self.invalid_frames or not self.state['needs_input'] or
                    self.state['menu'] not in ('shift_prompt','shift_party')):
                raise ValueError('Shift choices require the free-switch offer or its party menu')
            original_index=self.state['active_party_indexes'][0]
            original_pid=self.state['player'][original_index]['pid']
            if accept:
                if type(slot)!=int or not 1<=slot<=6:raise ValueError('Accepted Shift requires party slot 1..6')
                if slot-1==original_index:raise ValueError('That Pokemon is already active; explicitly decline to keep battling')
                if self.state['player'][slot-1]['hp']<=0:raise ValueError('Cannot switch to a fainted Pokemon')
                target_pid=self.state['player'][slot-1]['pid']
                if self.state['menu']=='shift_prompt':
                    wait_for_party_ui(self.e,'shift_offer')
                    self._touch(128,70,after=90) # Explicitly requested Switch Pokemon.
                if party_ui_kind(self.e.image())=='shift':self.e.press('B',after=20)
                wait_for_party_ui(self.e,'party_grid')
                self._party_slot(slot)
                wait_for_party_ui(self.e,'shift')
                self._touch(128,90,after=90)
                self._advance()
                if (self.state['active_party_indexes'][0]!=slot-1 or
                        self.state['player'][slot-1]['pid']!=target_pid):
                    raise RuntimeError('Free-switch control did not activate the explicitly requested Pokemon')
            else:
                if slot is not None:raise ValueError('Declining Shift must not include a replacement slot')
                if self.state['menu']=='shift_prompt':
                    wait_for_party_ui(self.e,'shift_offer')
                    self._touch(128,145,after=90) # Explicitly requested Keep battling.
                else:
                    if party_ui_kind(self.e.image())=='shift':self.e.press('B',after=20)
                    wait_for_party_ui(self.e,'party_grid')
                    self._touch(236,172,after=90) # Explicit on-screen cancel arrow.
                self._advance()
                if (self.state['active_party_indexes'][0]!=original_index or
                        self.state['player'][original_index]['pid']!=original_pid):
                    raise RuntimeError('Declining free switch unexpectedly changed the active Pokemon')
        elif kind=='acknowledge':
            if self.invalid_frames or self.state['menu']!='level_up_stats' or not self.state['needs_input']:
                raise ValueError('Acknowledge is restricted to the informational level-up stats screen')
            self.e.press('A',after=30)
            self._advance()
        elif kind=='learn_move':
            accept=action.get('accept');slot=action.get('slot')
            if type(accept) is not bool:raise ValueError('Learning accept must explicitly be true or false')
            offer=self.state.get('public_learning_offer')
            if (self.invalid_frames or not self.state['needs_input'] or not offer or
                    self.state['menu'] not in ('learn_move_prompt','learn_move_slots','learn_move_confirm_stop')):
                raise ValueError('Learning choices require a visible move-learning decision')
            learner=offer['party_slot']-1
            old_moves=list(self.state['player'][learner]['move_ids'])
            new_move=self.game.resolve('moves',offer['move'])['id']
            if accept:
                if type(slot)!=int or not 1<=slot<=4:raise ValueError('Accepted learning requires an existing move slot 1..4')
                # Original Diamond's eight HM moves cannot be forgotten here.
                if old_moves[slot-1] in (15,19,57,70,127,249,431,432):
                    raise ValueError('An HM move cannot be forgotten through the battle learning menu')
                if self.state['menu']=='learn_move_confirm_stop':
                    wait_for_party_ui(self.e,'learn_confirm_stop')
                    self._touch(128,145,after=90) # Explicitly resume learning.
                    self._advance()
                if self.state['menu']=='learn_move_prompt':
                    wait_for_party_ui(self.e,'learn_offer')
                    self._touch(128,70,after=90) # Explicitly forget one move.
                if party_ui_kind(self.e.image())=='learn_forget':
                    self._touch(236,172,after=30) # Return from an earlier raw selection.
                wait_for_party_ui(self.e,'learn_slots')
                x,y=[(64,72),(192,72),(64,120),(192,120)][slot-1]
                self._touch(x,y,after=90)
                wait_for_party_ui(self.e,'learn_forget')
                self._touch(100,172,after=90) # Confirm the explicitly named old move.
                self._advance()
                expected=old_moves[:];expected[slot-1]=new_move
                if self.state['player'][learner]['move_ids']!=expected:
                    raise RuntimeError('Learning control did not replace exactly the explicitly requested move')
            else:
                if slot is not None:raise ValueError('Declining a new move must not name a replacement slot')
                if self.state['menu']=='learn_move_prompt':
                    wait_for_party_ui(self.e,'learn_offer')
                    self._touch(128,145,after=90) # Keep old moves, then confirm.
                elif self.state['menu']=='learn_move_slots':
                    if party_ui_kind(self.e.image())=='learn_forget':
                        self._touch(236,172,after=30)
                    wait_for_party_ui(self.e,'learn_slots')
                    self._touch(236,172,after=90) # Explicit cancel arrow.
                wait_for_party_ui(self.e,'learn_confirm_stop')
                self._touch(128,70,after=90) # Explicitly give up this new move.
                self._advance()
                if self.state['player'][learner]['move_ids']!=old_moves:
                    raise RuntimeError('Declining learning unexpectedly changed the move set')
        elif kind=='item':
            name=action.get('item');target=action.get('target')
            if type(target)!=int or not 1<=target<=6:raise ValueError('Item target must be party slot 1..6')
            if name not in ('Hyper Potion','Full Heal','Revive'):raise ValueError('Unsupported semantic item; use raw controls for other items')
            if self.invalid_frames or self.state['menu']!='command' or not self.state['needs_input']:
                raise ValueError('Item helper starts at the command menu; use raw controls to leave another menu')
            if self.state['bag'].get(name,0)<1:raise ValueError('No such item remains in the bag')
            target_mon=self.state['player'][target-1]
            if (target-1==self.state['active_party_indexes'][0] and
                    self.state['active'][0].get('embargoed',False)):
                raise ValueError('Embargo currently prevents item use on that Pokemon')
            if name=='Revive' and target_mon['hp']>0:raise ValueError('Revive requires a fainted target')
            if name=='Hyper Potion' and (target_mon['hp']==0 or target_mon['hp']==target_mon['stats']['hp']):
                raise ValueError('Hyper Potion requires a living, injured target')
            confused=(target-1==self.state['active_party_indexes'][0] and
                      self.state['active'][0].get('public_volatile_conditions',{}).get('confusion',False))
            if name=='Full Heal' and target_mon['status']==0 and not confused:
                raise ValueError('Full Heal requires a persistent status condition or active confusion')
            category=[n for n,quantity in self.state['bag'].items()
                      if quantity>0 and (n=='Hyper Potion')==(name=='Hyper Potion')]
            # The curated bags contain only these three medicines. Their saved
            # order is the on-screen order; empties are skipped by the game.
            index=category.index(name)
            if len(category)>6:raise ValueError('Item helper requires one category page')
            before_quantity=self.state['bag'][name]
            self._touch(40,170,after=90)
            self._touch(64,45 if name=='Hyper Potion' else 115,after=60)
            self._touch(64 if index%2==0 else 192,30+(index//2)*48,after=60)
            self._touch(100,172,after=60)  # USE opens the target chooser.
            wait_for_party_ui(self.e,'party_grid')
            self._party_slot(target)
            for _ in range(300):
                if self.result_value or self.state['bag'].get(name,0)<before_quantity:break
                self.e.step(1)
            if self.state['bag'].get(name,0)>=before_quantity:
                raise ValueError('The game did not apply that item; inspect its message and return with B')
            # Item-use confirmation is informational. Only dismiss after the
            # engine confirms consumption, never choose another target/item.
            # HP items animate restoration after consumption; an early single
            # A can be ignored. While this committed item's dialog is still
            # pending, acknowledge it. Stop as soon as the bag returns a result,
            # before another strategic menu can receive an A press.
            for _ in range(30):
                if (self.result_value or self.state['menu']!='bag' or
                        self.state['input_response']!=0):break
                self.e.press('A',after=30)
            if not self.result_value and self.state['menu']=='bag' and self.state['input_response']==0:
                raise RuntimeError('Committed item confirmation did not close')
            self._advance()
        elif kind is None:
            frames=action.get('frames',1)
            if type(frames)!=int or not 1<=frames<=120:raise ValueError('frames must be 1..120')
            buttons=action.get('buttons',[])
            if not isinstance(buttons,list) or any(b not in BUTTONS for b in buttons):raise ValueError('Unknown DS button')
            point=action.get('touch')
            if point is not None:
                if buttons:raise ValueError('Choose touch or buttons')
                if not isinstance(point,dict) or any(type(point.get(k))!=int for k in ('x','y')):
                    raise ValueError('Touch needs integer x/y')
                if not 0<=point['x']<256 or not 0<=point['y']<192:raise ValueError('Touch outside lower screen')
                point=(point['x'],point['y'])
            self.e.step(frames,buttons,point)
            self.e.step(1)  # release so consecutive calls are separate button presses
        else:raise ValueError('Unknown action type')
        if self.actions>=MAX_ACTIONS and not self.result_value:
            self._finish('timeout',not bool(self.invalid_frames),'action_limit')
        return self.observe()

    def result(self):
        return self.result_value or {'team_id':self.team,'seed':self.seed,'trial_id':self.trial,
          'outcome':'ongoing','terminal':False,'valid':False,
          'progress_score':self.state['progress_percent'] if self.state else None,
          'opponent_fainted':self.state['opponent_knocked_out'] if self.state else None}

    def finalize(self,reason):
        if reason not in ('agent_stopped','timeout','runtime_error'):
            raise ValueError('Unsupported finalization reason')
        if not self.result_value:
            valid=reason!='runtime_error' and not bool(self.invalid_frames)
            self._finish('timeout' if valid else 'invalid',valid,reason)
        return self.result()

    def close(self):
        if self.closed:return self.result()
        if not self.result_value:self.finalize('runtime_error')
        if self.recorder:
            # Include a short passive tail for watchable game-result transitions.
            self.e.halted=False
            self.e.step(240)
            self.e.halted=True
            self.replay_metadata=self.recorder.close()
            self.result_value['recording']=self.replay_metadata
            self._persist_result()
        self.e.close();self.event_file.close();self.closed=True
        return self.result()


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--team',required=True);parser.add_argument('--seed',type=int,required=True)
    parser.add_argument('--trial',required=True);parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    # Native libraries write C-level stdout. Preserve a private response fd and
    # send native output to stderr, so it cannot corrupt the JSONL protocol.
    response=os.fdopen(os.dup(sys.stdout.fileno()),'w',buffering=1)
    os.dup2(sys.stderr.fileno(),sys.stdout.fileno())
    session=None
    try:
        session=BattleSession(args.team,args.seed,args.trial,args.output)
        response.write(json.dumps(session.observe())+'\n')
        for line in sys.stdin:
            try:
                request=json.loads(line);op=request.get('op')
                if op=='observe':result=session.observe()
                elif op=='action':result=session.action(request.get('action'))
                elif op=='result':result=session.result()
                elif op=='finalize':result=session.finalize(request.get('reason'))
                elif op=='close':result=session.close()
                else:raise ValueError('Unknown worker operation')
                response.write(json.dumps(result)+'\n')
                if op=='close':break
            except ValueError as exc:
                result=session.observe();result['error']=str(exc)
                response.write(json.dumps(result)+'\n')
    finally:
        if session and not session.closed:session.close()


if __name__=='__main__':main()
