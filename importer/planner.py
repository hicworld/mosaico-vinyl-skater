"""Compose beat-aligned, neutral-to-neutral clips without changing beat times."""
from __future__ import annotations

import hashlib
from copy import deepcopy
from bisect import bisect_right
import numpy as np
from .themes import (THEME_COUNT, LEGACY_THEME_COUNT, RECORD_COLOR_COUNT, EFFECT_COUNT,
                     theme_index, record_index, effect_index)
from .slalom_catalog import SLALOM_CLIPS

CLIPS = (
    {"id": 0, "name": "舒缓滑行", "beats": 4},
    {"id": 1, "name": "交替推蹬", "beats": 2},
    {"id": 2, "name": "蛇形切刃", "beats": 4},
    {"id": 3, "name": "低滑下压", "beats": 4, "retired": True},
    {"id": 4, "name": "舒展摆臂", "beats": 4, "retired": True},
    {"id": 5, "name": "左脚平衡", "beats": 8},
    {"id": 6, "name": "右脚平衡", "beats": 8},
    {"id": 7, "name": "剪刀步", "beats": 4},
    {"id": 8, "name": "重拍点步", "beats": 2},
    {"id": 9, "name": "舒展挥臂", "beats": 4},
    {"id": 10, "name": "交叉压步", "beats": 4},
    {"id": 11, "name": "Crazy 交叉步", "beats": 4},
    {"id": 12, "name": "蟹步开合", "beats": 4},
    {"id": 13, "name": "前后轮摆转", "beats": 4, "retired": True},
    {"id": 14, "name": "空中转体 360°", "beats": 4},
    {"id": 15, "name": "单脚摆转", "beats": 8},
    {"id": 16, "name": "蛇形前进", "beats": 4},
    {"id": 17, "name": "连续转体", "beats": 8},
    {"id": 18, "name": "空中转体 720°", "beats": 8},
) + SLALOM_CLIPS
RETIRED_CLIP_IDS = frozenset(clip["id"] for clip in CLIPS if clip.get("retired"))
ACTIVE_CLIPS = tuple(clip for clip in CLIPS if not clip.get("retired"))
ACTIVE_CLIP_IDS = tuple(clip["id"] for clip in ACTIVE_CLIPS)
DOWNBEAT, SENTINEL, UNCERTAIN = 1, 2, 4
CLIP_COMPLEXITY = {0: 0, 1: 0, 5: 0, 6: 0, 9: 0,
                   2: 1, 7: 1, 8: 1, 10: 1, 12: 1, 16: 1,
                   11: 2, 14: 2, 15: 2, 17: 2, 18: 3}
CLIP_COMPLEXITY.update({clip['id']: clip['level']-1 for clip in SLALOM_CLIPS})
LIFT_CLIP_IDS = frozenset((1, 8, 10, 16, 14, 18)) | frozenset(
    clip['id'] for clip in SLALOM_CLIPS if clip.get('has_lifts'))
AIR_CLIP_IDS = frozenset((14, 18, 31, 51))


def rhythm_complexity(analysis: dict) -> dict:
    """Local tempo and independent drum density; never audio amplitude."""
    beats = np.asarray(analysis["beats"], dtype=np.float64)
    rate = float(analysis["sample_rate"])
    intervals = np.diff(beats)
    if len(intervals) < 1 or np.any(intervals <= 0):
        raise ValueError("复杂度分析需要递增拍点。")
    drums = analysis.get("audio_reactivity", {}).get("drums", [])
    if len(drums) > 18000 or any(isinstance(event.get("sample"), bool)
                               or not isinstance(event.get("sample"), (int, np.integer))
                               or not 0 <= event["sample"] < analysis["sample_count"] for event in drums):
        raise ValueError("独立鼓点事件的数量或采样位置无效。")
    events = np.asarray([event["sample"] for event in drums], dtype=np.float64)
    if np.any(np.diff(events) <= 0):
        raise ValueError("独立鼓点事件必须严格递增且不重复。")
    tempi, densities, scores, levels = [], [], [], []
    end = float(analysis["sample_count"])
    for index, sample in enumerate(beats):
        interval = float(np.median(intervals[max(0, index-2):min(len(intervals), index+3)]))
        bpm = 60.*rate/interval
        start, stop = max(0., sample-rate), min(end, sample+rate)
        count = int(np.searchsorted(events, stop, side="right")-np.searchsorted(events, start))
        density = count/max((stop-start)/rate, .02)
        score = float(.72*np.clip((bpm-65.)/110., 0., 1.) + .28*np.clip(density/6., 0., 1.))
        level = int(np.searchsorted((.32, .58, .82), score, side="right"))
        tempi.append(bpm); densities.append(density); scores.append(score); levels.append(level)
    return {"method": "local-bpm-and-drum-density-v1", "uses_loudness": False,
            "bpm": tempi, "drums_per_second": densities, "score": scores, "level": levels,
            "clip_minimum_level": CLIP_COMPLEXITY.copy()}

# Strong phrase identities survive quieter passages. Alternation changes
# inside the identity instead of excluding its main moves after one use.
PROFILES = {
    "arc": {"name": "弧线律动", "motif": (16, 10, 9, 11, 17, 2, 12),
            "weights": (.7, .5, 4.0, 0., 0., .7, .7, 1.3, .4, 3.9,
                        3.1, 2.8, 2.0, 0., .5, 1.0, 4.5, 3.5, .3)},
    "drive": {"name": "推蹬节奏", "motif": (1, 10, 16, 11, 7, 8, 14, 17, 18, 1, 8),
              "weights": (.3, 5.3, 1.4, 0., 0., .1, .1, 2.8, 2.8, .7,
                          3.7, 3.4, 1.0, 0., 2.8, .3, 3.0, 2.6, 2.5)},
    "sway": {"name": "舒展轻舞", "motif": (9, 5, 12, 16, 6, 15, 17, 0),
             "weights": (1.3, .1, .6, 0., 0., 4.6, 4.3, .4, .3, 3.6,
                         .7, .5, 2.8, 0., .1, 3.3, 2.9, 2.4, .1)},
    "balanced": {"name": "均衡律动", "motif": (16, 10, 9, 11, 17, 12, 1, 2, 14, 15, 18, 0),
                 "weights": (1.3, 1.8, 2.1, 0., 0., 1.3, 1.3, 2.1, 1.3, 1.8,
                             2.0, 1.8, 1.8, 0., 1.8, 1.5, 2.6, 2.2, 1.4)},
}
SYNCOPATED_MOTIFS = {
    "arc": (9, 11, 16, 2, 17, 12, 10, 2),
    "drive": (7, 1, 8, 11, 16, 14, 8, 10, 18),
}

# Preserve the old IDs and their meanings. New automatic plans can draw from
# the full bank; whole phrases still finish on observed beats. Each sound
# profile favours different footwork and route families, while usage penalties
# prevent one high-scoring phrase from hiding all its peers.
_NEW_MOTIFS = {
    'arc': (19, 20, 23, 25, 30, 33, 34, 35, 38, 40, 42, 46, 48, 50, 52, 54),
    'drive': (20, 21, 22, 26, 28, 31, 32, 36, 37, 39, 41, 43, 44, 47, 49, 51, 53),
    'sway': (19, 23, 24, 27, 29, 30, 33, 35, 37, 40, 45, 48, 52, 54),
    'balanced': tuple(range(19, 55)),
}
for _key, _profile in PROFILES.items():
    _old_motif, _new_motif = _profile['motif'], _NEW_MOTIFS[_key]
    _profile['motif'] = tuple(value for pair in zip(_old_motif, _new_motif) for value in pair)
    _profile['motif'] += _old_motif[len(_new_motif):] + _new_motif[len(_old_motif):]
    _profile['weights'] += tuple(
        2.0 + (.8 if clip['id'] in _NEW_MOTIFS[_key] else 0.)
        for clip in SLALOM_CLIPS)
for _key in tuple(SYNCOPATED_MOTIFS):
    _old_motif, _new_motif = SYNCOPATED_MOTIFS[_key], _NEW_MOTIFS[_key]
    SYNCOPATED_MOTIFS[_key] = tuple(value for pair in zip(_old_motif, _new_motif) for value in pair)
    SYNCOPATED_MOTIFS[_key] += _old_motif[len(_new_motif):] + _new_motif[len(_old_motif):]


def motion_profile(analysis: dict) -> dict:
    """Select a movement tendency from sound, never from a title or hash."""
    features = analysis.get("music_features")
    if not features:
        return {"id": "balanced", "name": PROFILES["balanced"]["name"],
                "method": "legacy-analysis-compatible", "reason": "旧分析无音色特征，采用均衡律动。"}
    values = [features.get(key, 0.) for key in ("brightness", "percussive_density", "syncopation")]
    brightness, density, syncopation = [float(x) for x in values]
    if not np.all(np.isfinite(values)):
        raise ValueError("歌曲声音特征包含无效数值。")
    if (brightness < .009 and syncopation >= .34) or density < 1.3:
        profile, reason = "sway", "柔和音色与弱拍切分，或较稀疏起音：展臂、长平衡、蟹步和单脚摆转。"
    elif density >= 2.85:
        profile, reason = "drive", "中频起音密集：推蹬、交叉步、点步，强段加入空中转体。"
    else:
        profile, reason = "arc", "中等起音密度：蛇形前进、交叉压步、连续转体和舒展滑行。"
    result = {"id": profile, "name": PROFILES[profile]["name"],
              "method": "audible-feature-heuristic-v2", "is_genre_classifier": False,
              "reason": reason, "brightness": brightness,
              "percussive_density": density, "syncopation": syncopation}
    if (profile == "arc" and syncopation >= .34) or (profile == "drive" and syncopation >= .26):
        result.update(phrase_variant="syncopated",
                      phrase_reason="中频弱拍起音较强：增加剪刀步、摆臂或点步的短句，保留原动作风格。")
    return result


def make_plan(analysis: dict, title: str = "") -> dict:
    detected = list(analysis["beats"])
    end = int(analysis["sample_count"])
    if any(not 0 <= x < end for x in detected) or any(a >= b for a, b in zip(detected, detected[1:])):
        raise ValueError("拍点必须严格递增并位于歌曲范围内。")
    if len(detected) < 4:
        raise ValueError("至少需要 4 个拍点。")
    # A fingerprint only breaks genuinely equal scores. Renaming a song
    # must not change its movement identity or its choreography.
    reactive = bool(analysis.get("audio_reactivity"))
    # A pure gain change must only change the loudness track, including when
    # two clip scores tie. PCM hashes change under gain, so reactive plans
    # derive their deterministic tie breaker from the unchanged beat grid.
    seed_bytes = (np.asarray(detected, dtype="<u4").tobytes() if reactive else
                  analysis["pcm_sha256"].encode("ascii"))
    seed = int.from_bytes(hashlib.sha256(seed_bytes).digest()[:4], "little")
    rng = np.random.default_rng(seed)
    profile = motion_profile(analysis)
    recipe = PROFILES[profile["id"]]
    variant = profile.get("phrase_variant") == "syncopated"
    motif = SYNCOPATED_MOTIFS[profile["id"]] if variant else recipe["motif"]
    downs = set(analysis["downbeat_indices"])
    complexity = rhythm_complexity(analysis) if reactive else None
    movement = complexity["score"] if reactive else analysis["energy"]
    # Reserve the start of a stronger musical phrase before choosing clips.
    # Otherwise a four-beat crossover can skip past the downbeat on which
    # the accent should begin. Only full clips that finish before it fit.
    accent_boundaries = sorted(index for index in downs if 2 <= index < len(detected)-1
                              and float(np.mean(movement[index:index+2]))
                              - float(np.mean(movement[index-2:index])) > (.12 if reactive else .25))
    low_quality = analysis["quality"]["consistency_score"] < .55
    points = []
    if detected[0] > 0:
        points.append({"sample": 0, "strength": 0, "flags": SENTINEL})
    first_actual = len(points)
    for index, sample in enumerate(detected):
        points.append({"sample": sample, "strength": round(analysis["energy"][index] * 65535),
                       "flags": (DOWNBEAT if index in downs else 0) | (UNCERTAIN if low_quality else 0)})
    points.append({"sample": end, "strength": 0, "flags": SENTINEL})
    segments = []

    def segment(start, stop, clip, energy, beat_index=65535, count=0, reason=""):
        segments.append({"start_sample": int(start), "end_sample": int(stop),
                         "first_beat": int(beat_index), "beat_count": int(count),
                         "clip_id": int(clip), "energy": int(np.clip(round(energy * 65535), 0, 65535)),
                         "reason": reason})

    if detected[0] > 0:
        segment(0, detected[0], 0, .2, reason="首拍前舒缓滑行")
    index, previous, previous_previous, phrase, phrases_since_lift = 0, -1, -1, 0, 0
    clip_uses = {clip: 0 for clip in ACTIVE_CLIP_IDS}
    available_intervals = len(detected) - 1
    while index < available_intervals:
        remaining = available_intervals - index
        next_index = bisect_right(accent_boundaries, index)
        next_accent = (accent_boundaries[next_index] if next_index < len(accent_boundaries)
                       else available_intervals)
        phrase_room = min(remaining, next_accent-index)
        local = movement[index:min(index + 4, index + phrase_room, len(detected))]
        intensity = float(np.mean(local))
        level = int(np.searchsorted((.32, .58, .82), intensity, side="right")) if reactive else 3
        has_drums = reactive and float(np.mean(complexity["drums_per_second"][index:index+4])) > .05
        current_bpm = 60 * analysis["sample_rate"] / (detected[index+1] - detected[index])
        rising = index in accent_boundaries
        preferred = motif[phrase % len(motif)]
        reason = (f"{profile['name']}：节奏复杂度 {level}，鼓点抬脚与音量轨迹分别控制" if reactive else
                  profile["name"] + ("：弱段保持风格，动作舒缓" if intensity < .33 else "：按音乐短句编排"))
        candidates = []
        for clip in ACTIVE_CLIP_IDS:
            if reactive and CLIP_COMPLEXITY[clip] > level:
                continue
            if not reactive and clip in AIR_CLIP_IDS and intensity < .72:
                continue  # A hop belongs to a clearly energetic passage.
            count = CLIPS[clip]["beats"]
            if clip == 8:
                # This clip's accent apex is at phase .5. Two observed
                # beats put that apex exactly on the next true beat;
                # a one-beat placement would incorrectly peak offbeat.
                count = 2
            elif current_bpm > 155 and clip == 1:
                count *= 2  # Half-time stride; accent still follows the real beats.
            if count <= phrase_room:
                score = recipe["weights"][clip] + ((4. if variant else 3.) if clip == preferred else 0.)
                score -= min(4., .65*clip_uses[clip])
                if reactive and level >= 2:
                    score += 2.5*CLIP_COMPLEXITY[clip]
                if has_drums and clip in LIFT_CLIP_IDS:
                    score += 4. if phrases_since_lift >= 2 else .6
                if clip in AIR_CLIP_IDS:
                    if previous in AIR_CLIP_IDS:
                        score -= 6.
                    if previous_previous in AIR_CLIP_IDS:
                        score -= 3.
                if clip == previous:
                    score -= 2.2
                if clip == previous_previous:
                    score -= .8
                # Prefer a phrase ending on a detected downbeat, without
                # removing short pushes starting at the current downbeat.
                if index + count in downs:
                    score += .45
                if intensity < .33:
                    score += .8 if clip in (0, 5, 6, 9, 12, 15, 16, 17) else -.7
                    if not reactive and clip == 8:
                        score -= 1.5
                    if clip in AIR_CLIP_IDS:
                        score -= 3.0
                elif intensity > .7:
                    score += .6 if clip in (1, 2, 7, 8, 10, 11, 14, 16, 17, 18) else -.2
                if rising and index in downs:
                    score += 7. if clip == 8 else (1.2 if clip in (1, 16) else 0.)
                candidates.append((clip, count, score))
        if not candidates:
            # Close a short gap before an accent or the tail without ever
            # cutting an existing raised-foot action partway through.
            clip, count = 0, min(2, phrase_room)
            reason = "增强前收势" if next_accent < available_intervals else "尾句收势"
        else:
            best = max(item[2] for item in candidates)
            tied = [item for item in candidates if abs(item[2]-best) < 1e-9]
            clip, count, _ = tied[int(rng.integers(len(tied)))]
            if clip == 8 and rising:
                reason = profile["name"] + "：乐段增强，在真实拍点点步"
        segment(detected[index], detected[index + count], clip, intensity,
                first_actual + index, count, reason)
        previous_previous, previous = previous, clip
        clip_uses[clip] += 1
        phrases_since_lift = 0 if clip in LIFT_CLIP_IDS else phrases_since_lift+1
        index += count
        phrase += 1
    segment(detected[-1], end, 0, .2, reason="最后拍点后收势，回到滑行姿态")
    result = {"schema": "vinyl-adaptive-plan-v2", "title": title,
              "sample_rate": analysis["sample_rate"], "sample_count": end,
              "seed": seed, "choreography_version": "2.7.0", "motion_profile": profile,
              "beats": points, "segments": segments,
              "clips": list(CLIPS), "bpm_median": analysis["bpm_median"],
              "analysis_engine": analysis["engine"], "quality": analysis["quality"],
              "warnings": list(analysis["warnings"]), "pcm_sha256": analysis["pcm_sha256"]}
    if reactive:
        result["audio_reactivity"] = deepcopy(analysis["audio_reactivity"])
        result["rhythm_complexity"] = complexity
    result.update(compatibility_metadata(result))
    validate_plan(result)
    return result


def compatibility_metadata(plan: dict) -> dict:
    """Describe the action bank needed without altering VSK2's byte contract."""
    if theme_index(plan.get("theme_id", 0)) > LEGACY_THEME_COUNT:
        legacy = dict(plan, theme_id=1, scene_revision=3)
        result = compatibility_metadata(legacy)
        result.update(minimum_firmware="2.12.0", required_scene_revision=3,
                      required_theme_count=THEME_COUNT)
        return result
    if effect_index(plan.get("effect_id", 0)) or plan.get("scene_revision") == 3:
        legacy = dict(plan, effect_id=0, scene_revision=2)
        result = compatibility_metadata(legacy)
        result.update(minimum_firmware="2.9.0", required_scene_revision=3,
                      required_effect_revision=1, required_effect_count=EFFECT_COUNT)
        return result
    if any(segment['clip_id'] >= 19 for segment in plan['segments']):
        legacy = dict(plan, segments=[dict(segment, clip_id=0) for segment in plan['segments']])
        result = compatibility_metadata(legacy)
        result.update(required_clip_count=55, minimum_firmware='2.7.0',
                      required_motion_revision=3, required_slalom_revision=1)
        return result
    if record_index(plan.get("record_id", 0)) or plan.get("scene_revision") == 2:
        return {"required_clip_count": 19, "minimum_firmware": "2.6.0",
                "required_motion_revision": 3, "required_scene_revision": 2,
                "required_theme_count": LEGACY_THEME_COUNT, "required_record_color_count": RECORD_COLOR_COUNT,
                **({"required_reactivity": 1} if plan.get("audio_reactivity") else {})}
    if plan.get("theme_id", 0) or plan.get("scene_revision") == 1:
        return {"required_clip_count": 19, "minimum_firmware": "2.5.0",
                "required_motion_revision": 3, "required_scene_revision": 1,
                "required_theme_count": LEGACY_THEME_COUNT,
                **({"required_reactivity": 1} if plan.get("audio_reactivity") else {})}
    anime_actions = any(segment["clip_id"] not in (3, 4, 13) for segment in plan["segments"])
    if anime_actions:
        return {"required_clip_count": 19, "minimum_firmware": "2.4.0",
                "required_motion_revision": 3,
                **({"required_reactivity": 1} if plan.get("audio_reactivity") else {})}
    if plan.get("audio_reactivity"):
        return {"required_clip_count": 19, "minimum_firmware": "2.3.0", "required_reactivity": 1}
    extended = any(segment["clip_id"] >= 10 for segment in plan["segments"])
    return {"required_clip_count": 16 if extended else 10,
            "minimum_firmware": "2.1.0" if extended else "2.0.1"}


def validate_plan(plan: dict) -> None:
    theme_index(plan.get("theme_id", 0))
    record_index(plan.get("record_id", 0))
    effect_index(plan.get("effect_id", 0))
    if "scene_revision" in plan and (type(plan["scene_revision"]) is not int or plan["scene_revision"] not in (0, 1, 2, 3)):
        raise ValueError("场景资源版本不支持。")
    beats, segments, count = plan["beats"], plan["segments"], plan["sample_count"]
    if len(beats) > 10000 or len(segments) > 5000:
        raise ValueError("拍点数量超过设备上限。")
    assert beats[0]["sample"] == 0 and beats[-1]["sample"] == count
    assert all(a["sample"] < b["sample"] for a, b in zip(beats, beats[1:]))
    assert segments[0]["start_sample"] == 0 and segments[-1]["end_sample"] == count
    assert all(a["end_sample"] == b["start_sample"] for a, b in zip(segments, segments[1:]))
    for segment in segments:
        assert 0 <= segment["start_sample"] < segment["end_sample"] <= count
        assert 0 <= segment["clip_id"] < len(CLIPS) and 0 <= segment["energy"] <= 65535
        first, length = segment["first_beat"], segment["beat_count"]
        if first == 65535:
            assert length == 0
        else:
            assert 1 <= length <= 8 and first + length < len(beats)
            assert beats[first]["sample"] == segment["start_sample"]
            assert beats[first + length]["sample"] == segment["end_sample"]


def segment_phase(plan: dict, segment: dict, sample: int) -> float:
    """Reference to test native phase against nonuniform beat intervals."""
    count = segment["beat_count"]
    if not count:
        return float(np.clip((sample-segment["start_sample"])/(segment["end_sample"]-segment["start_sample"]), 0, 1))
    first = segment["first_beat"]
    samples = [x["sample"] for x in plan["beats"][first:first+count+1]]
    index = int(np.clip(np.searchsorted(samples, sample, side="right") - 1, 0, count-1))
    fraction = (sample-samples[index])/(samples[index+1]-samples[index])
    return float(np.clip((index+fraction)/count, 0, 1))
