"""Frozen-resource separation and worker dispatch, without a GUI or models."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np

from importer.analysis import beat_model_path, ffmpeg_path
import motion_studio
from studio import paths
from studio.lyric_recognition import _worker_main, model_directory


class FrozenRuntimeTests(unittest.TestCase):
    def test_source_resource_and_data_paths_stay_at_repository_root(self):
        with patch.object(sys, "frozen", False, create=True), patch.dict(os.environ, {}, clear=True):
            self.assertEqual(paths.resource_root(), Path(__file__).resolve().parents[1])
            self.assertEqual(paths.data_root(), paths.resource_root())

    def test_frozen_resources_and_portable_output_are_separate(self):
        with tempfile.TemporaryDirectory() as folder:
            executable = Path(folder) / "app" / "Skater.exe"
            bundle = Path(folder) / "app" / "_internal"
            with patch.object(sys, "frozen", True, create=True), patch.object(sys, "executable", str(executable)), \
                 patch.object(sys, "_MEIPASS", str(bundle), create=True), patch.dict(os.environ, {}, clear=True):
                self.assertEqual(paths.resource_path("studio", "assets"), bundle / "studio/assets")
                self.assertEqual(paths.data_root(), executable.resolve().parent)
                self.assertFalse((bundle / "projects").exists())

    def test_protected_exe_location_uses_user_local_appdata(self):
        with tempfile.TemporaryDirectory() as folder:
            executable = Path(folder) / "protected" / "Skater.exe"
            local = Path(folder) / "local"
            target = local / "MosaicoVinylSkater"
            with patch.object(sys, "frozen", True, create=True), patch.object(sys, "executable", str(executable)), \
                 patch.dict(os.environ, {"LOCALAPPDATA": str(local)}, clear=True), \
                 patch("studio.paths._writable_directory", side_effect=[False, True]) as writable:
                self.assertEqual(paths.data_root(), target)
                self.assertEqual(writable.call_args_list[0].args, (executable.resolve().parent,))
                self.assertEqual(writable.call_args_list[1].args, (target,))

    def test_explicit_data_location_is_honored_and_failure_is_visible(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {"MOSAICO_DATA_DIR": folder}, clear=True):
            self.assertEqual(paths.data_root(), Path(folder).resolve())
            with patch("studio.paths._writable_directory", return_value=False), self.assertRaisesRegex(OSError, "MOSAICO_DATA_DIR"):
                paths.data_root()

    def test_frozen_bundled_models_and_ffmpeg_work_without_path_or_cache(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for relative in ("bin/ffmpeg.exe", "models/beat-this-small0.ckpt", "models/whisper-base/base.pt"):
                file = root / relative
                file.parent.mkdir(parents=True, exist_ok=True)
                file.touch()
            with patch.object(sys, "frozen", True, create=True), patch.object(sys, "_MEIPASS", str(root), create=True), \
                 patch.dict(os.environ, {}, clear=True), patch("importer.analysis.shutil.which", return_value=None):
                self.assertEqual(ffmpeg_path(), str(root / "bin/ffmpeg.exe"))
                self.assertEqual(beat_model_path(), str(root / "models/beat-this-small0.ckpt"))
                self.assertEqual(model_directory(), root / "models/whisper-base")

    def test_missing_frozen_beat_model_is_explicit_instead_of_downloading(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(sys, "frozen", True, create=True), \
             patch.object(sys, "_MEIPASS", folder, create=True), patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(FileNotFoundError, "Windows 包缺少节拍模型"):
                beat_model_path()

    def test_worker_command_uses_frozen_exe_dispatch_and_keeps_source_module_mode(self):
        source, target = Path("音频.npy"), Path("输出.json")
        with patch.object(sys, "frozen", True, create=True):
            command = paths.lyric_worker_command(source, target, False)
            self.assertEqual(command, [sys.executable, "--lyric-worker", str(source), str(target), "0"])
            self.assertNotIn("-m", command)
        with patch.object(sys, "frozen", False, create=True):
            self.assertEqual(paths.lyric_worker_command(source, target, True)[1:4], ["-m", "studio.lyric_recognition", "--worker"])

    def test_entry_dispatches_worker_before_gui_and_preserves_arguments(self):
        with patch("studio.lyric_recognition._worker_main") as worker, patch("studio.app.main") as gui:
            code = motion_studio.main(["--lyric-worker", "input.npy", "out.json", "0"])
            self.assertEqual(code, 0)
            worker.assert_called_once_with(["--worker", "input.npy", "out.json", "0"])
            gui.assert_not_called()

    def test_self_test_dispatches_report_without_gui(self):
        calls = []
        fake = SimpleNamespace(main=lambda path: calls.append(path) or 0)
        with patch.dict(sys.modules, {"studio.frozen_check": fake}), patch("studio.app.main") as gui:
            self.assertEqual(motion_studio.main(["--self-test", "report.json"]), 0)
            self.assertEqual(calls, [Path("report.json")])
            gui.assert_not_called()

    def test_bundled_project_is_saved_as_user_copy_instead_of_resource(self):
        from studio.app import MotionStudio
        adopted = []
        app = SimpleNamespace(_adopt=lambda *args, **kwargs: adopted.append((args, kwargs)),
                              _error=lambda *args: self.fail(str(args)))
        project = object()
        with tempfile.TemporaryDirectory() as folder:
            bundle = Path(folder).resolve() / "_internal"
            sample = bundle / "examples/demo.vskproj"
            with patch.object(sys, "frozen", True, create=True), patch.object(sys, "_MEIPASS", str(bundle), create=True), \
                 patch("studio.app.EditorProject.load", return_value=project):
                MotionStudio._load(app, sample)
                self.assertEqual(adopted[-1][0], (project, None))
            with patch.object(sys, "frozen", False, create=True), patch("studio.app.EditorProject.load", return_value=project):
                MotionStudio._load(app, sample)
                self.assertEqual(adopted[-1][0], (project, sample))

    def test_windowed_worker_writes_results_when_stdout_is_none(self):
        with tempfile.TemporaryDirectory() as folder:
            source, output = Path(folder) / "audio.npy", Path(folder) / "out.json"
            np.save(source, np.zeros(16000, dtype=np.float32))
            result = {"version": 1, "sample_rate": 16000, "source": "whisper_words", "cues": [], "notes": []}
            def recognize(_audio, *, progress, allow_download):
                progress("正在处理")
                return result
            with patch("studio.lyric_recognition._recognize_in_process", side_effect=recognize), patch.object(sys, "stdout", None):
                _worker_main(["--worker", str(source), str(output), "0"])
            self.assertEqual(json.loads(output.read_text(encoding="utf8")), result)

    def test_frozen_entry_adds_its_own_binary_directory_only_to_process_path(self):
        with tempfile.TemporaryDirectory() as folder:
            binary = Path(folder) / "bin"
            binary.mkdir()
            with patch.object(sys, "frozen", True, create=True), patch.object(sys, "_MEIPASS", folder, create=True), \
                 patch.dict(os.environ, {"PATH": "existing"}, clear=True):
                motion_studio._configure_frozen_environment()
                self.assertEqual(os.environ["PATH"], str(binary) + os.pathsep + "existing")


if __name__ == "__main__":
    unittest.main(verbosity=2)
