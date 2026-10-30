"""Song import orchestration with honest stage progress and optional local LRC.

The ordinary import path never starts speech recognition. Existing analysis,
editor validation and LRC parsing stay authoritative; this module only joins
them and reports which of five processing stages is currently active.
"""
from __future__ import annotations

from pathlib import Path


def import_song(source, output_dir, *, progress=lambda event: None,
                lyric_path=None, recognize_lyrics=False, auto_lyrics=True) -> dict:
    """Analyse a song and attach optional display lyrics and scene endpoints.

    ``progress`` receives dictionaries with integer ``stage`` (1..5),
    ``total`` (5), and a human-readable ``message``. Stages are not a time or
    work percentage. A supplied LRC takes priority over a same-name file and
    over optional ASR. ``auto_lyrics=False`` honors a deliberately cleared
    selection without rediscovering a same-name file. Invalid lyrics cannot
    discard successful audio analysis.
    """
    from importer.workflow import analyse_song
    from .lrc import find_companion_lrc, read_lrc_bundle
    from .model import EditorProject
    from .phrase_cues import estimate_phrase_ends

    source = Path(source)
    current_stage = 1

    def report(stage, message):
        nonlocal current_stage
        current_stage = max(current_stage, min(5, max(1, int(stage))))
        event = {"stage": current_stage, "total": 5, "message": str(message)}
        # An unavailable status display must not invalidate a loaded song.
        try:
            progress(event)
        except Exception:
            pass

    def analysis_progress(message):
        text = str(message)
        if "转换" in text:
            stage = 1
        elif "Beat This" in text or "识别节拍" in text or "节拍分析" in text:
            stage = 2
        elif any(word in text for word in ("打击密度", "明亮度", "弱拍切分", "响应轨", "音量", "音乐特征")):
            stage = 3
        elif "安排" in text or "编排" in text or "分析完成" in text:
            stage = 4
        else:
            stage = current_stage
        report(stage, text)

    report(1, "正在读取歌曲并准备音频…")
    result = analyse_song(source, Path(output_dir), progress=analysis_progress)
    report(4, "音乐与动作分析完成，正在检查歌曲数据…")
    decoded = EditorProject.from_result(result)
    report(5, "正在检查歌词文件与句尾时间点…")

    explicit = lyric_path is not None
    selected, lyric_error = None, None
    if explicit:
        selected = Path(lyric_path)
    elif auto_lyrics:
        try:
            selected = find_companion_lrc(source)
        except Exception as exc:
            lyric_error = f"查找同名 LRC 失败：{exc}"

    def estimate(notice):
        report(5, "正在按音频停顿估算句尾；未启动人声识别…")
        try:
            document = estimate_phrase_ends(decoded.pcm)
        except Exception as exc:
            notice += f" 句尾停顿估算未完成：{exc}"
            document = {"version": 1, "sample_rate": 16000,
                        "source": "audio_pause_estimate", "cues": [],
                        "notes": [f"停顿估算未完成：{exc}"]}
        document["notes"] = list(document.get("notes", [])) + [notice]
        result["studio_scene_cues"] = document
        result["studio_scene_notice"] = notice

    if selected is not None:
        label = "所选 LRC" if explicit else "同名 LRC"
        report(5, f"发现{label}，正在对齐：{selected.name}…")
        try:
            bundle = read_lrc_bundle(selected, decoded.pcm)
        except Exception as exc:
            bundle = None
            lyric_error = f"{label}未能导入：{exc}"
        if bundle is not None and bundle["lyrics"]["lines"]:
            document = bundle["scene_cues"]
            result["studio_lyrics"] = bundle["lyrics"]
            result["studio_scene_cues"] = document
            result["studio_lrc_loaded"] = True
            result["studio_scene_notice"] = (
                f"已使用{label}：{selected.name}；预览底部随音乐显示当前句。"
                + ("可在句尾时间点中核对句末。" if document["cues"] else
                   "没有可用句末切换点，字幕仍可显示；可手动补充句末。"))
        else:
            notice = ((lyric_error or f"{label}没有可用歌词。")
                      + " 已保留音乐与动作，改用音频停顿估算；未启动人声识别。"
                      + "可点击“导入歌词（LRC）”重新选择歌词文件。")
            estimate(notice)
    elif lyric_error:
        estimate(lyric_error + " 已保留音乐与动作，使用音频停顿估算；可点击“导入歌词（LRC）”补充歌词。")
    elif recognize_lyrics:
        from .lyric_recognition import recognize_phrase_ends
        report(5, "已选择人声识别，正在分析歌词句尾…")
        result["studio_scene_cues"] = recognize_phrase_ends(
            decoded.pcm, progress=lambda message: report(5, message))
        result["studio_scene_notice"] = "句尾分析已完成，请试听核对；也可导入 LRC 显示逐句歌词。"
    else:
        reason = "未找到同名 LRC" if auto_lyrics else "本次未选择 LRC"
        estimate(reason + "，已按音频停顿估算句尾；这不是歌词识别。"
                 "可点击“导入歌词（LRC）”为当前歌曲补充逐句歌词。")

    report(5, "歌曲处理完成。" + result.get("studio_scene_notice", ""))
    return result
