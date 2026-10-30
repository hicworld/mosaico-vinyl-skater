"""Generate a reproducible, original synthetic demo without downloads."""
from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import sys

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from importer.analysis import extract_audio_reactivity
from importer.planner import make_plan
from studio.lyrics import lyrics_from_lrc
from studio.model import EditorProject

RATE, SECONDS, BPM = 16000, 16, 120
TEXT = """[00:00.00]黑胶上的小舞台
[00:02.00]脚步跟着鼓点出发
[00:04.00]沿着弧线转个弯
[00:06.00]跃起，再轻轻落下
[00:08.00]场景随时间切换
[00:10.00]星光留在脚边
[00:12.00]把这一段编排保存
[00:14.00]再导出一段自己的动画
"""


def synthetic_pcm():
    """All oscillators/noise are generated here; no sampled recording is used."""
    audio = np.zeros(RATE * SECONDS, dtype=np.float64)
    rng = np.random.default_rng(20261030)
    def add(start, signal):
        first = round(start * RATE)
        count = min(len(signal), len(audio) - first)
        if count > 0:
            audio[first:first + count] += signal[:count]
    for beat in range(32):
        t = np.arange(round(.30 * RATE)) / RATE
        phase = 2 * np.pi * (42 * t + 48 * .025 * (1 - np.exp(-t / .025)))
        add(beat / 2, .55 * np.sin(phase) * np.exp(-t * 18))
        t = np.arange(round(.08 * RATE)) / RATE
        noise = rng.standard_normal(len(t))
        noise = noise - np.roll(noise, 1)
        add(beat / 2 + .25, .045 * noise * np.exp(-t * 65))
        if beat % 4 in (1, 3):
            add(beat / 2, .06 * noise * np.exp(-t * 40))
        t = np.arange(round(.43 * RATE)) / RATE
        midi = (48, 48, 53, 55)[(beat // 8) % 4]
        frequency = 440 * 2 ** ((midi - 69) / 12)
        bass = np.sin(2*np.pi*frequency*t) + .22*np.sin(6*np.pi*frequency*t)
        add(beat / 2, .18 * bass * np.minimum(1, t/.012) * np.exp(-t*7))
    for note, midi in enumerate((72, 76, 79, 74, 77, 81, 79, 76) * 2):
        t = np.arange(round(.72 * RATE)) / RATE
        f = 440 * 2 ** ((midi - 69) / 12)
        tone = np.sin(2*np.pi*f*t) + .18*np.sin(4*np.pi*f*t)
        add(note + .125, .11 * tone * np.minimum(1, t/.018) * np.exp(-t*5.5))
    audio[-int(.15 * RATE):] *= np.linspace(1, 0, int(.15 * RATE))
    return np.rint(audio / max(1, np.max(np.abs(audio))) * 26000).astype("<i2")


def generate(output_dir):
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    pcm = synthetic_pcm()
    digest = hashlib.sha256(pcm.tobytes()).hexdigest()
    analysis = dict(engine="original-synthetic-score", sample_rate=RATE,
                    sample_count=len(pcm), duration=SECONDS,
                    beats=list(range(0, len(pcm), RATE//2)),
                    downbeat_indices=list(range(0, 32, 4)),
                    energy=[.72 if i % 4 == 0 else .48 for i in range(32)],
                    bpm_median=BPM, pcm_sha256=digest,
                    quality={"consistency_score":1.0}, warnings=[],
                    audio_reactivity=extract_audio_reactivity(pcm))
    plan = make_plan(analysis, "黑胶轮滑 · 原创合成示例")
    project = EditorProject(plan, pcm, analysis=analysis, source=None)
    project.set_theme(4)
    project.set_effect(0)
    project.set_lyrics(lyrics_from_lrc(TEXT, project.sample_count))
    project.set_scene_auto("lyric_start", True)
    project.place_clip(16, 0)
    project.place_clip(14, 8)
    project.place_clip(17, 16)
    path = project.save(output / "demo.vskproj")
    (output / "demo.lrc").write_text(TEXT, encoding="utf8")
    return path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "examples")
    parser.add_argument("--verify", action="store_true", help="Verify the bundled synthetic PCM")
    args = parser.parse_args()
    if args.verify:
        project = EditorProject.load(args.output_dir / "demo.vskproj")
        if not np.array_equal(project.pcm, synthetic_pcm()):
            raise SystemExit("Bundled demo does not match the synthesis source")
        print("Original synthetic demo verified")
    else:
        print(generate(args.output_dir))


if __name__ == "__main__":
    main()
