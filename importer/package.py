"""VSK2 legacy / VSK3 audio-control contract shared with the native player."""
from __future__ import annotations

import hashlib
from pathlib import Path
import struct
import zlib

from .planner import compatibility_metadata, validate_plan
from .reactivity import encode_reactivity, decode_reactivity
from .themes import style_word, style_from_word, effect_from_word

HEADER = struct.Struct("<4sHH10I80s")
BEAT = struct.Struct("<IHH")
SEGMENT = struct.Struct("<IIHBBHH")


def utf8_title(title: str) -> bytes:
    encoded = title.encode("utf-8")[:79]
    while True:
        try:
            encoded.decode("utf-8")
            return encoded + bytes(80 - len(encoded))
        except UnicodeDecodeError:
            encoded = encoded[:-1]


def package_bytes(plan: dict, pcm_bytes: bytes) -> bytes:
    validate_plan(plan)
    if len(pcm_bytes) != plan["sample_count"] * 2:
        raise ValueError("音频长度与动作计划不一致。")
    beats = b"".join(BEAT.pack(x["sample"], x["strength"], x["flags"]) for x in plan["beats"])
    segments = b"".join(SEGMENT.pack(x["start_sample"], x["end_sample"], x["first_beat"],
        x["beat_count"], x["clip_id"], x["energy"], 0) for x in plan["segments"])
    beat_offset = HEADER.size
    plan_offset = beat_offset + len(beats)
    features = plan.get("audio_reactivity")
    controls = encode_reactivity(features, plan["sample_count"]) if features is not None else b""
    version = 3 if controls else 2
    pcm_offset = plan_offset + len(segments) + len(controls)
    payload = beats + segments + controls + pcm_bytes
    header = HEADER.pack(b"VSK3" if version == 3 else b"VSK2", version, HEADER.size, plan["sample_rate"], plan["sample_count"],
        len(plan["beats"]), len(plan["segments"]), beat_offset, plan_offset, pcm_offset,
        HEADER.size + len(payload), zlib.crc32(payload),
        style_word(plan.get("theme_id", 0), plan.get("record_id", 0), plan.get("scene_revision", 0),
                   effect_id=plan.get("effect_id", 0)), utf8_title(plan["title"]))
    return header + payload


def write_package(plan: dict, pcm_bytes: bytes, path: Path) -> dict:
    data = package_bytes(plan, pcm_bytes)
    path.write_bytes(data)
    version = HEADER.unpack_from(data)[1]
    return {"path": str(path.resolve()), "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(),
            "payload_crc32": f"{zlib.crc32(data[128:]):08x}", "format": f"VSK{version}", "version": version,
            **compatibility_metadata(plan)}


def read_package(path: Path) -> dict:
    data = Path(path).read_bytes()
    if len(data) < HEADER.size:
        raise ValueError("歌曲包不完整。")
    magic, version, size, rate, samples, nbeats, nplans, boff, poff, aoff, total, crc, reserved, title = HEADER.unpack_from(data)
    if ((magic, version) not in ((b"VSK2", 2), (b"VSK3", 3))
            or (size, rate) != (128, 16000)):
        raise ValueError("歌曲包格式不支持。")
    theme, record_id, scene_revision = style_from_word(reserved)
    metadata_end = poff + nplans * 16
    if (not 4 * 16000 <= samples <= 30 * 60 * 16000 or not 1 <= nbeats <= 10000 or not 1 <= nplans <= 5000
            or not (boff == 128 and poff == boff + nbeats * 8 and aoff >= metadata_end
                    and total == aoff + samples * 2 == len(data))
            or (version == 2 and aoff != metadata_end)):
        raise ValueError("歌曲包长度或索引错误。")
    if zlib.crc32(data[128:]) != crc:
        raise ValueError("歌曲包 CRC 校验失败。")
    beats = [dict(zip(("sample", "strength", "flags"), BEAT.unpack_from(data, boff + index * 8))) for index in range(nbeats)]
    segments = []
    for index in range(nplans):
        start, end, first, length, clip, energy, padding = SEGMENT.unpack_from(data, poff+index*16)
        if padding or energy > 65535:
            raise ValueError("动作记录错误。")
        segments.append({"start_sample": start, "end_sample": end, "first_beat": first,
                         "beat_count": length, "clip_id": clip, "energy": energy})
    plan = {"beats": beats, "segments": segments, "sample_count": samples, "sample_rate": rate,
            "title": title.split(b"\0", 1)[0].decode("utf-8"), "theme_id": theme, "record_id": record_id}
    if scene_revision:
        plan["scene_revision"] = scene_revision
    if scene_revision == 3:
        plan["effect_id"] = effect_from_word(reserved)
    if version == 3:
        plan["audio_reactivity"] = decode_reactivity(data[metadata_end:aoff], samples)
    if not 4 * 16000 <= samples <= 30 * 60 * 16000 or b"\0" not in title:
        raise ValueError("歌曲长度或标题不符合设备格式。")
    if any(point["flags"] & ~7 for point in beats):
        raise ValueError("拍点标志不符合设备格式。")
    try:
        validate_plan(plan)
    except AssertionError as exc:
        raise ValueError("动作计划校验失败。") from exc
    plan.update(compatibility_metadata(plan))
    return {"plan": plan, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(), "format": f"VSK{version}", "version": version,
            "pcm_bytes": data[aoff:], **compatibility_metadata(plan)}
