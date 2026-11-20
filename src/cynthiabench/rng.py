"""Locate Diamond's LCRNG from verified ARM9 instruction/literal signatures.

The battle engine reuses the global LCRNG, reseeding it at battle initialization
and restoring the previous seed on exit. See docs/HARBOR.md.
"""
from __future__ import annotations
import struct

RAM_BASE=0x02000000
RAM_SIZE=0x400000
LCG_MULTIPLIER=0x41C64E6D
LCG_INCREMENT=0x6073
# Thumb LCRandom: load state pointer and multiplier, multiply/add, write state,
# return the high16 bits. Followed by ptr,multiplier,increment literal words.
RANDOM_CODE=bytes.fromhex('054906480a68131c4343054818180860000c0004000c7047')
GET_CODE=bytes.fromhex('014800687047c046')
SET_CODE=bytes.fromhex('014908607047c046')


def locate_lcrng(ram: bytes) -> dict:
    if len(ram)!=RAM_SIZE:raise ValueError('Expected full4MiB main RAM')
    matches=[];start=0
    while True:
        pos=ram.find(RANDOM_CODE,start)
        if pos<0:break
        start=pos+2
        if pos<24 or pos+36>len(ram):continue
        pointer,mult,inc=struct.unpack_from('<III',ram,pos+24)
        if (mult,inc)!=(LCG_MULTIPLIER,LCG_INCREMENT):continue
        if ram[pos-24:pos-16]!=GET_CODE or ram[pos-12:pos-4]!=SET_CODE:continue
        if struct.unpack_from('<I',ram,pos-16)[0]!=pointer or struct.unpack_from('<I',ram,pos-4)[0]!=pointer:continue
        if not RAM_BASE<=pointer<RAM_BASE+RAM_SIZE or pointer%4:continue
        matches.append({'state_address':pointer,'state_offset':pointer-RAM_BASE,
                        'get_function':RAM_BASE+pos-24,'set_function':RAM_BASE+pos-12,
                        'random_function':RAM_BASE+pos,'multiplier':mult,'increment':inc})
    if len(matches)!=1:raise ValueError(f'Expected one verified LCRNG signature, found{len(matches)}')
    return matches[0]


def read_lcrng(ram: bytes) -> int:
    location=locate_lcrng(ram)
    return struct.unpack_from('<I',ram,location['state_offset'])[0]


def advance_lcrng(seed: int,steps: int=1) -> int:
    if not 0<=seed<2**32 or steps<0:raise ValueError('Invalid seed/step count')
    for _ in range(steps):seed=(seed*LCG_MULTIPLIER+LCG_INCREMENT)&0xffffffff
    return seed
