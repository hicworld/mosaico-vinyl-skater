"""Analyse audio locally with the pretrained Beat This! small0 model.

There is deliberately no silent fixed-tempo fallback: if detection fails,
the user can supply BPM and an offset explicitly. The quality score below
describes consistency and onset agreement, not model confidence.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import wave

import numpy as np

from studio.paths import is_frozen, resource_path

RATE = 16000
MAX_SECONDS = 30 * 60
_TRACKER = None


def extract_audio_reactivity(pcm: np.ndarray) -> dict:
    """Independent offbeat drum attacks and an absolute, causal RMS envelope.

    Drum events are low/mid-band positive spectral-flux heuristics, not
    separated percussion stems or model confidence. Their times never alter
    the beat grid. Envelope item i is heard at i * 320 samples and measures
    the preceding 20 ms, so it never anticipates an unheard loud passage.
    Input float arrays, like int16 arrays, use raw PCM16 amplitude units.
    """
    from scipy.ndimage import median_filter
    from scipy.signal import find_peaks

    source = np.asarray(pcm)
    if source.ndim != 1 or not len(source) or not np.all(np.isfinite(source)):
        raise ValueError("响应轨需要有限数值的单声道 PCM 音频。")
    signal = source.astype(np.float32) / 32768.
    hop = 320
    rms = np.zeros((len(signal) + hop - 1) // hop + 1, dtype=np.float64)
    for index in range(1, len(rms)):
        chunk = signal[(index-1)*hop:min(index*hop, len(signal))]
        rms[index] = np.sqrt(np.mean(chunk.astype(np.float64)**2))
    dbfs = 20. * np.log10(np.maximum(rms, 1e-12))
    envelope = np.rint(255. * np.clip((dbfs + 60.) / 54., 0., 1.)).astype(np.uint8)

    fft_size, flux_hop = 1024, 80
    if len(signal) < fft_size:
        signal = np.pad(signal, (0, fft_size-len(signal)))
    frames = np.lib.stride_tricks.sliding_window_view(signal, fft_size)[::flux_hop]
    frequencies = np.fft.rfftfreq(fft_size, 1./RATE)
    masks = [(frequencies >= lo) & (frequencies < hi)
             for lo, hi in ((30, 220), (220, 3000))]
    flux = np.zeros((2, len(frames)), dtype=np.float64)
    relative_flux = np.zeros_like(flux)
    window, previous = np.hanning(fft_size), None
    for begin in range(0, len(frames), 512):
        magnitude = np.abs(np.fft.rfft(frames[begin:begin+512]*window, axis=1))
        prior = magnitude[0] if previous is None else previous
        changes = np.maximum(0., magnitude-np.vstack((prior, magnitude[:-1])))
        for band, mask in enumerate(masks):
            values = np.sum(changes[:, mask], axis=1)
            flux[band, begin:begin+len(magnitude)] = values
            relative_flux[band, begin:begin+len(magnitude)] = values / (np.sum(magnitude[:, mask], axis=1)+1e-9)
        previous = magnitude[-1]
    normalized = np.zeros_like(flux)
    for band in range(2):
        reference = float(np.percentile(flux[band], 95))
        if reference > 1e-7:
            normalized[band] = flux[band] / reference
    curve = np.convolve(.7*normalized[0]+normalized[1], np.ones(3)/3., mode="same")
    baseline = median_filter(curve, size=81, mode="nearest")
    # Spacing adapts for a 30 minute file so the event table remains bounded.
    distance = max(15, int(np.ceil(len(frames)/18000)))
    peaks, _ = find_peaks(curve, height=baseline*1.35+.25,
                         prominence=.15, distance=distance)
    valid = np.max(relative_flux[:, peaks], axis=0) > .035
    peaks = peaks[valid]
    if len(peaks) > 18000:
        peaks = np.sort(peaks[np.argsort(curve[peaks])[-18000:]])
    reference = max(float(np.percentile(curve[peaks], 95)) if len(peaks) else 0., 1e-9)
    events = []
    for peak in peaks:
        sample = int(peak*flux_hop + fft_size//2)
        if not 0 < sample < len(source):
            continue
        strength = int(np.clip(round(float(curve[peak])/reference*65535.), 1, 65535))
        if not events or sample > events[-1]["sample"]:
            events.append({"sample": sample, "strength": strength, "kind": 1})
    return {"envelope_hop": hop, "envelope": envelope.tolist(), "drums": events,
            "method": "low-mid-positive-spectral-flux-heuristic-v1",
            "is_source_separation": False, "drum_detector_hop": flux_hop,
            "drum_detector_window": fft_size, "max_drum_events": 18000,
            "envelope_method": "causal-20ms-absolute-rms-dbfs-v1",
            "envelope_dbfs_min": -60., "envelope_dbfs_max": -6.,
            "envelope_initial_sample": 0, "envelope_window_ends_at_timestamp": True}


def extract_music_features(pcm: np.ndarray, beat_samples: list[int] | np.ndarray) -> dict:
    """Describe audible texture and syncopation without moving detected beats.

    These are transparent signal heuristics, not a genre classifier or model
    confidence. Batched STFT keeps a 30 minute song from allocating a giant
    matrix. A song's title and fingerprint have no part in these features.
    """
    from scipy.signal import find_peaks

    signal = np.asarray(pcm, dtype=np.float32) / 32768.
    beats = np.asarray(beat_samples, dtype=np.float64)
    fft_size, hop = 512, 160
    starts = np.arange(0, max(0, len(signal)-fft_size+1), hop)
    frequencies = np.fft.rfftfreq(fft_size, 1./RATE)
    bands = ((30, 180), (180, 1500), (1500, 7800))
    masks = [(frequencies >= low) & (frequencies < high) for low, high in bands]
    flux = np.zeros((len(bands), len(starts)), dtype=np.float64)
    powers = np.zeros(len(frequencies))
    window = np.hanning(fft_size)
    previous = None
    frames = np.lib.stride_tricks.sliding_window_view(signal, fft_size)[::hop]
    for begin in range(0, len(starts), 512):
        magnitude = np.abs(np.fft.rfft(frames[begin:begin+512]*window, axis=1))
        powers += np.sum(magnitude*magnitude, axis=0)
        prior = magnitude[0] if previous is None else previous
        changes = np.maximum(0., magnitude-np.vstack((prior, magnitude[:-1])))
        for index, mask in enumerate(masks):
            flux[index, begin:begin+len(magnitude)] = np.sum(changes[:, mask], axis=1)
        previous = magnitude[-1]
    centres = starts + fft_size/2
    measurements = []
    for index, ((low, high), mask) in enumerate(zip(bands, masks)):
        curve = flux[index]
        threshold = float(np.percentile(curve, 80)) if len(curve) else 0.
        prominence = float(np.percentile(curve, 75))*.4 if len(curve) else 0.
        peaks, _ = find_peaks(curve, height=max(1e-8, threshold), distance=7,
                             prominence=max(1e-8, prominence))
        instants = centres[peaks]
        intervals = np.searchsorted(beats, instants, side="right")-1
        valid = (intervals >= 0) & (intervals+1 < len(beats))
        peaks, intervals, instants = peaks[valid], intervals[valid], instants[valid]
        phases = (instants-beats[intervals])/(beats[intervals+1]-beats[intervals])
        weak = np.abs(phases-.5) < .13
        onbeat = np.minimum(phases, 1.-phases) < .13
        strengths = curve[peaks]
        total = float(np.sum(strengths))
        count_by_beat = np.bincount(intervals, minlength=len(beats))
        strength_by_beat = np.bincount(intervals, weights=strengths, minlength=len(beats))
        weak_by_beat = np.bincount(intervals[weak], weights=strengths[weak], minlength=len(beats))
        measurements.append({
            "hz": [low, high], "power_share": float(np.sum(powers[mask])/(np.sum(powers)+1e-12)),
            "onsets_per_beat": float(len(peaks)/max(1, len(beats)-1)),
            "onbeat_fraction": float(np.mean(onbeat)) if len(peaks) else 0.,
            "weakbeat_flux_share": float(np.sum(strengths[weak])/(total+1e-12)),
            "onsets_per_beat_local": [int(x) for x in count_by_beat],
            "weakbeat_flux_share_local": [float(x) for x in weak_by_beat/(strength_by_beat+1e-12)],
        })
    return {"method": "batched-spectral-positive-flux-v1", "is_genre_classifier": False,
            "fft_size": fft_size, "hop_samples": hop, "bands": measurements,
            "brightness": measurements[2]["power_share"],
            "percussive_density": measurements[1]["onsets_per_beat"],
            "syncopation": measurements[1]["weakbeat_flux_share"]}


def ffmpeg_path() -> str:
    bundled = resource_path("bin", "ffmpeg.exe") if is_frozen() else None
    for item in (os.environ.get("VINYL_FFMPEG"), bundled, shutil.which("ffmpeg")):
        if item and Path(item).is_file():
            return str(item)
    raise RuntimeError("未找到 FFmpeg。请安装 FFmpeg，或设置 VINYL_FFMPEG 指向 ffmpeg.exe。")


def configure_model_cache() -> Path:
    """Keep optional downloaded weights in a user-owned, overridable cache."""
    override = os.environ.get("MOSAICO_MODEL_CACHE")
    base = (Path(os.path.expandvars(override)).expanduser() if override else
            Path.home() / ".cache" / "mosaico-vinyl-skater")
    os.environ.setdefault("TORCH_HOME", str(base / "torch"))
    return base


def beat_model_path() -> str:
    """Prefer a user-selected checkpoint or the complete Windows bundle."""
    override = os.environ.get("MOSAICO_BEAT_MODEL")
    if override:
        checkpoint = Path(os.path.expandvars(override)).expanduser().resolve()
        if not checkpoint.is_file():
            raise FileNotFoundError("MOSAICO_BEAT_MODEL 指定的节拍模型不存在。")
        return str(checkpoint)
    if is_frozen():
        checkpoint = resource_path("models", "beat-this-small0.ckpt")
        if not checkpoint.is_file():
            raise FileNotFoundError("Windows 包缺少节拍模型，请重新下载并完整解压发布包。")
        return str(checkpoint)
    return "small0"


def decode_audio(source: Path, output: Path) -> np.ndarray:
    """Use an argument vector so song titles are never interpreted by a shell."""
    source, output = Path(source).resolve(), Path(output).resolve()
    if not source.is_file():
        raise ValueError("歌曲文件不存在。")
    output.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [ffmpeg_path(), "-hide_banner", "-loglevel", "error", "-y", "-i", str(source),
         "-vn", "-ac", "1", "-ar", str(RATE), "-c:a", "pcm_s16le", str(output)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if result.returncode:
        raise RuntimeError("音频解码失败：" + result.stderr.strip()[-1200:])
    with wave.open(str(output), "rb") as reader:
        if (reader.getframerate(), reader.getnchannels(), reader.getsampwidth()) != (RATE, 1, 2):
            raise ValueError("转换后的音频格式错误。")
        pcm = np.frombuffer(reader.readframes(reader.getnframes()), dtype="<i2").copy()
    if len(pcm) < RATE * 4:
        raise ValueError("歌曲至少需要 4 秒，才能可靠安排动作。")
    if len(pcm) > RATE * MAX_SECONDS:
        raise ValueError("单首歌曲最长支持 30 分钟。")
    if np.max(np.abs(pcm.astype(np.int32))) < 32:
        raise ValueError("音频几乎没有声音，无法识别拍点。")
    return pcm


def write_wave(path: Path, pcm: np.ndarray) -> None:
    with wave.open(str(path), "wb") as writer:
        writer.setparams((1, 2, RATE, len(pcm), "NONE", "not compressed"))
        writer.writeframes(np.asarray(pcm, dtype="<i2").tobytes())


def detect_beats(wav_path: Path, pcm: np.ndarray, bpm: float | None = None,
                 offset: float = 0., progress=lambda message: None) -> dict:
    global _TRACKER
    duration = len(pcm) / RATE
    if bpm is not None:
        if not 35 <= bpm <= 300:
            raise ValueError("手动 BPM 应在 35–300 之间。")
        if not np.isfinite(offset) or not 0 <= offset < duration:
            raise ValueError("首拍位置应在歌曲时间范围内。")
        times = np.arange(offset, duration, 60. / bpm)
        downbeats = times[::4]
        engine = "manual-bpm"
    else:
        progress("正在用 Beat This! small0 在电脑上识别节拍…")
        try:
            if _TRACKER is None:
                configure_model_cache()
                import torch
                from beat_this.inference import File2Beats
                torch.set_num_threads(max(1, min(4, os.cpu_count() or 1)))
                _TRACKER = File2Beats(checkpoint_path=beat_model_path(), device="cpu", dbn=False)
            times, downbeats = _TRACKER(str(wav_path))
        except Exception as exc:
            raise RuntimeError("预训练节拍分析未完成。请检查导入环境；也可以填写 BPM 和首拍位置后重新分析。\n" + str(exc)) from exc
        engine = "beat-this-small0"
    times = np.asarray(times, dtype=float)
    times = times[np.isfinite(times) & (times >= 0) & (times < duration)]
    samples = np.unique(np.rint(times * RATE).astype(np.uint32))
    if len(samples) < 4:
        raise ValueError("没有识别到足够的拍点。请试听后填写 BPM 和首拍位置。")
    down_samples = np.rint(np.asarray(downbeats, dtype=float) * RATE)
    down_indices = set()
    for item in down_samples:
        index = int(np.argmin(np.abs(samples.astype(np.float64) - item)))
        if abs(float(samples[index]) - item) <= RATE * .08:
            down_indices.add(index)
    signal = pcm.astype(np.float64) / 32768.
    energy = []
    onset_ratios = []
    for index, sample in enumerate(samples):
        next_sample = int(samples[index + 1]) if index + 1 < len(samples) else len(pcm)
        chunk = signal[int(sample):next_sample]
        energy.append(float(np.sqrt(np.mean(chunk * chunk))) if len(chunk) else 0.)
        centre = int(sample)
        hit = signal[max(0, centre - RATE // 50):min(len(signal), centre + RATE // 12)]
        before = signal[max(0, centre - RATE // 5):max(0, centre - RATE // 25)]
        a = float(np.mean(hit * hit)) if len(hit) else 0.
        b = float(np.mean(before * before)) if len(before) else a
        onset_ratios.append(float(min(4., a / (b + 1e-7))))
    raw_energy = np.asarray(energy)
    lo, hi = np.percentile(raw_energy, [12, 90])
    # A nearly constant-energy track still has medium intensity, not random
    # high/low changes caused by quantisation noise in its waveform.
    if hi - lo < max(.004, float(hi) * .08):
        normalized = np.full(len(raw_energy), .5)
    else:
        normalized = np.clip((raw_energy - lo) / (hi - lo), 0, 1)
    intervals = np.diff(samples.astype(np.float64)) / RATE
    median = float(np.median(intervals))
    # Model output has a 20 ms frame grid. A median single interval can
    # report 130.4 BPM for a 128 BPM track (460/480 ms alternation). Measure
    # across up to 8 beats to show an accurate useful BPM without moving
    # any of the actual detected beat times.
    span = min(8, len(samples) - 1)
    long_intervals = (samples[span:].astype(np.float64) - samples[:-span].astype(np.float64)) / (RATE * span)
    tempo = 60. / float(np.median(long_intervals))
    deviation = float(np.median(np.abs(intervals - median)) / median)
    onset_agreement = float(np.mean(np.minimum(1., np.asarray(onset_ratios) / 1.6)))
    quality = float(np.clip(.55 * (1 - min(1., deviation * 5)) + .45 * onset_agreement, 0, 1))
    warnings = []
    if deviation > .15:
        warnings.append("节拍间隔变化较大，可能存在变速、自由节奏或漏拍；请试听拍点。")
    if quality < .55:
        warnings.append("拍点一致性偏低；建议试听并校正 BPM / 首拍位置。")
    if tempo < 45 or tempo > 230:
        warnings.append("识别速度较极端，可能需要按半速或倍速校正 BPM。")
    progress("正在分析打击密度、明亮度和弱拍切分，选择动作倾向…")
    music_features = extract_music_features(pcm, samples)
    progress("正在分析独立鼓点与绝对音量响应轨…")
    audio_reactivity = extract_audio_reactivity(pcm)
    return {
        "engine": engine, "model": "small0" if bpm is None else None,
        "sample_rate": RATE, "sample_count": len(pcm), "duration": duration,
        "bpm_median": tempo, "beats": [int(x) for x in samples],
        "downbeat_indices": sorted(down_indices),
        "energy": [float(x) for x in normalized], "rms_per_beat": energy,
        "onset_ratios": onset_ratios,
        "music_features": music_features,
        "audio_reactivity": audio_reactivity,
        "quality": {"consistency_score": quality, "interval_relative_deviation": deviation,
                    "onset_agreement": onset_agreement, "is_model_probability": False},
        "warnings": warnings, "pcm_sha256": hashlib.sha256(pcm.astype("<i2").tobytes()).hexdigest(),
    }


def click_preview(pcm: np.ndarray, analysis: dict, path: Path) -> None:
    mix = pcm.astype(np.float64) * .58
    downs = set(analysis["downbeat_indices"])
    length = int(RATE * .045)
    t = np.arange(length) / RATE
    for index, sample in enumerate(analysis["beats"]):
        frequency = 1400 if index in downs else 950
        click = np.sin(2 * np.pi * frequency * t) * np.exp(-t * 105) * 9500
        end = min(len(mix), sample + length)
        mix[sample:end] += click[:end-sample]
    write_wave(path, np.rint(np.clip(mix, -32767, 32767)).astype("<i2"))
