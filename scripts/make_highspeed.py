#!/usr/bin/env python3
"""Create a4x (or specified speed) content replay without modifying its original."""
from pathlib import Path
import argparse,json,sys
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'src'))
from cynthiabench.recording import make_highspeed
p=argparse.ArgumentParser(description=__doc__);p.add_argument('source',type=Path)
p.add_argument('--output',type=Path);p.add_argument('--speed',type=float,default=4)
p.add_argument('--scale',type=int,default=2)
a=p.parse_args();out=a.output or a.source.with_name(a.source.stem+f'-{a.speed:g}x.mp4')
print(json.dumps(make_highspeed(a.source,out,a.speed,a.scale),indent=2))
