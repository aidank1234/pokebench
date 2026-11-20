import struct,sys,unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'src'))
from cynthiabench.rng import *

class RngLocatorTests(unittest.TestCase):
    def fixture(self):
        ram=bytearray(RAM_SIZE);pos=0x1b9ec;addr=0x021c4d48
        ram[pos-24:pos-16]=GET_CODE;struct.pack_into('<I',ram,pos-16,addr)
        ram[pos-12:pos-4]=SET_CODE;struct.pack_into('<I',ram,pos-4,addr)
        ram[pos:pos+24]=RANDOM_CODE
        struct.pack_into('<III',ram,pos+24,addr,LCG_MULTIPLIER,LCG_INCREMENT)
        struct.pack_into('<I',ram,addr-RAM_BASE,0x12345678)
        return ram
    def test_verified_signature(self):
        ram=self.fixture();result=locate_lcrng(ram)
        self.assertEqual(result['state_address'],0x021c4d48)
        self.assertEqual(result['random_function'],0x0201b9ec)
        self.assertEqual(read_lcrng(ram),0x12345678)
        self.assertEqual(advance_lcrng(0),0x6073)
        self.assertEqual(advance_lcrng(0,2),0xe97e7b6a)
    def test_corruption_is_rejected(self):
        for pos in [0x1b9ec,0x1b9d4,0x1b9dc,0x1ba08,0x1ba0c]:
            ram=self.fixture();ram[pos]^=1
            with self.assertRaises(ValueError):locate_lcrng(ram)
    def test_real_ram_if_available(self):
        path=ROOT/'work/emulator-probe/ram.bin'
        if not path.exists():self.skipTest('No runtime RAM snapshot')
        result=locate_lcrng(path.read_bytes())
        self.assertEqual(result['state_address'],0x021c4d48)
if __name__=='__main__':unittest.main()
