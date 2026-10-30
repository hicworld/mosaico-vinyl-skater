"""Small desktop importer: choose, analyse, audition / correct, upload."""
from __future__ import annotations

from pathlib import Path
import queue
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from .planner import CLIPS
from .upload import upload_package
from .workflow import analyse_song


class ImporterWindow:
    def __init__(self, root: tk.Tk, output_dir: Path):
        self.root, self.output_dir = root, output_dir
        self.result = None
        self.messages = queue.Queue()
        self.busy = False
        self.source = tk.StringVar()
        self.manual = tk.BooleanVar(value=False)
        self.bpm = tk.StringVar()
        self.offset = tk.StringVar(value="0")
        self.status = tk.StringVar(value="选择一首歌曲，电脑会自动识别节拍并安排动作。")
        root.title("Mosaico · 音乐与动作")
        root.geometry("930x650")
        root.minsize(820, 590)
        outer = ttk.Frame(root, padding=22)
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text="让人物跟着你的音乐滑行", font=("Microsoft YaHei UI", 20, "bold")).pack(anchor="w")
        ttk.Label(outer, text="选歌曲 → 自动分析 → 试听拍点 → 导入 Mosaico。导入后拔掉电脑即可独立播放。",
                  wraplength=840).pack(anchor="w", pady=(8, 20))
        file_row = ttk.Frame(outer)
        file_row.pack(fill="x")
        self.source_entry = ttk.Entry(file_row, textvariable=self.source)
        self.source_entry.pack(side="left", fill="x", expand=True)
        self.choose = ttk.Button(file_row, text="选择歌曲…", command=self.select_file)
        self.choose.pack(side="left", padx=(10, 0))
        options = ttk.LabelFrame(outer, text="需要时校正拍点", padding=10)
        options.pack(fill="x", pady=14)
        self.manual_check = ttk.Checkbutton(options, text="使用手动 BPM", variable=self.manual, command=self.manual_changed)
        self.manual_check.pack(side="left")
        ttk.Label(options, text="BPM").pack(side="left", padx=(18, 5))
        self.bpm_entry = ttk.Entry(options, textvariable=self.bpm, width=8, state="disabled")
        self.bpm_entry.pack(side="left")
        ttk.Label(options, text="首拍位置（秒）").pack(side="left", padx=(18, 5))
        self.offset_entry = ttk.Entry(options, textvariable=self.offset, width=8, state="disabled")
        self.offset_entry.pack(side="left")
        ttk.Label(options, text="自动分析保留歌曲的变速拍点。", foreground="#626262").pack(side="right")
        action_row = ttk.Frame(outer)
        action_row.pack(fill="x")
        self.analyse = ttk.Button(action_row, text="自动分析并编排", command=self.run_analysis)
        self.analyse.pack(side="left")
        self.audition = ttk.Button(action_row, text="试听拍点", state="disabled", command=self.play_beats)
        self.audition.pack(side="left", padx=10)
        self.stop = ttk.Button(action_row, text="停止试听", command=self.stop_audio)
        self.stop.pack(side="left")
        self.upload = ttk.Button(action_row, text="导入 Mosaico", state="disabled", command=self.run_upload)
        self.upload.pack(side="right")
        ttk.Label(outer, textvariable=self.status, wraplength=850).pack(anchor="w", pady=(16, 10))
        self.timeline = tk.Canvas(outer, height=100, bg="#f6f3ed", highlightthickness=0)
        self.timeline.pack(fill="x")
        self.timeline.bind("<Configure>", lambda event: self.draw_timeline())
        ttk.Label(outer, text="竖线是拍点，深色线是小节重拍；彩色块是动作片段。", foreground="#626262").pack(anchor="w", pady=(5, 12))
        self.log = tk.Text(outer, height=9, wrap="word", font=("Microsoft YaHei UI", 9), state="disabled")
        self.log.pack(fill="both", expand=True)
        self.source.trace_add("write", self.source_changed)
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.after(100, self.poll)

    def select_file(self):
        path = filedialog.askopenfilename(title="选择歌曲", filetypes=[("音频文件", "*.mp3 *.wav *.m4a *.flac *.ogg *.aac *.wma"), ("全部文件", "*.*")])
        if path:
            self.source.set(path)
            self.result = None
            self.audition.configure(state="disabled")
            self.upload.configure(state="disabled")
            self.draw_timeline()

    def source_changed(self, *_):
        self.stop_audio()
        self.result = None
        self.audition.configure(state="disabled")
        self.upload.configure(state="disabled")
        self.draw_timeline()

    def manual_changed(self):
        state = "normal" if self.manual.get() and not self.busy else "disabled"
        self.bpm_entry.configure(state=state)
        self.offset_entry.configure(state=state)

    def set_busy(self, value: bool):
        self.busy = value
        self.source_entry.configure(state="disabled" if value else "normal")
        self.manual_check.configure(state="disabled" if value else "normal")
        self.manual_changed()
        self.choose.configure(state="disabled" if value else "normal")
        self.analyse.configure(state="disabled" if value else "normal")
        self.upload.configure(state="normal" if self.result and not value else "disabled")
        self.audition.configure(state="normal" if self.result and not value else "disabled")

    def progress(self, message):
        self.messages.put(("progress", message))

    def run_analysis(self):
        if self.busy:
            return
        if not self.source.get():
            self.select_file()
        if not self.source.get():
            return
        try:
            bpm = float(self.bpm.get()) if self.manual.get() else None
            offset = float(self.offset.get()) if self.manual.get() else 0.
        except ValueError:
            messagebox.showerror("拍点设置", "请填写数字 BPM 和首拍位置。")
            return
        self.stop_audio()
        self.set_busy(True)
        source = Path(self.source.get())
        def worker():
            try:
                result = analyse_song(source, self.output_dir, bpm, offset, progress=self.progress)
                self.messages.put(("analysed", result))
            except Exception as exc:
                self.messages.put(("error", str(exc)))
        threading.Thread(target=worker, daemon=True).start()

    def run_upload(self):
        if not self.result or self.busy:
            return
        self.stop_audio()
        self.set_busy(True)
        path = Path(self.result["package"]["path"])
        def worker():
            try:
                result = upload_package(path, progress=self.progress)
                self.messages.put(("uploaded", result))
            except Exception as exc:
                self.messages.put(("error", str(exc)))
        threading.Thread(target=worker, daemon=True).start()

    def append_log(self, text):
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def poll(self):
        try:
            while True:
                kind, value = self.messages.get_nowait()
                if kind == "progress":
                    self.status.set(value)
                    if not value.startswith("正在导入 Mosaico"):
                        self.append_log(value)
                elif kind == "analysed":
                    self.result = value
                    self.bpm.set(f"{value['analysis']['bpm_median']:.2f}")
                    self.offset.set(f"{value['analysis']['beats'][0]/16000:.3f}")
                    self.set_busy(False)
                    self.draw_timeline()
                    profile = value["plan"].get("motion_profile")
                    if profile:
                        self.append_log("本首动作倾向：" + profile["name"])
                    for warning in value["analysis"]["warnings"]:
                        self.append_log("拍点提示：" + warning)
                    names = sorted({CLIPS[x["clip_id"]]["name"] for x in value["plan"]["segments"]})
                    self.append_log("本首动作：" + "、".join(names))
                    self.append_log("可以试听；如拍点有偏差，启用手动 BPM 并重新分析。")
                elif kind == "uploaded":
                    self.set_busy(False)
                    self.status.set("导入完成！在 Mosaico 上长按右按钮切到这首歌曲，随后可拔掉 USB 独立播放。")
                    self.append_log(self.status.get())
                elif kind == "error":
                    self.set_busy(False)
                    self.status.set("未完成：" + value.splitlines()[0])
                    self.append_log(value)
                    messagebox.showerror("未完成", value)
        except queue.Empty:
            pass
        self.root.after(100, self.poll)

    def draw_timeline(self):
        self.timeline.delete("all")
        width = max(700, self.timeline.winfo_width())
        if not self.result:
            self.timeline.create_text(20, 40, anchor="w", text="分析后在这里查看动作时间线。", fill="#666666")
            return
        plan = self.result["plan"]
        colors = ("#83a994", "#f09b83", "#ddc46f", "#91a8cb", "#9ebadb", "#b9a0cb", "#cda3b7", "#9fbc99", "#d78676", "#9bbbb6",
                  "#d9a385", "#a1c593", "#8ebfba", "#bea4d1", "#dcb886", "#a6bad9",
                  "#72d6b8", "#e6b4ec", "#fab080")
        factor = (width - 28) / plan["sample_count"]
        for segment in plan["segments"]:
            left = 14 + segment["start_sample"] * factor
            right = 14 + segment["end_sample"] * factor
            clip = segment["clip_id"]
            self.timeline.create_rectangle(left, 26, right, 70, fill=colors[clip], outline="#f6f3ed")
            if right - left > 55:
                self.timeline.create_text((left+right)/2, 48, text=CLIPS[clip]["name"], font=("Microsoft YaHei UI", 8))
        for point in plan["beats"]:
            x = 14 + point["sample"] * factor
            self.timeline.create_line(x, 12, x, 23, fill="#334b42" if point["flags"] & 1 else "#aaaaaa", width=2 if point["flags"] & 1 else 1)
        self.timeline.create_text(14, 85, anchor="w", text="0:00")
        seconds = plan["sample_count"] // 16000
        self.timeline.create_text(width-14, 85, anchor="e", text=f"{seconds//60}:{seconds%60:02d}")

    def play_beats(self):
        if self.result:
            import winsound
            winsound.PlaySound(self.result["beat_preview_wav"], winsound.SND_FILENAME | winsound.SND_ASYNC)

    def stop_audio(self):
        import winsound
        winsound.PlaySound(None, 0)

    def close(self):
        if self.busy:
            messagebox.showinfo("正在处理", "请等待当前分析或导入结束。")
            return
        self.stop_audio()
        self.root.destroy()


def launch(output_dir: Path):
    root = tk.Tk()
    ttk.Style(root).theme_use("clam")
    ImporterWindow(root, output_dir)
    root.mainloop()
