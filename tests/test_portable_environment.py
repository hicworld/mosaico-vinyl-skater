"""Portable cache, launcher and explicit device identity contracts."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from importer.analysis import configure_model_cache, ffmpeg_path
from importer.upload import StoreConnection
from studio.lyric_recognition import model_directory


class FakeSerial:
    def __init__(self, mac="02:00:00:00:00:01"):
        self.mac, self.sent = mac, []
    def write(self, data):
        self.sent.append(data)
    def flush(self):
        pass
    def readline(self):
        return f"STORE INFO mac={self.mac} protocol=2 mounted=1\n".encode()
    def close(self):
        pass


class PortableEnvironmentTests(unittest.TestCase):
    def test_ffmpeg_env_override_then_path_without_machine_fallback(self):
        with tempfile.TemporaryDirectory() as folder:
            configured = Path(folder) / "custom ffmpeg"
            configured.touch()
            with patch.dict(os.environ, {"VINYL_FFMPEG": str(configured)}, clear=True), patch("importer.analysis.shutil.which", return_value=None):
                self.assertEqual(ffmpeg_path(), str(configured))
            with patch.dict(os.environ, {}, clear=True), patch("importer.analysis.shutil.which", return_value=str(configured)):
                self.assertEqual(ffmpeg_path(), str(configured))
            with patch.dict(os.environ, {}, clear=True), patch("importer.analysis.shutil.which", return_value=None):
                with self.assertRaisesRegex(RuntimeError, "VINYL_FFMPEG"):
                    ffmpeg_path()

    def test_model_cache_is_user_owned_and_overridable(self):
        with patch.dict(os.environ, {}, clear=True), patch("pathlib.Path.home", return_value=Path("user-directory")):
            base = configure_model_cache()
            self.assertEqual(base, Path("user-directory/.cache/mosaico-vinyl-skater"))
            self.assertEqual(os.environ["TORCH_HOME"], str(base / "torch"))
            self.assertEqual(model_directory(), (base / "whisper-base").resolve())
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {"MOSAICO_MODEL_CACHE": folder, "TORCH_HOME": "keep-explicit-value"}, clear=True):
            self.assertEqual(configure_model_cache(), Path(folder))
            self.assertEqual(os.environ["TORCH_HOME"], "keep-explicit-value")
            self.assertEqual(model_directory(), (Path(folder) / "whisper-base").resolve())
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {"MOSAICO_WHISPER_MODEL": folder}, clear=True):
            self.assertEqual(model_directory(), Path(folder).resolve())

    def test_device_must_be_configured_before_opening_a_serial_port(self):
        with patch.dict(os.environ, {}, clear=True), patch("serial.Serial") as create_serial:
            with self.assertRaisesRegex(ValueError, "MOSAICO_EXPECTED_DEVICE_MAC"):
                StoreConnection("COM999")
            create_serial.assert_not_called()

    def test_only_configured_device_passes_identity_and_protocol(self):
        serial = FakeSerial()
        with patch.dict(os.environ, {}, clear=True):
            connection = StoreConnection(serial_object=serial, expected_mac=serial.mac.upper())
            self.assertEqual(connection.info()["protocol"], "2")
            self.assertEqual(serial.sent, [b"STORE INFO\n"])
            connection = StoreConnection(serial_object=FakeSerial("02:00:00:00:00:02"), expected_mac=serial.mac)
            with self.assertRaisesRegex(RuntimeError, "身份"):
                connection.info()

    def test_invalid_identity_never_sends_commands(self):
        serial = FakeSerial()
        for identity in ("", "any", "123", 1, "02:00:00:00:00:GG"):
            with self.subTest(identity=identity), self.assertRaises(ValueError):
                StoreConnection(serial_object=serial, expected_mac=identity)
        self.assertEqual(serial.sent, [])

    def test_light_requirements_exclude_optional_models(self):
        light = (ROOT / "studio/requirements.txt").read_text(encoding="utf8").lower()
        self.assertNotIn("openai-whisper==", light)
        self.assertNotIn("torch==", light)
        self.assertIn("openai-whisper==", (ROOT / "requirements-lyrics.txt").read_text())
        self.assertIn("torch==2.6.0", (ROOT / "requirements-analysis.txt").read_text())

    @unittest.skipUnless(os.name == "nt" and shutil.which("powershell.exe"), "Windows launcher check")
    def test_launchers_parse_and_forward_arguments(self):
        environment = os.environ.copy()
        environment["MOSAICO_STUDIO_PYTHON"] = sys.executable
        for name, arguments in (("Motion-Studio.ps1", ["-Check"]), ("Import-Music.ps1", ["--help"])):
            command = ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ROOT / name), *arguments]
            result = subprocess.run(command, capture_output=True, timeout=30, env=environment,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            self.assertEqual(result.returncode, 0, result.stdout.decode(errors="replace") + result.stderr.decode(errors="replace"))
        for name in ("Motion-Studio.ps1", "Import-Music.ps1", "importer/Setup-Importer.ps1"):
            escaped_path = str(ROOT / name).replace("'", "''")
            command = f"$parseErrors=$null; [void][Management.Automation.Language.Parser]::ParseFile('{escaped_path}',[ref]$null,[ref]$parseErrors); if($parseErrors.Count){{$parseErrors;exit 1}}"
            result = subprocess.run(["powershell.exe", "-NoProfile", "-Command", command], capture_output=True, timeout=15,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            self.assertEqual(result.returncode, 0, result.stdout.decode(errors="replace") + result.stderr.decode(errors="replace"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
