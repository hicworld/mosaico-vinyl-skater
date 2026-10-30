"""One bounded, latest-frame worker; all Tk calls stay on the UI thread.

The audio transport remains the clock. A busy renderer skips superseded
requests instead of building a queue that would replay old music positions.
"""
from __future__ import annotations

from dataclasses import dataclass
import threading
import time
from typing import Any

from PIL import Image

from .lyric_overlay import draw_lyric_overlay


@dataclass(frozen=True)
class FrameResult:
    image: Image.Image | None
    pose: dict | None
    sample: int
    context: Any
    error: Exception | None
    elapsed_ms: float


@dataclass(frozen=True)
class _Request:
    sample: int
    skin: int
    playing: bool
    theme: int | None
    record_id: int | None
    effect_id: int | None
    lyric: str
    size: int
    context: Any


class LatestFrameWorker:
    """Render off-thread with at most one pending request and one result.

    ``context`` identifies a package/demo, appearance and preview destination.
    Keep music position out of it; otherwise each tick invalidates an in-flight
    frame. Call invalidate() before seeking or replacing a song package.

    A consumed result is retained only as a small request key for deduplication,
    so paused playback does not repeatedly render the same static image.
    No Tk objects, including PhotoImage, may be created here.
    """

    def __init__(self, renderer):
        self.renderer = renderer
        self._condition = threading.Condition()
        self._closed = False
        self._generation = 0
        self._context = None
        self._context_set = False
        self._pending: tuple[int, _Request] | None = None
        self._running: tuple[int, _Request] | None = None
        self._result: FrameResult | None = None
        self._completed: tuple[int, _Request] | None = None
        self._stats = {"submitted": 0, "rendered": 0, "overwritten": 0,
                       "discarded": 0, "errors": 0, "deduplicated": 0}
        self._thread = threading.Thread(target=self._run,
                                        name="MosaicoPreview", daemon=True)
        self._thread.start()

    @property
    def alive(self) -> bool:
        return self._thread.is_alive()

    @property
    def stats(self) -> dict:
        with self._condition:
            return dict(self._stats)

    def submit(self, sample: int, *, skin=0, playing=True, theme=None,
               record_id=None, effect_id=None, lyric="", size=480,
               context=None) -> None:
        if isinstance(size, bool) or not isinstance(size, int) or not 1 <= size <= 8192:
            raise ValueError("Preview size must be an integer from 1 to 8192.")
        request = _Request(int(sample), skin, bool(playing), theme, record_id,
                           effect_id, str(lyric), size, context)
        with self._condition:
            if self._closed:
                return
            if not self._context_set or self._context != context:
                self._context, self._context_set = context, True
                self._invalidate_locked()
            item = (self._generation, request)
            if item in (self._pending, self._running, self._completed):
                self._stats["deduplicated"] += 1
                return
            if self._pending is not None:
                self._stats["overwritten"] += 1
            self._pending = item
            self._stats["submitted"] += 1
            self._condition.notify()

    def take_latest(self) -> FrameResult | None:
        """Nonblocking: return the newest completed valid frame once."""
        with self._condition:
            result, self._result = self._result, None
            return result

    def _invalidate_locked(self) -> None:
        self._generation += 1
        self._pending = None
        self._result = None
        self._completed = None

    def invalidate(self) -> None:
        """Discard queued/completed frames and suppress any in-flight result."""
        with self._condition:
            self._invalidate_locked()

    def close(self, *, wait=False, timeout=1.0) -> bool:
        """Stop accepting work, dropping pending frames without blocking Tk.

        This worker never closes the shared renderer. Join the worker before
        releasing that renderer; a close/join can run on a cleanup thread.
        An in-flight C/WASM call is allowed to finish and its result is dropped.
        """
        with self._condition:
            self._closed = True
            self._invalidate_locked()
            self._condition.notify_all()
        return self.join(timeout) if wait else not self.alive

    def join(self, timeout=None) -> bool:
        if threading.current_thread() is self._thread:
            return False
        self._thread.join(timeout)
        return not self.alive

    def _run(self) -> None:
        while True:
            with self._condition:
                self._condition.wait_for(lambda: self._closed or self._pending is not None)
                if self._closed:
                    return
                item, self._pending = self._pending, None
                self._running = item
            generation, request = item
            started = time.perf_counter()
            image, pose, error = None, None, None
            try:
                image = self.renderer.render(
                    request.sample, skin=request.skin, playing=request.playing,
                    theme=request.theme, record_id=request.record_id,
                    effect_id=request.effect_id)
                pose = self.renderer.pose(request.sample)
                image = draw_lyric_overlay(image, request.lyric)
                dimensions = (request.size, request.size)
                if image.size != dimensions:
                    # Bilinear enlargement avoids an expensive high-order
                    # filter over millions of full-screen pixels. It preserves
                    # the existing picture, without inventing model details.
                    resampling = (Image.Resampling.BILINEAR
                                  if request.size > image.width
                                  else Image.Resampling.LANCZOS)
                    image = image.resize(dimensions, resampling)
            except Exception as exc:
                image, pose, error = None, None, exc
            result = FrameResult(image, pose, request.sample, request.context,
                                 error, (time.perf_counter() - started) * 1000)
            with self._condition:
                self._running = None
                self._stats["rendered"] += 1
                if error is not None:
                    self._stats["errors"] += 1
                if self._closed or generation != self._generation:
                    self._stats["discarded"] += 1
                    continue
                self._completed = item
                self._result = result
