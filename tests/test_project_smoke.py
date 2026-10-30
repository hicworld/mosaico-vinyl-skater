"""Portable project, renderer and offline-video checks; no real audio/serial."""
from pathlib import Path
import json
import shutil
import subprocess
import tempfile
import unittest

from importer.analysis import ffmpeg_path
from importer.package import package_bytes
from importer.planner import ACTIVE_CLIPS, CLIPS
from studio.model import EditorProject
from studio.runtime import StudioRenderer
from studio.scene_auto import SceneAutoController
from studio.video_export import ExportSettings, export_video, snapshot_project

ROOT = Path(__file__).resolve().parents[1]


class ProjectSmokeTests(unittest.TestCase):
    def setUp(self):
        self.project = EditorProject.load(ROOT / 'examples/demo.vskproj')

    def test_demo_audio_is_relative_and_contains_no_imported_source(self):
        raw = json.loads((ROOT / 'examples/demo.vskproj').read_text(encoding='utf8'))
        self.assertFalse(Path(raw['audio']['path']).is_absolute())
        self.assertIsNone(raw['source'])
        self.assertEqual(self.project.sample_count, 16 * 16000)
        self.assertGreater(int(abs(self.project.pcm.astype('int32')).max()), 1000)
        self.assertTrue(self.project.show_lyrics)

    def test_stable_action_catalog_and_lyric_cues(self):
        self.assertEqual((len(CLIPS), len(ACTIVE_CLIPS)), (55, 52))
        self.assertEqual(self.project.scene_auto['mode'], 'lyric_start')
        self.assertEqual(self.project.lyric_start_samples, tuple(range(0, 256000, 32000)))
        self.assertIn('小舞台', self.project.current_lyric(1000))

    def test_all_scene_overrides_render_with_the_frozen_wasm(self):
        renderer = StudioRenderer()
        try:
            renderer.set_package(package_bytes(self.project.build_plan(), self.project.pcm.tobytes()))
            for theme in range(1, 7):
                image = renderer.render(theme * 12000, skin=theme % 4, theme=theme,
                                        record_id=theme + 3, effect_id=0)
                self.assertEqual(image.size, (480, 480))
                self.assertGreater(max(image.getextrema()[0]) - min(image.getextrema()[0]), 15)
        finally:
            renderer.close()

    def test_snapshot_and_scene_switches_match_preview_clock(self):
        snap = snapshot_project(self.project, skin=2)
        controller = SceneAutoController().configure(theme=snap.theme_id,
            record_id=snap.record_id, skin=snap.skin, song_sample_count=snap.sample_count,
            mode=snap.auto_mode, include_character=snap.auto_character,
            cue_samples=snap.lyric_start_samples, loop=False)
        first, second = controller.resolve(0), controller.resolve(32000)
        self.assertNotEqual(first.theme, second.theme)
        self.assertEqual(second.record_id, second.theme + 3)
        self.assertNotEqual(first.skin, second.skin)

    def test_real_mp4_export_when_ffmpeg_is_available(self):
        try:
            encoder = ffmpeg_path()
        except RuntimeError:
            self.skipTest('FFmpeg is optional for running the other desktop tests')
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / 'demo.mp4'
            result = export_video(snapshot_project(self.project), output,
                                  ExportSettings(start_seconds=2, end_seconds=3, fps=24))
            self.assertEqual(result.frame_count, 24)
            self.assertGreater(output.stat().st_size, 1000)
            # Decode both streams, including the soundtrack, instead of just
            # checking the filename or trusting the encoder exit status.
            decoded = subprocess.run([encoder, '-v', 'error', '-i', str(output),
                '-map', '0:v:0', '-map', '0:a:0', '-f', 'null', '-'],
                capture_output=True, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            self.assertEqual(decoded.returncode, 0, decoded.stderr.decode(errors='replace'))


if __name__ == '__main__':
    unittest.main()
