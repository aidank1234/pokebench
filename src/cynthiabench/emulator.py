"""Small, synchronous libretro frontend. One core per process; host use only.

The benchmark agent never imports this module or receives memory/state access.
All game execution advances explicitly in frames, never while the model thinks.
"""
from __future__ import annotations

import ctypes as C
from pathlib import Path
import io
import shutil

ENV = C.CFUNCTYPE(C.c_bool, C.c_uint, C.c_void_p)
VIDEO = C.CFUNCTYPE(None, C.c_void_p, C.c_uint, C.c_uint, C.c_size_t)
AUDIO = C.CFUNCTYPE(None, C.c_int16, C.c_int16)
AUDIO_BATCH = C.CFUNCTYPE(C.c_size_t, C.c_void_p, C.c_size_t)
POLL = C.CFUNCTYPE(None)
INPUT = C.CFUNCTYPE(C.c_int16, C.c_uint, C.c_uint, C.c_uint, C.c_uint)


class GameInfo(C.Structure):
    _fields_ = [("path", C.c_char_p), ("data", C.c_void_p),
                ("size", C.c_size_t), ("meta", C.c_char_p)]


class Variable(C.Structure):
    _fields_ = [("key", C.c_char_p), ("value", C.c_char_p)]



class GameGeometry(C.Structure):
    _fields_ = [("base_width", C.c_uint), ("base_height", C.c_uint),
                ("max_width", C.c_uint), ("max_height", C.c_uint),
                ("aspect_ratio", C.c_float)]


class SystemTiming(C.Structure):
    _fields_ = [("fps", C.c_double), ("sample_rate", C.c_double)]


class SystemAVInfo(C.Structure):
    _fields_ = [("geometry", GameGeometry), ("timing", SystemTiming)]

BUTTONS = {"B": 0, "Y": 1, "SELECT": 2, "START": 3, "UP": 4,
           "DOWN": 5, "LEFT": 6, "RIGHT": 7, "A": 8, "X": 9,
           "L": 10, "R": 11}


class Emulator:
    def __init__(self, core: Path, rom: Path, save: Path, work: Path):
        self.work = Path(work).resolve()
        self.work.mkdir(parents=True, exist_ok=True)
        self._dir = str(self.work).encode()
        self._rompath = str(Path(rom).resolve()).encode()
        destination = self.work / (Path(rom).stem + ".sav")
        if Path(save).resolve() != destination:
            shutil.copyfile(save, destination)
        self.options = {
            b"melonds_console_mode": b"DS",
            b"melonds_boot_directly": b"enabled",
            b"melonds_screen_layout": b"Top/Bottom",
            b"melonds_screen_gap": b"0",
            b"melonds_touch_mode": b"Touch",
            b"melonds_threaded_renderer": b"disabled",
            b"melonds_randomize_mac_address": b"enabled",
            b"melonds_opengl_renderer": b"disabled",
            b"melonds_jit_enable": b"disabled",
            b"melonds_boot_mode": b"direct",
            b"melonds_console_mode": b"DS",
        }
        if "melondsds" in str(core):
            self.options.update({
                b"melonds_console_mode": b"ds",
                b"melonds_touch_mode": b"touch",
                b"melonds_firmware_mode": b"built-in",
                b"melonds_start_time_mode": b"absolute",
                b"melonds_start_time_absolute_year": b"2026",
                b"melonds_start_time_absolute_month": b"11",
                b"melonds_start_time_absolute_day": b"18",
                b"melonds_start_time_absolute_hour": b"12",
                b"melonds_start_time_absolute_minute": b"0",
                b"melonds_start_time_absolute_second": b"0",
                b"melonds_show_cursor": b"disabled",
                b"melonds_render_mode": b"software",
            })
        self.frame = 0
        self.buttons = set()
        self.touch = None
        self.pixels = None
        self.width = self.height = self.pitch = 0
        self.pixel_format = 1
        self.on_frame = None
        self.on_audio = None
        self.halted = False
        self.lib = C.CDLL(str(core))
        self._callbacks = [ENV(self._environment), VIDEO(self._video),
                           AUDIO(self._audio), AUDIO_BATCH(self._audio_batch),
                           POLL(lambda: None), INPUT(self._input)]
        for name, callback in zip(
            ["environment", "video_refresh", "audio_sample", "audio_sample_batch",
             "input_poll", "input_state"], self._callbacks
        ):
            function = getattr(self.lib, "retro_set_" + name)
            function.argtypes = [type(callback)]
            function(callback)
        self.lib.retro_init()
        # The legacy core's synthetic firmware otherwise has an all-FF MAC.
        # Pin the libc RNG used to generate the synthetic MAC (not game RNG).
        C.CDLL(None).srand(0xC17A)
        self.lib.retro_load_game.argtypes = [C.POINTER(GameInfo)]
        self.lib.retro_load_game.restype = C.c_bool
        self._rom = C.create_string_buffer(Path(rom).read_bytes())
        info = GameInfo(self._rompath, C.cast(self._rom, C.c_void_p), len(self._rom)-1, None)
        if not self.lib.retro_load_game(C.byref(info)):
            raise RuntimeError("libretro refused the supplied ROM")
        self.lib.retro_get_memory_data.argtypes = [C.c_uint]
        self.lib.retro_get_memory_data.restype = C.c_void_p
        self.lib.retro_get_memory_size.argtypes = [C.c_uint]
        self.lib.retro_get_memory_size.restype = C.c_size_t
        self.ram_address = self.lib.retro_get_memory_data(2)
        self.ram_size = self.lib.retro_get_memory_size(2)
        assert self.ram_address and self.ram_size == 0x400000
        save_address = self.lib.retro_get_memory_data(0)
        save_size = self.lib.retro_get_memory_size(0)
        if save_address:
            save_data = Path(save).read_bytes()
            if len(save_data) > save_size:
                raise ValueError(f"Save size {len(save_data)} exceeds core SRAM {save_size}")
            C.memmove(save_address, save_data, len(save_data))
        self.lib.retro_serialize_size.restype = C.c_size_t
        for name in ["retro_serialize", "retro_unserialize"]:
            f = getattr(self.lib, name)
            f.argtypes = [C.c_void_p, C.c_size_t]
            f.restype = C.c_bool

    def _environment(self, command, data):
        command &= 0xffff
        if command in (9, 31):  # system/save directory, stable buffer
            C.cast(data, C.POINTER(C.c_char_p))[0] = self._dir
            return True
        if command == 10:  # pixel format
            self.pixel_format = C.cast(data, C.POINTER(C.c_int))[0]
            return self.pixel_format == 1
        if command == 15:
            var = C.cast(data, C.POINTER(Variable)).contents
            if var.key in self.options:
                var.value = self.options[var.key]
                return True
            return False
        if command == 16:  # legacy core options, derive defaults
            values = C.cast(data, C.POINTER(Variable))
            i = 0
            while values[i].key:
                value = values[i].value.split(b"; ", 1)[-1].split(b"|")[0]
                self.options.setdefault(values[i].key, value)
                i += 1
            return True
        if command == 17:
            C.cast(data, C.POINTER(C.c_bool))[0] = False
            return True
        if command == 52:  # core options API version; request legacy options
            C.cast(data, C.POINTER(C.c_uint))[0] = 0
            return True
        if command == 3:
            C.cast(data, C.POINTER(C.c_bool))[0] = True
            return True
        if command == 39:  # language: English
            C.cast(data, C.POINTER(C.c_uint))[0] = 0
            return True
        if command in (1, 6, 11, 18, 32, 34, 35, 37, 53, 55, 65, 69):
            return True
        return False

    def _video(self, data, width, height, pitch):
        if data and data != C.c_void_p(-1).value:
            self.width, self.height, self.pitch = width, height, pitch
            self.pixels = C.string_at(data, height * pitch)

    def _audio(self, left, right):
        if self.on_audio:
            self.on_audio(C.c_int16(left).value.to_bytes(2, "little", signed=True) +
                          C.c_int16(right).value.to_bytes(2, "little", signed=True))

    def _audio_batch(self, data, frames):
        if self.on_audio:
            self.on_audio(C.string_at(data, frames * 4))
        return frames

    def _input(self, port, device, index, ident):
        if port != 0:
            return 0
        if device == 1:
            return int(ident in self.buttons)
        if device == 6 and self.touch is not None:
            x, y = self.touch
            if ident == 0:
                return round(x / max(1, self.width) * 65536 - 32768)
            if ident == 1:
                return round((y + 192) / max(1, self.height) * 65536 - 32768)
            if ident == 2:
                return 1
        return 0

    def get_av_info(self):
        """Return this loaded libretro core's authoritative geometry and clocks."""
        info = SystemAVInfo()
        self.lib.retro_get_system_av_info.argtypes = [C.POINTER(SystemAVInfo)]
        self.lib.retro_get_system_av_info.restype = None
        self.lib.retro_get_system_av_info(C.byref(info))
        return {"base_width": info.geometry.base_width,
                "base_height": info.geometry.base_height,
                "max_width": info.geometry.max_width,
                "max_height": info.geometry.max_height,
                "aspect_ratio": info.geometry.aspect_ratio,
                "fps": info.timing.fps, "sample_rate": info.timing.sample_rate}

    def step(self, frames=1, buttons=(), touch=None):
        self.buttons = {BUTTONS[b.upper()] for b in buttons}
        self.touch = touch
        for _ in range(frames):
            if self.halted:
                break
            self.lib.retro_run()
            self.frame += 1
            if self.on_frame:
                self.on_frame(self)
        self.buttons.clear()
        self.touch = None

    def press(self, button, hold=2, after=60):
        self.step(hold, [button])
        self.step(after)

    def ram(self):
        return C.string_at(self.ram_address, self.ram_size)

    def initialize_u32(self, address, value):
        """Host-only fixture construction; never expose this through agent RPC."""
        if not 0x02000000 <= address <= 0x023ffffc:
            raise ValueError("Outside main RAM")
        C.c_uint32.from_address(self.ram_address + address - 0x02000000).value = value

    def image(self):
        from PIL import Image
        if not self.pixels:
            raise RuntimeError("No video frame yet")
        return Image.frombytes("RGB", (self.width, self.height), self.pixels,
                               "raw", "BGRX", self.pitch)

    def png(self):
        buffer = io.BytesIO()
        self.image().save(buffer, format="PNG")
        return buffer.getvalue()

    def save_state(self, path):
        size = self.lib.retro_serialize_size()
        buffer = C.create_string_buffer(size)
        if not self.lib.retro_serialize(buffer, size):
            raise RuntimeError("Serialization failed")
        Path(path).write_bytes(buffer.raw)

    def load_state(self, path):
        data = Path(path).read_bytes()
        buffer = C.create_string_buffer(data)
        if not self.lib.retro_unserialize(buffer, len(data)):
            raise RuntimeError("State loading failed")

    def close(self):
        self.lib.retro_unload_game()
        self.lib.retro_deinit()
