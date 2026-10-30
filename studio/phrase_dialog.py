"""Edit song-relative scene cuts without changing music or choreography."""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk, messagebox
import math


def time_text(sample):
    minutes, remainder = divmod(round(sample * 1000 / 16000), 60000)
    seconds, milliseconds = divmod(remainder, 1000)
    return f"{minutes:02d}:{seconds:02d}.{milliseconds:03d}"


def parse_time(text):
    parts = text.strip().split(":")
    if len(parts) == 1:
        seconds = float(parts[0])
    elif len(parts) == 2:
        minutes = int(parts[0])
        remainder = float(parts[1])
        if minutes < 0 or not 0 <= remainder < 60:
            raise ValueError("时间请填写 分:秒，例如 01:23.450。")
        seconds = minutes * 60 + remainder
    else:
        raise ValueError("时间请填写 分:秒，或直接填写秒数。")
    if not math.isfinite(seconds) or seconds < 0:
        raise ValueError("请填写有效的非负时间。")
    return round(seconds * 16000)


class PhraseCueDialog(tk.Toplevel):
    def __init__(self, owner):
        super().__init__(owner)
        self.owner = owner
        self.project = owner.project
        self.title("歌词句尾 · 场景切换时间点")
        self.geometry("780x570")
        self.minsize(650, 480)
        self.transient(owner)
        self._document = None
        self._selected_sample = None
        self.time_value = tk.StringVar()
        self.text_value = tk.StringVar()
        self.note = tk.StringVar()
        self.columnconfigure(0, weight=1)
        self.rowconfigure(2, weight=1)
        ttk.Label(self, text="每到一个句尾，切换背景和同色系唱片；可试听后微调。\n"
                            "普通 LRC 时间通常是句首：优先采用带时间戳的空行句末标记，其余句尾由停顿或下一句开始估算。",
                  wraplength=740, padding=(14, 12)).grid(row=0, column=0, sticky="ew")
        tools = ttk.Frame(self, padding=(14, 0))
        tools.grid(row=1, column=0, sticky="ew")
        for label, command in (("识别人声句尾", owner._analyse_scene_cues),
                               ("按停顿估算", lambda: owner._analyse_scene_cues(estimate_only=True)),
                               ("导入 LRC", owner._import_scene_lrc),
                               ("当前位置添加", self._add_current)):
            ttk.Button(tools, text=label, command=command).pack(side="left", padx=(0, 5))
        body = ttk.Frame(self, padding=(14, 10))
        body.grid(row=2, column=0, sticky="nsew")
        body.columnconfigure(0, weight=1)
        body.rowconfigure(0, weight=1)
        self.tree = ttk.Treeview(body, columns=("time", "text", "source"), show="headings",
                                 selectmode="browse")
        for name, title, width in (("time", "切换时间", 100), ("text", "歌词 / 备注", 420),
                                   ("source", "来源", 140)):
            self.tree.heading(name, text=title)
            self.tree.column(name, width=width, minwidth=70, stretch=name == "text")
        self.tree.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(body, orient="vertical", command=self.tree.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.bind("<<TreeviewSelect>>", self._select)
        self.tree.bind("<Double-1>", lambda _event: self._listen())
        self.tree.bind("<Delete>", lambda _event: self._delete())
        fields = ttk.Frame(self, padding=(14, 0))
        fields.grid(row=3, column=0, sticky="ew")
        fields.columnconfigure(3, weight=1)
        ttk.Label(fields, text="句尾时间").grid(row=0, column=0, sticky="w")
        ttk.Entry(fields, textvariable=self.time_value, width=13).grid(row=0, column=1, padx=6)
        ttk.Label(fields, text="歌词 / 备注").grid(row=0, column=2)
        ttk.Entry(fields, textvariable=self.text_value).grid(row=0, column=3, sticky="ew", padx=6)
        ttk.Button(fields, text="应用", command=self._apply).grid(row=0, column=4)
        buttons = ttk.Frame(self, padding=(14, 10))
        buttons.grid(row=4, column=0, sticky="ew")
        for label, command in (("从句尾前试听", self._listen),
                               ("← 0.1秒", lambda: self._nudge(-1600)),
                               ("0.1秒 →", lambda: self._nudge(1600)),
                               ("删除时间点", self._delete)):
            ttk.Button(buttons, text=label, command=command).pack(side="left", padx=(0, 5))
        ttk.Button(buttons, text="关闭", command=self.destroy).pack(side="right")
        ttk.Label(self, textvariable=self.note, wraplength=740, padding=(14, 0, 14, 12)).grid(
            row=5, column=0, sticky="ew")
        self._refresh()

    def _current(self):
        if self.owner.project is not self.project:
            self.destroy()
            return False
        return True

    def refresh_document(self):
        """Update the existing table immediately without another timer loop."""
        if not self.winfo_exists():
            return False
        if not self._current():
            return False
        document = self.project.scene_cues
        if document != self._document:
            self._document = document
            self.tree.delete(*self.tree.get_children())
            for cue in document["cues"]:
                source = cue.get("source", document.get("source", ""))
                label = ("手动微调" if source == "manual" else
                         "句末标记" if source == "lrc_explicit_end" else
                         "人声识别" if "whisper" in source else
                         "歌词估算" if source.startswith("lrc") else "停顿估算")
                self.tree.insert("", "end", iid=str(cue["sample"]),
                                 values=(time_text(cue["sample"]), cue.get("text", ""), label))
            if self._selected_sample is not None and self.tree.exists(str(self._selected_sample)):
                self.tree.selection_set(str(self._selected_sample))
                self.tree.see(str(self._selected_sample))
            notes = "；".join(document.get("notes", []))
            self.note.set(f"共 {len(document['cues'])} 个句尾。" + notes)
        return True

    def _refresh(self):
        if self.refresh_document():
            self.after(300, self._refresh)

    def _select(self, _event=None):
        if not self._current():
            return
        selected = self.tree.selection()
        if selected:
            self._selected_sample = int(selected[0])
            cue = next(c for c in self.project.scene_cues["cues"]
                       if c["sample"] == self._selected_sample)
            self.time_value.set(time_text(cue["sample"]))
            self.text_value.set(cue.get("text", ""))

    def _commit(self, cues, select=None):
        if not self._current():
            return
        if self.owner.busy:
            self.note.set("正在分析，请等结果完成后再调整时间点。")
            return
        document = self.project.scene_cues
        document["cues"] = cues
        try:
            self.project.set_scene_cues(document)
            self._selected_sample = select
            self.owner._scene_cues_updated()
            self._document = None
        except Exception as exc:
            messagebox.showerror("无法调整句尾", str(exc), parent=self)

    def _add_current(self):
        if not self._current():
            return
        if self.owner.demo_id is not None:
            self.owner._return_project()
        sample = int(self.owner.audio.sample) % self.project.sample_count
        if not 0 < sample < self.project.sample_count:
            self.note.set("先将播放进度移到歌曲中的句尾，再添加时间点。")
            return
        cues = self.project.scene_cues["cues"]
        if any(c["sample"] == sample for c in cues):
            return
        cues.append(dict(sample=sample, text="手动句尾", source="manual"))
        self._commit(cues, sample)

    def _apply(self):
        if not self._current():
            return
        if self._selected_sample is None:
            return
        try:
            sample = parse_time(self.time_value.get())
            if not 0 < sample < self.project.sample_count:
                raise ValueError("句尾应位于歌曲开始之后、结束之前。")
            cues = [c for c in self.project.scene_cues["cues"]
                    if c["sample"] != self._selected_sample]
            if any(c["sample"] == sample for c in cues):
                raise ValueError("这个时间点已经有一个句尾。")
            cues.append(dict(sample=sample, text=self.text_value.get().strip(), source="manual"))
            self._commit(cues, sample)
        except Exception as exc:
            messagebox.showerror("时间格式不正确", str(exc), parent=self)

    def _nudge(self, delta):
        if not self._current():
            return
        if self._selected_sample is not None:
            self.time_value.set(time_text(max(1, min(self.project.sample_count-1,
                                                   self._selected_sample + delta))))
            self._apply()

    def _delete(self):
        if not self._current():
            return
        if self._selected_sample is not None:
            self._commit([c for c in self.project.scene_cues["cues"]
                          if c["sample"] != self._selected_sample])
            self._selected_sample = None

    def _listen(self):
        if not self._current():
            return
        if self._selected_sample is None:
            return
        self.owner._return_project()
        self.owner._seek(max(0, self._selected_sample - 16000))
        if not self.owner.audio.playing and not self.owner.audio.play():
            self.note.set(self.owner.audio.error)
