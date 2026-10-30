"""Bounded rendering and lifecycle regressions, using no Tk or audio device."""
from __future__ import annotations

import threading
import time
import unittest

from PIL import Image

from studio.preview_worker import LatestFrameWorker


class FakeRenderer:
    def __init__(self, *, block_first=False, fail_sample=None):
        self.started = threading.Event()
        self.release = threading.Event()
        if not block_first:
            self.release.set()
        self.calls = []
        self.threads = []
        self.fail_sample = fail_sample

    def render(self, sample, **kwargs):
        self.calls.append(sample)
        self.threads.append(threading.current_thread().name)
        self.started.set()
        if len(self.calls) == 1 and not self.release.wait(3):
            raise RuntimeError("test renderer was not released")
        if sample == self.fail_sample:
            raise RuntimeError("expected renderer failure")
        return Image.new("RGB", (48, 48), (sample % 256, 2, 3))

    def pose(self, sample):
        return {"clip": 0, "phase": sample / 1000}


def wait_until(predicate, timeout=3):
    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(.002)
    raise AssertionError("Timed out waiting for worker")


class PreviewWorkerTests(unittest.TestCase):
    def make_worker(self, renderer):
        worker = LatestFrameWorker(renderer)
        self.addCleanup(lambda: (renderer.release.set(), worker.close(wait=True, timeout=3)))
        return worker

    def test_render_pose_and_resize_are_on_background_thread(self):
        renderer = FakeRenderer()
        worker = self.make_worker(renderer)
        worker.submit(12, size=96, context=("song", 1))
        result = wait_until(worker.take_latest)
        self.assertIsNone(result.error)
        self.assertEqual(result.image.size, (96, 96))
        self.assertEqual(result.pose, {"clip": 0, "phase": .012})
        self.assertEqual(result.context, ("song", 1))
        self.assertEqual(renderer.threads, ["MosaicoPreview"])
        self.assertGreaterEqual(result.elapsed_ms, 0)

    def test_poll_and_submit_never_wait_for_slow_renderer(self):
        renderer = FakeRenderer(block_first=True)
        worker = self.make_worker(renderer)
        worker.submit(0, size=48, context="same")
        self.assertTrue(renderer.started.wait(1))
        start = time.perf_counter()
        for sample in range(1, 501):
            worker.submit(sample, size=48, context="same")
            self.assertIsNone(worker.take_latest())
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, .15, "UI calls must not wait on render")
        self.assertEqual(renderer.calls, [0])
        self.assertGreater(worker.stats["overwritten"], 490)
        renderer.release.set()
        wait_until(lambda: worker.stats["rendered"] >= 2)
        result = wait_until(worker.take_latest)
        self.assertEqual(renderer.calls, [0, 500])
        self.assertEqual(result.sample, 500)

    def test_same_context_does_not_starve_completed_frames(self):
        renderer = FakeRenderer(block_first=True)
        worker = self.make_worker(renderer)
        worker.submit(1, size=48, context="song")
        self.assertTrue(renderer.started.wait(1))
        for sample in range(2, 30):
            worker.submit(sample, size=48, context="song")
        renderer.release.set()
        wait_until(lambda: worker.stats["rendered"] >= 2)
        # Submitting a newer music position must not erase a completed frame.
        worker.submit(30, size=48, context="song")
        result = worker.take_latest()
        self.assertIsNotNone(result)
        self.assertGreaterEqual(result.sample, 1)

    def test_context_change_discards_inflight_and_completed_old_result(self):
        renderer = FakeRenderer(block_first=True)
        worker = self.make_worker(renderer)
        worker.submit(1, size=48, context="old")
        self.assertTrue(renderer.started.wait(1))
        worker.submit(2, size=48, context="new")
        renderer.release.set()
        result = wait_until(worker.take_latest)
        self.assertEqual((result.sample, result.context), (2, "new"))
        self.assertEqual(worker.stats["discarded"], 1)
        wait_until(lambda: worker.stats["rendered"] == 2)
        worker.submit(3, size=48, context="third")
        self.assertEqual(wait_until(worker.take_latest).context, "third")

    def test_seek_invalidation_suppresses_old_sample_without_context_change(self):
        renderer = FakeRenderer(block_first=True)
        worker = self.make_worker(renderer)
        worker.submit(900, size=48, context="song")
        self.assertTrue(renderer.started.wait(1))
        worker.invalidate()
        worker.submit(10, size=48, context="song")
        renderer.release.set()
        result = wait_until(worker.take_latest)
        self.assertEqual(result.sample, 10)
        self.assertEqual(worker.stats["discarded"], 1)

    def test_paused_duplicate_is_not_rerendered_after_result_consumed(self):
        renderer = FakeRenderer()
        worker = self.make_worker(renderer)
        worker.submit(55, playing=False, size=48, context="song")
        self.assertEqual(wait_until(worker.take_latest).sample, 55)
        for _ in range(100):
            worker.submit(55, playing=False, size=48, context="song")
        self.assertEqual(renderer.calls, [55])
        self.assertIsNone(worker.take_latest())
        self.assertEqual(worker.stats["deduplicated"], 100)
        worker.invalidate()
        worker.submit(55, playing=False, size=48, context="song")
        wait_until(worker.take_latest)
        self.assertEqual(renderer.calls, [55, 55])

    def test_renderer_error_is_reported_once_and_next_request_recovers(self):
        renderer = FakeRenderer(fail_sample=1)
        worker = self.make_worker(renderer)
        worker.submit(1, size=48)
        result = wait_until(worker.take_latest)
        self.assertIsInstance(result.error, RuntimeError)
        self.assertIsNone(result.image)
        worker.submit(2, size=48)
        self.assertIsNone(wait_until(worker.take_latest).error)
        self.assertEqual(worker.stats["errors"], 1)

    def test_close_is_nonblocking_and_drops_pending_and_inflight_frames(self):
        renderer = FakeRenderer(block_first=True)
        worker = self.make_worker(renderer)
        worker.submit(1, size=48)
        self.assertTrue(renderer.started.wait(1))
        worker.submit(2, size=48)
        start = time.perf_counter()
        self.assertFalse(worker.close())
        self.assertLess(time.perf_counter() - start, .05)
        worker.submit(3, size=48)
        renderer.release.set()
        self.assertTrue(worker.join(2))
        self.assertIsNone(worker.take_latest())
        self.assertEqual(renderer.calls, [1])
        self.assertFalse(worker.alive)

    def test_size_validation(self):
        worker = self.make_worker(FakeRenderer())
        for size in (0, -1, 8193, True, 12.4):
            with self.assertRaises(ValueError):
                worker.submit(1, size=size)


if __name__ == "__main__":
    unittest.main()
