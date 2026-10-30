"""Editable beat placements compiled to the unmodified Mosaico VSK2 format.

Editing never moves an analysed beat or cuts an authored clip in the middle.
All native clips have the same neutral, zero-velocity end poses.  A gap is
therefore filled with complete neutral clips rather than a cut balance pose.
This module has no GUI, renderer, serial, audio-device or model dependencies.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import math
import os
from pathlib import Path
import uuid
import wave

import numpy as np

from importer.package import read_package, write_package
from importer.planner import (ACTIVE_CLIPS, CLIPS, RETIRED_CLIP_IDS, SENTINEL,
                              compatibility_metadata, validate_plan, make_plan)
from importer.reactivity import validate_reactivity
from importer.themes import theme_index, record_index, effect_index
from .phrase_cues import validate_scene_cues
from .lyrics import validate_lyrics, current_lyric as lookup_lyric


SCHEMA = "vinyl-choreography-project-v1"
RATE = 16000


class ProjectError(ValueError):
    """An actionable project or beat-placement error for the desktop editor."""


def _integer(value, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, np.integer)):
        raise ProjectError(f"{label}必须是整数。")
    return int(value)


def _pcm_wave(path: Path) -> np.ndarray:
    try:
        with wave.open(str(path), "rb") as reader:
            if (reader.getframerate(), reader.getnchannels(), reader.getsampwidth(),
                    reader.getcomptype()) != (RATE, 1, 2, "NONE"):
                raise ProjectError("工程音频必须是 16 kHz、单声道、16 位 PCM WAV。")
            return np.frombuffer(reader.readframes(reader.getnframes()), dtype="<i2").copy()
    except (OSError, wave.Error) as exc:
        raise ProjectError(f"无法读取工程音频：{path}\n{exc}") from exc


def _sha(pcm: np.ndarray) -> str:
    return hashlib.sha256(pcm.astype("<i2", copy=False).tobytes()).hexdigest()


class EditorProject:
    """A PCM track, immutable detected grid, and undoable whole-clip placements."""

    def __init__(self, plan: dict, pcm: np.ndarray, analysis: dict | None = None,
                 source: str | None = None, pcm_wav: str | None = None):
        self._base_plan = deepcopy(plan)
        try:
            validate_plan(self._base_plan)
        except (AssertionError, KeyError, TypeError, IndexError, ValueError) as exc:
            raise ProjectError("歌曲动作计划不完整，无法打开。") from exc
        if plan.get("sample_rate") != RATE:
            raise ProjectError("歌曲采样率必须为 16 kHz。")
        samples = _integer(plan["sample_count"], "音频采样数")
        if not RATE * 4 <= samples <= RATE * 30 * 60:
            raise ProjectError("单首歌曲必须为 4 秒至 30 分钟。")
        incoming = np.asarray(pcm)
        if incoming.ndim != 1 or incoming.dtype.kind != "i" or incoming.dtype.itemsize != 2:
            raise ProjectError("工程需要单声道 int16 PCM 音频。")
        if len(incoming) != samples:
            raise ProjectError("工程音频长度与拍点计划不一致。")
        self._pcm = incoming.astype("<i2", copy=True)
        self._pcm.flags.writeable = False
        self._pcm_sha256 = _sha(self._pcm)
        expected = (analysis or {}).get("pcm_sha256") or plan.get("pcm_sha256")
        if expected and expected != self._pcm_sha256:
            raise ProjectError("音频校验失败：音频与拍点分析不是同一首歌，请重新分析。")
        self._analysis = deepcopy(analysis or self._analysis_from_plan(plan))
        self._analysis["pcm_sha256"] = self._pcm_sha256
        self._title = str(plan.get("title", "未命名歌曲"))
        self._theme_id = theme_index(plan.get("theme_id", 0))
        self._record_id = record_index(plan.get("record_id", 0))
        self._effect_id = effect_index(plan.get("effect_id", 0))
        self._scene_cues = validate_scene_cues(None, samples)
        self._lyrics = validate_lyrics(None, samples)
        self._show_lyrics = True
        self._scene_auto = {"mode": "off", "include_character": False}
        self.source = source
        self.pcm_wav = pcm_wav
        self.path: Path | None = None
        self._beats = deepcopy(plan["beats"])
        if any(not isinstance(b.get("sample"), int) or not 0 <= b.get("strength", 0) <= 65535
               or b.get("flags", 0) & ~7 for b in self._beats):
            raise ProjectError("拍点数据不符合 Mosaico 格式。")
        self._beat_samples = np.asarray([b["sample"] for b in self._beats], dtype=np.int64)
        actual = [i for i, beat in enumerate(self._beats) if not beat["flags"] & SENTINEL]
        if len(actual) < 2:
            raise ProjectError("歌曲至少需要两个真实拍点。")
        self.first_beat, self.last_beat = actual[0], actual[-1]
        self._intro_energy = (plan["segments"][0]["energy"]
                              if plan["segments"][0]["first_beat"] == 65535 else 13107)
        self._outro_energy = (plan["segments"][-1]["energy"]
                              if plan["segments"][-1]["first_beat"] == 65535 else 13107)
        self._placements: list[dict] = []
        self.migration_report: dict = {"removed_count": 0, "removed_clips": [], "message": ""}
        for segment in plan["segments"]:
            if segment["first_beat"] == 65535:
                if segment["clip_id"] != 0:
                    raise ProjectError("未对齐拍点的片段必须是舒缓滑行。")
                continue
            if segment["clip_id"] in RETIRED_CLIP_IDS:
                self._record_retired(segment["clip_id"])
                continue
            item = self._placement(segment["clip_id"], segment["first_beat"],
                                   segment["beat_count"], energy=segment["energy"],
                                   origin="automatic")
            self._placements.append(item)
        self._check_placements(self._placements, authored_duration=False)
        self._undo: list[dict] = []
        self._redo: list[dict] = []
        self.last_change: dict = {}
        self.continuity_report: dict = {}
        self._waveform = None

    def _record_retired(self, clip_id: int):
        report = self.migration_report
        report["removed_count"] += 1
        name = CLIPS[clip_id]["name"]
        if name not in report["removed_clips"]:
            report["removed_clips"].append(name)
        report["message"] = (f"已将旧编排中的 {report['removed_count']} 个停用动作（"
                             + "、".join(report["removed_clips"])
                             + "）替换为滑行衔接；原工程文件未修改，请另存新版。")

    @staticmethod
    def _analysis_from_plan(plan: dict) -> dict:
        actual = [b for b in plan["beats"] if not b["flags"] & SENTINEL]
        return {"engine": plan.get("analysis_engine", "existing-vsk2"),
                "sample_rate": plan["sample_rate"], "sample_count": plan["sample_count"],
                "duration": plan["sample_count"] / RATE,
                "beats": [b["sample"] for b in actual],
                "downbeat_indices": [i for i, b in enumerate(actual) if b["flags"] & 1],
                "energy": [b["strength"] / 65535. for b in actual],
                "bpm_median": plan.get("bpm_median", 120.),
                "quality": deepcopy(plan.get("quality", {})),
                "warnings": list(plan.get("warnings", []))}

    @classmethod
    def from_result(cls, result: dict, base_path: Path | str | None = None) -> "EditorProject":
        """Open an importer result or read_package result, checking the PCM hash."""
        base = Path(base_path).resolve() if base_path is not None else Path.cwd()

        def locate(value):
            path = Path(value)
            return path if path.is_absolute() else base / path

        pcm_wav = None
        if "pcm" in result:
            pcm = np.asarray(result["pcm"])
        elif "pcm_bytes" in result:
            raw = result["pcm_bytes"]
            if len(raw) % 2:
                raise ProjectError("PCM 音频字节数不正确。")
            pcm = np.frombuffer(raw, dtype="<i2")
        elif result.get("pcm_wav") and locate(result["pcm_wav"]).is_file():
            pcm_wav = str(locate(result["pcm_wav"]).resolve())
            pcm = _pcm_wave(Path(pcm_wav))
        elif result.get("package", {}).get("path") and locate(result["package"]["path"]).is_file():
            raw = read_package(locate(result["package"]["path"]))["pcm_bytes"]
            pcm = np.frombuffer(raw, dtype="<i2")
        else:
            raise ProjectError("找不到工程的 PCM 音频。请一并复制 WAV 文件，或重新导入歌曲。")
        return cls(result["plan"], pcm, result.get("analysis"), result.get("source"), pcm_wav)

    @property
    def title(self) -> str:
        return self._title

    @title.setter
    def title(self, value: str):
        self.set_title(value)

    def set_title(self, title: str):
        title = str(title).strip()
        if not title:
            raise ProjectError("歌曲标题不能为空。")
        if title != self._title:
            self._checkpoint()
            self._title = title

    @property
    def theme_id(self) -> int:
        return self._theme_id

    def set_theme(self, value) -> bool:
        value = theme_index(value)
        if value == self._theme_id:
            return False
        self._checkpoint()
        self._theme_id = value
        self.last_change = {"operation": "theme", "theme_id": value}
        return True

    @property
    def record_id(self) -> int:
        return self._record_id

    def set_record(self, value) -> bool:
        value = record_index(value)
        if value == self._record_id:
            return False
        self._checkpoint()
        self._record_id = value
        self.last_change = {"operation": "record", "record_id": value}
        return True

    @property
    def effect_id(self) -> int:
        return self._effect_id

    def set_effect(self, value) -> bool:
        value = effect_index(value)
        if value == self._effect_id:
            return False
        self._checkpoint()
        self._effect_id = value
        self.last_change = {"operation": "effect", "effect_id": value}
        return True

    @property
    def scene_cues(self) -> dict:
        """PC-only phrase endpoints belonging to this exact audio track."""
        return deepcopy(self._scene_cues)

    def _validate_scene_cues(self, document) -> dict:
        try:
            return validate_scene_cues(document, self.sample_count)
        except ValueError as exc:
            raise ProjectError(str(exc)) from exc

    def set_scene_cues(self, document) -> bool:
        value = self._validate_scene_cues(document)
        if value == self._scene_cues:
            return False
        self._checkpoint()
        self._scene_cues = value
        self.last_change = {"operation": "scene_cues", "count": len(value["cues"])}
        return True

    @property
    def lyrics(self) -> dict:
        """Independent PC subtitle starts/ends for this exact PCM track."""
        return deepcopy(self._lyrics)

    @property
    def lyric_start_samples(self) -> tuple[int, ...]:
        """Actual nonempty LRC line starts, independent of inferred phrase ends.

        Normalized lyric lines are sorted and merge equal starts; retaining
        explicit de-duplication here also guarantees one scene cue per time.
        A line at song sample zero is valid. Timed empty clears are not lines.
        """
        return tuple(sorted({line["start_sample"] for line in self._lyrics["lines"]
                             if line["text"].strip()}))

    def _validate_lyrics(self, document) -> dict:
        try:
            return validate_lyrics(document, self.sample_count)
        except ValueError as exc:
            raise ProjectError(str(exc)) from exc

    def set_lyrics(self, document) -> bool:
        value = self._validate_lyrics(document)
        if value == self._lyrics:
            return False
        self._checkpoint()
        self._lyrics = value
        self.last_change = {"operation": "lyrics", "count": len(value["lines"])}
        return True

    def current_lyric(self, sample, loop=False) -> str:
        """Find a subtitle without copying the private track each frame."""
        return lookup_lyric(self._lyrics, sample, self.sample_count, loop=loop)

    @property
    def show_lyrics(self) -> bool:
        """Preview visibility only; hiding retains the timed lyric track."""
        return self._show_lyrics

    @staticmethod
    def _validate_show_lyrics(value) -> bool:
        if type(value) is not bool:
            raise ProjectError("歌词显示设置必须是布尔值。")
        return value

    def set_show_lyrics(self, value) -> bool:
        value = self._validate_show_lyrics(value)
        if value == self._show_lyrics:
            return False
        self._checkpoint()
        self._show_lyrics = value
        self.last_change = {"operation": "show_lyrics", "show_lyrics": value}
        return True

    @property
    def scene_auto(self) -> dict:
        """Preview controller settings; never compiled into device songs."""
        return deepcopy(self._scene_auto)

    @staticmethod
    def _validate_scene_auto(mode, include_character) -> dict:
        if type(mode) is not str or mode not in ("off", "interval", "phrase", "lyric_start"):
            raise ProjectError("场景自动切换模式必须是 off、interval、phrase 或 lyric_start。")
        if type(include_character) is not bool:
            raise ProjectError("人物自动切换设置必须是布尔值。")
        return {"mode": mode, "include_character": include_character}

    def set_scene_auto(self, mode, include_character) -> bool:
        value = self._validate_scene_auto(mode, include_character)
        if value == self._scene_auto:
            return False
        self._checkpoint()
        self._scene_auto = value
        self.last_change = {"operation": "scene_auto", **value}
        return True

    def inherit_appearance(self, previous: "EditorProject") -> bool:
        """Seed a newly loaded song's look before its edit history starts.

        Music, detected beats and placements belong to this song. The current
        session supplies only the three appearance choices, including AUTO.
        """
        if self._undo or self._redo:
            raise ProjectError("继承外观只能用于刚载入的新歌曲。")
        values = (theme_index(previous.theme_id), record_index(previous.record_id),
                  effect_index(previous.effect_id))
        changed = values != (self._theme_id, self._record_id, self._effect_id)
        self._theme_id, self._record_id, self._effect_id = values
        return changed

    @property
    def pcm(self) -> np.ndarray:
        # Return a non-owning read-only view so callers cannot re-enable writes.
        result = self._pcm.view()
        result.flags.writeable = False
        return result

    @property
    def sample_count(self) -> int:
        return len(self._pcm)

    @property
    def duration(self) -> float:
        return self.sample_count / RATE

    @property
    def sample_rate(self) -> int:
        return RATE

    @property
    def analysis(self) -> dict:
        return deepcopy(self._analysis)

    @property
    def clips(self) -> list[dict]:
        # Registry IDs are stable for binary packages and existing callers.
        return deepcopy(list(CLIPS))

    @property
    def active_clips(self) -> list[dict]:
        return deepcopy(list(ACTIVE_CLIPS))

    @property
    def placements(self) -> list[dict]:
        return deepcopy(sorted(self._placements, key=lambda x: x["first_beat"]))

    @property
    def plan(self) -> dict:
        return self.build_plan()

    @property
    def beats(self) -> list[dict]:
        return deepcopy(self._beats)

    @property
    def waveform(self) -> list[tuple[float, float]]:
        """At most 1,200 signed normalized min/max pairs for a full-song view."""
        if self._waveform is None:
            edges = np.linspace(0, self.sample_count, min(1200, self.sample_count) + 1, dtype=int)
            self._waveform = [(float(self._pcm[a:b].min()) / 32768.,
                               float(self._pcm[a:b].max()) / 32768.)
                              for a, b in zip(edges, edges[1:])]
        return list(self._waveform)

    def sample_at_beat(self, beat: int) -> int:
        beat = _integer(beat, "拍点")
        if not 0 <= beat < len(self._beats):
            raise ProjectError("拍点超出歌曲范围。")
        return int(self._beat_samples[beat])

    def nearest_beat(self, sample: int | float, *, placement: bool = True) -> int:
        if not math.isfinite(sample):
            raise ProjectError("播放位置无效。")
        lo, hi = (self.first_beat, self.last_beat) if placement else (0, len(self._beats)-1)
        index = int(np.searchsorted(self._beat_samples, sample))
        choices = [i for i in (index-1, index) if lo <= i <= hi]
        return min(choices or [lo if sample <= self._beat_samples[lo] else hi],
                   key=lambda i: abs(int(self._beat_samples[i]) - sample))

    def allowed_counts(self, clip_id: int, start_beat: int | None = None) -> tuple[int, ...]:
        clip_id = self._clip_id(clip_id)
        if clip_id in RETIRED_CLIP_IDS:
            return ()
        if clip_id >= 19:
            # Keep every authored slalom cycle complete: a four/eight-beat
            # crossover or turn must not be compressed into a two-beat tap.
            authored = int(CLIPS[clip_id]["beats"])
            options = tuple(CLIPS[clip_id].get("allowed_counts", tuple(
                value for value in (2, 4, 8) if value >= authored)))
        elif clip_id in (17, 18):
            options = (8,)
        elif clip_id == 0:
            options = tuple(range(1, 9))
        elif clip_id in (5, 6, 14, 15, 16):
            options = (4, 8)
        elif clip_id == 8:
            options = (2, 4)  # The apex at phase .5 must land on a true beat.
        elif clip_id == 1:
            options = (2, 4, 8)
        else:
            options = (2, 4, 8)
        if start_beat is not None:
            start_beat = _integer(start_beat, "起始拍")
            options = tuple(x for x in options if self.first_beat <= start_beat
                            and start_beat + x <= self.last_beat)
        return options

    def default_count(self, clip_id: int, start_beat: int | None = None) -> int:
        clip_id = self._clip_id(clip_id)
        preferred = CLIPS[clip_id]["beats"]
        if clip_id == 1 and start_beat is not None and self.first_beat <= start_beat < self.last_beat:
            interval = self._beat_samples[start_beat+1] - self._beat_samples[start_beat]
            if 60. * RATE / interval > 155.:
                preferred = 4
        options = self.allowed_counts(clip_id, start_beat)
        if not options:
            raise ProjectError("歌曲结尾没有足够拍点放下完整动作，请向前拖动。")
        return min(options, key=lambda x: (abs(x-preferred), -x))

    @staticmethod
    def _clip_id(value) -> int:
        value = _integer(value, "动作编号")
        if not 0 <= value < len(CLIPS):
            raise ProjectError("动作编号不存在。")
        return value

    def _placement(self, clip_id: int, first: int, count: int, *, energy=None,
                   origin="manual", identifier=None) -> dict:
        clip_id, first, count = self._clip_id(clip_id), _integer(first, "起始拍"), _integer(count, "动作拍数")
        if first < self.first_beat or first + count > self.last_beat or not 1 <= count <= 8:
            raise ProjectError("动作必须完整放在歌曲的真实拍点之间。")
        if energy is None:
            strengths = [b["strength"] for b in self._beats[first:first+count]]
            energy = int(round(float(np.mean(strengths)))) if strengths else 32768
        energy = _integer(energy, "动作强度")
        if not 0 <= energy <= 65535:
            raise ProjectError("动作强度超出范围。")
        return {"id": identifier or uuid.uuid4().hex, "clip_id": clip_id,
                "first_beat": first, "start_beat": first, "beat_count": count,
                "end_beat": first + count, "energy": energy, "origin": origin}

    def _check_placements(self, items: list[dict], *, authored_duration: bool = True):
        seen = set()
        for index, item in enumerate(sorted(items, key=lambda x: x["first_beat"])):
            if not isinstance(item.get("id"), str) or not item["id"] or item["id"] in seen:
                raise ProjectError("动作编号重复或损坏。")
            seen.add(item["id"])
            checked = self._placement(item["clip_id"], item["first_beat"], item["beat_count"],
                                      energy=item["energy"], identifier=item["id"])
            if item.get("start_beat", item["first_beat"]) != checked["first_beat"] or item.get("end_beat", checked["end_beat"]) != checked["end_beat"]:
                raise ProjectError("动作拍点索引不一致。")
            if authored_duration and checked["beat_count"] not in self.allowed_counts(checked["clip_id"], checked["first_beat"]):
                raise ProjectError("该动作拍数过短或过长，会使肢体变化不自然。")
            if index and previous["end_beat"] > checked["first_beat"]:
                raise ProjectError("动作片段重叠。")
            previous = checked

    def _snapshot(self) -> dict:
        return {"placements": deepcopy(self._placements), "title": self._title,
                "theme_id": self._theme_id, "record_id": self._record_id,
                "effect_id": self._effect_id, "scene_cues": self.scene_cues,
                "lyrics": self.lyrics,
                "preview_show_lyrics": self.show_lyrics,
                "preview_scene_auto": self.scene_auto}

    def _checkpoint(self):
        self._undo.append(self._snapshot())
        if len(self._undo) > 100:
            self._undo.pop(0)
        self._redo.clear()

    @property
    def can_undo(self) -> bool:
        return bool(self._undo)

    @property
    def can_redo(self) -> bool:
        return bool(self._redo)

    def undo(self) -> bool:
        if not self._undo:
            return False
        self._redo.append(self._snapshot())
        state = self._undo.pop()
        self._placements, self._title = state["placements"], state["title"]
        self._theme_id = state["theme_id"]
        self._record_id = state["record_id"]
        self._effect_id = state["effect_id"]
        self._scene_cues = state["scene_cues"]
        self._lyrics = state["lyrics"]
        self._show_lyrics = state["preview_show_lyrics"]
        self._scene_auto = state["preview_scene_auto"]
        self.last_change = {"operation": "undo"}
        return True

    def redo(self) -> bool:
        if not self._redo:
            return False
        self._undo.append(self._snapshot())
        state = self._redo.pop()
        self._placements, self._title = state["placements"], state["title"]
        self._theme_id = state["theme_id"]
        self._record_id = state["record_id"]
        self._effect_id = state["effect_id"]
        self._scene_cues = state["scene_cues"]
        self._lyrics = state["lyrics"]
        self._show_lyrics = state["preview_show_lyrics"]
        self._scene_auto = state["preview_scene_auto"]
        self.last_change = {"operation": "redo"}
        return True

    def _put(self, item: dict, operation: str) -> dict:
        overlaps = [old for old in self._placements if old["id"] != item["id"]
                    and old["first_beat"] < item["end_beat"]
                    and item["first_beat"] < old["end_beat"]]
        removed = {old["id"] for old in overlaps} | {item["id"]}
        remaining = [old for old in self._placements if old["id"] not in removed] + [item]
        self._check_placements(remaining, authored_duration=False)
        self._checkpoint()
        self._placements = sorted(remaining, key=lambda x: x["first_beat"])
        self.last_change = {"operation": operation, "id": item["id"],
                            "replaced_ids": [old["id"] for old in overlaps],
                            "replaced": deepcopy(overlaps)}
        result = deepcopy(item)
        result["replaced_ids"] = list(self.last_change["replaced_ids"])
        return result

    def place_clip(self, clip_id: int, start_beat: int, count: int | None = None,
                   *, beat_count: int | None = None) -> dict:
        if count is not None and beat_count is not None and count != beat_count:
            raise ProjectError("动作拍数参数不一致。")
        count = beat_count if beat_count is not None else count
        count = self.default_count(clip_id, start_beat) if count is None else _integer(count, "动作拍数")
        if count not in self.allowed_counts(clip_id, start_beat):
            raise ProjectError("放不下完整动作，或拍数会让动作过快。请选择允许的拍数。")
        return self._put(self._placement(clip_id, start_beat, count), "place")

    def _find(self, identifier: str) -> dict:
        for item in self._placements:
            if item["id"] == identifier:
                return item
        raise ProjectError("这个动作已被替换或删除，请重新选择。")

    def update_clip(self, identifier: str, start_beat: int, beat_count: int) -> dict:
        """Apply position and duration together as one validated undo step.

        Checking the *new* duration at the *new* location matters: an eight
        beat balance can move near the end while changing to four beats even
        though moving its old eight-beat duration there would not fit.  Failed
        validation does not move the clip, remove overlaps, or alter history.
        """
        old = self._find(identifier)
        start_beat = _integer(start_beat, "起始拍")
        beat_count = _integer(beat_count, "动作拍数")
        if beat_count not in self.allowed_counts(old["clip_id"], start_beat):
            raise ProjectError("新位置放不下完整动作，或所选拍数会截短动作。请选择允许的拍数。")
        item = self._placement(old["clip_id"], start_beat, beat_count,
                               energy=old["energy"], identifier=identifier)
        if (item["first_beat"], item["beat_count"]) == (old["first_beat"], old["beat_count"]):
            return dict(deepcopy(old), replaced_ids=[])
        return self._put(item, "update")

    def move_clip(self, identifier: str, start_beat: int) -> dict:
        old = self._find(identifier)
        if old["beat_count"] not in self.allowed_counts(old["clip_id"], start_beat):
            raise ProjectError("新位置放不下完整动作，请向前拖动。")
        item = self._placement(old["clip_id"], start_beat, old["beat_count"],
                               energy=old["energy"], identifier=identifier)
        if item["first_beat"] == old["first_beat"]:
            return dict(deepcopy(old), replaced_ids=[])
        return self._put(item, "move")

    def resize_clip(self, identifier: str, beat_count: int) -> dict:
        old = self._find(identifier)
        if beat_count not in self.allowed_counts(old["clip_id"], old["first_beat"]):
            raise ProjectError("该动作仅允许完整的半速、原速或倍速拍数，不能截断抬腿动作。")
        item = self._placement(old["clip_id"], old["first_beat"], beat_count,
                               energy=old["energy"], identifier=identifier)
        if item["beat_count"] == old["beat_count"]:
            return dict(deepcopy(old), replaced_ids=[])
        return self._put(item, "resize")

    def remove_clip(self, identifier: str) -> dict:
        item = deepcopy(self._find(identifier))
        self._checkpoint()
        self._placements = [x for x in self._placements if x["id"] != identifier]
        self.last_change = {"operation": "delete", "id": identifier}
        return item

    delete_clip = remove_clip

    def clear(self):
        if self._placements:
            self._checkpoint()
            self._placements.clear()
            self.last_change = {"operation": "clear"}

    def set_audio_reactivity(self, controls: dict):
        """Attach measured audio controls without moving any authored clip."""
        validate_reactivity(controls, self.sample_count)
        self._base_plan["audio_reactivity"] = deepcopy(controls)
        self._analysis["audio_reactivity"] = deepcopy(controls)

    @property
    def audio_reactivity(self):
        return deepcopy(self._base_plan.get("audio_reactivity"))

    def recompose_automatic(self):
        """Rebuild automatic phrases while keeping all hand-placed actions."""
        analysis = self.analysis
        analysis.setdefault("quality", {}).setdefault("consistency_score", .7)
        analysis["audio_reactivity"] = self.audio_reactivity
        plan = make_plan(analysis, self.title)
        actual = [b["sample"] for b in plan["beats"] if not b["flags"] & SENTINEL]
        if actual != [b["sample"] for b in self._beats if not b["flags"] & SENTINEL]:
            raise ProjectError("重新编排不能更改原音乐拍点。")
        manual = [deepcopy(x) for x in self._placements if x.get("origin") == "manual"]
        automatic = []
        for segment in plan["segments"]:
            if segment["first_beat"] == 65535:
                continue
            item = self._placement(segment["clip_id"], segment["first_beat"], segment["beat_count"],
                                   energy=segment["energy"], origin="automatic")
            if not any(item["first_beat"] < old["end_beat"] and old["first_beat"] < item["end_beat"] for old in manual):
                automatic.append(item)
        self._checkpoint()
        self._placements = sorted(manual + automatic, key=lambda x: x["first_beat"])
        self.last_change = {"operation": "recompose", "preserved_manual": len(manual)}
        return self.last_change

    def build_plan(self) -> dict:
        """Compile full neutral-to-neutral clips, keeping every beat sample exact."""
        self._check_placements(self._placements, authored_duration=False)
        segments = []

        def append(first, count, clip, energy, reason):
            segments.append({"start_sample": self.sample_at_beat(first),
                             "end_sample": self.sample_at_beat(first+count),
                             "first_beat": first, "beat_count": count,
                             "clip_id": clip, "energy": energy, "reason": reason})

        def fill(first, last):
            while first < last:
                length = min(4, last-first)
                append(first, length, 0, 19660, "自动衔接：完整舒缓滑行")
                first += length

        if self.sample_at_beat(self.first_beat) > 0:
            segments.append({"start_sample": 0, "end_sample": self.sample_at_beat(self.first_beat),
                             "first_beat": 65535, "beat_count": 0,
                             "clip_id": 0, "energy": self._intro_energy, "reason": "首拍前自然入场"})
        cursor = self.first_beat
        for item in self.placements:
            fill(cursor, item["first_beat"])
            append(item["first_beat"], item["beat_count"], item["clip_id"], item["energy"],
                   "手动编排：完整动作衔接" if item["origin"] == "manual" else "原自动编排")
            cursor = item["end_beat"]
        fill(cursor, self.last_beat)
        if self.sample_at_beat(self.last_beat) < self.sample_count:
            segments.append({"start_sample": self.sample_at_beat(self.last_beat),
                             "end_sample": self.sample_count, "first_beat": 65535,
                             "beat_count": 0, "clip_id": 0, "energy": self._outro_energy,
                             "reason": "最后拍点后自然收势"})
        plan = deepcopy(self._base_plan)
        plan.update(title=self._title, beats=deepcopy(self._beats), segments=segments,
                    pcm_sha256=self._pcm_sha256, choreography_version="studio-1.9",
                    theme_id=self._theme_id,
                    record_id=self._record_id, effect_id=self._effect_id, scene_revision=3,
                    clips=deepcopy(list(CLIPS)),
                    editor={"schema": SCHEMA, "neutral_endpoints": True,
                            "unchanged_detected_beats": True})
        plan.update(compatibility_metadata(plan))
        try:
            validate_plan(plan)
        except AssertionError as exc:
            raise ProjectError("动作计划无法完整覆盖歌曲，请检查拍点。") from exc
        return plan

    def smooth(self) -> dict:
        plan = self.build_plan()
        self.continuity_report = {
            "segments": len(plan["segments"]), "joins": max(0, len(plan["segments"])-1),
            "gap_fill_segments": sum(x.get("reason", "").startswith("自动衔接") for x in plan["segments"]),
            "neutral_endpoints": True, "zero_endpoint_velocity": True,
            "unchanged_detected_beats": True, "truncated_actions": 0,
            "message": "已用完整动作和舒缓滑行补齐空隙；动作首尾回到同一姿态，保留原音乐拍点。"}
        return plan

    def export(self, path: Path | str) -> dict:
        path = Path(path).resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        return write_package(self.smooth(), self._pcm.tobytes(), path)

    def save(self, path: Path | str) -> Path:
        """Save portable JSON plus a neighbouring PCM WAV, with audio SHA-256."""
        path = Path(path).resolve()
        if path.suffix.lower() != ".vskproj":
            path = path.with_suffix(".vskproj")
        path.parent.mkdir(parents=True, exist_ok=True)
        # Content-addressed audio avoids a half-written save referring to new
        # audio under an older project file and makes repeated saves cheap.
        wav = path.with_name(path.stem + "." + self._pcm_sha256[:12] + ".wav")
        if not wav.is_file():
            temp = wav.with_name(wav.name + ".tmp-" + uuid.uuid4().hex)
            try:
                with wave.open(str(temp), "wb") as writer:
                    writer.setparams((1, 2, RATE, self.sample_count, "NONE", "not compressed"))
                    writer.writeframes(self._pcm.tobytes())
                os.replace(temp, wav)
            finally:
                if temp.exists():
                    temp.unlink()
        elif _sha(_pcm_wave(wav)) != self._pcm_sha256:
            raise ProjectError("工程旁已有同名音频，但校验失败。请换一个工程文件名保存。")
        document = {"schema": SCHEMA, "version": 1, "title": self._title,
                    "audio": {"path": wav.name, "pcm_sha256": self._pcm_sha256,
                              "sample_count": self.sample_count, "sample_rate": RATE},
                    "source": self.source, "analysis": self.analysis,
                    "plan": self.build_plan(), "placements": self.placements,
                    "scene_cues": self.scene_cues,
                    "lyrics": self.lyrics,
                    "preview_show_lyrics": self.show_lyrics,
                    "preview_scene_auto": self.scene_auto}
        temp = path.with_name(path.name + ".tmp-" + uuid.uuid4().hex)
        try:
            temp.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(temp, path)
        finally:
            if temp.exists():
                temp.unlink()
        self.path, self.pcm_wav = path, str(wav)
        return path

    @classmethod
    def load(cls, path: Path | str) -> "EditorProject":
        path = Path(path).resolve()
        if path.suffix.lower() in (".vsk2", ".vsk3"):
            result = cls.from_result(read_package(path))
            result.source = str(path)
            return result
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ProjectError(f"无法打开动作工程：{exc}") from exc
        if data.get("schema") != SCHEMA:
            if path.suffix.lower() == ".vskproj":
                raise ProjectError("该动作工程版本无法识别。")
            return cls.from_result(data, path.parent)
        if data.get("version") != 1:
            raise ProjectError("该动作工程版本无法识别。")
        try:
            audio = data["audio"]
            wav = Path(audio["path"])
            if not wav.is_absolute():
                wav = path.parent / wav
            pcm = _pcm_wave(wav)
            if _sha(pcm) != audio["pcm_sha256"] or len(pcm) != audio["sample_count"] or audio["sample_rate"] != RATE:
                raise ProjectError("工程音频被替换或损坏，SHA-256 校验失败。")
            result = cls(data["plan"], pcm, data.get("analysis"), data.get("source"), str(wav.resolve()))
            placements = deepcopy(data["placements"])
            result._check_placements(placements, authored_duration=False)
            # Preserve the original on disk while retiring its unwanted
            # actions in memory. The UI prompts Save As for migrated files.
            result.migration_report = {"removed_count": 0, "removed_clips": [], "message": ""}
            for item in placements:
                if item["clip_id"] in RETIRED_CLIP_IDS:
                    result._record_retired(item["clip_id"])
            placements = [item for item in placements if item["clip_id"] not in RETIRED_CLIP_IDS]
            # Automatic old neutral tail clips may use 1 or 3 beats. Manual
            # clips always obey authored duration rules, including on load.
            for item in placements:
                if item.get("origin") == "manual" and item["beat_count"] not in result.allowed_counts(item["clip_id"], item["first_beat"]):
                    raise ProjectError("工程含有被截短的手动动作。")
            result._placements = placements
            result._title = str(data.get("title", result._title))
            result._scene_cues = result._validate_scene_cues(data.get("scene_cues"))
            result._lyrics = result._validate_lyrics(data.get("lyrics"))
            result._show_lyrics = result._validate_show_lyrics(data.get("preview_show_lyrics", True))
            if "preview_scene_auto" in data:
                setting = data["preview_scene_auto"]
                if (not isinstance(setting, dict)
                        or set(setting) != {"mode", "include_character"}):
                    raise ProjectError("工程的场景自动切换设置不完整。")
                result._scene_auto = result._validate_scene_auto(
                    setting["mode"], setting["include_character"])
            result.build_plan()
            result.path = path
            return result
        except (KeyError, TypeError, IndexError) as exc:
            raise ProjectError("工程数据不完整。") from exc
