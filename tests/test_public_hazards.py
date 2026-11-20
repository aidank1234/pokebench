"""Real-game cast evidence and side/layer separation for public hazards."""
import struct
from pathlib import Path
import pytest
from cynthiabench.battle_memory import read_state, RAM_BASE, MemoryValidationError

ROOT=Path(__file__).resolve().parents[1]

@pytest.mark.parametrize('name,field,expected',[
    ('toxic-spikes/first-cast','toxic_spikes_layers',1),
    ('toxic-spikes/second-cast','toxic_spikes_layers',2),
    ('stealth-rock/first-cast','stealth_rock',True),
])
def test_actual_hazard_cast(name,field,expected):
    path=ROOT/'work/public-hazard-controls'/(name+'.ram')
    if not path.exists():pytest.skip('Private control RAM not redistributed')
    s=read_state(path.read_bytes())
    assert s['public_field_conditions']['opponent'][field]==expected
    assert not s['public_field_conditions']['player'][field]

def test_actual_disable_hides_duration():
    path=ROOT/'work/public-hazard-controls/disable/first-cast.ram'
    if not path.exists():pytest.skip('Private control RAM not redistributed')
    s=read_state(path.read_bytes())
    assert s['active'][1]['public_volatile_conditions']['disable'] is True
    assert s['active'][0]['public_volatile_conditions']['disable'] is False

def test_layers_are_side_specific_and_gated_by_presence():
    path=ROOT/'assets/private/validation/first-command.ram'
    if not path.exists():pytest.skip('Private game RAM not redistributed')
    initial=path.read_bytes();ctx=read_state(initial)['context_address']-RAM_BASE
    for side,name in enumerate(('player','opponent')):
        b=bytearray(initial)
        struct.pack_into('<I',b,ctx+0x1BC+4*side,0x484)
        struct.pack_into('<I',b,ctx+0x1C8+8*side,0x1000000|11)
        fields=read_state(bytes(b))['public_field_conditions']
        assert fields[name]['spikes_layers']==3
        assert fields[name]['toxic_spikes_layers']==2
        assert fields[name]['stealth_rock'] is True
        other=fields['opponent' if side==0 else 'player']
        assert not any(other.values())
        struct.pack_into('<I',b,ctx+0x1BC+4*side,0)
        fields=read_state(bytes(b))['public_field_conditions'][name]
        assert fields['spikes_layers']==fields['toxic_spikes_layers']==0
        struct.pack_into('<I',b,ctx+0x1BC+4*side,0x400)
        struct.pack_into('<I',b,ctx+0x1C8+8*side,12)
        with pytest.raises(MemoryValidationError,match='Toxic Spikes'):
            read_state(bytes(b))
