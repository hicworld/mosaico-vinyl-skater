"""Optional offline-capable lyric estimates using actual Whisper word ends.

Call from the editor's background worker. Importing this module never imports
Whisper, downloads weights, opens an audio device, or contacts a board.
"""
from __future__ import annotations

from collections import Counter
import hashlib
import json
import math
from numbers import Integral
import os
from pathlib import Path
import re
import queue
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import zlib

import numpy as np

from .phrase_cues import MAX_CUES, MAX_TEXT, RATE, estimate_phrase_ends, validate_scene_cues
from .paths import data_root, is_frozen, lyric_worker_command, resource_path, resource_root

_MODEL_LOCK = threading.RLock()
_INFERENCE_LOCK = threading.Lock()
_CACHED_MODEL = None
_CACHED_PATH = None
_BASE_MODEL_BYTES = 145262807
_PAUSE_SECONDS = .35
_TERMINAL = re.compile(r'[.!?;。！？；][\s"\'”’）)\]]*$')
_ASR_NOTE = ("这是 Whisper 自动歌词估计：句尾取最后一个可信词的结束时间，"
             "不是人工对齐；唱歌、伴奏和口音可能造成误识别或漏句，请试听并微调。")


def _report(progress, message):
    if progress is not None:
        try:
            progress(message)
        except Exception:
            # A UI progress callback cannot damage a loaded song or its cues.
            pass


def _audio(pcm, sample_rate):
    if isinstance(sample_rate, bool) or not isinstance(sample_rate, Integral) or sample_rate != RATE:
        raise ValueError("歌词识别需要 16 kHz 单声道 PCM。")
    values = np.asarray(pcm)
    if values.ndim != 1 or not len(values):
        raise ValueError("歌词识别需要非空的单声道音频。")
    if values.dtype.kind == 'i' and values.dtype.itemsize == 2:
        return values.astype(np.float32) / 32768.0
    if (values.dtype.kind != 'f' or not np.all(np.isfinite(values))
            or np.max(np.abs(values)) > 1.00001):
        raise ValueError("音频必须是 int16 或 -1 至 1 之间的有限浮点 PCM。")
    return values.astype(np.float32, copy=True)


def model_directory():
    override = os.environ.get('MOSAICO_WHISPER_MODEL')
    if override:
        return Path(os.path.expandvars(override)).expanduser().resolve()
    if is_frozen():
        bundled = resource_path('models', 'whisper-base')
        if (bundled / 'base.pt').is_file():
            return bundled
    cache = os.environ.get('MOSAICO_MODEL_CACHE')
    base = (Path(os.path.expandvars(cache)).expanduser() if cache else
            Path.home() / '.cache' / 'mosaico-vinyl-skater')
    return (base / 'whisper-base').resolve()


def _model_ready(directory):
    # The pinned official multilingual base checkpoint has a fixed size;
    # load_model subsequently verifies its SHA256 before loading weights.
    return ((directory / 'base.pt').is_file()
            and (directory / 'base.pt').stat().st_size == _BASE_MODEL_BYTES)


def _load_model(allow_download, progress):
    global _CACHED_MODEL, _CACHED_PATH
    directory = model_directory()
    with _MODEL_LOCK:
        if _CACHED_MODEL is not None and _CACHED_PATH == directory:
            return _CACHED_MODEL
        import torch
        import whisper
        if not _model_ready(directory):
            if not allow_download:
                raise FileNotFoundError("本机尚未缓存歌词模型，本次未允许下载。")
            _report(progress, "首次使用正在下载多语言歌词模型（约 150 MB），以后可离线使用…")
        elif not allow_download:
            # Official load_model verifies and may re-download a corrupt cache.
            # Refuse it before that call when this job explicitly forbids network.
            expected = whisper._MODELS['base'].split('/')[-2]
            with (directory / 'base.pt').open('rb') as stream:
                actual = hashlib.file_digest(stream, 'sha256').hexdigest()
            if actual != expected:
                raise ValueError("本机歌词模型校验失败；本次未允许重新下载。")
        _report(progress, "正在载入本机歌词模型…")
        torch.set_num_threads(4)
        model = whisper.load_model('base', device='cpu', download_root=str(directory))
        _CACHED_PATH, _CACHED_MODEL = directory, model
        return model


def _number(value, default=None):
    if isinstance(value, (bool, np.bool_)):
        return default
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return number if math.isfinite(number) else default


def _repetitive(text, words):
    compact = re.sub(r'[\W_]+', '', text, flags=re.UNICODE).lower()
    if len(compact) >= 16 and re.search(r'(.{2,40})\1{3,}', compact):
        return True
    tokens = [re.sub(r'[\W_]+', '', str(w.word), flags=re.UNICODE).lower() for w in words]
    tokens = [x for x in tokens if x]
    return len(tokens) >= 8 and Counter(tokens).most_common(1)[0][1] >= len(tokens) * .7


def _word_cues(segments, sample_count, progress=None):
    duration = sample_count / RATE
    cues, pending = [], []
    rejected_ranges = []
    repeated_ranges = []
    repeated_candidates = 0
    rejected_segments = rejected_words = missing_words = 0
    last_end = -1.0

    def finish():
        if not pending:
            return
        text = re.sub(r'\s+', ' ', ''.join(word[2] for word in pending)).strip()
        sample = round(pending[-1][1] * RATE)
        if text and 0 < sample < sample_count and len(cues) < MAX_CUES:
            cues.append({'sample': sample, 'text': text[:MAX_TEXT], 'source': 'whisper_word_end'})
        pending.clear()

    for segment in segments:
        words = list(getattr(segment, 'words', None) or ())
        logprob = _number(getattr(segment, 'avg_logprob', None))
        no_speech = _number(getattr(segment, 'no_speech_prob', None))
        text = str(getattr(segment, 'text', ''))
        # Official transcribe copies a decoding WINDOW's compression ratio
        # onto every child segment. A repeated outro can otherwise reject
        # earlier real lyrics in that window. Apply the same 2.4 zlib test to
        # the actual segment text, while retaining the word confidence guards.
        encoded = text.encode('utf-8')
        compression = len(encoded) / len(zlib.compress(encoded))
        # Repetition is normal in choruses. Reject it only together with weak
        # word confidence; strong repeated vocals remain reviewable candidates.
        repeated_text = _repetitive(text, words) or compression > 2.4
        trusted_share = (sum(_number(getattr(w, 'probability', None), 0) >= .5 for w in words)
                         / len(words)) if words else 0
        repeated = repeated_text and trusted_share < .7
        # Match official Whisper's joint no-speech rule: a good acoustic
        # log-probability overrides a high no_speech score (common in singing).
        no_voice = no_speech is not None and no_speech > .6 and logprob is not None and logprob <= -1.0
        if (logprob is None or no_speech is None or logprob < -1.0 or no_voice
                or repeated):
            rejected_segments += 1
            begin, end = _number(getattr(segment, 'start', None)), _number(getattr(segment, 'end', None))
            if begin is not None and end is not None and 0 <= begin < end <= duration and len(rejected_ranges) < 5:
                rejected_ranges.append(f'{begin:.1f}–{end:.1f} 秒')
            finish()
            continue
        if not words:
            missing_words += 1
            finish()
            continue
        if repeated_text:
            # Do not merge a continuous repeated outro into the preceding
            # lyric phrase or invent regular intervals inside its vocalization.
            finish()
            repeated_candidates += 1
            begin, end = _number(getattr(segment, 'start', None)), _number(getattr(segment, 'end', None))
            if begin is not None and end is not None and 0 <= begin < end <= duration and len(repeated_ranges) < 5:
                repeated_ranges.append(f'{begin:.1f}–{end:.1f} 秒')
        for word in words:
            start, end = _number(getattr(word, 'start', None)), _number(getattr(word, 'end', None))
            confidence = _number(getattr(word, 'probability', None), 0)
            text = getattr(word, 'word', '')
            if (not isinstance(text, str) or not text.strip() or confidence < .25
                    or start is None or end is None or not 0 <= start < end <= duration
                    or end <= last_end or start < last_end - .1
                    or not re.search(r'\w', text, flags=re.UNICODE)):
                rejected_words += 1
                continue
            if pending and start - pending[-1][1] >= _PAUSE_SECONDS - 1e-7:
                finish()
            pending.append((start, end, text))
            last_end = end
            if _TERMINAL.search(text):
                finish()
        _report(progress, f"正在识别歌词：{min(duration, max(0, last_end)):.1f} / {duration:.1f} 秒")
        if len(cues) >= MAX_CUES:
            break
    finish()
    notes = [_ASR_NOTE, "按句末标点或至少 350 毫秒的词间停顿分句；可在时间线上试听、移动或删除候选。"]
    if rejected_segments or rejected_words or missing_words:
        notes.append(f"已过滤 {rejected_segments} 个低可信/重复片段、{rejected_words} 个无效词；"
                     f"{missing_words} 个片段没有词级时间，未用片段粗尾代替句尾。")
    if rejected_ranges:
        notes.append("未可靠识别的重复/低可信区间：" + '、'.join(rejected_ranges)
                     + "；这些区间没有自动编造歌词句尾，可试听后手动补点。")
    if repeated_candidates:
        location = '、'.join(repeated_ranges) or f'{repeated_candidates} 个片段'
        notes.append("检测到重复人声/连续吟唱候选（" + location + "）：已保留可信词尾；"
                     "连续部分未强行细分，请试听后补点，重复文本也可能含识别误差。")
    if not cues:
        notes.append("没有识别到可信歌词句尾，未把音乐停顿自动当成歌词。")
    if len(cues) >= MAX_CUES:
        notes.append(f"候选已达到 {MAX_CUES} 个上限，请检查过密的识别结果。")
    return validate_scene_cues({'version': 1, 'sample_rate': RATE, 'source': 'whisper_words',
                                'cues': cues, 'notes': notes}, sample_count)


def _fallback(audio, exc, progress):
    _report(progress, "歌词模型暂不可用，正在改用音频停顿估算…")
    result = estimate_phrase_ends(audio, RATE)
    reason = re.sub(r'\s+', ' ', str(exc)).strip()[:400] or type(exc).__name__
    result['notes'].insert(0, f"歌词识别未完成（{reason}）；本次结果来自音乐停顿估算，不是歌词识别。")
    return validate_scene_cues(result, len(audio))


def _recognize_in_process(pcm, sample_rate=RATE, progress=None, allow_download=True):
    """Child-only inference. Native model faults must not run in the GUI."""
    audio = _audio(pcm, sample_rate)
    if type(allow_download) is not bool:
        raise ValueError("是否下载歌词模型必须是布尔值。")
    try:
        _report(progress, "正在准备歌词识别…")
        # Serialize inference and path changes without blocking the GUI thread.
        with _INFERENCE_LOCK:
            model = _load_model(allow_download, progress)
            _report(progress, "正在识别人声与词级时间；唱歌片段请在完成后试听校正…")
            decoded = model.transcribe(audio, task='transcribe', beam_size=5,
                temperature=0.0, condition_on_previous_text=False, word_timestamps=True,
                fp16=False, verbose=None, logprob_threshold=-1.0, no_speech_threshold=.6,
                compression_ratio_threshold=2.4, hallucination_silence_threshold=1.0)
            segments = (SimpleNamespace(**{**segment, 'words': [SimpleNamespace(**word)
                         for word in segment.get('words', [])]})
                        for segment in decoded['segments'])
            result = _word_cues(segments, len(audio), progress)
            result['source'] = 'whisper_torch_words'
        _report(progress, f"歌词识别完成：{len(result['cues'])} 个句尾候选，建议试听微调。")
        return result
    except Exception as exc:
        return _fallback(audio, exc, progress)


def _run_child(audio, allow_download, progress):
    """One isolated model process per job; the downloaded model stays cached.

    Beat analysis already loads Torch in the editor. ASR has its own process,
    so model faults or native DLL failures become a labelled fallback.
    No shell, inherited stdin, audio device, or serial connection is used.
    """
    root = data_root() if is_frozen() else resource_root()
    with tempfile.TemporaryDirectory(prefix='mosaico-lyrics-') as temporary:
        folder = Path(temporary)
        input_path, output_path = folder / 'audio.npy', folder / 'cues.json'
        np.save(input_path, audio, allow_pickle=False)
        env = os.environ.copy()
        env['PYTHONUTF8'] = '1'
        env['PYTHONIOENCODING'] = 'utf-8'
        env['MOSAICO_WHISPER_MODEL'] = str(model_directory())
        command = lyric_worker_command(input_path, output_path, allow_download)
        events = queue.Queue()
        with (folder / 'stderr.log').open('w+b') as errors:
            child = subprocess.Popen(command, cwd=root, env=env, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=errors, text=True, encoding='utf-8', errors='replace',
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))

            def read_progress():
                try:
                    for line in child.stdout:
                        try:
                            message = json.loads(line)
                            if isinstance(message, dict) and isinstance(message.get('progress'), str):
                                events.put(message['progress'][:2048])
                        except (ValueError, TypeError):
                            pass
                finally:
                    events.put(None)

            reader = threading.Thread(target=read_progress, daemon=True)
            reader.start()
            # Download/inference are bounded without holding the GUI thread.
            # Long songs get proportional time; a stuck native backend is killed.
            deadline = time.monotonic() + max(600, len(audio) / RATE * 10 + 180)
            try:
                while child.poll() is None:
                    if time.monotonic() >= deadline:
                        raise TimeoutError('独立歌词识别进程超时')
                    try:
                        message = events.get(timeout=.1)
                        if message is not None:
                            _report(progress, message)
                    except queue.Empty:
                        pass
                reader.join(timeout=2)
                while not events.empty():
                    message = events.get_nowait()
                    if message is not None:
                        _report(progress, message)
                if child.returncode != 0:
                    errors.seek(0)
                    detail = errors.read(4096).decode('utf-8', errors='replace').strip()
                    raise RuntimeError(f'独立歌词识别进程退出 {child.returncode}'
                                       + (f'：{detail[-500:]}' if detail else '（本机模型运行库异常）'))
                if not output_path.is_file() or output_path.stat().st_size > 20_000_000:
                    raise RuntimeError('独立歌词识别结果缺失或过大')
                return validate_scene_cues(json.loads(output_path.read_text(encoding='utf-8')), len(audio))
            finally:
                if child.poll() is None:
                    child.kill()
                    child.wait(timeout=5)
                reader.join(timeout=2)
                child.stdout.close()


def recognize_phrase_ends(pcm, sample_rate=RATE, progress=None, allow_download=True):
    """Return reviewable word endpoints from a separate CPU model process.

    Call from the GUI's background worker. Successful ASR with no credible
    words remains empty. Download/native failures have explicit pause estimates.
    """
    audio = _audio(pcm, sample_rate)
    if type(allow_download) is not bool:
        raise ValueError("是否下载歌词模型必须是布尔值。")
    try:
        _report(progress, "正在启动独立歌词识别进程…")
        with _INFERENCE_LOCK:
            return _run_child(audio, allow_download, progress)
    except Exception as exc:
        return _fallback(audio, exc, progress)


def _worker_main(argv):
    if len(argv) != 4 or argv[0] != '--worker' or argv[3] not in ('0', '1'):
        raise ValueError('Invalid isolated lyric worker arguments')
    audio = np.load(argv[1], allow_pickle=False)
    def progress(message):
        # A PyInstaller --windowed worker may have no Python stdout even
        # though its parent created a pipe. Results still use the JSON file.
        if sys.stdout is not None:
            print(json.dumps({'progress': message}, ensure_ascii=False), flush=True)
    result = _recognize_in_process(audio, progress=progress, allow_download=argv[3] == '1')
    Path(argv[2]).write_text(json.dumps(result, ensure_ascii=False), encoding='utf-8')


if __name__ == '__main__':
    _worker_main(sys.argv[1:])
