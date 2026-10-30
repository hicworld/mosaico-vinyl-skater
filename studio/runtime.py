"""Exact Mosaico scene preview and a clock tied to the sound card's DAC.

The shipped WASM is compiled from the board's C renderer. Starting the studio
does not require a compiler, the ESP SDK, a connected board, or a network.
"""
from __future__ import annotations

from collections import deque
import hashlib
import json
from pathlib import Path
import struct
import threading
import zlib

import numpy as np
from PIL import Image
import wasmtime

from importer.planner import CLIPS
from importer.themes import (theme_index, theme_from_word, record_index, effect_index,
                             EFFECT_COUNT, THEME_COUNT, LEGACY_THEME_COUNT)

ASSETS = Path(__file__).resolve().parent / "assets"
SAMPLE_RATE = 16000
_HEADER = struct.Struct("<4sHH10I80s")
_CLIP_BEATS = tuple(clip["beats"] for clip in CLIPS)


class StudioRenderer:
    """Run precisely the same RGB565 scene and motion code as the board."""

    def __init__(self, assets: Path | None = None):
        assets = Path(assets or ASSETS)
        manifest = json.loads((assets / "renderer-manifest.json").read_text(encoding="utf8"))
        binary = (assets / "renderer.wasm").read_bytes()
        if hashlib.sha256(binary).hexdigest() != manifest["wasm_sha256"]:
            raise ValueError("电脑模拟器资源校验失败，请重新安装动作工作台。")
        self.manifest = manifest
        self.theme_count = manifest.get("theme_count", LEGACY_THEME_COUNT)
        if type(self.theme_count) is not int or self.theme_count not in (3, 4, THEME_COUNT):
            raise ValueError("电脑模拟器的背景资源数量不正确，请重新安装动作工作台。")
        # Historical four-theme bundles counted the retired mint ID 0.
        self.theme_ids = tuple(range(1, THEME_COUNT+1)) if self.theme_count == THEME_COUNT else (1, 2, 3)
        if (self.theme_count == THEME_COUNT
                and manifest.get("theme_ids", list(self.theme_ids)) != list(self.theme_ids)):
            raise ValueError("电脑模拟器的背景资源编号不正确，请重新安装动作工作台。")
        # Legacy preview bundles predate the independent Neon character.
        self.skin_count = manifest.get("skin_count", 3)
        if (isinstance(self.skin_count, bool) or not isinstance(self.skin_count, int)
                or not 1 <= self.skin_count <= 4):
            raise ValueError("电脑模拟器的人物资源数量不正确，请重新安装动作工作台。")
        self.effect_count = manifest.get("effect_count", 1)
        self.effect_revision = manifest.get("effect_revision", 0)
        if (type(self.effect_count) is not int or self.effect_count not in (1, EFFECT_COUNT)
                or type(self.effect_revision) is not int or self.effect_revision not in (0, 1)
                or (self.effect_revision == 1) != (self.effect_count == EFFECT_COUNT)):
            raise ValueError("电脑模拟器的脚边特效资源版本不正确，请重新安装动作工作台。")
        self._lock = threading.RLock()
        self._engine = wasmtime.Engine()
        self._store = wasmtime.Store(self._engine)
        linker = wasmtime.Linker(self._engine)
        linker.define_wasi()
        self._store.set_wasi(wasmtime.WasiConfig())
        self._instance = linker.instantiate(self._store, wasmtime.Module(self._engine, binary))
        self._exports = self._instance.exports(self._store)
        self._memory = self._exports["memory"]
        self._loaded = False
        self.sample_count = 0
        self.sample_rate = SAMPLE_RATE
        self.title = ""
        self.pcm = np.empty(0, dtype=np.int16)
        self.beats = []
        self.plans = []

    def _call(self, name, *arguments):
        return self._exports[name](self._store, *arguments)

    def set_package(self, packet: bytes):
        packet = bytes(packet)
        if not 128 <= len(packet) <= 64 * 1024 * 1024:
            raise ValueError("歌曲包长度不符合设备格式。")
        header = _HEADER.unpack_from(packet)
        self._require_theme(theme_index(theme_from_word(header[12])))
        with self._lock:
            self._loaded = False
            self._memory.write(self._store, packet, self._call("input_ptr"))
            if self._call("load_package", len(packet)) != 1:
                raise ValueError("设备解析器拒绝了这个歌曲包，请检查拍点、动作覆盖和 CRC。")
            if self._call("init_scene") != 1:
                raise RuntimeError("无法初始化设备画面。")
            self.sample_count, beat_count, plan_count = header[4:7]
            self.beats = [struct.unpack_from("<IHH", packet, header[7] + i * 8)
                          for i in range(beat_count)]
            self.plans = [struct.unpack_from("<IIHBBHH", packet, header[8] + i * 16)
                          for i in range(plan_count)]
            self.pcm = np.frombuffer(packet, dtype="<i2", offset=header[9]).copy()
            self.title = header[-1].split(b"\0", 1)[0].decode("utf8")
            self._loaded = True

    def _require_theme(self, theme):
        if theme not in self.theme_ids:
            raise ValueError("雪山、森林与风暴背景需要 1.12.0 或更新的工作台，请更新电脑模拟器资源。")

    @property
    def duration(self):
        return self.sample_count / self.sample_rate

    @staticmethod
    def _sample(sample):
        return max(0, min(int(sample), (1 << 63) - 1))

    @property
    def package_theme(self) -> int:
        with self._lock:
            if not self._loaded:
                raise RuntimeError("请先加载歌曲或动作演示。")
            return int(self._call("get_package_theme"))

    @property
    def package_record(self) -> int:
        with self._lock:
            if not self._loaded:
                raise RuntimeError("请先加载歌曲或动作演示。")
            return int(self._call("get_package_record"))

    @property
    def package_effect(self) -> int:
        with self._lock:
            if not self._loaded:
                raise RuntimeError("请先加载歌曲或动作演示。")
            return int(self._call("get_package_effect")) if self.effect_revision else 0

    def render(self, sample: int, skin=0, playing=True, theme=None, record_id=None,
               effect_id=None) -> Image.Image:
        if isinstance(skin, np.integer):
            skin = int(skin)
        if isinstance(skin, bool) or not isinstance(skin, int) or not 0 <= skin < self.skin_count:
            raise ValueError(f"人物编号必须是 0 至 {self.skin_count - 1} 的整数。")
        if theme is not None:
            if isinstance(theme, np.integer):
                theme = int(theme)
            theme = theme_index(theme)
            self._require_theme(theme)
        if record_id is not None:
            if isinstance(record_id, np.integer):
                record_id = int(record_id)
            # Paired scene colors are transient preview overrides. They are
            # not additional manual/project palette entries or package IDs.
            scene_revision = self.manifest.get("scene_revision", 0)
            paired_record = (type(scene_revision) is int and scene_revision >= 3
                             and self.theme_count == THEME_COUNT
                             and type(record_id) is int and 4 <= record_id <= 9)
            if not paired_record:
                record_id = record_index(record_id)
        if effect_id is not None:
            if isinstance(effect_id, np.integer):
                effect_id = int(effect_id)
            effect_id = effect_index(effect_id)
            if not self.effect_revision and effect_id:
                raise ValueError("当前模拟器不支持脚边特效选择，请更新到 1.9.0 或更新工作台。")
        with self._lock:
            if not self._loaded:
                raise RuntimeError("请先加载歌曲或动作演示。")
            arguments = (self._sample(sample), int(skin), 0, int(playing))
            if self.effect_revision:
                selected_theme = self.package_theme if theme is None else theme
                selected_record = self.package_record if record_id is None else record_id
                selected_effect = self.package_effect if effect_id is None else effect_id
                pointer = self._call("render_frame_effect", *arguments,
                                     selected_theme, selected_record, selected_effect)
            elif theme is None and record_id is None:
                pointer = self._call("render_frame", *arguments)
            else:
                selected_theme = self.package_theme if theme is None else theme
                selected_record = self.package_record if record_id is None else record_id
                pointer = self._call("render_frame_style", *arguments, selected_theme, selected_record)
            if not pointer:
                raise RuntimeError("设备画面渲染失败。")
            raw = self._memory.read(self._store, pointer, pointer + 480 * 488 * 2)
            pixels = np.frombuffer(raw, dtype="<u2").reshape(480, 488)[:, :480]
            rgb = np.stack((((pixels >> 11) & 31) * 255 // 31,
                            ((pixels >> 5) & 63) * 255 // 63,
                            (pixels & 31) * 255 // 31), axis=2).astype(np.uint8)
            return Image.fromarray(rgb)

    def pose(self, sample: int) -> dict:
        with self._lock:
            if not self._loaded:
                raise RuntimeError("请先加载歌曲或动作演示。")
            pointer = self._call("evaluate", self._sample(sample))
            if not pointer:
                raise RuntimeError("设备动作计算失败。")
            values = struct.unpack("<II7f", self._memory.read(self._store, pointer, pointer + 36))
            return dict(zip(("clip", "plan", "phase", "beat_position", "beat_phase",
                             "energy", "radius", "u", "v"), values))

    def close(self):
        # Release WASM's large song/scene buffers when a preview window closes.
        with self._lock:
            self._loaded = False
            self._instance = self._exports = self._memory = None
            self._store = self._engine = None


def make_clip_demo(clip_id: int, bpm=120.0, repetitions=2, **_ignored) -> bytes:
    """A valid, silent VSK2 package that repeats one complete library action."""
    if isinstance(clip_id, bool) or not isinstance(clip_id, int) or clip_id not in range(len(CLIPS)) or not 40 <= float(bpm) <= 240:
        raise ValueError("动作编号或演示速度不正确。")
    beats_per_clip = _CLIP_BEATS[clip_id]
    repetitions = max(1, min(16, int(repetitions)))
    repetitions = max(repetitions, int(np.ceil(4 * float(bpm) / 60 / beats_per_clip)))
    beat_count = beats_per_clip * repetitions
    samples = [round(i * SAMPLE_RATE * 60 / float(bpm)) for i in range(beat_count + 1)]
    total = samples[-1]
    beats = b"".join(struct.pack("<IHH", sample, 50000,
                                (1 if i % 4 == 0 else 0) | (2 if i == beat_count else 0))
                     for i, sample in enumerate(samples))
    plans = b"".join(struct.pack("<IIHBBHH", samples[i], samples[i + beats_per_clip],
                                 i, beats_per_clip, clip_id, 45000, 0)
                     for i in range(0, beat_count, beats_per_clip))
    payload = beats + plans + bytes(total * 2)
    return _HEADER.pack(b"VSK2", 2, 128, SAMPLE_RATE, total, beat_count + 1, repetitions,
                        128, 128 + len(beats), 128 + len(beats) + len(plans),
                        128 + len(payload), zlib.crc32(payload), 0,
                        ("Action demo %d" % clip_id).encode().ljust(80, b"\0")) + payload


class AudioTransport:
    """Mono PCM playback with a position measured at the output DAC.

    No audio device means play() returns False and .error explains the problem.
    There is deliberately no wall-clock imitation of successful audio playback.
    An optional injected sounddevice-compatible module enables deterministic
    tests without producing sound or recording from any microphone.
    """

    def __init__(self, pcm=None, samplerate=SAMPLE_RATE, *, audio_module=None):
        self.sample_rate = int(samplerate)
        if self.sample_rate <= 0:
            raise ValueError("音频采样率必须大于零。")
        self._audio_module = audio_module
        self._lock = threading.RLock()
        self._stream = None
        self._playing = False
        self._position = 0
        self._origin = 0
        self._output_frames = 0
        self._anchors = deque(maxlen=64)
        self._end_queued = False
        self._pcm = np.empty(0, dtype=np.int16)
        self.loop = False
        self.error = ""
        self.underruns = 0
        if pcm is not None:
            self.load(pcm)

    def load(self, pcm):
        self.stop()
        array = np.asarray(pcm)
        if array.ndim != 1 or array.dtype.kind != "i" or array.dtype.itemsize != 2:
            raise ValueError("预览音频需要单声道 PCM16 数据。")
        with self._lock:
            self._pcm = np.ascontiguousarray(array, dtype=np.int16).copy()
            self.error = ""

    @property
    def sample_count(self):
        return len(self._pcm)

    @property
    def duration(self):
        return self.sample_count / self.sample_rate

    def _sample_locked(self):
        if self._playing and self._stream is not None and self._anchors:
            try:
                now = float(self._stream.time)
            except Exception:
                return int(self._position)
            # A callback generally runs ahead of the speakers. The newest
            # buffer may still be queued, so locate the buffer audible now.
            position = self._position
            for dac_time, start, end in self._anchors:
                if now < dac_time:
                    break
                position = min(end, start + max(0.0, now - dac_time) * self.sample_rate)
            self._position = max(self._position, position)
        return int(self._position)

    @property
    def sample(self):
        with self._lock:
            return self._sample_locked()

    @property
    def playing(self):
        with self._lock:
            self._sample_locked()
            return bool(self._playing)

    def _callback(self, outdata, frames, timing, status):
        with self._lock:
            if not self._playing:
                outdata.fill(0)
                return
            if getattr(status, "output_underflow", False):
                self.underruns += 1
            ratio = self.sample_rate / self._output_rate
            start = self._origin + self._output_frames * ratio
            positions = start + np.arange(frames, dtype=np.float64) * ratio
            end = start + frames * ratio
            if self.loop:
                positions %= len(self._pcm)
                left = np.floor(positions).astype(np.int64)
                right = (left + 1) % len(self._pcm)
                fraction = positions - left
                mono = self._pcm[left] * (1.0 - fraction) + self._pcm[right] * fraction
            else:
                valid = positions < len(self._pcm)
                left = np.minimum(np.floor(positions).astype(np.int64), len(self._pcm) - 1)
                right = np.minimum(left + 1, len(self._pcm) - 1)
                fraction = positions - np.floor(positions)
                mono = (self._pcm[left] * (1.0 - fraction) + self._pcm[right] * fraction) * valid
                end = min(end, len(self._pcm))
            outdata[:] = (mono / 32768.0)[:, None]
            self._anchors.append((float(timing.outputBufferDacTime), start, end))
            self._output_frames += frames
            if not self.loop and self._origin + self._output_frames * ratio >= len(self._pcm):
                self._end_queued = True
                raise self._audio_module.CallbackStop

    def _finished(self):
        with self._lock:
            if self._playing and self._end_queued:
                self._position = len(self._pcm)
                self._playing = False
            elif self._playing:
                self._sample_locked()
                self._playing = False
                self.error = "系统音频输出已停止，请检查扬声器连接后重新播放。"

    def play(self):
        with self._lock:
            if self._playing:
                return True
            if not len(self._pcm):
                self.error = "请先选择歌曲。"
                return False
        self._dispose_stream()
        try:
            if self._audio_module is None:
                import sounddevice
                self._audio_module = sounddevice
            device = self._audio_module.query_devices(kind="output")
            channels = min(2, int(device["max_output_channels"]))
            if channels < 1:
                raise RuntimeError("系统默认设备没有音频输出。")
            # The host's native rate works with WASAPI shared output as well
            # as devices that refuse a 16 kHz hardware stream.
            self._output_rate = int(round(float(device["default_samplerate"])))
            stream = self._audio_module.OutputStream(
                samplerate=self._output_rate, channels=channels, dtype="float32",
                blocksize=0, latency="low", callback=self._callback,
                finished_callback=self._finished)
            with self._lock:
                if self._position >= len(self._pcm) and not self.loop:
                    self._position = 0
                self._origin = self._position
                self._output_frames = 0
                self._anchors.clear()
                self._end_queued = False
                self._stream = stream
                self._playing = True
                self.error = ""
            stream.start()
            return True
        except Exception as exc:
            with self._lock:
                self._playing = False
                self.error = "无法播放系统音频：%s" % exc
            self._dispose_stream()
            return False

    def pause(self):
        with self._lock:
            self._sample_locked()
            self._playing = False
        self._dispose_stream()

    def stop(self):
        self.pause()
        with self._lock:
            self._position = 0
            self._anchors.clear()

    def seek(self, sample):
        was_playing = self.playing
        self.pause()
        with self._lock:
            self._position = max(0, min(int(sample), len(self._pcm)))
            self._anchors.clear()
        if was_playing:
            return self.play()
        return True

    def _dispose_stream(self):
        with self._lock:
            stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.abort()
            except Exception:
                pass
            try:
                stream.close()
            except Exception:
                pass

    def close(self):
        self.pause()
