"""MP4 export settings and a cancellable, Tk-safe progress dialog."""
from __future__ import annotations

import os
from pathlib import Path
import queue
import re
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from .video_export import ExportCancelled, ExportSettings, export_video, snapshot_project


def safe_video_name(title):
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(title)).strip(" .")[:100]
    if not name or name.upper().split(".")[0] in {
        "CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }:
        name = "轮滑动画"
    return name + ".mp4"


class AnimationExportDialog(tk.Toplevel):
    def __init__(self, parent, project, *, skin, default_directory, on_close):
        super().__init__(parent)
        self.withdraw()
        self.title("导出动画 · MP4")
        self.configure(bg="#10171e")
        self.resizable(False, False)
        self.transient(parent)
        self.project, self.skin, self.on_close = project, skin, on_close
        self.events = queue.Queue()
        self.cancel_event = threading.Event()
        self.running = False
        self.finished = False
        self.result = None
        self._closed = False
        self._poll_id = None
        self.path = tk.StringVar(value=str(Path(default_directory) / safe_video_name(project.title)))
        self.size = tk.StringVar(value="480 × 480（原始）")
        self.fps = tk.StringVar(value="30")
        self.range_mode = tk.StringVar(value="full")
        self.start = tk.StringVar(value="0")
        self.end = tk.StringVar(value=f"{project.duration:.6f}".rstrip("0").rstrip("."))
        self.include_audio = tk.BooleanVar(value=True)
        self.include_lyrics = tk.BooleanVar(value=project.show_lyrics)
        self.progress = tk.DoubleVar(value=0)
        self.progress_text = tk.StringVar(value="设置好后点击“开始导出”。")
        self.destination_text = tk.StringVar(value="")
        self._fields = []
        self._build()
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.bind("<Escape>", lambda _event: self._close())
        self.update_idletasks()
        x = parent.winfo_rootx() + max(0, (parent.winfo_width() - self.winfo_reqwidth()) // 2)
        y = parent.winfo_rooty() + max(0, (parent.winfo_height() - self.winfo_reqheight()) // 2)
        self.geometry(f"+{x}+{y}")
        self.deiconify()
        self.grab_set()
        self._poll_id = self.after(80, self._poll)

    def _build(self):
        body = ttk.Frame(self, padding=20)
        body.pack(fill="both", expand=True)
        body.columnconfigure(1, weight=1)
        ttk.Label(body, text="导出当前歌曲编排", style="Title.TLabel").grid(row=0, column=0, columnspan=3, sticky="w")
        ttk.Label(body, text="人物、动作、场景与脚边特效沿用当前设置。", style="Muted.TLabel").grid(
            row=1, column=0, columnspan=3, sticky="w", pady=(5, 14))
        ttk.Label(body, text="保存位置").grid(row=2, column=0, sticky="w", padx=(0, 12))
        path_entry = ttk.Entry(body, textvariable=self.path, width=54)
        path_entry.grid(row=2, column=1, sticky="ew")
        browse = ttk.Button(body, text="选择…", command=self._browse)
        browse.grid(row=2, column=2, padx=(8, 0))
        self._fields.extend((path_entry, browse))
        ttk.Label(body, text="画面大小").grid(row=3, column=0, sticky="w", pady=(12, 0))
        size = ttk.Combobox(body, textvariable=self.size, state="readonly", width=23,
                            values=("480 × 480（原始）", "960 × 960（放大）", "1080 × 1080（放大）"))
        size.grid(row=3, column=1, sticky="w", pady=(12, 0))
        fps_line = ttk.Frame(body)
        fps_line.grid(row=4, column=1, columnspan=2, sticky="w", pady=(10, 0))
        ttk.Label(body, text="帧率").grid(row=4, column=0, sticky="w", pady=(10, 0))
        fps = ttk.Combobox(fps_line, textvariable=self.fps, state="readonly", width=6, values=("24", "30"))
        fps.pack(side="left")
        ttk.Label(fps_line, text=" 帧/秒 · 离线逐帧生成", style="Muted.TLabel").pack(side="left")
        self._fields.extend((size, fps))
        ttk.Label(body, text="导出范围").grid(row=5, column=0, sticky="w", pady=(12, 0))
        ranges = ttk.Frame(body)
        ranges.grid(row=5, column=1, columnspan=2, sticky="w", pady=(12, 0))
        for text, value in (("整首歌曲", "full"), ("指定片段", "clip")):
            button = ttk.Radiobutton(ranges, text=text, variable=self.range_mode, value=value, command=self._range_state)
            button.pack(side="left", padx=(0, 16))
            self._fields.append(button)
        times = ttk.Frame(body)
        times.grid(row=6, column=1, columnspan=2, sticky="w", pady=(8, 0))
        ttk.Label(times, text="从").pack(side="left")
        self.start_entry = ttk.Entry(times, textvariable=self.start, width=11)
        self.start_entry.pack(side="left", padx=7)
        ttk.Label(times, text="到").pack(side="left")
        self.end_entry = ttk.Entry(times, textvariable=self.end, width=11)
        self.end_entry.pack(side="left", padx=7)
        ttk.Label(times, text=f"秒（全长 {self.project.duration:.2f} 秒）", style="Muted.TLabel").pack(side="left")
        self._fields.extend((self.start_entry, self.end_entry))
        options = ttk.Frame(body)
        options.grid(row=7, column=1, columnspan=2, sticky="w", pady=(13, 0))
        for text, variable in (("包含音乐", self.include_audio), ("叠加歌词", self.include_lyrics)):
            check = ttk.Checkbutton(options, text=text, variable=variable)
            check.pack(side="left", padx=(0, 18))
            self._fields.append(check)
        ttk.Label(body, text="MP4 · H.264 视频 / AAC 音频", style="Muted.TLabel").grid(
            row=8, column=1, columnspan=2, sticky="w", pady=(8, 15))
        self.bar = ttk.Progressbar(body, variable=self.progress, maximum=100, mode="determinate")
        self.bar.grid(row=9, column=0, columnspan=3, sticky="ew")
        ttk.Label(body, textvariable=self.progress_text, wraplength=600).grid(
            row=10, column=0, columnspan=3, sticky="w", pady=(7, 6))
        ttk.Label(body, textvariable=self.destination_text, wraplength=600, style="Muted.TLabel").grid(
            row=11, column=0, columnspan=3, sticky="w", pady=(0, 7))
        actions = ttk.Frame(body)
        actions.grid(row=12, column=0, columnspan=3, sticky="ew")
        self.folder_button = ttk.Button(actions, text="打开文件夹", command=self._open_folder, state="disabled")
        self.folder_button.pack(side="left")
        self.close_button = ttk.Button(actions, text="关闭", command=self._close)
        self.close_button.pack(side="right")
        self.start_button = ttk.Button(actions, text="开始导出", command=self._start, style="Accent.TButton")
        self.start_button.pack(side="right", padx=8)
        self._range_state()

    def _range_state(self):
        state = "!disabled" if self.range_mode.get() == "clip" and not self.running else "disabled"
        for entry in (self.start_entry, self.end_entry):
            entry.state([state])

    def _browse(self):
        path = Path(self.path.get())
        chosen = filedialog.asksaveasfilename(parent=self, title="保存动画视频", defaultextension=".mp4",
                    filetypes=(("MP4 视频", "*.mp4"),), initialdir=str(path.parent), initialfile=path.name,
                    confirmoverwrite=False)
        if chosen:
            self.path.set(chosen)

    def _start(self):
        if self.running or self.finished:
            return
        try:
            target = Path(self.path.get().strip()).expanduser()
            if not self.path.get().strip() or target.suffix.lower() != ".mp4":
                raise ValueError("请选择以 .mp4 结尾的视频文件。")
            if target.is_dir():
                raise ValueError("保存位置应为一个 MP4 文件。")
            if self.range_mode.get() == "full":
                first, last = 0.0, None
            else:
                first, last = float(self.start.get()), float(self.end.get())
            settings = ExportSettings(size=int(self.size.get().split()[0]), fps=int(self.fps.get()),
                        start_seconds=first, end_seconds=last, include_audio=self.include_audio.get(),
                        include_lyrics=self.include_lyrics.get())
            import math
            end = self.project.duration if last is None else last
            if not math.isfinite(first) or not math.isfinite(end) or not 0 <= first < end <= self.project.duration:
                raise ValueError(f"导出范围应在 0 到 {self.project.duration:.3f} 秒之间，结束时间须大于开始时间。")
            if target.exists() and not messagebox.askyesno("替换视频", f"文件已存在，导出成功后替换它吗？\n{target}", parent=self):
                return
        except (ValueError, TypeError, OSError) as exc:
            messagebox.showerror("导出设置有误", str(exc), parent=self)
            return
        self.running = True
        self.cancel_event.clear()
        self.progress.set(0)
        self.progress_text.set("正在准备当前编排…")
        self.destination_text.set(str(target))
        for field in self._fields:
            field.state(["disabled"])
        self.start_button.state(["disabled"])
        self.close_button.configure(text="取消导出")
        project, skin = self.project, self.skin
        def work():
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                snapshot = snapshot_project(project, skin=skin)
                result = export_video(snapshot, target, settings, progress=lambda value: self.events.put(("progress", value)),
                                      cancel_event=self.cancel_event)
                self.events.put(("success", result))
            except ExportCancelled:
                self.events.put(("cancelled", None))
            except Exception as exc:
                self.events.put(("error", str(exc)))
        threading.Thread(target=work, name="MosaicoVideoExport", daemon=True).start()

    def _poll(self):
        self._poll_id = None
        if self._closed:
            return
        try:
            while True:
                kind, value = self.events.get_nowait()
                if kind == "progress":
                    # Only success after the encoder and atomic commit may show 100%.
                    percent = max(0, min(99.9, float(value["percent"])))
                    self.progress.set(percent)
                    elapsed = float(value.get("elapsed_seconds", 0))
                    frames, total = value.get("completed_frames", 0), value.get("total_frames", 0)
                    self.progress_text.set(f"{value['stage']} · {percent:.1f}% · {frames}/{total} 帧 · 已用 {elapsed:.0f} 秒")
                elif kind == "success":
                    self.running, self.finished, self.result = False, True, value
                    self.progress.set(100)
                    self.progress_text.set(f"导出完成 · {value.frame_count} 帧 · {value.duration:.2f} 秒")
                    self.destination_text.set(str(value.path))
                    self.folder_button.state(["!disabled"])
                    self.close_button.configure(text="完成", state="normal")
                elif kind in ("error", "cancelled"):
                    self.running = False
                    self.progress.set(0)
                    self.progress_text.set("已取消导出。" if kind == "cancelled" else f"导出失败：{value}")
                    for field in self._fields:
                        field.state(["!disabled"])
                    self._range_state()
                    self.start_button.state(["!disabled"])
                    self.close_button.configure(text="关闭", state="normal")
        except queue.Empty:
            pass
        self._poll_id = self.after(80, self._poll)

    def _open_folder(self):
        if self.result is not None:
            os.startfile(str(Path(self.result.path).parent))

    def _close(self):
        if self.running:
            self.cancel_event.set()
            self.close_button.state(["disabled"])
            self.progress_text.set("正在取消并清理临时文件…")
            return
        self._closed = True
        if getattr(self, "_poll_id", None) is not None:
            self.after_cancel(self._poll_id)
            self._poll_id = None
        self.grab_release()
        self.destroy()
        self.on_close(self.result)
