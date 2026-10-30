"""Shared GUI / CLI workflow; inputs, PCM, analysis and plan remain inspectable."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .analysis import click_preview, decode_audio, detect_beats
from .package import write_package
from .planner import make_plan


def analyse_song(source: Path, output_dir: Path, bpm=None, offset=0., title=None,
                 progress=lambda message: None) -> dict:
    source = Path(source).resolve()
    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    # A stable ASCII basename also works in third-party render tools.
    digest = hashlib.sha256()
    with source.open("rb") as reader:
        for block in iter(lambda: reader.read(1024 * 1024), b""):
            digest.update(block)
    stem = digest.hexdigest()[:16]
    wav_path = output_dir / (stem + ".wav")
    progress("正在转换为 Mosaico 音频格式…")
    pcm = decode_audio(source, wav_path)
    analysis = detect_beats(wav_path, pcm, bpm, offset, progress)
    progress("正在按鼓点安排抬脚、按局部节奏选择动作复杂度…")
    plan = make_plan(analysis, title or source.stem)
    bundle = write_package(plan, pcm.astype("<i2").tobytes(), output_dir / (stem + ".vsk2"))
    preview_path = output_dir / (stem + "-beats.wav")
    click_preview(pcm, analysis, preview_path)
    result = {"source": str(source), "pcm_wav": str(wav_path), "beat_preview_wav": str(preview_path),
              "analysis": analysis, "plan": plan, "package": bundle}
    result_path = output_dir / (stem + ".json")
    result["report_path"] = str(result_path)
    result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    progress(f"分析完成：{analysis['bpm_median']:.1f} BPM，{plan['motion_profile']['name']}，{len(analysis['beats'])} 个拍点，{len(plan['segments'])} 个动作片段。")
    return result


def export_builtin_header(results: list[dict], output: Path, names=None) -> None:
    names = names or [x["plan"]["title"] for x in results]
    version = results[0]["plan"].get("choreography_version", "unknown")
    rows = [f"/* Real Beat This! small0 beats; audio-feature choreography v{version}. Do not edit. */",
            "#pragma once", '#include "song_format.h"',
            "typedef struct { const char *name; uint32_t sample_count, beat_count, plan_count; const song_beat_t *beats; const song_plan_t *plans; song_audio_features_t audio; } builtin_choreography_t;"]
    for index, result in enumerate(results):
        plan = result["plan"]
        rows.append(f"static const song_beat_t builtin_beats_{index}[] = {{")
        rows.extend("    {%du, %du, %du}," % (x["sample"], x["strength"], x["flags"]) for x in plan["beats"])
        rows.append("};")
        audio = plan.get("audio_reactivity")
        if audio:
            rows.append(f"static const uint8_t builtin_envelope_{index}[] = {{")
            rows.extend("    " + ",".join(str(x) for x in audio["envelope"][i:i+32]) + ","
                        for i in range(0, len(audio["envelope"]), 32))
            rows.append("};")
            # An empty drum track still has a valid C backing object.
            rows.append(f"static const song_drum_t builtin_drums_{index}[] = {{")
            rows.extend("    {%du,%du,%du}," % (d["sample"], d["strength"], d.get("kind", 1)) for d in audio["drums"])
            if not audio["drums"]:
                rows.append("    {0u,0u,0u},")
            rows.append("};")
        rows.append(f"static const song_plan_t builtin_plans_{index}[] = {{")
        rows.extend("    {%du, %du, %du, %du, %du, %du, 0u}," % (x["start_sample"], x["end_sample"],
            x["first_beat"], x["beat_count"], x["clip_id"], x["energy"]) for x in plan["segments"])
        rows.append("};")
    rows.append("static const builtin_choreography_t builtin_choreographies[] = {")
    for index, result in enumerate(results):
        plan = result["plan"]
        name = json.dumps(names[index], ensure_ascii=True)
        audio = plan.get("audio_reactivity")
        controls = (f"{{{audio['envelope_hop']}u,{len(audio['envelope'])}u,{len(audio['drums'])}u,builtin_envelope_{index},builtin_drums_{index}}}"
                    if audio else "{0}")
        rows.append(f"    {{{name}, {plan['sample_count']}u, {len(plan['beats'])}u, {len(plan['segments'])}u, builtin_beats_{index}, builtin_plans_{index}, {controls}}},")
    rows.append("};")
    output.write_text("\n".join(rows) + "\n", encoding="utf-8")
