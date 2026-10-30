"""Tk desktop editor: exact board preview, library audition and beat editing."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import queue
import threading
import time
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

from PIL import ImageTk

from importer.package import package_bytes
from importer.planner import ACTIVE_CLIPS, CLIPS
from .model import EditorProject
from .runtime import StudioRenderer, AudioTransport, make_clip_demo
from .preview_worker import LatestFrameWorker
from .export_dialog import AnimationExportDialog
from .paths import data_root, is_frozen, resource_root
from .scene_auto import SceneAutoController
from .task_progress import TaskProgress
from .timeline import Timeline, CLIP_COLORS
from .themes import (THEMES, RECORD_COLORS, FOOT_EFFECTS,
                     theme_by_id, record_by_id, effect_by_id)
from .library import filtered_clips, level_choices, clip_description, clip_level, clip_english_name, LEVEL_NAMES

ROOT = resource_root()
OUTPUT = data_root() / "projects"
COLORS = CLIP_COLORS
SKIN_LABELS = ("人物 1 · 澄音", "人物 2 · 凛", "人物 3 · 绮星", "人物 4 · 霓虹 Neon")
SKIN_DESCRIPTIONS = (
    "澄音 · 长双马尾与百褶裙，随动作拖曳和回弹。",
    "凛 · 银发与机能外套，发梢和衣摆轻微跟随。",
    "绮星 · 粉发星饰，发束与裙边自然摆动。",
    "霓虹 Neon · 保留原造型，发束加入惯性甩动。",
)
SCENE_AUTO_LABELS = {"关闭": "off", "每 2 秒": "interval", "歌词句首": "lyric_start", "歌词句尾": "phrase"}


def stamp(samples):
    minutes, remainder = divmod(round(max(0, samples) * 100 / 16000), 6000)
    seconds, hundredths = divmod(remainder, 100)
    return f"{minutes:02d}:{seconds:02d}.{hundredths:02d}"


class MotionStudio(tk.Tk):
    def __init__(self, initial=None, initial_skin=0):
        super().__init__()
        self.title("Mosaico 动作编排 1.20.9 · 电脑模拟器")
        self.geometry("1390x900")
        self.minsize(1120, 820)
        self.configure(bg="#10171e")
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)
        self.project = None
        self.project_path = None
        self.dirty = False
        self.busy = False
        self._task_failed = False
        self.events = queue.Queue()
        self.renderer = StudioRenderer()
        self.preview_worker = LatestFrameWorker(self.renderer)
        self._preview_revision = 0
        self._last_render_submit = 0.0
        self._render_context = None
        self._fullscreen = None
        self._last_preview_result = None
        self._closing = False
        if not 0 <= initial_skin < self.renderer.skin_count:
            raise ValueError("所选人物未包含在当前模拟器资源中，请更新工作台。")
        self.audio = AudioTransport()
        self.demo_id = None
        self.demo_start = 0.
        self.demo_packet = None
        self.project_packet = None
        self.drag_state = None
        self.drag_ghost = None
        self.skin = tk.StringVar(value=SKIN_LABELS[initial_skin])
        self.skin_description = tk.StringVar(value=SKIN_DESCRIPTIONS[initial_skin])
        self.theme = tk.StringVar(value=THEMES[0]["name"])
        self.theme_description = tk.StringVar(value=THEMES[0].get("description", ""))
        self.record_color = tk.StringVar(value=RECORD_COLORS[0]["name"])
        self.foot_effect = tk.StringVar(value=FOOT_EFFECTS[0]["name"])
        self.loop = tk.BooleanVar(value=True)
        self.show_lyrics = tk.BooleanVar(value=True)
        self.status = tk.StringVar(value="正在打开示例编排…")
        self.song_label = tk.StringVar(value="MOSAICO / MOTION STUDIO")
        self.mode_label = tk.StringVar(value="歌曲编排 · 音乐与动作同步预览")
        self.clock_label = tk.StringVar(value="00:00.00 / 00:00.00")
        self.pose_label = tk.StringVar(value="")
        self.selection_label = tk.StringVar(value="点击时间线上的动作进行调整")
        self.action_detail = tk.StringVar(value="点击动作查看演示与脚下变化；拖到拍点即可编排。")
        self.library_level = tk.StringVar(value="全部动作")
        self.library_query = tk.StringVar(value="")
        self.library_count = tk.StringVar()
        self.demo_bpm = tk.StringVar(value="120")
        self.beat_text = tk.StringVar()
        self.count_text = tk.StringVar()
        self.scene_auto_controller = SceneAutoController()
        self.scene_auto_mode = tk.StringVar(value="关闭")
        self.scene_auto_character = tk.BooleanVar(value=False)
        self.scene_auto_status = tk.StringVar(value="场景手动切换")
        self._cue_dialog = None
        self.skin.trace_add("write", lambda *_: self._configure_scene_auto())
        self._style()
        self._build()
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.bind("<F11>", self._toggle_fullscreen)
        for key, action in (("<Control-s>", self._save), ("<Control-z>", self._undo),
                            ("<Control-y>", self._redo), ("<Delete>", self._delete),
                            ("<space>", self._toggle)):
            self.bind(key, lambda event, fn=action: self._shortcut(event, fn))
        self.after(100, self._poll)
        self.after(25, self._frame)
        self.after(200, lambda: self._initial(initial))

    def _style(self):
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("TFrame", background="#10171e")
        style.configure("Panel.TFrame", background="#18232d")
        style.configure("TLabel", background="#10171e", foreground="#e4eaf0", font=("Microsoft YaHei UI", 10))
        style.configure("Muted.TLabel", foreground="#a2b3c4")
        style.configure("Panel.TLabel", background="#18232d")
        style.configure("Title.TLabel", foreground="#f4f7fa", font=("Microsoft YaHei UI", 16, "bold"))
        style.configure("TButton", padding=(10, 7), font=("Microsoft YaHei UI", 10))
        style.configure("TButton", background="#293c49", foreground="#e4eaf0", borderwidth=1)
        style.map("TButton", background=[("active", "#3b5363")])
        style.configure("Accent.TButton", background="#3c827c", foreground="#ffffff")
        style.map("Accent.TButton", background=[("active", "#4b9a92")])
        style.configure("TCheckbutton", background="#10171e", foreground="#d7e0e9")
        style.configure("TCombobox", padding=4, font=("Microsoft YaHei UI", 10), fieldbackground="#22313e", foreground="#e4eaf0")
        style.map("TCombobox", fieldbackground=[("readonly", "#22313e")], foreground=[("readonly", "#e4eaf0")])
        style.configure("TEntry", fieldbackground="#22313e", foreground="#e4eaf0", insertcolor="#e4eaf0")
        style.configure("Import.Horizontal.TProgressbar", background="#54b8aa", troughcolor="#22313e", thickness=7)

    def _build(self):
        header = ttk.Frame(self)
        header.grid(row=0, column=0, sticky="ew")
        toolbar = ttk.Frame(header, padding=(18, 10))
        toolbar.pack(fill="x")
        ttk.Label(toolbar, text="MOSAICO", style="Title.TLabel").pack(side="left", padx=(0, 22))
        self.task_buttons = []
        for text, cmd in (("导入歌曲…", self._import_song), ("导入歌词（LRC）", self._import_scene_lrc),
                          ("打开编排", self._open), ("保存编排", self._save), ("导出动画…", self._export_animation)):
            button = ttk.Button(toolbar, text=text, command=cmd)
            button.pack(side="left", padx=4)
            self.task_buttons.append(button)
        more = ttk.Menubutton(toolbar, text="更多…")
        menu = tk.Menu(more, tearoff=False)
        menu.add_command(label="导出设备歌曲包", command=self._export)
        menu.add_command(label="重编自动动作", command=self._recompose)
        more.configure(menu=menu)
        more.pack(side="right", padx=(6, 0))
        self.task_buttons.append(more)
        upload = ttk.Button(toolbar, text="传入 Mosaico", command=self._upload)
        upload.pack(side="right", padx=(8, 0))
        self.task_buttons.append(upload)
        self.progress_panel = TaskProgress(header)
        self.progress_panel.pack(fill="x", padx=18, pady=(0, 8))

        body = ttk.Frame(self, padding=(18, 0))
        self.body_panel = body
        body.grid(row=1, column=0, sticky="nsew")
        body.columnconfigure(0, weight=0, minsize=232)
        body.columnconfigure(1, weight=1, minsize=480)
        body.columnconfigure(2, weight=0, minsize=255)
        body.rowconfigure(0, weight=1)

        library = ttk.Frame(body, padding=12, style="Panel.TFrame")
        library.grid(row=0, column=0, sticky="nsew", padx=(0, 14))
        ttk.Label(library, text="预制动作库", style="Panel.TLabel", font=("Microsoft YaHei UI", 13, "bold")).pack(anchor="w")
        ttk.Label(library, textvariable=self.library_count, style="Panel.TLabel").pack(anchor="w", pady=(2, 4))
        self.level_combo = ttk.Combobox(library, textvariable=self.library_level,
                                        values=level_choices(), state="readonly", width=22)
        self.level_combo.pack(fill="x", pady=(0, 4))
        self.level_combo.bind("<<ComboboxSelected>>", self._refresh_library)
        self.search_entry = ttk.Entry(library, textvariable=self.library_query)
        self.search_entry.pack(fill="x", pady=(0, 2))
        ttk.Label(library, text="搜索名称 / 英文名 / 脚法", style="Panel.TLabel",
                  font=("Microsoft YaHei UI", 8)).pack(anchor="w", pady=(0, 6))
        speed_line = ttk.Frame(library, style="Panel.TFrame")
        speed_line.pack(fill="x", pady=(0, 6))
        ttk.Label(speed_line, text="演示速度", style="Panel.TLabel").pack(side="left")
        self.demo_speed_combo = ttk.Combobox(speed_line, textvariable=self.demo_bpm,
                                             values=("80", "100", "120", "150"), state="readonly", width=5)
        self.demo_speed_combo.pack(side="left", padx=(6, 3))
        self.demo_speed_combo.bind("<<ComboboxSelected>>", self._change_demo_speed)
        ttk.Label(speed_line, text="BPM", style="Panel.TLabel").pack(side="left")
        library_pane = ttk.Frame(library, style="Panel.TFrame")
        library_pane.pack(fill="both", expand=True)
        self.library_canvas = tk.Canvas(library_pane, width=215, height=1, bg="#18232d", highlightthickness=0)
        library_scroll = ttk.Scrollbar(library_pane, orient="vertical", command=self.library_canvas.yview)
        self.library_canvas.configure(yscrollcommand=library_scroll.set)
        library_scroll.pack(side="right", fill="y")
        self.library_canvas.pack(side="left", fill="both", expand=True)
        self.library_cards = tk.Frame(self.library_canvas, bg="#18232d")
        library_window = self.library_canvas.create_window(0, 0, window=self.library_cards, anchor="nw")
        self.library_cards.bind("<Configure>", lambda e: self.library_canvas.configure(scrollregion=self.library_canvas.bbox("all")))
        self.library_canvas.bind("<Configure>", lambda e: self.library_canvas.itemconfigure(library_window, width=e.width))
        self.library_canvas.bind("<MouseWheel>", self._library_wheel)
        self.library_cards.bind("<MouseWheel>", self._library_wheel)
        self.library_query.trace_add("write", lambda *_: self._refresh_library())
        self._refresh_library()

        center = ttk.Frame(body)
        self.center_panel = center
        center.grid(row=0, column=1, sticky="nsew")
        center.columnconfigure(0, weight=1)
        center.rowconfigure(2, weight=1)
        ttk.Label(center, textvariable=self.song_label, style="Title.TLabel", anchor="center").grid(row=0, column=0, sticky="ew", pady=(4, 4))
        preview_header = ttk.Frame(center)
        preview_header.grid(row=1, column=0, sticky="ew", pady=(0, 8))
        preview_header.columnconfigure(0, weight=1)
        ttk.Label(preview_header, textvariable=self.mode_label, style="Muted.TLabel", anchor="center").grid(row=0, column=0, sticky="ew")
        self.fullscreen_button = ttk.Button(preview_header, text="全屏预览 ⛶", command=self._toggle_fullscreen)
        self.fullscreen_button.grid(row=0, column=1, padx=(6, 0))
        self.preview = tk.Canvas(center, bg="#10171e", width=480, height=440, highlightthickness=0)
        self.preview.grid(row=2, column=0, sticky="nsew")
        self.preview_image = self.preview.create_image(0, 0, anchor="center")
        self.preview.bind("<Double-Button-1>", self._toggle_fullscreen)
        # Playback text must not contribute a changing requested width/height.
        self.pose_caption = tk.Label(center, textvariable=self.pose_label, width=1, height=1,
                                     bg="#10171e", fg="#a2b3c4", font=("Microsoft YaHei UI", 10),
                                     anchor="center", borderwidth=0, highlightthickness=0)
        self.pose_caption.grid(row=3, column=0, sticky="ew", pady=6)
        scene_tools = ttk.Frame(center)
        scene_tools.grid(row=4, column=0, sticky="ew", pady=(0, 6))
        scene_tools.columnconfigure(1, weight=1)
        ttk.Label(scene_tools, text="自动切场景").grid(row=0, column=0, padx=(0, 7))
        self.scene_auto_combo = ttk.Combobox(scene_tools, textvariable=self.scene_auto_mode,
                                             values=tuple(SCENE_AUTO_LABELS), state="readonly", width=10)
        self.scene_auto_combo.grid(row=0, column=1, sticky="w")
        self.scene_auto_combo.bind("<<ComboboxSelected>>", self._change_scene_auto)
        ttk.Checkbutton(scene_tools, text="同步换人物", variable=self.scene_auto_character,
                        command=self._change_scene_auto).grid(row=0, column=2, padx=5)
        ttk.Button(scene_tools, text="导入 LRC", command=self._import_scene_lrc).grid(row=0, column=3, padx=(0, 5))
        ttk.Button(scene_tools, text="句尾时间点…", command=self._open_cue_editor).grid(row=0, column=4)
        self.auto_status_caption = tk.Label(scene_tools, textvariable=self.scene_auto_status,
                                            width=1, height=1, anchor="w", bg="#10171e", fg="#a2b3c4",
                                            font=("Microsoft YaHei UI", 10), borderwidth=0, highlightthickness=0)
        self.auto_status_caption.grid(row=1, column=0, columnspan=5, sticky="ew", pady=(5, 0))

        right = ttk.Frame(body, width=255, padding=14, style="Panel.TFrame")
        self.sidebar = right
        right.grid(row=0, column=2, sticky="nsew", padx=(14, 0))
        # Keep this column independent of the active character's text metrics.
        # Vertical room still follows the main window through sticky="nsew".
        right.grid_propagate(False)
        right.columnconfigure(0, weight=1)
        right.rowconfigure(0, weight=1)
        # Keep scene selection and the way back from a demo visible even at
        # the minimum window height. Only the action settings may scroll.
        settings_pane = ttk.Frame(right, style="Panel.TFrame")
        settings_pane.grid(row=0, column=0, sticky="nsew")
        settings_pane.columnconfigure(0, weight=1)
        settings_pane.rowconfigure(0, weight=1)
        self.settings_canvas = tk.Canvas(settings_pane, width=200, height=1,
                                          bg="#18232d", highlightthickness=0)
        settings_scroll = ttk.Scrollbar(settings_pane, orient="vertical", command=self.settings_canvas.yview)
        self.settings_canvas.configure(yscrollcommand=settings_scroll.set)
        self.settings_canvas.grid(row=0, column=0, sticky="nsew")
        settings_scroll.grid(row=0, column=1, sticky="ns")
        self.settings_fields = ttk.Frame(self.settings_canvas, style="Panel.TFrame")
        settings_window = self.settings_canvas.create_window(0, 0, window=self.settings_fields, anchor="nw")
        self.settings_fields.bind("<Configure>", lambda e: self.settings_canvas.configure(scrollregion=self.settings_canvas.bbox("all")))
        self.settings_canvas.bind("<Configure>", lambda e: self.settings_canvas.itemconfigure(settings_window, width=e.width))
        ttk.Label(self.settings_fields, text="动作设置", style="Panel.TLabel", font=("Microsoft YaHei UI", 13, "bold")).pack(anchor="w")
        ttk.Label(self.settings_fields, textvariable=self.selection_label, style="Panel.TLabel", wraplength=195).pack(anchor="w", pady=(6, 8))
        ttk.Label(self.settings_fields, textvariable=self.action_detail, style="Panel.TLabel",
                  wraplength=195, font=("Microsoft YaHei UI", 9)).pack(anchor="w", pady=(0, 6))
        beat_fields = ttk.Frame(self.settings_fields, style="Panel.TFrame")
        beat_fields.pack(fill="x")
        beat_fields.columnconfigure((0, 1), weight=1, uniform="beat-fields")
        ttk.Label(beat_fields, text="起始拍（1起）", style="Panel.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(beat_fields, text="时长（拍）", style="Panel.TLabel").grid(row=0, column=1, sticky="w", padx=(6, 0))
        self.beat_entry = ttk.Entry(beat_fields, textvariable=self.beat_text, width=7)
        self.beat_entry.grid(row=1, column=0, sticky="ew", pady=(3, 8), padx=(0, 6))
        self.count_combo = ttk.Combobox(beat_fields, textvariable=self.count_text, values=(), state="readonly", width=6)
        self.count_combo.grid(row=1, column=1, sticky="ew", pady=(3, 8), padx=(6, 0))
        action_buttons = ttk.Frame(self.settings_fields, style="Panel.TFrame")
        action_buttons.pack(fill="x")
        action_buttons.columnconfigure((0, 1), weight=1, uniform="action-buttons")
        ttk.Button(action_buttons, text="应用设置", command=self._apply_selection).grid(row=0, column=0, sticky="ew", padx=(0, 3))
        ttk.Button(action_buttons, text="删除动作", command=self._delete).grid(row=0, column=1, sticky="ew", padx=(3, 0))
        def bind_settings_wheel(widget):
            widget.bind("<MouseWheel>", self._settings_wheel)
            for child in widget.winfo_children():
                bind_settings_wheel(child)
        bind_settings_wheel(self.settings_canvas)

        ttk.Separator(right).grid(row=1, column=0, sticky="ew", pady=8)
        appearance = ttk.Frame(right, style="Panel.TFrame")
        appearance.grid(row=2, column=0, sticky="ew")
        appearance_header = ttk.Frame(appearance, style="Panel.TFrame")
        appearance_header.pack(fill="x")
        ttk.Label(appearance_header, text="预览人物", style="Panel.TLabel").pack(side="left")
        ttk.Button(appearance_header, text="返回编排", command=self._return_project).pack(side="right")
        ttk.Combobox(appearance, textvariable=self.skin, state="readonly",
                     values=SKIN_LABELS[:self.renderer.skin_count]).pack(fill="x", pady=(4, 5))
        self.skin_description_caption = tk.Label(appearance, textvariable=self.skin_description,
                width=1, height=2, wraplength=225, anchor="nw", justify="left", bg="#18232d",
                fg="#e4eaf0", font=("Microsoft YaHei UI", 8), borderwidth=0, highlightthickness=0)
        self.skin_description_caption.pack(fill="x", pady=(0, 3))
        ttk.Label(appearance, text="背景主题", style="Panel.TLabel").pack(anchor="w", pady=(3, 0))
        self.theme_combo = ttk.Combobox(appearance, textvariable=self.theme, state="readonly",
                                        values=tuple(theme["name"] for theme in THEMES))
        self.theme_combo.pack(fill="x", pady=(4, 3))
        self.theme_combo.bind("<<ComboboxSelected>>", self._change_theme)
        ttk.Label(appearance, text="唱片颜色", style="Panel.TLabel").pack(anchor="w", pady=(3, 0))
        self.record_combo = ttk.Combobox(appearance, textvariable=self.record_color, state="readonly",
                                         values=tuple(record["name"] for record in RECORD_COLORS))
        self.record_combo.pack(fill="x", pady=(3, 3))
        self.record_combo.bind("<<ComboboxSelected>>", self._change_record)
        ttk.Label(appearance, text="脚边特效", style="Panel.TLabel").pack(anchor="w", pady=(2, 0))
        self.effect_combo = ttk.Combobox(appearance, textvariable=self.foot_effect, state="readonly",
                                         values=tuple(effect["name"] for effect in FOOT_EFFECTS[:self.renderer.effect_count]))
        self.effect_combo.pack(fill="x", pady=(3, 3))
        self.effect_combo.bind("<<ComboboxSelected>>", self._change_effect)
        self.theme_description_caption = tk.Label(appearance, textvariable=self.theme_description,
                width=1, height=2, wraplength=225, anchor="nw", justify="left", bg="#18232d",
                fg="#e4eaf0", font=("Microsoft YaHei UI", 8), borderwidth=0, highlightthickness=0)
        self.theme_description_caption.pack(fill="x", pady=(0, 2))

        controls = ttk.Frame(self, padding=(18, 8))
        controls.grid(row=2, column=0, sticky="ew")
        self.play_button = ttk.Button(controls, text="▶ 播放", style="Accent.TButton", command=self._toggle)
        self.play_button.pack(side="left")
        ttk.Button(controls, text="回到开头", command=self._stop).pack(side="left", padx=6)
        ttk.Label(controls, textvariable=self.clock_label, font=("Consolas", 11)).pack(side="left", padx=10)
        ttk.Checkbutton(controls, text="循环", variable=self.loop).pack(side="left")
        self.show_lyrics_check = ttk.Checkbutton(controls, text="显示歌词", variable=self.show_lyrics,
                                               command=self._change_show_lyrics)
        self.show_lyrics_check.pack(side="left", padx=(8, 0))
        ttk.Button(controls, text="整理衔接", command=self._smooth).pack(side="right", padx=4)
        ttk.Button(controls, text="重做", command=self._redo).pack(side="right", padx=4)
        ttk.Button(controls, text="撤销", command=self._undo).pack(side="right", padx=4)
        ttk.Button(controls, text="＋", command=lambda: self.timeline.zoom(1.35)).pack(side="right")
        ttk.Button(controls, text="－", command=lambda: self.timeline.zoom(1 / 1.35)).pack(side="right", padx=4)
        self.timeline = Timeline(self, on_seek=self._seek, on_change=self._edited, on_select=self._selection)
        self.timeline.grid(row=3, column=0, sticky="ew", padx=18)
        ttk.Label(self, textvariable=self.status, style="Muted.TLabel", anchor="w", padding=(18, 9), wraplength=1300).grid(row=4, column=0, sticky="ew")

    def _initial(self, initial):
        if initial:
            self._load(Path(initial))
            return
        demo = ROOT / "examples" / "demo.vskproj"
        if demo.is_file():
            self._load(demo)
        else:
            self.status.set("未找到示例。可打开已有编排，或导入歌曲开始。")

    def _load(self, path):
        try:
            project = EditorProject.load(path)
            full_project = path.suffix.lower() == ".vskproj"
            save_path = path if full_project else None
            if is_frozen() and path.resolve().is_relative_to(resource_root().resolve()):
                # Bundled examples are read-only resources, even when the
                # extracted portable folder itself happens to be writable.
                save_path = None
            self._adopt(project, save_path,
                        preserve_appearance=not full_project)
        except Exception as exc:
            self._error("无法打开编排", exc)

    def _adopt(self, project, path=None, *, preserve_appearance=False):
        keep_appearance = preserve_appearance and self.project is not None
        appearance_changed = project.inherit_appearance(self.project) if keep_appearance else False
        packet = package_bytes(project.build_plan(), project.pcm.astype("<i2").tobytes())
        try:
            self._set_renderer_package(packet)
        except Exception:
            if self.project_packet:
                self._set_renderer_package(self.demo_packet if self.demo_id is not None else self.project_packet)
            raise
        self.audio.stop()
        migrated = bool(project.migration_report.get("removed_count"))
        self.project, self.project_path = project, None if migrated else path
        self._sync_theme()
        self.demo_id = None
        self.audio.load(project.pcm)
        self.project_packet = packet
        self.timeline.set_project(project)
        self.song_label.set(project.title)
        self.mode_label.set("歌曲编排 · 音乐与动作同步预览")
        self._set_dirty(migrated or appearance_changed)
        self._selection(None)
        bpm = project.analysis.get("bpm_median", 0)
        self.status.set(project.migration_report["message"] if migrated else
                        f"已打开：{project.title} · {project.duration:.1f} 秒 · {float(bpm):.1f} BPM。"
                        + ("外观保持当前选择。" if keep_appearance else "")
                        + "点击动作演示，或拖入下方时间线。")
        if project.audio_reactivity is None:
            self._background("正在补分析鼓点与音量，保留现有手工编排…", self._measure_controls, project, False)
        else:
            self.mode_label.set("鼓点抬脚 · 音量控制线宽 · 节奏决定复杂度")

    @staticmethod
    def _measure_controls(project, recompose):
        from importer.analysis import extract_audio_reactivity
        return ("audio_controls", (project, project.audio_reactivity or extract_audio_reactivity(project.pcm), recompose))

    def _recompose(self):
        if self.project and not self.busy:
            self.audio.pause()
            self._background("正在按鼓点与局部节奏重编自动动作，保留手工片段…",
                             self._measure_controls, self.project, True)

    def _set_dirty(self, value=True):
        self.dirty = value
        self.title(f"{'● ' if value else ''}Mosaico 动作编排 1.20.9 · {self.project.title if self.project else '电脑模拟器'}")

    def _sync_theme(self):
        selected = theme_by_id(self.project.theme_id)
        record = record_by_id(self.project.record_id)
        self.theme.set(selected["name"])
        self.record_color.set(record["name"])
        self.foot_effect.set(effect_by_id(self.project.effect_id)["name"])
        self.theme_description.set("背景、唱片与特效随工程保存。")
        if hasattr(self, "show_lyrics"):
            self.show_lyrics.set(self.project.show_lyrics)
        if hasattr(self, "scene_auto_mode"):
            self._sync_scene_auto()

    def _change_show_lyrics(self):
        if not self.project:
            return
        if self.busy:
            self.show_lyrics.set(self.project.show_lyrics)
            return
        if self.project.set_show_lyrics(self.show_lyrics.get()):
            self._set_dirty()
            self.status.set("已显示歌词，按当前音乐进度继续。" if self.project.show_lyrics else
                            "已隐藏歌词，音乐、动作和场景切换继续。")

    def _sync_scene_auto(self):
        if not self.project:
            return
        settings = self.project.scene_auto
        self.scene_auto_mode.set(next(label for label, mode in SCENE_AUTO_LABELS.items()
                                      if mode == settings["mode"]))
        self.scene_auto_character.set(settings["include_character"])
        self._configure_scene_auto()

    def _configure_scene_auto(self):
        if not self.project:
            return
        settings = self.project.scene_auto
        self.scene_auto_controller.configure(
            theme=self.project.theme_id, record_id=self.project.record_id,
            skin=SKIN_LABELS.index(self.skin.get()), mode=settings["mode"],
            include_character=settings["include_character"],
            cue_samples=(self.project.lyric_start_samples if settings["mode"] == "lyric_start"
                         else [cue["sample"] for cue in self.project.scene_cues["cues"]]),
            song_sample_count=self.project.sample_count, loop=self.loop.get())

    def _change_scene_auto(self, _event=None):
        if not self.project:
            return
        if self.busy:
            self._sync_scene_auto()
            return
        mode = SCENE_AUTO_LABELS[self.scene_auto_mode.get()]
        if self.project.set_scene_auto(mode, self.scene_auto_character.get()):
            self._set_dirty()
        self._sync_scene_auto()
        if mode == "phrase" and not self.project.scene_cues["cues"]:
            self._analyse_scene_cues()
        elif mode == "lyric_start":
            self.status.set("已按 LRC 歌词句首切换场景，唱片同步使用同色系。"
                            if self.project.lyric_start_samples else
                            "尚无歌词句首，请点击顶部“导入歌词（LRC）”；导入后按每句开始时间切换。")

    def _open_cue_editor(self):
        if not self.project:
            return
        if self._cue_dialog is not None and self._cue_dialog.winfo_exists():
            if self._cue_dialog.project is self.project:
                self._cue_dialog.refresh_document()
                self._cue_dialog.lift()
                return
            self._cue_dialog.destroy()
        from .phrase_dialog import PhraseCueDialog
        self._cue_dialog = PhraseCueDialog(self)

    def _analyse_scene_cues(self, *, estimate_only=False):
        if not self.project or self.busy:
            return
        project = self.project
        def work():
            if estimate_only:
                from .phrase_cues import estimate_phrase_ends
                document = estimate_phrase_ends(project.pcm)
            else:
                from .lyric_recognition import recognize_phrase_ends
                document = recognize_phrase_ends(
                    project.pcm, progress=lambda text: self.events.put(("status", str(text))))
            return ("scene_cues", (project, document))
        self._background("正在分析句尾时间点，音乐可继续试听…", work)

    def _import_scene_lrc(self, path=None):
        if self.busy:
            return
        if not self.project:
            self.status.set("请先导入音乐或打开歌曲工程，再为这首歌导入 LRC 歌词。")
            messagebox.showinfo("先选择歌曲", "请先导入音乐或打开歌曲工程，再导入与歌曲对应的 LRC。", parent=self)
            return
        value = path if path is not None else filedialog.askopenfilename(
            title="为当前歌曲导入 LRC 歌词", filetypes=[("LRC 歌词", "*.lrc *.LRC"), ("所有文件", "*.*")])
        if not value:
            return
        project = self.project
        def work():
            from .lrc import read_lrc_bundle
            try:
                bundle = read_lrc_bundle(Path(value), project.pcm)
            except Exception as exc:
                return ("scene_lrc_error", (project, str(exc)))
            return ("scene_lrc_bundle", (project, bundle))
        self._background("正在为当前歌曲对齐 LRC；音乐进度和动作编排保留…", work)

    def _scene_cues_updated(self):
        self._sync_scene_auto()
        self._set_dirty()
        document = self.project.scene_cues
        self.status.set(f"已更新 {len(document['cues'])} 个句尾时间点。"
                        + "；".join(document.get("notes", []))
                        + " 可在句尾时间点中试听、微调；设置随电脑工程保存。")

    def _change_theme(self, _event=None):
        if not self.project:
            return
        if self.busy:
            self._sync_theme()
            return
        try:
            position = self.theme_combo.current()
            if position not in range(len(THEMES)):
                raise ValueError("请选择有效的背景主题。")
            index = THEMES[position]["index"]
            if index == self.project.theme_id:
                return
            self.project.set_theme(index)
            # Theme overrides are visual only. Keep the output-DAC clock and
            # any library demonstration running, and cache the export package.
            self.project_packet = package_bytes(self.project.build_plan(), self.project.pcm.astype("<i2").tobytes())
            self._sync_theme()
            self._set_dirty()
            self.status.set(f"已切换为{theme_by_id(index)['name']}。音乐进度、人物和动作编排保留；背景与唱片颜色独立保存，可撤销。")
        except Exception as exc:
            self._sync_theme()
            self._error("无法切换场景", exc)

    def _change_record(self, _event=None):
        if not self.project:
            return
        if self.busy:
            self._sync_theme()
            return
        try:
            position = self.record_combo.current()
            if position not in range(len(RECORD_COLORS)):
                raise ValueError("请选择有效的唱片颜色。")
            index = RECORD_COLORS[position]["index"]
            if index == self.project.record_id:
                return
            self.project.set_record(index)
            self.project_packet = package_bytes(self.project.build_plan(), self.project.pcm.astype("<i2").tobytes())
            self._sync_theme()
            self._set_dirty()
            self.status.set(f"唱片已切换为{record_by_id(index)['name']}。背景、音乐进度和动作编排保留；保存或导出会带上此配色，可撤销。")
        except Exception as exc:
            self._sync_theme()
            self._error("无法切换唱片颜色", exc)

    def _change_effect(self, _event=None):
        if not self.project:
            return
        if self.busy:
            self._sync_theme()
            return
        try:
            position = self.effect_combo.current()
            if position not in range(self.renderer.effect_count):
                raise ValueError("请选择有效的脚边特效。")
            index = FOOT_EFFECTS[position]["index"]
            if not self.project.set_effect(index):
                return
            # Cache the new device package. The renderer override below applies
            # immediately to the current song or demo without touching clocks.
            self.project_packet = package_bytes(self.project.build_plan(), self.project.pcm.astype("<i2").tobytes())
            self._sync_theme()
            self._set_dirty()
            self.status.set(f"脚边特效：{effect_by_id(index)['name']}。音乐进度和动作状态保持；保存、导出或传入设备会带上此设置，可撤销。")
        except Exception as exc:
            self._sync_theme()
            self._error("无法切换脚边特效", exc)

    def _confirm_discard(self):
        if not self.dirty:
            return True
        answer = messagebox.askyesnocancel("保存编排", "当前编排有修改，要先保存吗？", parent=self)
        return False if answer is None else (self._save() if answer else True)

    def _open(self):
        if self.busy:
            return
        value = filedialog.askopenfilename(title="打开编排、歌曲包或歌词", filetypes=[
            ("编排 / 歌曲包 / LRC", "*.vskproj *.vsk2 *.vsk3 *.json *.lrc *.LRC"),
            ("LRC 歌词（用于当前歌曲）", "*.lrc *.LRC"), ("所有文件", "*.*")])
        if not value:
            return
        path = Path(value)
        if path.suffix.lower() == ".lrc":
            self._import_scene_lrc(path)
        elif self._confirm_discard():
            self._load(path)

    def _import_song(self):
        if self.busy or not self._confirm_discard():
            return
        from .import_dialog import ask_song_import
        selection = ask_song_import(self)
        if selection is None:
            return
        self.audio.pause()
        self._background("1/5 · 正在读取歌曲…", self._analyse,
                         selection["source"], selection["lyric_path"], selection["recognize_lyrics"],
                         selection["auto_lyrics"])

    def _analyse(self, path, lyric_path=None, recognize_lyrics=False, auto_lyrics=True):
        from .song_import import import_song
        result = import_song(path, OUTPUT / "analysed",
                             lyric_path=lyric_path, recognize_lyrics=recognize_lyrics,
                             auto_lyrics=auto_lyrics,
                             progress=lambda event: self.events.put(("progress", event)))
        return ("analysis", result)

    def _background(self, label, fn, *args):
        if self.busy:
            return
        self.busy = True
        self._task_failed = False
        self.timeline.set_enabled(False)
        self.status.set(label)
        panel = getattr(self, "progress_panel", None)
        if panel is not None:
            panel.start(label)
        for button in getattr(self, "task_buttons", ()):
            button.state(["disabled"])
        def work():
            try:
                self.events.put(fn(*args))
            except Exception as exc:
                self.events.put(("error", str(exc)))
            finally:
                self.events.put(("done", None))
        threading.Thread(target=work, daemon=True).start()

    def _poll(self):
        try:
            while True:
                kind, value = self.events.get_nowait()
                if kind == "status":
                    self.status.set(value)
                    panel = getattr(self, "progress_panel", None)
                    if panel is not None and self.busy:
                        panel.update_message(value)
                elif kind == "progress":
                    self.status.set(value["message"])
                    panel = getattr(self, "progress_panel", None)
                    if panel is not None and self.busy:
                        panel.update_stage(value)
                elif kind == "analysis":
                    requested_start = (self.project is not None and self.project.scene_auto["mode"] == "lyric_start")
                    include_character = (self.scene_auto_character.get()
                                         if hasattr(self, "scene_auto_character") else False)
                    self._adopt(EditorProject.from_result(value), preserve_appearance=True)
                    if "studio_lyrics" in value:
                        self.project.set_lyrics(value["studio_lyrics"])
                    if requested_start:
                        if "studio_scene_cues" in value:
                            self.project.set_scene_cues(value["studio_scene_cues"])
                        self.project.set_scene_auto("lyric_start", include_character)
                        self._sync_scene_auto()
                        self.status.set("已按歌词句首切换场景。" if self.project.lyric_start_samples else
                                        "歌曲已导入。请导入 LRC 歌词，以启用句首切换。")
                    elif value.get("studio_scene_cues", {}).get("cues"):
                        self.project.set_scene_cues(value["studio_scene_cues"])
                        self.project.set_scene_auto("phrase", include_character)
                        self._scene_cues_updated()
                        if value.get("studio_lrc_loaded"):
                            self._open_cue_editor()
                    elif "studio_scene_cues" in value:
                        # This is a newly adopted song, so keep empty-result
                        # provenance/notes without enabling an empty auto mode.
                        self.project.set_scene_cues(value["studio_scene_cues"])
                        self.status.set("歌曲已导入，尚无可靠句尾。可导入更多 LRC 时间戳或打开句尾时间点手动添加。")
                    if value.get("studio_scene_notice"):
                        self.status.set(value["studio_scene_notice"])
                    self._set_dirty()
                elif kind == "scene_lrc_bundle":
                    project, bundle = value
                    if self.project is project:
                        lyrics, document = bundle["lyrics"], bundle["scene_cues"]
                        if not lyrics["lines"]:
                            self._task_failed = True
                            self.status.set("LRC 没有可用歌词，原字幕和时间点已保留。请检查歌词时间戳。")
                            continue
                        project.set_lyrics(lyrics)
                        self._set_dirty()
                        if project.scene_auto["mode"] == "lyric_start":
                            if document["cues"]:
                                project.set_scene_cues(document)
                            self._sync_scene_auto()
                            self.status.set(f"已导入 {len(lyrics['lines'])} 句歌词，按每句开始时间切换场景，唱片颜色同步。")
                        elif document["cues"]:
                            project.set_scene_cues(document)
                            project.set_scene_auto("phrase", project.scene_auto["include_character"])
                            self._scene_cues_updated()
                            self._open_cue_editor()
                            self.status.set(f"已导入 {len(lyrics['lines'])} 句歌词，预览底部随音乐显示当前句。"
                                            + self.status.get())
                        else:
                            self.status.set(f"已导入 {len(lyrics['lines'])} 句歌词，预览底部随音乐显示当前句。"
                                            "LRC 没有可用句末，原场景时间点已保留；可手动补充句末。")
                elif kind == "scene_cues":
                    project, document = value
                    if self.project is project:
                        is_lrc = document.get("source", "").startswith("lrc")
                        if is_lrc and not document.get("cues"):
                            self._task_failed = True
                            self.status.set("LRC 没有可用句尾，原时间点已保留。请补充更多时间戳或句末空行标记，也可在句尾时间点中手动微调。")
                        else:
                            project.set_scene_cues(document)
                            if project.scene_auto["mode"] != "lyric_start":
                                project.set_scene_auto("phrase", project.scene_auto["include_character"])
                            self._scene_cues_updated()
                            if is_lrc and project.scene_auto["mode"] != "lyric_start":
                                self._open_cue_editor()
                elif kind == "scene_lrc_error":
                    self._task_failed = True
                    project, error = value
                    if self.project is project:
                        self._error("无法导入 LRC", f"{error}\n原字幕、时间点、歌曲与动作已保留。请检查时间戳，或在句尾时间点中手动微调。")
                elif kind == "audio_controls":
                    project, controls, recompose = value
                    if self.project is project:
                        project.set_audio_reactivity(controls)
                        if recompose:
                            project.recompose_automatic()
                        self.timeline.refresh()
                        self._edited()
                        self.mode_label.set("鼓点抬脚 · 音量控制线宽 · 节奏决定复杂度")
                        prefix = project.migration_report.get("message", "")
                        self.status.set(prefix + ("已按节奏重编自动动作，手工片段保留，可撤销。" if recompose else
                                        "已补齐鼓点和音量联动，现有动作位置与拍点保留；可点“重编自动动作”更新自动片段。"))
                elif kind == "uploaded":
                    note = ("句尾和自动切场景设置目前仅用于电脑预览。"
                            if self.project.scene_auto["mode"] != "off" else "")
                    self.status.set("已传入 Mosaico，设备可独立播放这份动作编排。" + note)
                elif kind == "error":
                    self._task_failed = True
                    self._error("操作失败", value)
                elif kind == "done":
                    self.busy = False
                    self.timeline.set_enabled(True)
                    panel = getattr(self, "progress_panel", None)
                    if panel is not None:
                        panel.finish(success=not getattr(self, "_task_failed", False))
                    for button in getattr(self, "task_buttons", ()):
                        button.state(["!disabled"])
        except queue.Empty:
            pass
        except Exception as exc:
            self._task_failed = True
            # The worker's finally block still owns the pending "done".
            # Keep its lock until that message is consumed, so an old done
            # cannot accidentally unlock a newer analysis started in between.
            self._error("读取结果失败", exc)
        self.after(100, self._poll)

    def _save(self):
        if not self.project or self.busy:
            return False
        path = self.project_path
        if not path:
            OUTPUT.mkdir(parents=True, exist_ok=True)
            value = filedialog.asksaveasfilename(title="保存可继续编辑的编排", initialdir=OUTPUT, initialfile=self.project.title + ".vskproj", defaultextension=".vskproj", filetypes=[("Mosaico 编排", "*.vskproj")])
            if not value:
                return False
            path = Path(value)
        try:
            self.project_path = self.project.save(path)
            self._set_dirty(False)
            self.status.set(f"编排已保存：{path}（请保留同名音频旁文件）")
            return True
        except Exception as exc:
            self._error("保存失败", exc)
            return False

    def _export_animation(self):
        if not self.project or self.busy:
            return
        self.audio.pause()
        self._return_project()
        default_directory = data_root() / "exports"
        self.busy = True
        self.timeline.set_enabled(False)
        for button in self.task_buttons:
            button.state(["disabled"])
        def finished(result):
            self.busy = False
            self.timeline.set_enabled(True)
            for button in self.task_buttons:
                button.state(["!disabled"])
            self.status.set(f"动画已导出：{result.path}" if result is not None else "已返回编排。")
        try:
            self._animation_export_dialog = AnimationExportDialog(
                self, self.project, skin=SKIN_LABELS.index(self.skin.get()),
                default_directory=default_directory, on_close=finished)
        except Exception as exc:
            finished(None)
            self._error("无法打开动画导出", exc)

    def _export(self):
        if not self.project or self.busy:
            return
        OUTPUT.mkdir(parents=True, exist_ok=True)
        path = filedialog.asksaveasfilename(title="导出设备可独立播放的歌曲包", initialdir=OUTPUT, initialfile=self.project.title + ".vsk2", defaultextension=".vsk2", filetypes=[("Mosaico 歌曲包", "*.vsk2")])
        if path:
            try:
                info = self.project.export(Path(path))
                compatibility = f"Mosaico 需 {info['minimum_firmware']} 或更新固件。"
                if self.project.scene_auto["mode"] != "off":
                    compatibility += "句尾和自动切场景设置目前仅保存在电脑工程中。"
                self.status.set(f"已导出：{path}。{compatibility}")
            except Exception as exc:
                self._error("导出失败", exc)

    def _upload(self):
        if not self.project or self.busy:
            return
        OUTPUT.mkdir(parents=True, exist_ok=True)
        path = OUTPUT / "studio-upload.vsk2"
        try:
            info = self.project.export(path)
        except Exception as exc:
            self._error("导出失败", exc)
            return
        def run():
            from importer.upload import upload_package
            return ("uploaded", upload_package(path, progress=lambda text: self.events.put(("status", str(text)))))
        self._background(f"正在连接 Mosaico 并传入编排…需 {info['minimum_firmware']} 或更新固件。", run)

    def _library_wheel(self, event):
        if self.library_cards.winfo_height() > self.library_canvas.winfo_height():
            delta = -1 if event.delta > 0 else 1
            self.library_canvas.yview_scroll(delta * 3, "units")
        return "break"

    def _refresh_library(self, _event=None):
        if not hasattr(self, "library_cards"):
            return
        for widget in self.library_cards.winfo_children():
            widget.destroy()
        clips = filtered_clips(self.library_level.get(), self.library_query.get())
        self.visible_clip_ids = tuple(clip["id"] for clip in clips)
        self.library_count.set(f"{len(clips)} / {len(ACTIVE_CLIPS)} 套 · 点击演示 / 拖入拍点")
        for clip in clips:
            color = COLORS[clip["id"] % len(COLORS)]
            card = tk.Frame(self.library_cards, bg="#22313e", cursor="hand2", padx=9, pady=7)
            card.pack(fill="x", pady=3)
            stripe = tk.Frame(card, bg=color, width=4)
            stripe.pack(side="left", fill="y", padx=(0, 8))
            content = tk.Frame(card, bg="#22313e")
            content.pack(side="left", fill="x", expand=True)
            label = tk.Label(content, text=clip["name"], bg="#22313e", fg="#e6edf4", anchor="w",
                              wraplength=173, justify="left", font=("Microsoft YaHei UI", 10))
            label.pack(fill="x")
            reference = clip_english_name(clip)
            caption = reference or LEVEL_NAMES.get(clip_level(clip), "花式动作")
            hint = tk.Label(content, text=f"{caption} · {clip['beats']} 拍  ⋮⋮", bg="#22313e", fg=color,
                             anchor="w", wraplength=173, justify="left", font=("Microsoft YaHei UI", 8))
            hint.pack(fill="x", pady=(2, 0))
            for widget in (card, stripe, content, label, hint):
                widget.bind("<MouseWheel>", self._library_wheel)
                widget.bind("<ButtonPress-1>", lambda e, cid=clip["id"]: self._card_press(e, cid))
                widget.bind("<B1-Motion>", self._card_drag)
                widget.bind("<ButtonRelease-1>", self._card_release)
        if not clips:
            tk.Label(self.library_cards, text="没有匹配动作\n可缩短关键词或选择全部动作", bg="#18232d", fg="#a2b3c4",
                      wraplength=190, justify="left", font=("Microsoft YaHei UI", 9)).pack(anchor="w", pady=14)
        self.library_canvas.yview_moveto(0)

    def _change_demo_speed(self, _event=None):
        if self.demo_id is not None:
            self._demo(self.demo_id)

    def _settings_wheel(self, event):
        if self.settings_fields.winfo_height() > self.settings_canvas.winfo_height():
            self.settings_canvas.yview_scroll(-3 if event.delta > 0 else 3, "units")
        return "break"

    def _card_press(self, event, clip_id):
        self.drag_state = (clip_id, event.x_root, event.y_root, False)

    def _card_drag(self, event):
        if not self.drag_state or self.busy:
            return
        clip, x, y, moved = self.drag_state
        if not moved and abs(event.x_root-x) + abs(event.y_root-y) < 8:
            return
        self.drag_state = (clip, x, y, True)
        if self.drag_ghost is None:
            self.drag_ghost = tk.Toplevel(self)
            self.drag_ghost.overrideredirect(True)
            self.drag_ghost.attributes("-topmost", True)
            tk.Label(self.drag_ghost, text=CLIPS[clip]["name"], bg=COLORS[clip % len(COLORS)], fg="#10171e", padx=12, pady=6).pack()
        self.drag_ghost.geometry(f"+{event.x_root+14}+{event.y_root+12}")
        self.timeline.preview_external_drag(clip, event.x_root, event.y_root)

    def _card_release(self, event):
        state, self.drag_state = self.drag_state, None
        if self.drag_ghost:
            self.drag_ghost.destroy()
            self.drag_ghost = None
        if not state or self.busy:
            return
        clip, _, _, moved = state
        if moved:
            self.timeline.finish_external_drop(clip, event.x_root, event.y_root)
            self.timeline.clear_external_drag()
        else:
            self._demo(clip)

    def _demo(self, clip):
        self.audio.pause()
        try:
            bpm = int(self.demo_bpm.get())
            packet = make_clip_demo(clip, bpm=bpm)
            self._set_renderer_package(packet)
            self.demo_id, self.demo_packet = clip, packet
            self.demo_start = time.perf_counter()
            self.mode_label.set(f"动作演示 · {CLIPS[clip]['name']} · {bpm} BPM · 静音")
            self.action_detail.set(clip_description(CLIPS[clip]))
            self.status.set("正在循环演示完整动作。拖到时间线即可编排；点击播放或“返回编排”回到音乐。")
        except Exception as exc:
            self._error("动作演示失败", exc)

    def _return_project(self):
        if self.demo_id is not None and self.project_packet:
            self._set_renderer_package(self.project_packet)
            self.demo_id = None
            self.mode_label.set("鼓点抬脚 · 音量控制线宽 · 节奏决定复杂度")
            self._selection(self.timeline.selected_id)
            self.status.set("已返回音乐编排；点击播放试听，或继续把完整动作拖到拍点。")

    def _toggle(self):
        if not self.project:
            return
        self._return_project()
        if self.audio.playing:
            self.audio.pause()
        else:
            if not self.loop.get() and self.audio.sample >= self.project.sample_count:
                self.audio.seek(0)
            self.audio.loop = self.loop.get()
            if not self.audio.play():
                self._error("无法播放音频", self.audio.error)

    def _stop(self):
        self._return_project()
        self.audio.stop()
        self._invalidate_preview()

    def _seek(self, sample):
        self._return_project()
        self.audio.seek(int(sample))
        self._invalidate_preview()

    def _edited(self):
        if not self.project:
            return
        try:
            self.project_packet = package_bytes(self.project.build_plan(), self.project.pcm.astype("<i2").tobytes())
            self._sync_theme()
            if self.demo_id is not None:
                self.demo_id = None
                self.mode_label.set("歌曲编排 · 音乐与动作同步预览")
            self._set_renderer_package(self.project_packet)
            self._set_dirty()
            self._selection(self.timeline.selected_id)
            self.status.set("编排已更新：吸附原拍点，保留完整动作收尾，空白区自动补入滑行。重叠会替换整段动作，可撤销。")
        except Exception as exc:
            self._error("编排未通过检查", exc)

    def _selection(self, _id):
        selected = self.timeline.get_selection()
        if not selected:
            self.selection_label.set("点击时间线上的动作进行调整")
            self.beat_text.set("")
            self.count_text.set("")
            self.count_combo.configure(values=())
            if self.demo_id is None:
                self.action_detail.set("点击动作查看演示与脚下变化；拖到拍点即可编排。")
            return
        self.action_detail.set(clip_description(CLIPS[selected['clip_id']]))
        self.selection_label.set(f"{CLIPS[selected['clip_id']]['name']}\n{stamp(selected['start_sample'])} — {stamp(selected['end_sample'])}")
        self.beat_text.set(str(selected["first_beat"] - self.project.first_beat + 1))
        counts = self.project.allowed_counts(selected["clip_id"], selected["first_beat"])
        self.count_combo.configure(values=counts)
        self.count_text.set(str(selected["beat_count"]))

    def _apply_selection(self):
        selected = self.timeline.get_selection()
        if not selected or self.busy:
            return
        try:
            beat = int(self.beat_text.get()) + self.project.first_beat - 1
            count = int(self.count_text.get())
            self.project.update_clip(selected["id"], beat, count)
            self.timeline.refresh()
            self._edited()
        except Exception as exc:
            self.timeline.refresh()
            self._selection(self.timeline.selected_id)
            self._error("无法应用动作设置", exc)

    def _delete(self):
        if not self.busy:
            self.timeline.delete_selected()

    def _undo(self):
        self._history_step(False)

    def _redo(self):
        self._history_step(True)

    def _history_step(self, redo):
        if not self.project or self.busy:
            return
        before = (self.project.title, self.project.placements)
        changed = self.project.redo() if redo else self.project.undo()
        if not changed:
            return
        if before == (self.project.title, self.project.placements):
            # Appearance-only history applies the same live override as its
            # dropdown. In particular a silent action demo keeps its phase.
            self.project_packet = package_bytes(self.project.build_plan(), self.project.pcm.astype("<i2").tobytes())
            self._sync_theme()
            self._set_dirty()
            self.status.set("已重做工程设置；音乐进度和动作状态保持。" if redo else
                            "已撤销工程设置；音乐进度和动作状态保持。")
            return
        self.timeline.refresh()
        self._edited()

    def _smooth(self):
        if not self.project or self.busy:
            return
        self.project.smooth()
        self.timeline.refresh()
        self._edited()
        report = self.project.continuity_report
        self.status.set(f"已检查 {report['joins']} 处动作衔接，补齐 {report['gap_fill_segments']} 段滑行。手工动作完整保留，轨迹连续连接，音乐拍点保持不变。")

    def _shortcut(self, event, command):
        if isinstance(self.focus_get(), (tk.Entry, ttk.Entry, ttk.Combobox, tk.Text)):
            return
        command()
        return "break"

    def _invalidate_preview(self):
        self._preview_revision += 1
        self._last_render_submit = 0.0
        self.preview_worker.invalidate()

    def _set_renderer_package(self, packet):
        self._invalidate_preview()
        self.renderer.set_package(packet)

    def _toggle_fullscreen(self, _event=None):
        if self._fullscreen is not None:
            self._exit_fullscreen()
            return "break"
        window = tk.Toplevel(self)
        self._fullscreen = window
        window.title("Mosaico · 全屏预览")
        window.configure(bg="#080d12")
        window.attributes("-fullscreen", True)
        window.columnconfigure(0, weight=1)
        window.rowconfigure(0, weight=1)
        self.fullscreen_preview = tk.Canvas(window, bg="#080d12", highlightthickness=0)
        self.fullscreen_preview.grid(row=0, column=0, sticky="nsew")
        self.fullscreen_image = self.fullscreen_preview.create_image(0, 0, anchor="center")
        self._fullscreen_photo = None
        self._fullscreen_image_center = None
        self.fullscreen_preview.bind("<Double-Button-1>", self._toggle_fullscreen)
        controls = ttk.Frame(window, padding=(20, 10))
        controls.grid(row=1, column=0, sticky="ew")
        self.fullscreen_play = ttk.Button(controls, text="▶ 播放", command=self._toggle)
        self.fullscreen_play.pack(side="left")
        ttk.Label(controls, textvariable=self.clock_label, font=("Consolas", 11)).pack(side="left", padx=14)
        ttk.Label(controls, text="空格 播放/暂停 · Esc 返回", style="Muted.TLabel").pack(side="left")
        ttk.Button(controls, text="退出全屏", command=self._exit_fullscreen).pack(side="right")
        self.fullscreen_progress = tk.DoubleVar(value=0)
        self._updating_fullscreen_progress = False
        self.fullscreen_seek = ttk.Scale(window, from_=0, to=1, variable=self.fullscreen_progress,
                                        command=self._seek_fullscreen)
        self.fullscreen_seek.grid(row=2, column=0, sticky="ew", padx=20, pady=(0, 12))
        window.protocol("WM_DELETE_WINDOW", self._exit_fullscreen)
        window.bind("<Escape>", self._exit_fullscreen)
        window.bind("<F11>", self._toggle_fullscreen)
        window.bind("<space>", lambda event: self._shortcut(event, self._toggle))
        window.focus_set()
        self._invalidate_preview()
        return "break"

    def _exit_fullscreen(self, _event=None):
        window, self._fullscreen = self._fullscreen, None
        if window is not None:
            window.destroy()
            self._fullscreen_photo = None
            self._invalidate_preview()
            self.preview.focus_set()
        return "break"

    def _seek_fullscreen(self, value):
        if self.project and not self._updating_fullscreen_progress:
            self._seek(float(value) * self.project.sample_count)

    def _present_frame(self, result):
        if result.error:
            self.status.set(f"预览错误：{result.error}")
            return
        canvas = self.fullscreen_preview if self._fullscreen is not None else self.preview
        item = self.fullscreen_image if self._fullscreen is not None else self.preview_image
        photo_attribute = "_fullscreen_photo" if self._fullscreen is not None else "_photo"
        photo = getattr(self, photo_attribute, None)
        if photo is not None and (photo.width(), photo.height()) == result.image.size:
            # Rebinding a new Tk image each frame invalidates expensive canvas
            # image state on Windows. Keep the item and update its pixels.
            photo.paste(result.image)
        else:
            photo = ImageTk.PhotoImage(result.image, master=canvas)
            canvas.itemconfigure(item, image=photo)
            setattr(self, photo_attribute, photo)
        center_attribute = "_fullscreen_image_center" if self._fullscreen is not None else "_preview_image_center"
        center = (canvas.winfo_width() / 2, canvas.winfo_height() / 2)
        if getattr(self, center_attribute, None) != center:
            canvas.coords(item, *center)
            setattr(self, center_attribute, center)
        pose = result.pose
        caption = f"{CLIPS[pose['clip']]['name']}   ·   动作进度 {pose['phase'] * 100:02.0f}%"
        if self.pose_label.get() != caption:
            self.pose_label.set(caption)
        self._last_preview_result = result

    def _frame(self):
        if self._closing:
            return
        deadline = time.perf_counter() + 1.0 / 60.0
        try:
            if self.project:
                absolute_sample = self.audio.sample
                sample = absolute_sample
                if self.audio.loop != self.loop.get():
                    if absolute_sample >= self.project.sample_count:
                        self.audio.seek(absolute_sample % self.project.sample_count)
                        absolute_sample = self.audio.sample
                        sample = absolute_sample
                    self.audio.loop = self.loop.get()
                    if hasattr(self, "scene_auto_controller"):
                        self._configure_scene_auto()
                if self.loop.get():
                    sample %= self.project.sample_count
                showing = absolute_sample if self.loop.get() else min(sample, self.project.sample_count - 1)
                playing = self.audio.playing
                if self.demo_id is not None:
                    showing = int((time.perf_counter() - self.demo_start) * 16000) % self.renderer.sample_count
                    playing = True
                skin = SKIN_LABELS.index(self.skin.get())
                theme, record = self.project.theme_id, self.project.record_id
                if hasattr(self, "scene_auto_controller"):
                    auto = self.scene_auto_controller.resolve(absolute_sample, demo=self.demo_id is not None)
                    skin, theme, record = auto.skin, auto.theme, auto.record_id
                    if self.demo_id is not None:
                        detail = "动作演示 · 自动场景暂不参与"
                    elif auto.mode == "lyric_start" and not auto.active:
                        detail = "暂无歌词句首 · 点击顶部“导入歌词（LRC）”"
                    elif auto.mode == "phrase" and not auto.active:
                        detail = "暂无可靠句尾 · 打开“句尾时间点”识别或手动添加"
                    elif auto.active:
                        next_time = (stamp(auto.next_cue_sample % self.project.sample_count)
                                     if auto.next_cue_sample is not None else "歌曲结束")
                        detail = f"{theme_by_id(theme)['name']} · 下次切换 {next_time}"
                        if auto.mode == "lyric_start":
                            detail += f" · 已到第 {auto.phrase_index} 个句首"
                        elif auto.mode == "phrase":
                            cue_label = ("展示点" if self.project.scene_cues["source"] == "anime_cast_showcase"
                                         else "句尾")
                            detail += f" · 已到第 {auto.phrase_index} 个{cue_label}"
                    else:
                        detail = "场景手动切换 · 句尾设置仅用于电脑预览"
                    if self.scene_auto_status.get() != detail:
                        self.scene_auto_status.set(detail)
                if self.skin_description.get() != SKIN_DESCRIPTIONS[skin]:
                    self.skin_description.set(SKIN_DESCRIPTIONS[skin])
                lyric = ("" if self.demo_id is not None or not self.project.show_lyrics else
                         self.project.current_lyric(absolute_sample, loop=self.loop.get()))
                canvas = self.fullscreen_preview if self._fullscreen is not None else self.preview
                size = max(1, min(canvas.winfo_width(), canvas.winfo_height()))
                context = (self._preview_revision, self.demo_id, skin, theme, record,
                           self.project.effect_id, lyric, size, playing, self._fullscreen is not None)
                now = time.perf_counter()
                if context != self._render_context or now - self._last_render_submit >= 1.0 / 30.0:
                    self.preview_worker.submit(showing, skin=skin, playing=playing, theme=theme,
                                               record_id=record, effect_id=self.project.effect_id,
                                               lyric=lyric, size=size, context=context)
                    self._render_context = context
                    self._last_render_submit = now
                result = self.preview_worker.take_latest()
                if result is not None and result.context == context:
                    self._present_frame(result)
                self.clock_label.set(f"{stamp(sample)} / {stamp(self.project.sample_count)}")
                self.timeline.set_playhead(sample)
                label = "Ⅱ 暂停" if playing and self.demo_id is None else "▶ 播放"
                if str(self.play_button.cget("text")) != label:
                    self.play_button.configure(text=label)
                if self._fullscreen is not None:
                    self.fullscreen_play.configure(text=label)
                    self._updating_fullscreen_progress = True
                    try:
                        self.fullscreen_progress.set(sample / self.project.sample_count)
                    finally:
                        self._updating_fullscreen_progress = False
        except Exception as exc:
            self.status.set(f"预览错误：{exc}")
        # The DAC clock and controls keep updating while the background
        # renderer works. A single latest-frame slot prevents a catch-up queue.
        delay_ms = max(1, round((deadline - time.perf_counter()) * 1000))
        self.after(delay_ms, self._frame)

    def _error(self, title, exc):
        self.status.set(f"{title}：{exc}")
        messagebox.showerror(title, str(exc), parent=self)

    def _close(self):
        if self.busy:
            messagebox.showinfo("任务进行中", "请等当前任务完成，或取消动画导出后再关闭。", parent=self)
            return
        if self._confirm_discard():
            self._closing = True
            self.audio.close()
            self.preview_worker.close(wait=False)
            def release_renderer():
                self.preview_worker.join()
                self.renderer.close()
            threading.Thread(target=release_renderer, name="preview-cleanup", daemon=True).start()
            self.destroy()


def main():
    parser = argparse.ArgumentParser(description="Mosaico 桌面动作工作台")
    parser.add_argument("project", nargs="?", help="编排、分析结果或 VSK2 文件")
    parser.add_argument("--skin", type=int, choices=range(len(SKIN_LABELS)), default=0,
                        help="初始预览人物：0 澄音、1 凛、2 绮星、3 霓虹 Neon")
    args = parser.parse_args()
    MotionStudio(args.project, initial_skin=args.skin).mainloop()
