"""Deterministic offline MP4 export, independent of Tk and preview playback.

The project's actual PCM samples are the single clock for frames, lyrics,
scene cues, and the muxed soundtrack. A private WASM renderer avoids changing
the package, timeline, or appearance of the editor's live preview.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import wave

from PIL import Image

from importer.analysis import ffmpeg_path
from importer.package import package_bytes
from .lyric_overlay import draw_lyric_overlay
from .lyrics import current_lyric
from .runtime import StudioRenderer
from .scene_auto import SceneAutoController


RATE = 16000


class ExportCancelled(Exception):
    """The user cancelled; no completed destination was committed."""


@dataclass(frozen=True)
class ExportSettings:
    size: int = 480
    fps: int = 30
    start_seconds: float = 0.0
    end_seconds: float | None = None
    include_audio: bool = True
    include_lyrics: bool = True


@dataclass(frozen=True)
class ExportSnapshot:
    title: str
    sample_count: int
    plan_json: str
    pcm_bytes: bytes
    lyrics_json: str
    scene_cues: tuple[int, ...]
    lyric_start_samples: tuple[int, ...]
    auto_mode: str
    auto_character: bool
    skin: int
    theme_id: int
    record_id: int
    effect_id: int
    show_lyrics: bool

    @property
    def duration(self) -> float:
        return self.sample_count / RATE


@dataclass(frozen=True)
class ExportResult:
    path: Path
    frame_count: int
    duration: float
    audio_included: bool
    width: int
    height: int
    fps: int
    start_sample: int
    end_sample: int


def snapshot_project(project, skin: int = 0) -> ExportSnapshot:
    """Capture current placements and settings without retaining live objects.

    The caller must keep the project unchanged while this snapshot is built.
    A modal/busy editor may also capture it on its background export thread.
    Undo history, Tk variables, renderer state and playback state are excluded.
    Immutable PCM bytes and serialized metadata isolate later editor changes.
    """
    if type(skin) is not int or not 0 <= skin <= 3:
        raise ValueError("请选择有效的预览人物。")
    auto = project.scene_auto
    return ExportSnapshot(
        str(project.title), int(project.sample_count),
        json.dumps(project.build_plan(), ensure_ascii=False),
        project.pcm.astype("<i2", copy=False).tobytes(),
        json.dumps(project.lyrics, ensure_ascii=False),
        tuple(int(cue["sample"]) for cue in project.scene_cues["cues"]),
        tuple(project.lyric_start_samples), str(auto["mode"]),
        bool(auto["include_character"]), skin, project.theme_id,
        project.record_id, project.effect_id, bool(project.show_lyrics))


def validate_settings(snapshot: ExportSnapshot, settings: ExportSettings) -> tuple[int, int, int]:
    """Return the exact PCM interval and the required CFR frame count."""
    if not isinstance(snapshot, ExportSnapshot) or not isinstance(settings, ExportSettings):
        raise ValueError("导出参数不完整。")
    if type(settings.size) is not int or settings.size not in (480, 960, 1080):
        raise ValueError("画面尺寸请选择 480、960 或 1080。")
    if type(settings.fps) is not int or settings.fps not in (24, 30):
        raise ValueError("帧率请选择 24 或 30。")
    if type(settings.include_audio) is not bool or type(settings.include_lyrics) is not bool:
        raise ValueError("音乐与歌词选项必须为布尔值。")
    end = snapshot.duration if settings.end_seconds is None else settings.end_seconds
    for value in (settings.start_seconds, end):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError("导出时间必须是有效秒数。")
    if not 0 <= settings.start_seconds < end <= snapshot.duration:
        raise ValueError("导出范围须满足：0 ≤ 开始时间 < 结束时间 ≤ 歌曲时长。")
    start_sample = round(settings.start_seconds * RATE)
    end_sample = min(snapshot.sample_count, round(end * RATE))
    if start_sample >= end_sample:
        raise ValueError("导出片段过短，请增大时间范围。")
    if (end_sample - start_sample) * settings.fps < RATE:
        raise ValueError(f"导出片段至少需要一帧（当前帧率下约 {1 / settings.fps:.3f} 秒）。")
    frames = math.ceil((end_sample - start_sample) * settings.fps / RATE)
    if len(snapshot.pcm_bytes) != snapshot.sample_count * 2:
        raise ValueError("歌曲快照的音频长度不正确。")
    return start_sample, end_sample, frames


def _cancelled(event) -> None:
    if event.is_set():
        raise ExportCancelled("已取消动画导出，原有文件未改动。")


def _stop_process(process) -> None:
    if process.poll() is not None:
        return
    try:
        process.terminate()
        process.wait(timeout=1.0)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=2.0)
    except (OSError, ProcessLookupError):
        pass


def _write_all(stream, data: bytes) -> None:
    pending = memoryview(data)
    while pending:
        count = stream.write(pending)
        if not count:
            raise BrokenPipeError("视频编码器已停止接收画面。")
        pending = pending[count:]


def export_video(snapshot: ExportSnapshot, path, settings: ExportSettings = ExportSettings(),
                 *, progress=lambda _event: None, cancel_event=None,
                 renderer_factory=StudioRenderer) -> ExportResult:
    """Render a CFR H.264/AAC MP4 and atomically commit it only on success.

    Run on a background thread. ``progress`` receives ordinary dictionaries;
    its callback must not touch Tk. Cancellation also terminates FFmpeg from
    a watchdog thread, so a full stdin pipe cannot indefinitely hold a cancel.
    """
    start_sample, end_sample, frame_count = validate_settings(snapshot, settings)
    destination = Path(path).expanduser().resolve()
    if destination.suffix.lower() != ".mp4":
        raise ValueError("动画视频请保存为 .mp4 文件。")
    if not destination.parent.is_dir():
        raise ValueError("保存目录不存在，请重新选择。")
    if destination.is_dir():
        raise ValueError("请选择视频文件路径。")
    cancel = cancel_event if cancel_event is not None else threading.Event()
    _cancelled(cancel)
    encoder = ffmpeg_path()
    started = time.perf_counter()
    duration = (end_sample - start_sample) / RATE
    renderer = None
    process = None
    watcher = None
    finished = threading.Event()
    temporary_manager = None

    def report(stage, percent, completed=0):
        progress(dict(stage=stage, percent=float(percent), completed_frames=completed,
                      total_frames=frame_count, elapsed_seconds=time.perf_counter() - started))

    report("准备歌曲与画面", 0)
    try:
        temporary_manager = tempfile.TemporaryDirectory(prefix=".mosaico-export-", dir=destination.parent)
        temporary = Path(temporary_manager.name)
        video = temporary / "render.mp4"
        log = temporary / "ffmpeg.log"
        audio = temporary / "soundtrack.wav"
        if settings.include_audio:
            with wave.open(str(audio), "wb") as writer:
                writer.setparams((1, 2, RATE, end_sample - start_sample, "NONE", "not compressed"))
                writer.writeframes(snapshot.pcm_bytes[start_sample * 2:end_sample * 2])
        _cancelled(cancel)
        plan = json.loads(snapshot.plan_json)
        lyrics = json.loads(snapshot.lyrics_json)
        packet = package_bytes(plan, snapshot.pcm_bytes)
        _cancelled(cancel)
        renderer = renderer_factory()
        renderer.set_package(packet)
        controller = SceneAutoController().configure(
            theme=snapshot.theme_id, record_id=snapshot.record_id, skin=snapshot.skin,
            song_sample_count=snapshot.sample_count, mode=snapshot.auto_mode,
            include_character=snapshot.auto_character,
            cue_samples=(snapshot.lyric_start_samples if snapshot.auto_mode == "lyric_start"
                         else snapshot.scene_cues), loop=False)
        command = [encoder, "-hide_banner", "-loglevel", "error", "-y",
                   "-f", "rawvideo", "-pix_fmt", "rgb24", "-video_size",
                   f"{settings.size}x{settings.size}", "-framerate", str(settings.fps),
                   "-i", "pipe:0"]
        if settings.include_audio:
            command += ["-i", str(audio), "-map", "0:v:0", "-map", "1:a:0"]
        else:
            command += ["-map", "0:v:0", "-an"]
        command += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                    "-pix_fmt", "yuv420p", "-threads", "2"]
        if settings.include_audio:
            command += ["-c:a", "aac", "-b:a", "128k"]
        # Do not use -shortest: AAC packet boundaries can cut a final
        # video frame. The common PCM range controls both stream lengths.
        command += ["-t", f"{duration:.8f}", "-movflags", "+faststart", str(video)]
        with log.open("wb") as errors:
            _cancelled(cancel)
            process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                       stderr=errors, bufsize=0,
                                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

            def watch_cancel():
                while not finished.wait(.05):
                    if cancel.is_set():
                        _stop_process(process)
                        return

            watcher = threading.Thread(target=watch_cancel, name="MosaicoExportCancel", daemon=True)
            watcher.start()
            report("正在生成动画", 1)
            last_report = 0.0
            try:
                for index in range(frame_count):
                    _cancelled(cancel)
                    sample = min(end_sample - 1, start_sample + round(index * RATE / settings.fps))
                    look = controller.resolve(sample)
                    image = renderer.render(sample, skin=look.skin, playing=True,
                                            theme=look.theme, record_id=look.record_id,
                                            effect_id=snapshot.effect_id)
                    if settings.include_lyrics:
                        image = draw_lyric_overlay(image, current_lyric(
                            lyrics, sample, snapshot.sample_count, loop=False))
                    if image.size != (settings.size, settings.size):
                        mode = Image.Resampling.LANCZOS if settings.size < image.width else Image.Resampling.BILINEAR
                        image = image.resize((settings.size, settings.size), mode)
                    if image.mode != "RGB":
                        image = image.convert("RGB")
                    _cancelled(cancel)
                    _write_all(process.stdin, image.tobytes())
                    now = time.perf_counter()
                    if now - last_report >= .1 or index == frame_count - 1:
                        report("正在生成动画", 1 + 96 * (index + 1) / frame_count, index + 1)
                        last_report = now
                process.stdin.close()
                report("正在完成音乐合成与封装", 98, frame_count)
                return_code = process.wait()
            except (BrokenPipeError, OSError) as error:
                _cancelled(cancel)
                _stop_process(process)
                detail = log.read_text(encoding="utf8", errors="replace")[-1600:]
                raise RuntimeError("视频编码失败：" + (detail.strip() or str(error))) from error
            _cancelled(cancel)
            if return_code:
                detail = log.read_text(encoding="utf8", errors="replace")[-1600:]
                raise RuntimeError("视频编码失败：" + (detail.strip() or f"FFmpeg 返回 {return_code}"))
        if not video.is_file() or video.stat().st_size < 32:
            raise RuntimeError("编码器未生成有效的视频文件。")
        _cancelled(cancel)
        os.replace(video, destination)
        finished.set()
        result = ExportResult(destination, frame_count, duration, settings.include_audio,
                              settings.size, settings.size, settings.fps, start_sample, end_sample)
        report("导出完成", 100, frame_count)
        return result
    finally:
        finished.set()
        if process is not None:
            if process.stdin is not None and not process.stdin.closed:
                try:
                    process.stdin.close()
                except OSError:
                    pass
            _stop_process(process)
        if watcher is not None:
            watcher.join(timeout=1.5)
        try:
            if renderer is not None:
                renderer.close()
        finally:
            if temporary_manager is not None:
                temporary_manager.cleanup()
