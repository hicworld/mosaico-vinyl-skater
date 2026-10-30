"""Stable scene IDs shared by projects, packages and the desktop selector."""
from __future__ import annotations

from numbers import Integral

THEMES = (
    {"index": 1, "id": "sunset", "name": "日落迪斯科", "description": "暖橙落日、奶油色与复古几何"},
    {"index": 2, "id": "neon", "name": "午夜霓虹", "description": "深蓝紫底色、冷色灯带与地面线条"},
    {"index": 3, "id": "film", "name": "奶油胶片", "description": "奶油纸感、焦糖色、胶片孔与画格"},
    {"index": 4, "id": "snow", "name": "雪山晨曦", "description": "雪峰、晨曦与轻盈飘雪"},
    {"index": 5, "id": "forest", "name": "萤光森林", "description": "深林、柔和光束与点点萤火"},
    {"index": 6, "id": "storm", "name": "雷雨风暴", "description": "雷云、斜雨与远处闪电"},
)
THEME_COUNT = len(THEMES)
LEGACY_THEME_COUNT = 3
THEME_TAG = 0x54480000
STYLE_TAG = 0x56430000
EFFECT_TAG = 0x46580000
DEFAULT_THEME = 1
RECORD_COLORS = (
    {"index": 0, "id": "obsidian", "name": "经典曜石黑", "description": "深黑盘面与克制的蓝灰光泽"},
    {"index": 1, "id": "grape", "name": "葡萄酒紫", "description": "深紫盘面与酒红色反光"},
    {"index": 2, "id": "ocean", "name": "深海蓝", "description": "深海蓝盘面与青蓝色反光"},
    {"index": 3, "id": "amber", "name": "琥珀棕", "description": "暖棕盘面与琥珀色反光"},
)
RECORD_COLOR_COUNT = len(RECORD_COLORS)
FOOT_EFFECTS = (
    {"index": 0, "id": "auto", "name": "自动搭配", "description": "随人物搭配脚边特效，霓虹使用蓝黄闪电"},
    {"index": 1, "id": "off", "name": "关闭", "description": "保留滑行轨迹与落地四溅"},
    {"index": 2, "id": "lightning", "name": "蓝黄闪电", "description": "脚边电弧与蓝黄电光"},
    {"index": 3, "id": "sparks", "name": "熔金火花", "description": "蹬步飞溅的金色火星"},
    {"index": 4, "id": "ice", "name": "冰晶星屑", "description": "冰蓝晶粒与闪亮星屑"},
    {"index": 5, "id": "rings", "name": "星环波纹", "description": "轮下扩散的彩色光环"},
    {"index": 6, "id": "flame", "name": "能量流焰", "description": "跟随轮鞋的渐隐能量焰"},
)
EFFECT_COUNT = len(FOOT_EFFECTS)


def _theme_value(value) -> int:
    if isinstance(value, bool) or not isinstance(value, Integral) or not 0 <= value <= THEME_COUNT:
        raise ValueError("场景主题编号必须是 1 至 6 的整数。")
    return int(value)


def theme_index(value) -> int:
    value = _theme_value(value)
    return DEFAULT_THEME if value == 0 else value


def record_index(value) -> int:
    if isinstance(value, bool) or not isinstance(value, Integral) or not 0 <= value < RECORD_COLOR_COUNT:
        raise ValueError("唱片颜色编号必须是 0 至 3 的整数。")
    return int(value)


def effect_index(value) -> int:
    if isinstance(value, bool) or not isinstance(value, Integral) or not 0 <= value < EFFECT_COUNT:
        raise ValueError("脚边特效编号必须是 0 至 6 的整数。")
    return int(value)


def theme_word(value, *, explicit=False) -> int:
    # Keep raw legacy theme 0 roundtrippable; editors canonicalize it to sunset.
    value = _theme_value(value)
    if value > LEGACY_THEME_COUNT:
        return EFFECT_TAG | value
    return THEME_TAG | value if value or explicit else 0


def style_word(theme, record_id=0, scene_revision=0, *, effect_id=0) -> int:
    raw_theme = _theme_value(theme)
    record_id = record_index(record_id)
    effect_id = effect_index(effect_id)
    if raw_theme > LEGACY_THEME_COUNT or scene_revision == 3 or effect_id:
        return EFFECT_TAG | (effect_id << 12) | (record_id << 8) | theme_index(raw_theme)
    if scene_revision == 2 or record_id:
        return STYLE_TAG | (record_id << 8) | theme_index(raw_theme)
    return theme_word(raw_theme, explicit=scene_revision == 1)


def style_from_word(value: int) -> tuple[int, int, int]:
    if value == 0:
        return 0, 0, 0
    if value in tuple(THEME_TAG | i for i in range(4)):
        return value & 0xFFFF, 0, 1
    theme, record_id, effect_id = value & 0xFF, (value >> 8) & 15, (value >> 12) & 15
    if (1 <= theme <= THEME_COUNT and record_id < RECORD_COLOR_COUNT and effect_id < EFFECT_COUNT
            and value == EFFECT_TAG | (effect_id << 12) | (record_id << 8) | theme):
        return theme, record_id, 3
    theme, record_id = value & 0xFF, (value >> 8) & 0xFF
    if theme in (1, 2, 3) and record_id < RECORD_COLOR_COUNT and value == STYLE_TAG | (record_id << 8) | theme:
        return theme, record_id, 2
    raise ValueError("歌曲包的场景、唱片颜色或脚边特效标记不支持。")


def effect_from_word(value: int) -> int:
    _theme, _record, revision = style_from_word(value)
    return (value >> 12) & 15 if revision == 3 else 0


def theme_from_word(value: int) -> int:
    return style_from_word(value)[0]


def theme_by_id(value) -> dict:
    return THEMES[theme_index(value)-1]


def record_by_id(value) -> dict:
    return RECORD_COLORS[record_index(value)]


def effect_by_id(value) -> dict:
    return FOOT_EFFECTS[effect_index(value)]


def theme_name(value) -> str:
    return theme_by_id(value)["name"]
