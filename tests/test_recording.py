import hashlib,json,math,struct,sys,tempfile,unittest,wave
from pathlib import Path
from types import SimpleNamespace
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'src'))
from cynthiabench.recording import Recorder,RecordingError,probe,DEFAULT_FPS,DEFAULT_SAMPLE_RATE,make_highspeed


def emulator(width=256,height=384,padding=0):
    return SimpleNamespace(width=width,height=height,pitch=width*4+padding,pixels=None,
      pixel_format=1,frame=1000,buttons=set(),touch=None,
      get_av_info=lambda:{'fps':DEFAULT_FPS,'sample_rate':DEFAULT_SAMPLE_RATE,'base_width':width,'base_height':height})


class RecordingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import shutil
        if not all(shutil.which(command) for command in ('ffmpeg', 'ffprobe')):
            raise unittest.SkipTest('FFmpeg and ffprobe are required for encoding tests')
    def test_eighty_real_encoded_frames_with_audio_and_exact_clock(self):
        with tempfile.TemporaryDirectory() as directory:
            rec=Recorder(directory);emu=emulator();sample=0
            for i in range(80):
                target=round((i+1)*DEFAULT_SAMPLE_RATE/DEFAULT_FPS)
                pcm=bytearray()
                for n in range(sample,target):
                    tone=round(1500*math.sin(2*math.pi*440*n/DEFAULT_SAMPLE_RATE))
                    pcm.extend(struct.pack('<hh',tone,tone))
                rec.audio(bytes(pcm));sample=target
                emu.frame+=1;emu.pixels=bytes((i*3%256,80,255-i*3%256,0))*(256*384)
                emu.buttons={8} if i<2 else set();emu.touch=(80,120) if i==5 else None
                rec.frame(emu)
            info=rec.close();self.assertEqual(info['status'],'complete')
            self.assertEqual(info['frames'],80);self.assertEqual(info['first_emulator_frame'],1001)
            self.assertEqual(info['last_emulator_frame'],1080)
            self.assertAlmostEqual(info['game_duration_seconds'],80/DEFAULT_FPS,places=9)
            self.assertEqual(info['audio_samples'],sample)
            streams=probe(Path(directory)/'replay.mp4')['streams']
            video=next(s for s in streams if s['codec_type']=='video')
            audio=next(s for s in streams if s['codec_type']=='audio')
            self.assertEqual(int(video['nb_read_frames']),80)
            self.assertEqual((video['width'],video['height']),(256,384))
            self.assertEqual(audio['channels'],2);self.assertEqual(audio['codec_name'],'aac')
            self.assertLess(abs(float(video['duration'])-float(audio['duration'])),.12)
            with wave.open(str(Path(directory)/'audio.wav')) as wav:
                self.assertEqual(wav.getframerate(),32768);self.assertEqual(wav.getnframes(),sample)
                self.assertEqual(wav.getnchannels(),2);self.assertEqual(wav.getsampwidth(),2)
            events=[json.loads(x) for x in (Path(directory)/'events.jsonl').read_text().splitlines()]
            frames=[x for x in events if x['type']=='frame']
            self.assertEqual(len(frames),80);self.assertEqual(frames[0]['buttons'],['A'])
            self.assertEqual(frames[5]['touch'],[80,120])
            self.assertEqual([x['recorded_frames'] for x in frames],list(range(1,81)))
            fast=make_highspeed(Path(directory)/'replay.mp4',Path(directory)/'replay-4x.mp4',4,2)
            self.assertAlmostEqual(fast['duration_seconds'],80/DEFAULT_FPS/4,delta=.12)
            fast_video=next(s for s in probe(Path(directory)/'replay-4x.mp4')['streams'] if s['codec_type']=='video')
            self.assertEqual((fast_video['width'],fast_video['height']),(512,768))
            self.assertLess(int(fast_video['nb_read_frames']),80)
            self.assertEqual(rec.close(),info) # idempotent finalization
            with self.assertRaises(FileExistsError):Recorder(directory)


    def test_padded_rows_and_silent_recording(self):
        with tempfile.TemporaryDirectory() as directory:
            rec=Recorder(directory);emu=emulator(64,48,8);expected=hashlib.sha256()
            for i in range(4):
                row=bytes((20,40,i*60,0))*64
                emu.pixels=(row+b'PADDING!')*48;emu.frame+=1;rec.frame(emu);expected.update(row*48)
            info=rec.close();self.assertEqual(info['raw_video_sha256'],expected.hexdigest())
            self.assertEqual(info['audio_samples'],0);self.assertEqual(info['frames'],4)
            self.assertFalse(any(s['codec_type']=='audio' for s in probe(Path(directory)/'replay.mp4')['streams']))

    def test_frame_gap_is_an_explicit_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            rec=Recorder(directory);emu=emulator(64,48);emu.pixels=bytes(64*48*4)
            rec.frame(emu);emu.frame+=2
            with self.assertRaisesRegex(RecordingError,'discontinuity'):rec.frame(emu)
            with self.assertRaises(RecordingError):rec.close()
            meta=json.loads((Path(directory)/'recording.json').read_text())
            self.assertEqual(meta['status'],'recording_error');self.assertEqual(meta['frames'],1)
            self.assertTrue((Path(directory)/'.video.partial.mp4').exists())

    def test_audio_callback_errors_are_not_silently_lost(self):
        with tempfile.TemporaryDirectory() as directory:
            rec=Recorder(directory);rec.audio(b'bad')
            with self.assertRaisesRegex(RecordingError,'PCM'):rec.frame(emulator())
            with self.assertRaises(RecordingError):rec.close()
            self.assertEqual(json.loads((Path(directory)/'recording.json').read_text())['status'],'recording_error')

    def test_large_audio_clock_mismatch_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            rec=Recorder(directory);emu=emulator(64,48);emu.pixels=bytes(64*48*4)
            rec.audio(bytes(32768*4)) # one second audio, one frame video
            rec.frame(emu)
            with self.assertRaisesRegex(RecordingError,'clock mismatch'):rec.close()

if __name__=='__main__':unittest.main()
