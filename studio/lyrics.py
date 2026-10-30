"""PCM-clock lyric lines, independent from inferred scene-switch endpoints.

LRC timestamps are lyric starts. A timestamped empty line clears the display;
without one, the line lasts to the next lyric start or the end of the song.
These are line subtitles, never word-by-word alignment or speech recognition.
"""
from __future__ import annotations

from bisect import bisect_right
from collections import defaultdict
from numbers import Integral

from .phrase_cues import (RATE, MAX_CUES, MAX_TEXT, MAX_NOTES,
                          _STAMP, _OFFSET, _WORD_STAMP)

MAX_LINES = MAX_CUES
_LYRIC_NOTE = ("歌词按 LRC 句首时间显示；带时间空行清屏，普通行显示到下一句开始，"
               "末句无结束标记时显示到歌曲结束，不表示逐字对齐。")


def _integer(value, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, Integral):
        raise ValueError(f"{label}必须是整数。")
    return int(value)


def _text(value, label: str, maximum: int, *, empty: bool = True) -> str:
    if not isinstance(value, str) or len(value) > maximum:
        raise ValueError(f"{label}必须是长度不超过 {maximum} 的文字。")
    result = value.strip()
    if not empty and not result:
        raise ValueError(f"{label}不能为空。")
    if any(ord(char) < 32 and char not in "\t\n\r" for char in result):
        raise ValueError(f"{label}包含无效控制字符。")
    return result


def validate_lyrics(document, sample_count) -> dict:
    """Validate/copy a bounded v1 track; merge text at identical starts.

    Duplicate-start lines use the longest supplied end and unique texts in
    their original order. Other overlaps are rejected, so a binary lookup has
    exactly one unambiguous active line. No input dictionary is modified.
    """
    total = _integer(sample_count, "歌曲采样数")
    if total < 1:
        raise ValueError("歌曲采样数必须大于零。")
    if document is None:
        document = {"version": 1, "sample_rate": RATE, "source": "none", "lines": [], "notes": []}
    if not isinstance(document, dict):
        raise ValueError("歌词轨道必须是对象。")
    if _integer(document.get("version"), "歌词轨道版本") != 1:
        raise ValueError("不支持的歌词轨道版本。")
    if _integer(document.get("sample_rate"), "歌词采样率") != RATE:
        raise ValueError("歌词轨道需要 16 kHz 采样率。")
    source = _text(document.get("source"), "歌词来源", 128, empty=False)
    if source not in ("lrc", "none"):
        raise ValueError("歌词来源必须是 lrc 或 none。")
    lines, notes = document.get("lines"), document.get("notes", [])
    if not isinstance(lines, list) or len(lines) > MAX_LINES:
        raise ValueError(f"歌词行必须是列表，最多 {MAX_LINES} 行。")
    if not isinstance(notes, list) or len(notes) > MAX_NOTES:
        raise ValueError(f"歌词说明必须是列表，最多 {MAX_NOTES} 条。")
    normalized = {}
    for line in lines:
        if not isinstance(line, dict):
            raise ValueError("每行歌词必须是对象。")
        start = _integer(line.get("start_sample"), "歌词开始采样位置")
        end = _integer(line.get("end_sample"), "歌词结束采样位置")
        if not 0 <= start < end <= total:
            raise ValueError("歌词起止时间必须位于歌曲范围内，且结束晚于开始。")
        words = _text(line.get("text"), "歌词", MAX_TEXT, empty=False)
        if start not in normalized:
            normalized[start] = {"end": end, "texts": [words]}
        else:
            normalized[start]["end"] = max(end, normalized[start]["end"])
            if words not in normalized[start]["texts"]:
                normalized[start]["texts"].append(words)
    result = []
    for start in sorted(normalized):
        item = normalized[start]
        words = " / ".join(item["texts"])
        if len(words) > MAX_TEXT:
            raise ValueError("同一时间合并后的歌词过长。")
        if result and result[-1]["end_sample"] > start:
            raise ValueError("歌词行的时间范围重叠，请检查起止时间。")
        result.append({"start_sample": start, "end_sample": item["end"], "text": words})
    normalized_notes = list(dict.fromkeys(_text(note, "歌词说明", 2048) for note in notes))
    return {"version": 1, "sample_rate": RATE, "source": source,
            "lines": result, "notes": normalized_notes}


def lyrics_from_lrc(text, sample_count, sample_rate=RATE) -> dict:
    """Parse actual LRC starts, offset and explicit empty-line clears.

    Enhanced-LRC word markers are removed but do not create karaoke timings.
    A valid final lyric always has a display interval ending at song end.
    """
    total = _integer(sample_count, "歌曲采样数")
    if total < 1:
        raise ValueError("歌曲采样数必须大于零。")
    if _integer(sample_rate, "采样率") != RATE:
        raise ValueError("歌词轨道需要 16 kHz 采样率。")
    if not isinstance(text, str) or len(text) > 2 * 1024 * 1024:
        raise ValueError("LRC 必须是小于 2 MiB 的文字。")
    offsets = _OFFSET.findall(text)
    offset = int(offsets[-1]) if offsets else 0
    if abs(offset) > 24 * 60 * 60 * 1000:
        raise ValueError("LRC offset 超出合理范围。")
    lyrics = defaultdict(list)
    empty = set()
    ignored = timestamps = valid_timestamps = 0
    for line in text.lstrip("\ufeff").splitlines():
        stamps = list(_STAMP.finditer(line))
        if not stamps:
            continue
        timestamps += len(stamps)
        words = _WORD_STAMP.sub("", _STAMP.sub("", line)).strip()
        if len(words) > MAX_TEXT:
            raise ValueError("单句 LRC 歌词过长。")
        for stamp in stamps:
            minute, second, fraction = stamp.groups()
            milliseconds = (int(minute) * 60 + int(second)) * 1000
            milliseconds += int((fraction or "0").ljust(3, "0")) + offset
            sample = milliseconds * RATE // 1000
            if not 0 <= sample < total:
                ignored += 1
                continue
            valid_timestamps += 1
            if words:
                if words not in lyrics[sample]:
                    lyrics[sample].append(words)
            else:
                empty.add(sample)
    if not timestamps:
        raise ValueError("没有找到有效的 LRC 时间戳（如 [00:12.34]）；请导入带时间轴的歌词。")
    if not valid_timestamps:
        raise ValueError("LRC 时间戳全部超出歌曲范围，请检查歌曲长度和 offset 偏移。")
    if not lyrics:
        raise ValueError("LRC 没有位于歌曲范围内的带文字歌词。")
    if len(lyrics) + len(empty) > MAX_LINES:
        raise ValueError(f"LRC 时间位置最多 {MAX_LINES} 个。")
    starts = sorted(lyrics)
    lines = []
    for index, start in enumerate(starts):
        following = starts[index + 1] if index + 1 < len(starts) else total
        ends = [end for end in empty if start < end <= following]
        end = min(ends) if ends else following
        lines.append({"start_sample": start, "end_sample": end, "text": " / ".join(lyrics[start])})
    notes = [_LYRIC_NOTE]
    if offsets:
        notes.append(f"已应用 LRC offset {offset} 毫秒。")
    if ignored:
        notes.append(f"忽略 {ignored} 个位于音频范围以外的 LRC 时间戳。")
    return validate_lyrics({"version": 1, "sample_rate": RATE, "source": "lrc",
                            "lines": lines, "notes": notes}, total)


def _start(line):
    return line["start_sample"]


def current_lyric(document, sample, sample_count, *, loop=False) -> str:
    """Look up a normalized track at a PCM sample; no copies or wall clock.

    Starts are inclusive and ends exclusive. Negative samples clear the
    display; nonnegative loop positions wrap by the exact PCM sample count.
    """
    total = _integer(sample_count, "歌曲采样数")
    position = _integer(sample, "歌词播放位置")
    if total < 1:
        raise ValueError("歌曲采样数必须大于零。")
    if position < 0 or not loop and position >= total:
        return ""
    if loop:
        position %= total
    lines = document.get("lines", []) if isinstance(document, dict) else []
    index = bisect_right(lines, position, key=_start) - 1
    if index >= 0 and position < lines[index]["end_sample"]:
        return lines[index]["text"]
    return ""
