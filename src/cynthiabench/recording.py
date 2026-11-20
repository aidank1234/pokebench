"""Record every explicitly executed emulator frame; never record model wait time.

Video uses a bounded ffmpeg stdin pipe with synchronous backpressure. Audio is
streamed to PCM WAV and timing-corrected from the declared core clock when
muxed. No frames are dropped or sampled; errors and frame gaps are explicit.
"""
from __future__ import annotations

from datetime import datetime,timezone
from fractions import Fraction
import hashlib
import json
import math
from pathlib import Path
import shutil
import subprocess
import wave

DEFAULT_FPS=33513982/560190
DEFAULT_SAMPLE_RATE=33513982/1024
WAV_RATE=32768
BUTTON_NAMES={0:'B',1:'Y',2:'SELECT',3:'START',4:'UP',5:'DOWN',6:'LEFT',7:'RIGHT',8:'A',9:'X',10:'L',11:'R'}


class RecordingError(RuntimeError):
    def __init__(self,message,metadata=None):
        super().__init__(message);self.metadata=metadata


def probe(path: Path) -> dict:
    binary=shutil.which('ffprobe')
    if not binary:raise RecordingError('ffprobe is unavailable')
    result=subprocess.run([binary,'-v','error','-count_frames','-show_streams','-show_format','-of','json',str(path)],capture_output=True,text=True,timeout=120)
    if result.returncode:raise RecordingError(f'ffprobe failed: {result.stderr[-2000:]}')
    return json.loads(result.stdout)


class Recorder:
    def __init__(self,directory: Path|str):
        self.directory=Path(directory).resolve();self.directory.mkdir(parents=True,exist_ok=True)
        self.ffmpeg=shutil.which('ffmpeg')
        if not self.ffmpeg or not shutil.which('ffprobe'):raise RecordingError('ffmpeg and ffprobe are required')
        # Do not silently replace the recording of an earlier trial.
        for name in ['replay.mp4','audio.wav','events.jsonl','recording.json','.video.partial.mp4','.mux.partial.mp4']:
            if (self.directory/name).exists():raise FileExistsError(self.directory/name)
        self.frames=0;self.audio_samples=0;self.missing_video_frames=0;self.first_emulator_frame=None;self.last_emulator_frame=None
        self.fps=DEFAULT_FPS;self.sample_rate=DEFAULT_SAMPLE_RATE;self.av_info=None
        self.width=256;self.height=384;self._process=None;self._closed=False;self._metadata=None
        self._errors=[];self._frame_hash=hashlib.sha256();self._audio_hash=hashlib.sha256()
        self._events=(self.directory/'events.jsonl').open('x',encoding='utf8')
        self._stderr=(self.directory/'ffmpeg.log').open('x',encoding='utf8')
        self._wav=wave.open(str(self.directory/'audio.wav'),'wb');self._wav.setnchannels(2);self._wav.setsampwidth(2);self._wav.setframerate(WAV_RATE)
        self.started_utc=datetime.now(timezone.utc).isoformat()
        self.event('recording_started',capture='every retro_run; wall-clock model waits excluded')

    def event(self,event_type: str,**details):
        if self._closed:raise RecordingError('Recorder is closed')
        payload={'type':event_type,'recorded_frames':self.frames,'emulator_frame':self.last_emulator_frame,**details}
        self._events.write(json.dumps(payload,separators=(',',':'),ensure_ascii=False)+'\n')

    def _fail(self,message):
        self._errors.append(str(message));raise RecordingError(str(message))

    def _start_video(self,emulator):
        if hasattr(emulator,'get_av_info'):
            self.av_info=emulator.get_av_info()
            self.fps=float(self.av_info['fps']);self.sample_rate=float(self.av_info['sample_rate'])
        if not 1<=self.fps<=240 or not 1000<=self.sample_rate<=192000:
            self._fail('Invalid libretro frame/sample clock')
        if emulator.width and emulator.height:self.width,self.height=int(emulator.width),int(emulator.height)
        if self.width%2 or self.height%2:self._fail('H264 yuv420p capture requires even dimensions')
        rate=Fraction(self.fps).limit_denominator(1000000)
        command=[self.ffmpeg,'-hide_banner','-loglevel','warning','-nostdin','-y',
            '-f','rawvideo','-pixel_format','bgr0','-video_size',f'{self.width}x{self.height}',
            '-framerate',f'{rate.numerator}/{rate.denominator}','-i','pipe:0','-an',
            '-c:v','libx264','-preset','veryfast','-crf','18','-pix_fmt','yuv420p',
            '-fps_mode','passthrough','-movflags','+faststart',str(self.directory/'.video.partial.mp4')]
        self._process=subprocess.Popen(command,stdin=subprocess.PIPE,stdout=subprocess.DEVNULL,stderr=self._stderr)
        self.event('capture_clock',fps=self.fps,fps_rational=f'{rate.numerator}/{rate.denominator}',
                   audio_sample_rate=self.sample_rate,wav_container_rate=WAV_RATE,
                   width=self.width,height=self.height,av_info=self.av_info)

    def frame(self,emulator):
        if self._closed:raise RecordingError('Recorder is closed')
        if self._errors:raise RecordingError(self._errors[-1])
        try:
            number=int(emulator.frame)
            if self.last_emulator_frame is not None and number!=self.last_emulator_frame+1:
                self._fail(f'Emulator frame discontinuity: expected{self.last_emulator_frame+1}, received{number}')
            if self._process is None:self._start_video(emulator)
            if self._process.poll() is not None:self._fail(f'ffmpeg exited early with status{self._process.returncode}')
            missing=emulator.pixels is None
            if missing:
                self.missing_video_frames+=1
                pixels=bytes(self.width*self.height*4)
            else:
                if (emulator.width,emulator.height)!=(self.width,self.height):self._fail('Video geometry changed during trial')
                if getattr(emulator,'pixel_format',1)!=1:self._fail('Expected libretro XRGB8888/BGRX pixels')
                pitch=int(emulator.pitch);row_size=self.width*4
                if pitch<row_size or len(emulator.pixels)!=pitch*self.height:self._fail('Invalid video buffer length/pitch')
                pixels=bytes(emulator.pixels) if pitch==row_size else b''.join(emulator.pixels[y*pitch:y*pitch+row_size] for y in range(self.height))
            # The pipe/stdio buffers are bounded. A slow encoder blocks emulation
            # rather than dropping frames or growing an unbounded work queue.
            count=self._process.stdin.write(pixels)
            if count!=len(pixels):self._fail('Short write to ffmpeg video pipe')
            self._frame_hash.update(pixels)
            if self.first_emulator_frame is None:self.first_emulator_frame=number
            self.last_emulator_frame=number;self.frames+=1
            buttons=[BUTTON_NAMES.get(x,str(x)) for x in sorted(getattr(emulator,'buttons',()))]
            self.event('frame',buttons=buttons,touch=getattr(emulator,'touch',None),missing_video=missing)
        except RecordingError:raise
        except Exception as exc:self._fail(f'Video capture failure: {exc}')

    def audio(self,pcm: bytes):
        # ctypes callback exceptions are otherwise printed and swallowed. Store
        # any failure so frame()/close() reliably turn it into an episode error.
        if self._closed:
            self._errors.append('Audio received after recorder close');return
        if self._errors:return
        try:
            if len(pcm)%4:raise ValueError('Stereo16-bit PCM length must be divisible by4')
            self._wav.writeframesraw(pcm);self.audio_samples+=len(pcm)//4;self._audio_hash.update(pcm)
        except Exception as exc:self._errors.append(f'Audio capture failure: {exc}')

    def close(self) -> dict:
        if self._closed:
            if self._errors:raise RecordingError('; '.join(self._errors),self._metadata)
            return self._metadata
        self.event('recording_stopped',audio_samples=self.audio_samples)
        self._closed=True
        try:
            self._wav.close()
            if self._process is not None:
                try:self._process.stdin.close()
                except BrokenPipeError:self._errors.append('Video encoder pipe was broken at close')
                try:code=self._process.wait(timeout=120)
                except subprocess.TimeoutExpired:
                    self._process.kill();self._process.wait();code=-9
                if code:self._errors.append(f'Video encoder exited with status{code}')
            if self.frames==0:self._errors.append('No video frames captured')
            video_duration=self.frames/self.fps
            audio_duration=self.audio_samples/self.sample_rate if self.audio_samples else 0
            if self.audio_samples and abs(audio_duration-video_duration)>.12:
                self._errors.append(f'Audio/video clock mismatch: {audio_duration:.6f}s audio vs{video_duration:.6f}s video')
            replay=self.directory/'replay.mp4';source=self.directory/'.video.partial.mp4'
            if not self._errors:
                if self.audio_samples:
                    # WAV only supports an integer rate. Correct the small clock
                    # discrepancy explicitly while preserving pitch, then AAC.
                    tempo=self.sample_rate/WAV_RATE
                    command=[self.ffmpeg,'-hide_banner','-loglevel','warning','-nostdin','-y',
                      '-i',str(source),'-i',str(self.directory/'audio.wav'),
                      '-map','0:v:0','-map','1:a:0','-c:v','copy','-c:a','aac','-b:a','128k',
                      '-af',f'atempo={tempo:.15g},aresample=48000','-movflags','+faststart',
                      str(self.directory/'.mux.partial.mp4')]
                    result=subprocess.run(command,stdout=subprocess.DEVNULL,stderr=self._stderr,timeout=120)
                    if result.returncode:raise RecordingError(f'Audio mux failed with status{result.returncode}')
                    (self.directory/'.mux.partial.mp4').replace(replay)
                else:source.replace(replay)
                info=probe(replay);videos=[s for s in info['streams'] if s['codec_type']=='video']
                if len(videos)!=1 or int(videos[0].get('nb_read_frames',-1))!=self.frames:
                    raise RecordingError('ffprobe decoded frame count does not equal captured frame count')
                if (videos[0]['width'],videos[0]['height'])!=(self.width,self.height):
                    raise RecordingError('Encoded replay geometry mismatch')
                reported_duration=float(videos[0]['duration'])
                if abs(reported_duration-video_duration)>1/self.fps+.001:
                    raise RecordingError('Encoded replay duration differs from emulator frame clock')
                audios=[s for s in info['streams'] if s['codec_type']=='audio']
                if bool(audios)!=bool(self.audio_samples):raise RecordingError('Encoded audio stream presence mismatch')
                if audios and (audios[0]['channels']!=2 or abs(float(audios[0]['duration'])-audio_duration)>.12):
                    raise RecordingError('Encoded audio duration/channel mismatch')
                if self.audio_samples:source.unlink() # only after final replay validates
                (self.directory/'ffprobe.json').write_text(json.dumps(info,indent=2)+'\n')
        except Exception as exc:self._errors.append(str(exc))
        finally:
            self._events.flush();self._events.close();self._stderr.flush();self._stderr.close()
        self._metadata={
            'status':'recording_error' if self._errors else 'complete','errors':self._errors,
            'started_utc':self.started_utc,'closed_utc':datetime.now(timezone.utc).isoformat(),
            'frames':self.frames,'missing_video_frames':self.missing_video_frames,'first_emulator_frame':self.first_emulator_frame,
            'last_emulator_frame':self.last_emulator_frame,'fps':self.fps,
            'game_duration_seconds':self.frames/self.fps,'width':self.width,'height':self.height,
            'audio_samples':self.audio_samples,'core_sample_rate':self.sample_rate,
            'audio_duration_seconds':self.audio_samples/self.sample_rate,'wav_container_sample_rate':WAV_RATE,
            'audio_tempo_correction':self.sample_rate/WAV_RATE,
            'raw_video_sha256':self._frame_hash.hexdigest(),'raw_audio_sha256':self._audio_hash.hexdigest(),
            'replay':'replay.mp4' if (self.directory/'replay.mp4').exists() else None,
            'frame_policy':'one frame per retro_run; synchronous bounded backpressure; no model-wait frames',
            'clock_source':'libretro retro_get_system_av_info' if self.av_info else 'modern melonDS DS declared constants (fallback)',
        }
        if self._metadata['replay']:
            self._metadata['replay_sha256']=hashlib.sha256((self.directory/'replay.mp4').read_bytes()).hexdigest()
        (self.directory/'recording.json').write_text(json.dumps(self._metadata,indent=2)+'\n')
        if self._errors:raise RecordingError('; '.join(self._errors),self._metadata)
        return self._metadata

    def __enter__(self):return self
    def __exit__(self,exc_type,exc,tb):
        if exc is not None:self.event('episode_exception',exception=repr(exc))
        self.close()


def make_highspeed(source: Path|str,output: Path|str,speed: float=4.0,scale: int=2) -> dict:
    """Make a shareable60fps accelerated derivative; preserve the full original."""
    source=Path(source).resolve();output=Path(output).resolve()
    if source==output or output.exists():raise FileExistsError(output)
    if not 1<=speed<=16 or type(scale) is not int or not 1<=scale<=4:
        raise ValueError('Speed must be1..16 and integer scale1..4')
    source_info=probe(source);has_audio=any(s['codec_type']=='audio' for s in source_info['streams'])
    filters=f'setpts=PTS/{speed:g},fps=60,scale=iw*{scale}:ih*{scale}:flags=neighbor'
    command=[shutil.which('ffmpeg'),'-hide_banner','-loglevel','warning','-nostdin','-n',
             '-i',str(source),'-map','0:v:0','-vf',filters,'-c:v','libx264','-preset','veryfast','-crf','18','-pix_fmt','yuv420p']
    if has_audio:
        tempos=[];remaining=speed
        while remaining>2:tempos.append('atempo=2');remaining/=2
        tempos.append(f'atempo={remaining:.12g}')
        command+=['-map','0:a:0','-af',','.join(tempos),'-c:a','aac','-b:a','128k']
    else:command+=['-an']
    output.parent.mkdir(parents=True,exist_ok=True)
    command+=['-movflags','+faststart',str(output)]
    result=subprocess.run(command,capture_output=True,text=True,timeout=600)
    if result.returncode:raise RecordingError(f'Highspeed export failed: {result.stderr[-2000:]}')
    result_info=probe(output)
    metadata={'source':str(source),'source_sha256':hashlib.sha256(source.read_bytes()).hexdigest(),
              'output':str(output),'output_sha256':hashlib.sha256(output.read_bytes()).hexdigest(),
              'speed_factor':speed,'nearest_neighbor_scale':scale,'fps':60,
              'duration_seconds':float(result_info['format']['duration']),
              'purpose':'shareable derivative; authoritative recording retains every original emulator frame'}
    output.with_suffix(output.suffix+'.json').write_text(json.dumps(metadata,indent=2)+'\n')
    return metadata
