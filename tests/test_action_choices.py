"""Explicit-choice guards: malformed strategy requests must send no game input."""
import pytest
from cynthiabench.worker import BattleSession, MAX_ACTIONS
from cynthiabench.save_editor import GameData


def session(menu):
    s=BattleSession.__new__(BattleSession)
    s.result_value=None;s.actions=0;s.invalid_frames=0;s._event=lambda *_:None
    from types import SimpleNamespace
    s.game=SimpleNamespace(resolve=lambda kind, name: {'id':396})
    s.state={'menu':menu,'needs_input':True,'active_party_indexes':[0,0],
             'player':[{'pid':i+10,'hp':10,'move_ids':[232,395,198,98]} for i in range(6)],
             'public_learning_offer':{'party_slot':2,'species':'Lucario','move':'Aura Sphere'}}
    # Any unexpected controller access would fail the test immediately.
    class NoInput:
        def press(self,*a,**k):raise AssertionError('Unexpected game input')
        def step(self,*a,**k):raise AssertionError('Unexpected game input')
        def image(self):raise AssertionError('Unexpected game input')
    s.e=NoInput()
    return s


@pytest.mark.parametrize('action',[
    {'type':'shift'},{'type':'shift','accept':1},
    {'type':'shift','accept':True}, {'type':'shift','accept':True,'slot':1},
    {'type':'shift','accept':False,'slot':2},
])
def test_shift_requires_an_unambiguous_explicit_choice(action):
    with pytest.raises(ValueError):session('shift_prompt').action(action)


@pytest.mark.parametrize('action',[
    {'type':'learn_move'},{'type':'learn_move','accept':1},
    {'type':'learn_move','accept':True},
    {'type':'learn_move','accept':False,'slot':2},
])
def test_learning_requires_an_unambiguous_explicit_choice(action):
    with pytest.raises(ValueError):session('learn_move_prompt').action(action)


def test_acknowledge_cannot_dismiss_a_strategic_prompt():
    for menu in ('command','shift_prompt','learn_move_prompt','learn_move_confirm_stop'):
        with pytest.raises(ValueError):session(menu).action({'type':'acknowledge'})


def test_learning_rejects_a_protected_hm_without_substituting_another_slot():
    s=session('learn_move_prompt');s.state['player'][1]['move_ids'][0]=57
    with pytest.raises(ValueError,match='HM'):
        s.action({'type':'learn_move','accept':True,'slot':1})


def test_rejected_choices_cannot_extend_the_action_allowance():
    s=session('command');s.actions=MAX_ACTIONS-1
    with pytest.raises(ValueError):
        s.action({'type':'acknowledge'})
    assert s.actions==MAX_ACTIONS
    finished=[]
    s._finish=lambda outcome,valid,reason:finished.append((outcome,valid,reason))
    s.observe=lambda:{'terminal':True}
    assert s.action({'type':'advance'})=={'terminal':True}
    assert finished==[('timeout',True,'action_limit')]
    assert s.actions==MAX_ACTIONS
