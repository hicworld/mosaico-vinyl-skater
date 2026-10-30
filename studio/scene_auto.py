"""Deterministic preview looks driven only by the audible PCM sample clock.

This module never edits a project, seeks audio, reads wall time, or calls the
renderer. Cue samples are phrase ends in ``phrase`` mode and actual LRC line
starts in ``lyric_start`` mode. The caller must reconfigure the baseline when
the song, lyric track or manual look changes.

Paired record IDs 4..9 are renderer-only overrides. The existing project/import
record palette remains 0..3; do not pass these overrides to its record_index().
"""
from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
from numbers import Integral
from typing import Iterable


def _integer(value, name: str, *, minimum: int, maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, Integral):
        raise ValueError(f"{name} must be an integer")
    result = int(value)
    if result < minimum or (maximum is not None and result > maximum):
        raise ValueError(f"{name} is outside its supported range")
    return result


@dataclass(frozen=True)
class SceneAutoState:
    """Resolved preview values; ``active`` does not mean audio is playing.

    ``phrase_index`` counts the selected timed cues already reached in the
    current loop: phrase ends or lyric starts, according to ``mode``.
    ``next_cue_sample`` is an absolute sample in the same clock as resolve().
    It is None for off/demo/missing-cue modes or after a non-looping song ends.
    ``switch_count`` accumulates across loops, making seeks reproducible.
    """

    theme: int
    record_id: int
    skin: int
    active: bool
    mode: str
    switch_count: int
    phrase_index: int | None
    loop_index: int
    next_cue_sample: int | None
    song_sample: int
    reason: str


@dataclass(frozen=True)
class _Configuration:
    theme: int = 1
    record_id: int = 0
    skin: int = 0
    mode: str = "off"
    include_character: bool = False
    cue_samples: tuple[int, ...] = ()
    song_sample_count: int = 1
    sample_rate: int = 16000
    music_start_sample: int = 0
    interval_samples: int = 32000
    loop: bool = True


class SceneAutoController:
    """Configure a manual baseline, then resolve any sample in any order.

    Enabling a usable auto mode pairs the baseline scene with its matching
    record immediately. The scene advances by one when a selected timed cue
    (or interval) is reached. Character changes use the same switch count.
    Empty timed cues keep the complete manual look; they never become a
    two-second timer. ``demo=True`` always keeps the complete manual look.
    """

    def __init__(self):
        self._configuration = _Configuration()

    def configure(self, *, theme: int, record_id: int, skin: int,
                  song_sample_count: int, mode: str = "off",
                  include_character: bool = False,
                  cue_samples: Iterable[int] | None = (), sample_rate: int = 16000,
                  music_start_sample: int = 0, interval_samples: int | None = None,
                  loop: bool = True) -> SceneAutoController:
        """Commit validated values atomically; failed validation changes nothing.

        Song/cue positions are PCM samples, not seconds. ``music_start_sample``
        identifies song sample zero in the absolute transport clock, normally
        zero. Values before this baseline retain the manual look. Cues are
        sorted/deduplicated. Phrase ends must be in (0, song_sample_count]; an
        end cue is counted once at each loop boundary. Lyric starts must be in
        [0, song_sample_count), so a first line at zero triggers on song start.
        """
        theme = _integer(theme, "theme", minimum=0, maximum=6) or 1
        record_id = _integer(record_id, "record_id", minimum=0, maximum=3)
        skin = _integer(skin, "skin", minimum=0, maximum=3)
        count = _integer(song_sample_count, "song_sample_count", minimum=1)
        rate = _integer(sample_rate, "sample_rate", minimum=1)
        start = _integer(music_start_sample, "music_start_sample", minimum=0)
        if mode not in ("off", "interval", "phrase", "lyric_start"):
            raise ValueError("mode must be off, interval, phrase, or lyric_start")
        if not isinstance(include_character, bool) or not isinstance(loop, bool):
            raise ValueError("include_character and loop must be booleans")
        interval = 2 * rate if interval_samples is None else _integer(
            interval_samples, "interval_samples", minimum=1)
        try:
            cue_minimum, cue_maximum = (0, count - 1) if mode == "lyric_start" else (1, count)
            cues = tuple(sorted({_integer(cue, "cue_sample", minimum=cue_minimum, maximum=cue_maximum)
                                 for cue in (() if cue_samples is None else cue_samples)}))
        except TypeError as error:
            raise ValueError("cue_samples must be an iterable of integers") from error
        self._configuration = _Configuration(
            theme, record_id, skin, mode, include_character, cues, count, rate,
            start, interval, loop)
        return self

    def resolve(self, absolute_sample: int, *, demo: bool = False) -> SceneAutoState:
        """Return the same result for the same sample/configuration, even on seek.

        Looping AudioTransport.sample grows beyond the song length; pass it
        directly rather than applying modulo before this call. Pausing needs no
        special flag because the audible sample remains fixed. Non-loop mode
        clamps after the song ends instead of inventing further transitions.
        """
        sample = _integer(absolute_sample, "absolute_sample", minimum=0)
        if not isinstance(demo, bool):
            raise ValueError("demo must be a boolean")
        cfg = self._configuration
        elapsed = max(0, sample - cfg.music_start_sample)
        if cfg.loop:
            loop_index, local_sample = divmod(elapsed, cfg.song_sample_count)
        else:
            loop_index, local_sample = 0, min(elapsed, cfg.song_sample_count)

        inactive_reason = ("demo" if demo else "off" if cfg.mode == "off" else
                           "missing_cues" if cfg.mode in ("phrase", "lyric_start") and not cfg.cue_samples else None)
        if inactive_reason:
            return SceneAutoState(cfg.theme, cfg.record_id, cfg.skin, False,
                                  cfg.mode, 0, None, loop_index, None,
                                  local_sample, inactive_reason)

        if sample < cfg.music_start_sample:
            timed = cfg.mode in ("phrase", "lyric_start")
            next_cue = cfg.music_start_sample + (cfg.cue_samples[0] if timed else cfg.interval_samples)
            return SceneAutoState(cfg.theme, cfg.record_id, cfg.skin, False,
                                  cfg.mode, 0, 0 if timed else None, 0, next_cue,
                                  0, "before_start")

        if cfg.mode in ("phrase", "lyric_start"):
            phrase_index = bisect_right(cfg.cue_samples, local_sample)
            switches = loop_index * len(cfg.cue_samples) + phrase_index
            if phrase_index < len(cfg.cue_samples):
                next_cue = (cfg.music_start_sample + loop_index * cfg.song_sample_count
                            + cfg.cue_samples[phrase_index])
            elif cfg.loop:
                next_cue = (cfg.music_start_sample + (loop_index + 1) * cfg.song_sample_count
                            + cfg.cue_samples[0])
            else:
                next_cue = None
        else:
            phrase_index = None
            effective_elapsed = elapsed if cfg.loop else local_sample
            switches = effective_elapsed // cfg.interval_samples
            next_offset = (switches + 1) * cfg.interval_samples
            next_cue = (cfg.music_start_sample + next_offset
                        if cfg.loop or next_offset <= cfg.song_sample_count else None)

        theme = (cfg.theme - 1 + switches) % 6 + 1
        skin = (cfg.skin + switches) % 4 if cfg.include_character else cfg.skin
        return SceneAutoState(theme, theme + 3, skin, True, cfg.mode, switches,
                              phrase_index, loop_index, next_cue, local_sample,
                              "active")
