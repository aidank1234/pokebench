"""The playing agent must see observations, not future choices or hidden counters."""
import json
from pathlib import Path

import pytest

from cynthiabench.battle_memory import read_state
from cynthiabench.save_editor import GameData
from cynthiabench.worker import BattleSession, public_status


def test_sleep_observation_does_not_disclose_remaining_duration():
    assert {public_status(counter) for counter in range(1, 8)} == {'sleep'}
    assert {public_status(0x80 | (counter << 8)) for counter in range(1, 16)} == {'badly_poisoned'}
    assert public_status(0) is None


def test_public_state_omits_verifier_and_opponent_secrets():
    root = Path(__file__).resolve().parents[1]
    ram = root / 'assets/private/validation/first-command.ram'
    if not ram.exists():
        pytest.skip('Private game RAM is not redistributed')
    session = object.__new__(BattleSession)
    session.state = read_state(ram.read_bytes())
    session.state['bag'] = {'Hyper Potion': 2}
    session.game = GameData()
    session.invalid_frames = 0
    session.public_events = []
    result = session._public_state()
    assert result['opponent_active']['species'] == 'Spiritomb'
    assert result['opponent_active']['hp_bar_pixels'] == 48
    assert not {'hp', 'max_hp', 'stats', 'moves', 'pp', 'pid', 'ability', 'held_item'} & result['opponent_active'].keys()
    assert not {'seed', 'rng', 'context_address', 'opponent', 'progress_percent'} & result.keys()
    assert len(result['player']) == 6
    assert 'Roserade' not in json.dumps(result['opponent_active'])
    conditions = result['opponent_active']['volatile_conditions']
    assert set(conditions) == {'confusion', 'taunt', 'leech_seed', 'aqua_ring', 'drowsy', 'recharging', 'encore', 'disable'}
    assert all(type(value) is bool for value in conditions.values())
    assert all(p['volatile_conditions'] is None for p in result['player'] if not p['active'])
    # The game initializes the next foe before a Shift offer. Only its announced
    # name is public until it is sent out; no premature HP/stats/level exposure.
    session.state['public_shift_offer']={'species':'Roserade'}
    result=session._public_state()
    assert result['shift_offer']=={'species':'Roserade'}
    assert result['opponent_active'] is None
