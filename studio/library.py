"""Searchable views of the shared, stable device action registry."""
from __future__ import annotations
import re

from importer.planner import ACTIVE_CLIPS


LEVEL_NAMES = {0: "原有动作", 1: "一级", 2: "二级", 3: "三级", 4: "四级"}


def clip_english_name(clip: dict) -> str:
    # reference_name describes the teaching source, not the action's name.
    name = str(clip.get("name", ""))
    return name.split(" · ", 1)[1] if " · " in name else ""


def clip_level(clip: dict) -> int:
    return int(clip.get("level", 0))


def level_choices() -> tuple[str, ...]:
    return ("全部动作",) + tuple(
        f"{label} · {sum(clip_level(c) == level for c in ACTIVE_CLIPS)}"
        for level, label in LEVEL_NAMES.items()
        if any(clip_level(c) == level for c in ACTIVE_CLIPS))


def filtered_clips(level_label="全部动作", query="") -> tuple[dict, ...]:
    label = str(level_label).split(" · ", 1)[0]
    level = next((key for key, value in LEVEL_NAMES.items() if value == label), None)
    def normalize(value):
        value = re.sub(r"[\W_]", "", str(value).casefold())
        return {"snakeavt": "snakeavant", "nakeavt": "snakeavant", "piedavt": "piedavant",
                "mecicaine": "mexicaine"}.get(value, value)
    words = [normalize(word) for word in str(query).split() if normalize(word)]
    return tuple(clip for clip in ACTIVE_CLIPS
                 if (level is None or clip_level(clip) == level)
                 and all(word in normalize(" ".join(str(clip.get(key, "")) for key in
                         ("id", "name", "slug", "reference_name", "description", "family")))
                         for word in words))


def clip_description(clip: dict) -> str:
    text = clip.get("description", "")
    label = LEVEL_NAMES.get(clip_level(clip), "花式动作")
    reference = clip_english_name(clip)
    headline = f"{label} · {clip['beats']} 拍完整动作"
    return "\n".join(part for part in (headline, reference, text) if part)
