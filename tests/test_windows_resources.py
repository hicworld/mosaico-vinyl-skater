"""Resource collection checks without binary downloads or model loading."""
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tools.windows.prepare_resources import (
    _safe_relative, collect_distribution, collect_native_licenses, copy_resource, record)


class WindowsResourceTests(unittest.TestCase):
    def test_license_record_paths_cannot_escape_bundle(self):
        self.assertEqual(_safe_relative("../../share/LICENSE"), Path("share/LICENSE"))
        for value in ("/absolute/LICENSE", "C:/private/LICENSE", "\\\\host\\share\\LICENSE"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                _safe_relative(value)

    def test_copy_and_manifest_keep_hash_and_relative_path(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            source, output = base / "source.txt", base / "bundle"
            source.write_bytes(b"sample license text\n")
            target = output / "licenses/component/LICENSE.txt"
            copy_resource(source, target)
            first = record(target, output)
            copy_resource(source, target)
            self.assertEqual(record(target, output), first)
            self.assertEqual(first["path"], "licenses/component/LICENSE.txt")
            self.assertEqual(first["bytes"], len(source.read_bytes()))
            self.assertNotIn(str(base), str(first))

    def test_package_licenses_are_preserved_without_machine_paths(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            source = base / "sample.dist-info/LICENSE.txt"
            source.parent.mkdir()
            source.write_text("license body", encoding="utf8")
            distribution = SimpleNamespace(metadata={"Name": "Sample-Package", "License-Expression": "MIT"},
                version="1.0", files=[Path("sample.dist-info/LICENSE.txt")],
                locate_file=lambda item: base / item)
            output = base / "bundle"
            component = collect_distribution(distribution, output)
            self.assertEqual(component["version"], "1.0")
            self.assertEqual(component["license_expression"], "MIT")
            self.assertEqual(len(component["files"]), 1)
            self.assertNotIn(str(base), str(component))

    def test_native_sndfile_copying_is_explicitly_preserved(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            license_path = base / "_soundfile_data/COPYING"
            license_path.parent.mkdir()
            license_path.write_bytes(b"native LGPL license text\n")
            soundfile = SimpleNamespace(version="test", files=[Path("_soundfile_data/COPYING")],
                locate_file=lambda item: base / item)
            sounddevice = SimpleNamespace(version="test", files=[])
            output = base / "bundle"
            with patch("tools.windows.prepare_resources.metadata.distribution",
                       side_effect=lambda name: {"soundfile":soundfile, "sounddevice":sounddevice}[name]):
                components = collect_native_licenses(output)
            component = next(item for item in components if item["component"] == "libsndfile")
            self.assertEqual(component["files"][0]["path"], "licenses/native-libraries/libsndfile/COPYING")
            self.assertEqual((output / component["files"][0]["path"]).read_bytes(), license_path.read_bytes())


if __name__ == "__main__":
    unittest.main()
