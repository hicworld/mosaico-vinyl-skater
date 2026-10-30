"""Portable VSA1 controls: absolute loudness and independent drum onsets."""
from __future__ import annotations

import struct

CONTROL_HEADER = struct.Struct("<4sHHIIII")
DRUM = struct.Struct("<IHH")
ENVELOPE_HOP = 320
MAX_DRUMS = 18000


def _integer(value, lower, upper):
    return isinstance(value, int) and not isinstance(value, bool) and lower <= value <= upper


def validate_reactivity(controls: dict, sample_count: int) -> None:
    if not isinstance(controls, dict) or controls.get("envelope_hop") != ENVELOPE_HOP:
        raise ValueError("音量控制轨必须以 20 ms 采样。")
    envelope, drums = controls.get("envelope"), controls.get("drums")
    expected = (sample_count + ENVELOPE_HOP - 1) // ENVELOPE_HOP + 1
    if (not isinstance(envelope, list) or len(envelope) != expected or envelope[0] != 0
            or any(not _integer(x, 0, 255) for x in envelope)):
        raise ValueError("音量控制轨长度或数值错误。")
    if not isinstance(drums, list) or len(drums) > MAX_DRUMS:
        raise ValueError("鼓点控制轨长度错误。")
    previous = -1
    for drum in drums:
        if (not isinstance(drum, dict)
                or not _integer(drum.get("sample"), 0, sample_count - 1)
                or drum["sample"] <= previous
                or not _integer(drum.get("strength"), 1, 65535)
                or drum.get("kind", 1) != 1):
            raise ValueError("鼓点控制轨必须严格递增且处于歌曲范围内。")
        previous = drum["sample"]


def encode_reactivity(controls: dict, sample_count: int) -> bytes:
    validate_reactivity(controls, sample_count)
    envelope, drums = controls["envelope"], controls["drums"]
    return (CONTROL_HEADER.pack(b"VSA1", 1, CONTROL_HEADER.size, ENVELOPE_HOP,
                                len(envelope), len(drums), 0)
            + bytes(envelope) + bytes((-len(envelope)) % 4)
            + b"".join(DRUM.pack(d["sample"], d["strength"], d.get("kind", 1)) for d in drums))


def decode_reactivity(data: bytes, sample_count: int) -> dict:
    if len(data) < CONTROL_HEADER.size:
        raise ValueError("歌曲缺少音量和鼓点控制轨。")
    magic, version, size, hop, count, drum_count, reserved = CONTROL_HEADER.unpack_from(data)
    if ((magic, version, size, hop, reserved) != (b"VSA1", 1, 24, ENVELOPE_HOP, 0)
            or count != (sample_count + hop - 1) // hop + 1 or drum_count > MAX_DRUMS):
        raise ValueError("歌曲声音控制轨格式错误。")
    drum_offset = size + (count + 3) // 4 * 4
    if len(data) != drum_offset + drum_count * DRUM.size or any(data[size + count:drum_offset]):
        raise ValueError("歌曲声音控制轨长度或填充错误。")
    controls = {"envelope_hop": hop, "envelope": list(data[size:size + count]),
                "drums": [dict(zip(("sample", "strength", "kind"), DRUM.unpack_from(data, drum_offset + i * 8)))
                          for i in range(drum_count)]}
    validate_reactivity(controls, sample_count)
    return controls
