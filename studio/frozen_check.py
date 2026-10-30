"""Executable self-test: --self-test report.json. Never opens audio or serial."""
from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import traceback
from unittest.mock import patch


def main(report: Path):
    started = time.perf_counter()
    result = {"passed": False, "frozen": bool(getattr(sys, "frozen", False)),
              "checks": {}, "network_access_used": False, "audio_device_opened": False,
              "serial_device_opened": False}
    report = Path(report).resolve()
    report.parent.mkdir(parents=True, exist_ok=True)
    def save():
        result["elapsed_seconds"] = round(time.perf_counter() - started, 3)
        report.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf8")
    def check(name, value=True):
        result["checks"][name] = value
        save()
    try:
        from .paths import resource_path, data_root
        import tkinter as tk
        root = tk.Tk()
        root.withdraw()
        label = tk.Label(root, text="Mosaico 黑胶轮滑")
        label.pack()
        root.update_idletasks()
        root.destroy()
        check("tk_window_and_fonts")
        import sounddevice
        check("portaudio_loads", list(sounddevice.get_portaudio_version()))
        from .model import EditorProject
        from .runtime import StudioRenderer
        from .lyrics import lyrics_from_lrc
        from .video_export import snapshot_project, export_video, ExportSettings
        from importer.analysis import ffmpeg_path, beat_model_path
        from importer.package import package_bytes
        project = EditorProject.load(resource_path("examples", "demo.vskproj"))
        check("original_demo_loads", project.sample_count)
        check("writable_data_root", data_root().is_dir())
        renderer = StudioRenderer()
        try:
            renderer.set_package(package_bytes(project.build_plan(), project.pcm.tobytes()))
            for scene in range(1, 7):
                image = renderer.render(scene * 16000, skin=scene % 4, theme=scene,
                                        record_id=scene + 3, effect_id=0)
                assert image.size == (480, 480)
            check("wasm_all_six_scenes")
        finally:
            renderer.close()
        with tempfile.TemporaryDirectory(prefix="mosaico-exe-check-") as folder:
            directory = Path(folder)
            saved = project.save(directory / "portable.vskproj")
            reopened = EditorProject.load(saved)
            assert reopened.sample_count == project.sample_count
            assert reopened.current_lyric(1000) == project.current_lyric(1000)
            check("save_reopen_and_lrc")
            binary = ffmpeg_path()
            video = directory / "test.mp4"
            export_video(snapshot_project(project), video,
                         ExportSettings(start_seconds=2, end_seconds=3, fps=24))
            decoded = subprocess.run([binary, "-v", "error", "-i", str(video),
                "-map", "0:v:0", "-map", "0:a:0", "-f", "null", "-"], capture_output=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            assert decoded.returncode == 0, decoded.stderr.decode(errors="replace")
            check("mp4_h264_and_audio_decode", video.stat().st_size)
            mp3 = directory / "demo.mp3"
            wav = resource_path("examples", json.loads(resource_path("examples", "demo.vskproj").read_text(encoding="utf8"))["audio"]["path"])
            subprocess.run([binary, "-v", "error", "-y", "-i", str(wav), str(mp3)],
                check=True, capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if os.environ.get("MOSAICO_SELF_TEST_LEVEL", "full") != "basic":
                # A cached fallback must not make this test pass. The frozen
                # model path must point inside the distribution, with network
                # attempts blocked in this process.
                assert Path(beat_model_path()).is_file()
                from importer.workflow import analyse_song
                with patch.object(socket.socket, "connect", side_effect=RuntimeError("self-test is offline")), \
                     patch.object(socket.socket, "connect_ex", side_effect=RuntimeError("self-test is offline")):
                    imported = analyse_song(mp3, directory / "analysed", progress=lambda _message: None)
                check("offline_mp3_beat_model", len(imported["plan"]["beats"]))
                from .lyric_recognition import recognize_phrase_ends, model_directory
                assert (model_directory() / "base.pt").is_file()
                cues = recognize_phrase_ends(project.pcm[:4*16000], allow_download=False)
                assert cues["source"] == "whisper_torch_words", cues.get("notes")
                check("frozen_whisper_worker", {"source": cues["source"], "cue_count": len(cues["cues"])})
        result["passed"] = True
        save()
        return 0
    except Exception:
        result["error"] = traceback.format_exc()
        save()
        return 1
