"""Choose a song and its optional LRC before starting any background work."""
from __future__ import annotations

import os
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, ttk

from .lrc import find_companion_lrc


AUDIO_SUFFIXES = frozenset({".mp3", ".wav", ".m4a", ".flac", ".ogg", ".aac", ".wma"})
AUDIO_FILETYPES = [
    ("歌曲文件（MP3 / WAV / M4A / FLAC / OGG / AAC / WMA）", "*.mp3 *.wav *.m4a *.flac *.ogg *.aac *.wma"),
    ("所有文件", "*.*"),
]
LRC_FILETYPES = [("LRC 歌词文件", "*.lrc *.LRC"), ("所有文件", "*.*")]


def _path_text(value: str) -> str:
    text = value.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {'"', "'"}:
        text = text[1:-1].strip()
    return text


def _identity(value: str) -> str:
    text = _path_text(value)
    return os.path.normcase(os.path.abspath(os.path.expanduser(text))) if text else ""


def validate_selection(source_text: str, lyric_text: str = "", recognize_lyrics: bool = False,
                       auto_lyrics: bool = True) -> dict:
    """Validate locally without analysing files or changing the current project."""
    source_value = _path_text(source_text)
    if not source_value:
        raise ValueError("请先选择一首歌曲。")
    source = Path(source_value).expanduser()
    if source.suffix.lower() not in AUDIO_SUFFIXES:
        raise ValueError("请选择 MP3、WAV、M4A、FLAC、OGG、AAC 或 WMA 音频文件。")
    if not source.is_file():
        raise ValueError("找不到歌曲文件，请检查路径或重新选择。")
    lyric_value = _path_text(lyric_text)
    lyric_path = None
    if lyric_value:
        lyric_path = Path(lyric_value).expanduser()
        if lyric_path.suffix.lower() != ".lrc":
            raise ValueError("歌词请选择扩展名为 .lrc 的文件。")
        if not lyric_path.is_file():
            raise ValueError("找不到歌词文件，请重新选择，或清空后稍后导入。")
        lyric_path = lyric_path.resolve()
    return {"source": source.resolve(), "lyric_path": lyric_path,
            "recognize_lyrics": bool(recognize_lyrics) and lyric_path is None,
            "auto_lyrics": bool(auto_lyrics)}


class SongImportDialog(tk.Toplevel):
    def __init__(self, owner):
        super().__init__(owner)
        self.result = None
        self._submitted = False
        self._source_identity = ""
        self._manual_lyric = False
        self._auto_lyrics = True
        self._setting_lyric = False
        self._companion_after = None
        self.source = tk.StringVar(self)
        self.lyric = tk.StringVar(self)
        self.recognize_lyrics = tk.BooleanVar(self, value=False)
        self.error = tk.StringVar(self)
        self.lyric_note = tk.StringVar(self, value="可选。歌曲旁的同名 LRC 会自动带入，也可以手动选择。")
        self.title("导入歌曲与歌词")
        self.geometry("840x420")
        self.minsize(720, 390)
        self.configure(bg="#10171e")
        self.transient(owner)
        self.protocol("WM_DELETE_WINDOW", self._cancel)
        self._style()
        self._build()
        self.source.trace_add("write", self._source_changed)
        self.lyric.trace_add("write", self._lyric_changed)
        self.bind("<Return>", self._submit)
        self.bind("<Escape>", self._cancel)

    def _style(self):
        style = ttk.Style(self)
        style.configure("Import.TFrame", background="#10171e")
        style.configure("Import.TLabel", background="#10171e", foreground="#e4eaf0",
                        font=("Microsoft YaHei UI", 10))
        style.configure("ImportMuted.TLabel", background="#10171e", foreground="#a2b3c4",
                        font=("Microsoft YaHei UI", 9))
        style.configure("ImportError.TLabel", background="#10171e", foreground="#ffc39e",
                        font=("Microsoft YaHei UI", 9))
        style.configure("ImportTitle.TLabel", background="#10171e", foreground="#f4f7fa",
                        font=("Microsoft YaHei UI", 16, "bold"))
        style.configure("Import.TCheckbutton", background="#10171e", foreground="#d7e0e9",
                        font=("Microsoft YaHei UI", 10))
        style.map("Import.TCheckbutton", background=[("active", "#10171e")],
                  foreground=[("active", "#ffffff")])

    def _build(self):
        outer = ttk.Frame(self, padding=20, style="Import.TFrame")
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        ttk.Label(outer, text="音乐和歌词一起选", style="ImportTitle.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(outer, text="也可以先导入歌曲，稍后从顶部导入 LRC 歌词。",
                  style="ImportMuted.TLabel").grid(row=1, column=0, sticky="w", pady=(6, 14))
        ttk.Label(outer, text="歌曲文件", style="Import.TLabel").grid(row=2, column=0, sticky="w")
        song_line = ttk.Frame(outer, style="Import.TFrame")
        song_line.grid(row=3, column=0, sticky="ew", pady=(5, 12))
        song_line.columnconfigure(0, weight=1)
        self.source_entry = ttk.Entry(song_line, textvariable=self.source)
        self.source_entry.grid(row=0, column=0, sticky="ew")
        ttk.Button(song_line, text="选择歌曲…", command=self._choose_song).grid(row=0, column=1, padx=(8, 0))
        ttk.Label(outer, text="歌词文件（LRC，可选）", style="Import.TLabel").grid(row=4, column=0, sticky="w")
        lyric_line = ttk.Frame(outer, style="Import.TFrame")
        lyric_line.grid(row=5, column=0, sticky="ew", pady=(5, 3))
        lyric_line.columnconfigure(0, weight=1)
        self.lyric_entry = ttk.Entry(lyric_line, textvariable=self.lyric)
        self.lyric_entry.grid(row=0, column=0, sticky="ew")
        ttk.Button(lyric_line, text="选择 LRC…", command=self._choose_lyric).grid(row=0, column=1, padx=(8, 0))
        ttk.Button(lyric_line, text="清空", command=self._clear_lyric).grid(row=0, column=2, padx=(6, 0))
        ttk.Label(outer, textvariable=self.lyric_note, style="ImportMuted.TLabel",
                  wraplength=670).grid(row=6, column=0, sticky="ew", pady=(3, 10))
        ttk.Checkbutton(outer, text="没有 LRC 时识别演唱句尾（较慢）", variable=self.recognize_lyrics,
                        style="Import.TCheckbutton").grid(row=7, column=0, sticky="w")
        ttk.Label(outer, textvariable=self.error, style="ImportError.TLabel",
                  wraplength=670).grid(row=8, column=0, sticky="ew", pady=(8, 4))
        outer.rowconfigure(8, weight=1)
        buttons = ttk.Frame(outer, style="Import.TFrame")
        buttons.grid(row=9, column=0, sticky="e")
        ttk.Button(buttons, text="取消", command=self._cancel).pack(side="left", padx=(0, 8))
        self.start_button = ttk.Button(buttons, text="开始分析", style="Accent.TButton", command=self._submit)
        self.start_button.pack(side="left")

    def _set_lyric(self, value):
        self._setting_lyric = True
        try:
            self.lyric.set(str(value) if value else "")
        finally:
            self._setting_lyric = False

    def _source_changed(self, *_):
        self.error.set("")
        identity = _identity(self.source.get())
        if identity == self._source_identity:
            return
        self._source_identity = identity
        self._manual_lyric = False
        self._auto_lyrics = True
        self._set_lyric("")
        self.lyric_note.set("可选。歌曲旁的同名 LRC 会自动带入，也可以手动选择。")
        if self._companion_after is not None:
            self.after_cancel(self._companion_after)
        self._companion_after = self.after(250, self._detect_companion)

    def _detect_companion(self):
        if self._companion_after is not None:
            self.after_cancel(self._companion_after)
            self._companion_after = None
        if self._manual_lyric:
            return
        source_value = _path_text(self.source.get())
        if not source_value:
            return
        source = Path(source_value).expanduser()
        if not source.is_file() or source.suffix.lower() not in AUDIO_SUFFIXES:
            return
        try:
            companion = find_companion_lrc(source)
        except OSError:
            companion = None
        if companion is not None:
            self._set_lyric(companion)
            self.lyric_note.set(f"已找到同名歌词：{companion.name}。也可改选另一份 LRC。")
        else:
            self.lyric_note.set("没有找到同名 LRC。可手动选择，也可以稍后从顶部导入歌词。")

    def _lyric_changed(self, *_):
        self.error.set("")
        if not self._setting_lyric:
            self._manual_lyric = True
            self._auto_lyrics = bool(self.lyric.get().strip())
            self.lyric_note.set("已手动指定歌词；有 LRC 时优先使用歌词文件。" if self.lyric.get().strip()
                                else "未选择歌词文件。也可以先导入歌曲，稍后再添加。")

    def _choose_song(self):
        value = filedialog.askopenfilename(parent=self, title="选择要导入的歌曲", filetypes=AUDIO_FILETYPES)
        if value:
            self.source.set(value)
            self._detect_companion()

    def _choose_lyric(self):
        options = {"parent": self, "title": "选择这首歌的 LRC 歌词", "filetypes": LRC_FILETYPES}
        source_value = _path_text(self.source.get())
        if source_value:
            source = Path(source_value).expanduser()
            if source.parent.is_dir():
                options["initialdir"] = str(source.parent)
        value = filedialog.askopenfilename(**options)
        if value:
            self.lyric.set(value)

    def _clear_lyric(self):
        self._manual_lyric = True
        self.lyric.set("")

    def _submit(self, _event=None):
        if self._submitted:
            return "break"
        # A user can paste a path and immediately press Enter before the delayed lookup.
        self._detect_companion()
        try:
            selected = validate_selection(self.source.get(), self.lyric.get(), self.recognize_lyrics.get(),
                                          auto_lyrics=self._auto_lyrics)
        except (ValueError, OSError) as exc:
            self.error.set(str(exc))
            return "break"
        self._submitted = True
        self.start_button.configure(state="disabled")
        self.result = selected
        self._destroy()
        return "break"

    def _cancel(self, _event=None):
        if not self._submitted:
            self.result = None
            self._destroy()
        return "break"

    def _destroy(self):
        if self._companion_after is not None:
            self.after_cancel(self._companion_after)
            self._companion_after = None
        try:
            self.grab_release()
        except tk.TclError:
            pass
        self.destroy()


def ask_song_import(parent):
    """Return the accepted local paths, or None; no analysis starts in this dialog."""
    dialog = SongImportDialog(parent)
    dialog.wait_visibility()
    dialog.grab_set()
    dialog.source_entry.focus_set()
    parent.wait_window(dialog)
    return dialog.result
