"""Bounded local LRC loading; ordinary timestamps still mean lyric starts.

This module supplies file/encoding handling only. Phrase end inference remains
in ``phrase_cues.parse_lrc`` and does not claim transcript or word alignment.
"""
from __future__ import annotations

import codecs
from pathlib import Path
import re
import stat

from .phrase_cues import RATE, parse_lrc, validate_scene_cues
from .lyrics import lyrics_from_lrc, validate_lyrics

MAX_LRC_BYTES = 1024 * 1024
_METADATA = re.compile(r"^\s*\[(ar|al|ti|au|by|re|ve|length)\s*:(.*?)\]\s*$", re.I)
_METADATA_LABELS = {"ar": "歌手", "al": "专辑", "ti": "歌名", "au": "作者",
                    "by": "制作者", "re": "工具", "ve": "版本", "length": "时长"}


def _decode(data: bytes) -> tuple[str, str]:
    if data.startswith(codecs.BOM_UTF8):
        choices = (("utf-8-sig", "UTF-8 BOM"),)
    elif data.startswith(codecs.BOM_UTF16_LE):
        choices = (("utf-16", "UTF-16 LE BOM"),)
    elif data.startswith(codecs.BOM_UTF16_BE):
        choices = (("utf-16", "UTF-16 BE BOM"),)
    else:
        # GB18030 includes the GBK repertoire. No replacement decoding or
        # permissive Latin-1 fallback can disguise a malformed lyric file.
        choices = (("utf-8", "UTF-8"), ("gb18030", "GB18030 / GBK"))
    for codec, label in choices:
        try:
            text = data.decode(codec, errors="strict")
        except UnicodeError:
            continue
        if any((ord(char) < 32 and char not in "\t\r\n") or 0x7F <= ord(char) < 0xA0
               for char in text):
            raise ValueError("LRC 包含二进制或控制字符，请使用 UTF-8、带 BOM 的 UTF-16 或 GBK 歌词文件。")
        if not text.strip():
            raise ValueError("LRC 文件没有歌词文字。")
        return text, label
    raise ValueError("无法严格解码 LRC，请将歌词保存为 UTF-8、带 BOM 的 UTF-16 或 GBK / GB18030。")


def _metadata_note(text: str) -> str | None:
    metadata = {}
    for line in text.lstrip("\ufeff").splitlines():
        match = _METADATA.fullmatch(line)
        if match:
            key, value = match.groups()
            value = " ".join(value.split())
            if value:
                metadata[key.lower()] = value[:100]
    if not metadata:
        return None
    # Bounded display notes, never an absolute source path or a new schema.
    fields = [f"{_METADATA_LABELS[key]}：{value}" for key, value in metadata.items()]
    return "LRC 信息：" + "；".join(fields[:6]) + "。"


def _read_lrc_text(path) -> tuple[str, str, str]:
    """Load one bounded local file once, returning text, encoding and name."""
    target = Path(path)
    try:
        info = target.stat()
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("请选择 LRC 歌词文件。")
        if info.st_size > MAX_LRC_BYTES:
            raise ValueError("LRC 文件超过 1 MiB，请使用较小的歌词文件。")
        with target.open("rb") as handle:
            # The bounded read also protects against growth after stat().
            data = handle.read(MAX_LRC_BYTES + 1)
    except OSError as exc:
        raise ValueError("无法读取 LRC 文件，请检查文件是否存在且可访问。") from exc
    if len(data) > MAX_LRC_BYTES:
        raise ValueError("LRC 文件超过 1 MiB，请使用较小的歌词文件。")
    if not data:
        raise ValueError("LRC 文件为空。")
    text, encoding = _decode(data)
    return text, encoding, target.name[:180]


def read_lrc_bundle(path, pcm, sample_rate=RATE) -> dict:
    """Decode once; return scene endpoints and true-start display lyrics.

    Each document is fully validated before returning. A valid single line
    may provide no scene cue but still display through the end of the song.
    """
    text, encoding, filename = _read_lrc_text(path)
    scene_cues = parse_lrc(text, pcm, sample_rate)
    lyrics = lyrics_from_lrc(text, len(pcm), sample_rate)
    notes = [f"歌词文件：{filename}；编码：{encoding}。"]
    metadata = _metadata_note(text)
    if metadata:
        notes.append(metadata)
    scene_cues["notes"].extend(notes)
    lyrics["notes"].extend(notes)
    return {"scene_cues": validate_scene_cues(scene_cues, len(pcm)),
            "lyrics": validate_lyrics(lyrics, len(pcm))}


def read_lrc_file(path, pcm, sample_rate=RATE) -> dict:
    """Backward-compatible scene-cue-only view of a bounded local LRC.

    Invalid files raise ValueError. A valid single line with no usable scene
    endpoint returns zero cues; callers should retain their existing cues.
    """
    return read_lrc_bundle(path, pcm, sample_rate)["scene_cues"]


def find_companion_lrc(audio_path) -> Path | None:
    """Find only same-directory, same-stem .lrc (case-insensitive).

    Similar song names, subdirectories and generic lyric files never match.
    Deterministic ordering handles case variants on case-sensitive volumes.
    """
    audio = Path(audio_path)
    try:
        candidates = [candidate for candidate in audio.parent.iterdir()
                      if candidate.suffix.casefold() == ".lrc"
                      and candidate.stem.casefold() == audio.stem.casefold()
                      and candidate.is_file()]
    except OSError:
        return None
    if not candidates:
        return None
    return min(candidates, key=lambda candidate: (candidate.stem != audio.stem,
               candidate.suffix != ".lrc", candidate.name.casefold(), candidate.name))
