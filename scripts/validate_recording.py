#!/usr/bin/env python3
"""Local engineering preflight: synthetic codec tests + passive real-game capture.

Requires locally supplied private assets. No model is invoked, no battle choice
is made, and this produces no benchmark score or release approval.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import uuid

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))


def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--case',default='hard-01-seed1')
    args=parser.parse_args()
    cases=json.loads((ROOT/'provenance/task-manifest.json').read_text())
    case=next((c for c in cases if c['name']==args.case),None)
    if case is None:parser.error('Unknown case')
    subprocess.run([sys.executable,'-m','pytest','-q','tests/test_recording.py'],cwd=ROOT,check=True)
    from cynthiabench.worker import BattleSession
    from cynthiabench.recording import probe
    output=ROOT/'work/recording-preflight'/uuid.uuid4().hex
    session=BattleSession(case['team_id'],case['seed'],'engineering-recording-check',output)
    initial=session.state
    # BattleSession.close records a passive tail while no player input is sent.
    result=session.close()
    recording=result['recording']
    video=output/'video/replay.mp4'
    streams=probe(video)['streams']
    v=next(s for s in streams if s['codec_type']=='video')
    a=next(s for s in streams if s['codec_type']=='audio')
    checks={
        'validated_command_state':initial['menu']=='command' and initial['needs_input'],
        'six_opponents':len(initial['opponent'])==6,
        'full_initial_hp':all(p['hp']==p['stats']['hp'] for p in initial['player']+initial['opponent']),
        'no_strategy_actions':session.actions==0,
        'recording_complete':recording['status']=='complete',
        'decoded_frame_count':int(v['nb_read_frames'])==recording['frames'],
        'stereo_audio':a['channels']==2,
        'replay_hash':sha(video)==recording['replay_sha256'],
    }
    if not all(checks.values()):raise RuntimeError('Recording preflight failed: '+str(checks))
    reports=ROOT/'reports';reports.mkdir(exist_ok=True)
    stamp=datetime.now(timezone.utc).isoformat()
    files=('src/cynthiabench/recording.py','src/cynthiabench/emulator.py','src/cynthiabench/worker.py')
    (reports/'recording-validation.json').write_text(json.dumps({'status':'PASS','captured_at':stamp,
        'scope':'Synthetic codec tests plus passive private game capture; no model calls or gameplay validation.',
        'files_sha256':{name:sha(ROOT/name) for name in files}},indent=2)+'\n')
    (reports/'recording-game-verification.json').write_text(json.dumps({'status':'PASS','captured_at':stamp,
        'checks':checks,'capture':str(output.relative_to(ROOT)),
        'scope':'Engineering capture only; unfinished battle is invalid and never scored.'},indent=2)+'\n')
    print('Recording preflight passed; this does not approve a scored run.')


if __name__=='__main__':main()
