"""Visible task phases and elapsed time; unknown work never uses a fake percent."""
from __future__ import annotations

import time
from tkinter import ttk
import tkinter as tk


def elapsed_label(seconds):
    minutes, seconds = divmod(max(0, int(seconds)), 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes:02d}:{seconds:02d}"


class TaskProgress(ttk.Frame):
    def __init__(self, parent):
        super().__init__(parent)
        self.columnconfigure(0, weight=1)
        self.message = tk.StringVar(value="准备就绪 · 可导入歌曲，或为当前歌曲导入 LRC 歌词")
        self.elapsed = tk.StringVar(value="")
        ttk.Label(self, textvariable=self.message, style="Muted.TLabel", wraplength=950).grid(row=0, column=0, sticky="w")
        ttk.Label(self, textvariable=self.elapsed, style="Muted.TLabel").grid(row=0, column=1, sticky="e", padx=(12, 0))
        self.bar = ttk.Progressbar(self, mode="determinate", maximum=100, value=0, style="Import.Horizontal.TProgressbar")
        self.bar.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(4, 0))
        self.running = False
        self.started = 0.
        self._timer = None
        self.stage = 0

    def start(self, message):
        self.started = time.monotonic()
        self.running = True
        self.stage = 0
        self.message.set(message)
        self.bar.stop()
        self.bar.configure(mode="indeterminate", value=0)
        self.bar.start(14)
        self._tick()

    def update_stage(self, event):
        self.stage = max(self.stage, int(event.get("stage", 1)))
        total = max(self.stage, int(event.get("total", 5)))
        self.message.set(f"{self.stage}/{total} · {event['message']}")

    def update_message(self, message):
        self.message.set(message)

    def _tick(self):
        self._timer = None
        self.elapsed.set("已用时 " + elapsed_label(time.monotonic() - self.started))
        if self.running:
            self._timer = self.after(500, self._tick)

    def finish(self, success=True):
        self.running = False
        if self._timer is not None:
            self.after_cancel(self._timer)
            self._timer = None
        self.bar.stop()
        self.bar.configure(mode="determinate", value=100 if success else 0)
        self.message.set("处理完成" if success else "处理未完成，请查看下方提示")
        self.elapsed.set("用时 " + elapsed_label(time.monotonic() - self.started))
