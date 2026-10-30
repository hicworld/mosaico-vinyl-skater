"""Desktop-only phrase boundary candidates, never automatic lyric recognition.

The audio fallback measures sustained low RMS in a 250--3500 Hz activity
proxy. Instruments also occupy that band: these candidates are reviewable
estimates, not a vocal separator, transcript, or forced alignment. There is
no beat-grid or fixed-time fallback when the audio supplies no useful pause.

Filtering uses SciPy's stable SOS form; RMS silence thresholds follow the
usual energy-relative approach, with additional duration/activity guards:
https://docs.scipy.org/doc/scipy/reference/generated/scipy.signal.sosfilt.html
https://librosa.org/doc/latest/generated/librosa.effects.split.html
"""
from __future__ import annotations

from collections import defaultdict
from numbers import Integral
import re

import numpy as np
from scipy.ndimage import median_filter
from scipy.signal import butter, sosfilt

RATE = 16000
MAX_CUES = 4096
MAX_TEXT = 4000
MAX_NOTES = 32
_HOP = 160                         # 10 ms; each RMS uses two adjacent hops.
_MIN_PAUSE_FRAMES = 28              # 280 ms, after closing <=50 ms holes.
_AUDIO_NOTE = ("这是音频停顿估算，不是歌词识别：中频也包含乐器，可能漏切或误切；"
               "连续伴奏、纯音乐或没有明显停顿时可以没有切换点。")
_LRC_NOTE = ("普通 LRC 的时间戳通常是句首；没有带时间空行结束标记时，"
             "句尾取音频停顿估计，或以下一句开始时间作为边界估计，均不是精确句末。")


def _int(value, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, Integral):
        raise ValueError(f"{label}必须是整数。")
    return int(value)


def _string(value, label: str, maximum: int, *, empty: bool = True) -> str:
    if not isinstance(value, str) or len(value) > maximum:
        raise ValueError(f"{label}必须是长度不超过 {maximum} 的文字。")
    value = value.strip()
    if not empty and not value:
        raise ValueError(f"{label}不能为空。")
    return value


def validate_scene_cues(document, sample_count) -> dict:
    """Validate and copy v1 cues; sort and collapse duplicate sample positions.

    Invalid times are rejected rather than silently clamped. At an equal
    sample the first cue wins, except that a later nonempty text may fill an
    empty text. Unknown source names survive for future transcript providers.
    ``None`` is the empty legacy-project default. No argument is mutated.
    """
    total = _int(sample_count, "音频采样数")
    if total < 1:
        raise ValueError("音频采样数必须大于零。")
    if document is None:
        document = {"version": 1, "sample_rate": RATE, "source": "none",
                    "cues": [], "notes": []}
    if not isinstance(document, dict):
        raise ValueError("句尾切换数据必须是对象。")
    if _int(document.get("version"), "句尾数据版本") != 1:
        raise ValueError("不支持的句尾数据版本。")
    if _int(document.get("sample_rate"), "句尾采样率") != RATE:
        raise ValueError("句尾数据需要 16 kHz 采样率。")
    source = _string(document.get("source"), "句尾来源", 128, empty=False)
    cues, notes = document.get("cues"), document.get("notes", [])
    if not isinstance(cues, list) or len(cues) > MAX_CUES:
        raise ValueError(f"句尾切换点必须是列表，最多 {MAX_CUES} 个。")
    if not isinstance(notes, list) or len(notes) > MAX_NOTES:
        raise ValueError(f"句尾说明必须是列表，最多 {MAX_NOTES} 条。")
    normalized = {}
    for cue in cues:
        if not isinstance(cue, dict):
            raise ValueError("每个句尾切换点必须是对象。")
        sample = _int(cue.get("sample"), "句尾采样位置")
        if not 0 < sample < total:
            raise ValueError("句尾切换点必须位于音频内部。")
        text = _string(cue.get("text", ""), "歌词", MAX_TEXT)
        origin = _string(cue.get("source", source), "切换点来源", 128, empty=False)
        if sample not in normalized:
            normalized[sample] = {"sample": sample, "text": text, "source": origin}
        elif not normalized[sample]["text"] and text:
            normalized[sample]["text"] = text
    normalized_notes = list(dict.fromkeys(_string(n, "句尾说明", 2048) for n in notes))
    return {"version": 1, "sample_rate": RATE, "source": source,
            "cues": [normalized[s] for s in sorted(normalized)],
            "notes": normalized_notes}


def _pcm(pcm, sample_rate):
    if _int(sample_rate, "采样率") != RATE:
        raise ValueError("请先将歌曲转换为 16 kHz 单声道 PCM。")
    values = np.asarray(pcm)
    if values.ndim != 1 or values.dtype.kind not in "if" or not len(values):
        raise ValueError("句尾分析需要非空的单声道 PCM 数组。")
    if values.dtype.kind == "i":
        if values.dtype.itemsize != 2:
            raise ValueError("整数 PCM 需要 int16 格式。")
        return values.astype(np.float32) / 32768.0
    if not np.all(np.isfinite(values)) or np.max(np.abs(values)) > 1.00001:
        raise ValueError("浮点 PCM 必须有限且位于 -1 至 1。")
    return values.astype(np.float32, copy=True)


def _runs(mask):
    changes = np.diff(np.r_[False, mask, False].astype(np.int8))
    return list(zip(np.flatnonzero(changes == 1), np.flatnonzero(changes == -1)))


def _features(values):
    """Return sustained quiet runs and activity at a 10 ms sample grid."""
    bins = len(values) // _HOP
    if bins < 2:
        return [], np.zeros(0, dtype=bool)
    # float32 SOS keeps whole-song storage bounded; no window-sized copy of
    # every overlapping frame and no spectrogram or model download is needed.
    sos = butter(3, (250, 3500), btype="bandpass", fs=RATE, output="sos").astype(np.float32)
    band = sosfilt(sos, values)

    def rms(signal):
        block = signal[:bins * _HOP].reshape(bins, _HOP)
        power = np.einsum("ij,ij->i", block, block, dtype=np.float64) / _HOP
        return np.sqrt((power[:-1] + power[1:]) * 0.5)

    mid, full = median_filter(rms(band), size=5, mode="nearest"), rms(values)
    reference = float(np.percentile(mid, 85))
    full_reference = float(np.percentile(full, 85))
    if reference < 0.00015 or full_reference < 0.0002:
        return [], np.zeros(len(mid), dtype=bool)
    quiet = (mid < reference * 0.28) & (full < full_reference * 1.25)
    # A brief drum/hihat transient inside a breath must not split the breath.
    for a, b in _runs(~quiet):
        if a and b < len(quiet) and b - a <= 5:
            quiet[a:b] = True
    runs = [(int(a), int(b)) for a, b in _runs(quiet) if b - a >= _MIN_PAUSE_FRAMES]
    active = mid >= reference * 0.40
    return runs, active


def estimate_phrase_ends(pcm, sample_rate=RATE) -> dict:
    """Conservatively propose audio pause onsets, not lyric/transcript ends."""
    values = _pcm(pcm, sample_rate)
    runs, active = _features(values)
    cues = []
    last = -2 * RATE
    first_active = int(np.argmax(active)) if np.any(active) else len(active)
    for a, b in runs:
        sample = a * _HOP
        preceding = active[max(0, a - 200):a]
        # Reject leading silence, isolated kick/snare hits, syllable gaps and
        # repeated fast switches. Never insert a fabricated periodic cue.
        if (sample - last < 2 * RATE or a - first_active < 80 or
                np.count_nonzero(preceding) < 70 or
                np.mean(preceding) < 0.45 or not 0 < sample < len(values)):
            continue
        cues.append({"sample": sample, "text": "", "source": "audio_pause_estimate"})
        last = sample
    notes = [_AUDIO_NOTE, "候选取至少 280 毫秒停顿的起点；两次候选至少间隔 2 秒，不按鼓点或固定时钟补点。"]
    if not cues:
        notes.append("没有找到可靠的持续停顿候选，未生成切换点。")
    return validate_scene_cues({"version": 1, "sample_rate": RATE,
                               "source": "audio_pause_estimate", "cues": cues,
                               "notes": notes}, len(values))


_STAMP = re.compile(r"\[(\d{1,3}):([0-5]\d)(?:[.:](\d{1,3}))?\]")
_OFFSET = re.compile(r"\[offset\s*:\s*([+-]?\d+)\s*\]", re.I)
_WORD_STAMP = re.compile(r"<\d{1,3}:[0-5]\d(?:[.:]\d{1,3})?>")


def parse_lrc(text, pcm, sample_rate=RATE) -> dict:
    """Read authored empty-line ends; infer ordinary start-only LRC ends.

    A timestamped empty line explicitly ends its preceding lyric. Otherwise
    a sustained pause before the next lyric is preferred. With no such pause,
    that next lyric's *start* is an explicitly labelled boundary estimate.
    The last lyric without an end marker or usable pause produces no cue.
    """
    values = _pcm(pcm, sample_rate)
    if not isinstance(text, str) or len(text) > 2 * 1024 * 1024:
        raise ValueError("LRC 必须是小于 2 MiB 的文字。")
    offsets = _OFFSET.findall(text)
    offset = int(offsets[-1]) if offsets else 0
    if abs(offset) > 24 * 60 * 60 * 1000:
        raise ValueError("LRC offset 超出合理范围。")
    lyrics = defaultdict(list)
    empty = set()
    ignored = 0
    timestamp_count = 0
    valid_timestamps = 0
    for line in text.lstrip("\ufeff").splitlines():
        stamps = list(_STAMP.finditer(line))
        if not stamps:
            continue
        timestamp_count += len(stamps)
        words = _WORD_STAMP.sub("", _STAMP.sub("", line)).strip()
        if len(words) > MAX_TEXT:
            raise ValueError("单句 LRC 歌词过长。")
        for stamp in stamps:
            minute, second, fraction = stamp.groups()
            milliseconds = (int(minute) * 60 + int(second)) * 1000
            milliseconds += int((fraction or "0").ljust(3, "0")) + offset
            sample = milliseconds * RATE // 1000
            if not 0 <= sample < len(values):
                ignored += 1
                continue
            valid_timestamps += 1
            if words:
                if words not in lyrics[sample]:
                    lyrics[sample].append(words)
            else:
                empty.add(sample)
    if len(lyrics) + len(empty) > MAX_CUES:
        raise ValueError(f"LRC 时间位置最多 {MAX_CUES} 个。")
    if not timestamp_count:
        raise ValueError("没有找到有效的 LRC 时间戳（如 [00:12.34]）；请导入带时间轴的歌词。")
    if not valid_timestamps:
        raise ValueError("LRC 时间戳全部超出歌曲范围，请检查歌曲长度和 offset 偏移。")
    if not lyrics:
        raise ValueError("LRC 没有位于歌曲范围内的带文字歌词；仅有空时间行不能生成句尾切换点。")
    starts = sorted(lyrics)
    quiet, _ = _features(values) if starts else ([], None)
    cues = []
    for index, start in enumerate(starts):
        following = starts[index + 1] if index + 1 < len(starts) else len(values)
        explicit = sorted(s for s in empty if start < s <= following and s < len(values))
        origin = "lrc_explicit_end"
        if explicit:
            sample = explicit[0]
        else:
            # Last phrase has no following start to delimit the search. Do not
            # attach a distant outro pause to a lyric minutes earlier.
            upper = following if index + 1 < len(starts) else min(following, start + 12 * RATE)
            pauses = [a * _HOP for a, b in quiet
                      if start + RATE // 4 < a * _HOP < upper and b * _HOP > start]
            if pauses:
                sample, origin = pauses[-1], "lrc_pause_estimate"
            elif index + 1 < len(starts):
                sample, origin = following, "lrc_next_start_estimate"
            else:
                continue
        if 0 < sample < len(values):
            cues.append({"sample": sample, "text": " / ".join(lyrics[start]), "source": origin})
    notes = [_LRC_NOTE]
    if ignored:
        notes.append(f"忽略 {ignored} 个位于音频范围以外的 LRC 时间戳。")
    if offsets:
        notes.append(f"已应用 LRC offset {offset} 毫秒。")
    if not cues:
        notes.append("没有可用的歌词结束标记或句尾候选。")
    return validate_scene_cues({"version": 1, "sample_rate": RATE, "source": "lrc",
                               "cues": cues, "notes": notes}, len(values))
