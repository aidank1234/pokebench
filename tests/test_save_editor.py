"""Tests include known donor facts, independently calculated stats and tamper checks."""
import binascii
import copy
import json
from pathlib import Path
import struct
import sys
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from cynthiabench.save_editor import (
    GameData,GENERAL_SIZE,HALF_SIZE,STORAGE_SIZE,PARTY_OFFSET,PARTY_SIZE,
    build_save,inspect_save,create_pokemon,decrypt_pk4,encrypt_pk4,
    active_offset,newer,u16,u32,put16,put32,decode_pokemon,decode_pokemon_memory,decode_party_memory,decode_bag_memory,crypt_words,
)


def reference_decode(raw):
    """Read fields by direct physical-block addressing, as Diamond GetSubstruct does.

    Deliberately does not call editor cipher or shuffle functions. This provides
    a separate implementation for generated-record verification, not an
    independently invented file-format oracle. Table follows pret/pokediamond.
    """
    pid,flags,check=struct.unpack_from('<IHH',raw)
    assert flags==0
    words=list(struct.unpack('<64H',raw[8:136]))
    state=check
    for n in range(64):
        state=(1103515245*state+24691)%(2**32)
        words[n]^=state//65536
    assert sum(words)%65536==check
    blocks=[words[i:i+16] for i in range(0,64,16)]
    permutations=['ABCD','ABDC','ACBD','ADBC','ACDB','ADCB',
                  'BACD','BADC','CABD','DABC','CADB','DACB',
                  'BCAD','BDAC','CBAD','DBAC','CDAB','DCAB',
                  'BCDA','BDCA','CBDA','DBCA','CDBA','DCBA']
    positions=permutations[((pid&0x3e000)>>13)%24]
    a,b,c,d=[blocks[ord(ch)-ord('A')] for ch in positions]
    tail=list(struct.unpack('<50H',raw[136:]));state=pid
    for n in range(50):
        state=(1103515245*state+24691)%(2**32);tail[n]^=state//65536
    ivword=b[8]|b[9]<<16
    return {'species':a[0],'held':a[1],'moves':b[:4],
      'is_nicknamed':bool(ivword&0x80000000),'name_codes':c[:11],
      'level':tail[2]&255,'hp':tail[3],'stats':tail[4:10]}


class SaveEditorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from cynthiabench.paths import runtime_paths
        paths = runtime_paths()
        if not (paths.data/'gen4-diamond.json').exists() or not (paths.private/'base.sav').exists():
            raise unittest.SkipTest('Private ROM-derived data and donor save are not distributed')
        cls.game=GameData();cls.base=(paths.private/'base.sav').read_bytes()
        cls.specs=json.loads((ROOT/'teams/team-specs.json').read_text())['teams']
        cls.outputs=[build_save(cls.base,t,cls.game) for t in cls.specs]

    def test_known_donor_facts_and_bad_bag_detected(self):
        original=inspect_save(self.base,self.game,strict_bag=False)
        if original['party'][0]['name'] != 'CREEPYGHOS':
            self.skipTest('Historical donor fixture not supplied')
        self.assertEqual(original['party'][0]['species'],'Gengar')
        self.assertEqual(original['party'][0]['name'],'CREEPYGHOS')
        self.assertEqual(original['party'][0]['level'],100)
        self.assertEqual(original['party'][0]['hp'],269)
        self.assertEqual(original['party'][0]['moves'],['Shadow Ball','Thunder','Hypnosis','Rain Dance'])
        self.assertTrue(original['bag_warnings'])
        with self.assertRaises(ValueError):inspect_save(self.base,self.game)

    def test_every_team_matches_requested_fields_and_default_names(self):
        for spec,(save,audit) in zip(self.specs,self.outputs):
            with self.subTest(team=spec['id']):
                self.assertEqual(audit['battle_style'],spec.get('battle_style','set'))
                self.assertTrue(audit['battle_animations'])
                self.assertEqual(audit['text_speed'],2)
                self.assertEqual(audit['bag'],spec['bag'])
                self.assertEqual(audit['bag_warnings'],[])
                for wanted,mon in zip(spec['party'],audit['party']):
                    self.assertEqual(mon['species'],wanted['species'])
                    for key in ['level','nature','ability','held_item','ivs','evs','friendship']:
                        self.assertEqual(mon[key],wanted[key])
                    self.assertEqual(mon['moves'],wanted['moves'])
                    self.assertFalse(mon['is_nicknamed']);self.assertFalse(mon['is_egg'])
                    self.assertEqual(mon['name'],wanted['species'].upper())
                    self.assertEqual(mon['hp'],mon['stats']['hp']);self.assertEqual(mon['status'],0)
                    self.assertEqual(mon['pp'],[self.game.resolve('moves',m)['pp'] for m in wanted['moves']])
                    self.assertEqual(mon['pp_ups'],[0]*4)
                    self.assertEqual(mon['ot_id'],u32(self.base,0x74))
                    self.assertTrue(mon['original_trainer'])

    def test_independent_direct_block_decoder_for_all_sixty(self):
        for save,audit in self.outputs:
            for i,mon in enumerate(audit['party']):
                raw=save[PARTY_OFFSET+i*PARTY_SIZE:PARTY_OFFSET+(i+1)*PARTY_SIZE]
                ref=reference_decode(raw)
                self.assertEqual(ref['species'],mon['species_id'])
                self.assertEqual(ref['held'],mon['held_item_id'])
                self.assertEqual(ref['moves'],mon['move_ids'])
                self.assertEqual(ref['level'],mon['level'])
                self.assertEqual(ref['hp'],mon['hp'])
                self.assertFalse(ref['is_nicknamed'])
                chars=''.join(self.game.code_to_char[n] for n in ref['name_codes'] if n not in (0,0xffff))
                self.assertEqual(chars,mon['species'].upper())

    def test_known_stat_vector_independent_arithmetic(self):
        # Infernape Lv59, 20 IV /48 EV each, Naive. Base stats 76/104/71/108/104/71.
        species=self.game.resolve('species','Infernape')
        values=self.game.stats(species,59,dict.fromkeys(['hp','atk','def','spe','spa','spd'],20),
              dict.fromkeys(['hp','atk','def','spe','spa','spd'],48),14)
        self.assertEqual(values,{'hp':177,'atk':146,'def':107,'spe':166,'spa':146,'spd':96})
        # Donor Gengar is a known in-game cached-stat vector, all six values agree.
        donor=inspect_save(self.base,self.game,strict_bag=False)['party'][0]
        if donor['species'] == 'Gengar' and donor['level'] == 100:
            self.assertEqual(self.game.stats(self.game.resolve('species','Gengar'),100,donor['ivs'],donor['evs'],10),donor['stats'])

    def test_battle_style_changes_only_option_bit_and_checksum(self):
        spec=copy.deepcopy(self.specs[0])
        spec['battle_style']='set';set_save,set_audit=build_save(self.base,spec,self.game)
        spec['battle_style']='shift';shift_save,shift_audit=build_save(self.base,spec,self.game)
        self.assertEqual(set_audit['battle_style'],'set')
        self.assertEqual(shift_audit['battle_style'],'shift')
        offset=shift_audit['general_offset']
        self.assertEqual(u16(set_save,offset+0x60)^u16(shift_save,offset+0x60),0x40)
        self.assertEqual(set_audit['party'],shift_audit['party'])
        self.assertEqual(set_audit['bag'],shift_audit['bag'])
        self.assertEqual(set_save[:offset+0x60],shift_save[:offset+0x60])
        self.assertEqual(set_save[offset+0x62:offset+GENERAL_SIZE-2],shift_save[offset+0x62:offset+GENERAL_SIZE-2])
        spec['battle_style']='unknown'
        with self.assertRaises(ValueError):build_save(self.base,spec,self.game)

    def test_all_32_personality_shuffle_values(self):
        plain=bytearray(decrypt_pk4(self.base[PARTY_OFFSET:PARTY_OFFSET+PARTY_SIZE]))
        for shuffle in range(32):
            put32(plain,0,(0x12345678&~0x3e000)|(shuffle<<13))
            raw=encrypt_pk4(plain)
            decoded=decrypt_pk4(raw)
            self.assertEqual(decoded[8:],plain[8:])
            self.assertEqual(reference_decode(raw)['species'],94)
            self.assertEqual(reference_decode(raw)['moves'],[247,87,95,240])

    def test_rom_data_is_original_diamond(self):
        self.assertEqual(self.game.data['moves'][95]['accuracy'],70) # not Platinum's 60
        self.assertEqual(self.game.data['species'][94]['abilities'],[26,0]) # Levitate, not modern Cursed Body
        self.assertEqual(self.game.data['moves'][57]['power'],95) # Gen4 Surf
        self.assertEqual(self.game.data['items'][158]['pocket'],4) # Sitrus Berry, indirect index135
        self.assertEqual(self.game.data['items'][158]['resource_index'],135)
        self.assertEqual(self.game.data['items'][234]['held_effect'],68) # Leftovers, indirect index211

    def test_preserves_environment_storage_and_backups(self):
        for save,audit in self.outputs:
            self.assertEqual(save[GENERAL_SIZE:],self.base[GENERAL_SIZE:])
            self.assertEqual(save[0xd9c:GENERAL_SIZE-2],self.base[0xd9c:GENERAL_SIZE-2])
            self.assertEqual((audit['map'],audit['x'],audit['z']),(185,8,18))
            self.assertEqual(save[GENERAL_SIZE-20:GENERAL_SIZE-2],self.base[GENERAL_SIZE-20:GENERAL_SIZE-2])
            self.assertEqual(save[0x64:0x90],self.base[0x64:0x90])

    def test_secondary_half_selection_and_preservation(self):
        base=bytearray(self.base)
        base[HALF_SIZE:HALF_SIZE+GENERAL_SIZE]=base[:GENERAL_SIZE]
        put32(base,HALF_SIZE+GENERAL_SIZE-20,u32(base,GENERAL_SIZE-20)+1)
        self.assertEqual(active_offset(base),HALF_SIZE)
        out,audit=build_save(bytes(base),self.specs[0],self.game)
        self.assertEqual(audit['general_offset'],HALF_SIZE)
        self.assertEqual(out[:HALF_SIZE],base[:HALF_SIZE])
        self.assertEqual(audit['party'][0]['species'],self.specs[0]['party'][0]['species'])

    def test_counter_wrap_rules(self):
        self.assertLess(newer(0xffffffff,1),0)
        self.assertGreater(newer(1,0xffffffff),0)
        self.assertGreater(newer(0xffffffff,0xfffffffe),0)
        self.assertEqual(newer(3,3),0)

    def test_corruption_is_detected(self):
        save=bytearray(self.outputs[0][0]);save[0x1238]^=1
        with self.assertRaisesRegex(ValueError,'checksum'):inspect_save(save,self.game)
        save=bytearray(self.outputs[0][0]);save[0x98+40]^=1
        put16(save,GENERAL_SIZE-2,binascii.crc_hqx(save[:GENERAL_SIZE-20],0xffff))
        with self.assertRaisesRegex(ValueError,'Pokemon checksum'):inspect_save(save,self.game)

    def test_rejects_invalid_fixture_specs(self):
        changes=[('nickname','NICK'),('ability','Multiscale'),('level',101),('ivs',dict.fromkeys(['hp','atk','def','spe','spa','spd'],32)),('evs',dict.fromkeys(['hp','atk','def','spe','spa','spd'],255))]
        for key,value in changes:
            team=copy.deepcopy(self.specs[0]);team['party'][0][key]=value
            with self.subTest(key=key),self.assertRaises(ValueError):build_save(self.base,team,self.game)
        team=copy.deepcopy(self.specs[0]);team['bag']={'Hyper Potion':1000}
        with self.assertRaises(ValueError):build_save(self.base,team,self.game)

    def test_ram_encrypted_and_both_lock_states(self):
        save,audit=self.outputs[0]
        raw=save[PARTY_OFFSET:PARTY_OFFSET+PARTY_SIZE]
        decoded=decode_pokemon_memory(raw,self.game)
        self.assertEqual(decoded['species'],self.specs[0]['party'][0]['species'])
        self.assertFalse(decoded['box_lock'])
        self.assertTrue(decoded['checksum_valid'])
        for flags in (2,3):
            ram=bytearray(raw)
            ram[8:136]=crypt_words(ram[8:136],u16(ram,6))
            if flags&1:ram[136:]=crypt_words(ram[136:],u32(ram))
            put16(ram,4,flags)
            value=decode_pokemon_memory(bytes(ram),self.game)
            self.assertEqual(value['species'],decoded['species'])
            self.assertEqual(value['moves'],decoded['moves'])
            self.assertEqual(value['stats'],decoded['stats'])
            self.assertEqual(value['hp'],decoded['hp'])
            self.assertEqual(value['level'],decoded['level'])
            self.assertTrue(value['checksum_valid'])
            with self.assertRaisesRegex(ValueError,'On-disk'):decode_pokemon(bytes(ram),self.game)
        party=save[0x90:0x620]
        group=decode_party_memory(party,self.game)
        self.assertEqual(group['count'],6)
        self.assertEqual([p['species'] for p in group['party']],[p['species'] for p in audit['party']])

    def test_ram_pending_checksum_and_invalid_locks(self):
        raw=self.outputs[0][0][PARTY_OFFSET:PARTY_OFFSET+PARTY_SIZE]
        ram=bytearray(raw);ram[8:136]=crypt_words(ram[8:136],u16(ram,6));ram[136:]=crypt_words(ram[136:],u32(ram));put16(ram,4,3)
        # Simulate a legitimate field change while locked; header checksum not refreshed yet.
        from cynthiabench.save_editor import BLOCK_ORDERS
        a=BLOCK_ORDERS[((u32(ram)>>13)&31)%24][0]
        ram[8+a*32+12]=190 # friendship field in physical blockA
        value=decode_pokemon_memory(bytes(ram),self.game)
        self.assertEqual(value['friendship'],190)
        self.assertFalse(value['checksum_valid']);self.assertTrue(value['checksum_pending'])
        for flags in (1,4,8):
            broken=bytearray(raw);put16(broken,4,flags)
            with self.assertRaises(ValueError):decode_pokemon_memory(bytes(broken),self.game)
        broken=bytearray(raw);broken[30]^=1
        with self.assertRaisesRegex(ValueError,'checksum'):decode_pokemon_memory(bytes(broken),self.game)

    def test_live_bag_reader_matches_all_fixtures(self):
        for team,(save,audit) in zip(self.specs,self.outputs):
            result=decode_bag_memory(save[0x624:0xd98],self.game)
            self.assertEqual(result['quantities'],team['bag'])
            self.assertEqual(result['warnings'],[])
            self.assertEqual(result['registered_item_id'],0)
        wrong=bytearray(self.outputs[0][0][0x624:0xd98]);put16(wrong,0,25);put16(wrong,2,2)
        with self.assertRaises(ValueError):decode_bag_memory(wrong,self.game)

    def test_reproducible_generation(self):
        data,audit=build_save(self.base,self.specs[0],self.game)
        self.assertEqual(data,self.outputs[0][0])
        self.assertEqual(audit,self.outputs[0][1])

if __name__=='__main__':unittest.main()
