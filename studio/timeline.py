"""A bounded Canvas timeline for placing neutral-to-neutral skating clips.

The editor model owns timing, overlap replacement, and undo. This widget only
previews edits while dragging, then commits one model operation on release.
All coordinates are based on the actual beat samples, including changing tempi.
"""
from __future__ import annotations

from bisect import bisect_left, bisect_right
import math
import tkinter as tk
from tkinter import ttk
from typing import Any, Callable


BG = "#18232D"
INK = "#E4EAF0"
MUTED = "#A2B3C4"
LINE = "#314150"
CORAL = "#64C7BD"
CLIP_INK = "#10232C"
CLIP_COLORS = ("#64C7BD", "#EFB572", "#9DA9FA", "#E78F9D", "#88C7EC",
               "#BBABEA", "#E8A7D1", "#7CC89E", "#F3CF70", "#8CB9E7",
               "#F09F7C", "#A5D69B", "#96CEC5", "#D3ACEC", "#F1C491", "#AAC5EB",
               "#72D6B8", "#E6B4EC", "#FAB080")


class Timeline(ttk.Frame):
    """Song waveform, real beat grid, and one editable choreography lane."""

    MARGIN = 26
    HEIGHT = 235
    RULER_BOTTOM = 29
    WAVE_TOP, WAVE_BOTTOM = 38, 85
    BEAT_TOP, BEAT_BOTTOM = 98, 119
    LANE_TOP, LANE_BOTTOM = 132, 190
    MIN_PPS, MAX_PPS = 2.0, 560.0

    def __init__(self, parent: tk.Misc, on_seek: Callable[[int], None] | None = None,
                 on_change: Callable[[], None] | None = None,
                 on_select: Callable[[str | None], None] | None = None, **kwargs: Any):
        super().__init__(parent, **kwargs)
        self.on_seek = on_seek or (lambda sample: None)
        self.on_change = on_change or (lambda: None)
        self.on_select = on_select or (lambda item_id: None)
        self.canvas = tk.Canvas(self, height=self.HEIGHT, background=BG,
                                highlightthickness=0, takefocus=True, cursor="arrow")
        self.scrollbar = ttk.Scrollbar(self, orient="horizontal", command=self._scroll)
        self.canvas.configure(xscrollcommand=self.scrollbar.set)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.scrollbar.grid(row=1, column=0, sticky="ew")
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        self.project: Any = None
        self.enabled = True
        self.selected_id: str | None = None
        self.pps = 70.0
        self._sample_rate = 16000
        self._sample_count = 1
        self._beats: list[dict] = []
        self._samples: list[int] = []
        self._downbeats: list[int] = []
        self._placements: list[dict] = []
        self._automatic_gaps: list[tuple[int, int]] = []
        self._clips: dict[int, dict] = {}
        self._waveform: list = []
        self._drums: list[int] = []
        self._wave_scale = 1.0
        self._playhead = 0
        self._drag: dict | None = None
        self._ghost: dict | None = None
        self._redraw_id: str | None = None
        self._fit_pending = False
        self._auto_scroll_id: str | None = None
        self._last_drag_xy: tuple[float, float] | None = None
        self.canvas.bind("<Configure>", self._configure)
        self.canvas.bind("<ButtonPress-1>", self._press)
        self.canvas.bind("<B1-Motion>", self._motion)
        self.canvas.bind("<ButtonRelease-1>", self._release)
        self.canvas.bind("<Motion>", self._hover)
        self.canvas.bind("<MouseWheel>", self._wheel)
        self.canvas.bind("<Button-4>", lambda event: self._wheel_linux(event, -1))
        self.canvas.bind("<Button-5>", lambda event: self._wheel_linux(event, 1))
        self.canvas.bind("<Escape>", self._cancel)
        self.canvas.bind("<Delete>", self._key_delete)
        self.canvas.bind("<BackSpace>", self._key_delete)
        self.bind("<Destroy>", self._destroy, add="+")
        self._schedule_redraw()

    def set_project(self, project: Any) -> None:
        changed = project is not self.project
        self.project = project
        if changed:
            self.selected_id = None
            self._playhead = 0
            self._drag = self._ghost = None
            self._fit_pending = True
            self.canvas.xview_moveto(0)
        self.refresh()

    def set_enabled(self, enabled: bool) -> None:
        """Suspend gestures while a song analysis or upload owns the project."""
        self.enabled = bool(enabled)
        if not self.enabled:
            self._cancel()
            self.canvas.configure(cursor="arrow")

    def _key_delete(self, event: tk.Event) -> str:
        self.delete_selected()
        return "break"

    def refresh(self) -> None:
        """Read the model after undo/redo, import, or an outside edit."""
        if self.project is None:
            self._beats = self._samples = self._downbeats = self._placements = []
            self._automatic_gaps = []
            self._waveform = []
            self._drums = []
        else:
            plan = self.project.plan
            self._sample_rate = int(plan.get("sample_rate", 16000))
            self._sample_count = max(1, int(plan["sample_count"]))
            self._beats = list(plan["beats"])
            self._drums = [d["sample"] for d in plan.get("audio_reactivity", {}).get("drums", [])]
            self._samples = [int(beat["sample"]) for beat in self._beats]
            self._downbeats = [i for i, beat in enumerate(self._beats)
                               if int(beat.get("flags", 0)) & 1]
            self._placements = [dict(item) for item in self.project.placements]
            # Show the same full-song cruise fill that the model compiles,
            # merging adjacent four-beat fills into a quiet background region.
            # These are deliberately not placements and cannot be selected.
            self._automatic_gaps = []
            cursor = 0
            for item in self._placements:
                first, last = self._bounds(item)
                start, end = self._samples[first], self._samples[last]
                if cursor < start:
                    self._automatic_gaps.append((cursor, start))
                cursor = max(cursor, end)
            if cursor < self._sample_count:
                self._automatic_gaps.append((cursor, self._sample_count))
            clips = getattr(self.project, "clips", plan.get("clips", ()))
            self._clips = {int(clip["id"]): clip for clip in clips}
            waveform = getattr(self.project, "waveform", ())
            self._waveform = waveform.tolist() if hasattr(waveform, "tolist") else list(waveform)
            peak = max((max(abs(float(p[0])), abs(float(p[1])))
                        if hasattr(p, "__len__") else abs(float(p))
                        for p in self._waveform), default=1.0)
            self._wave_scale = 32768.0 if peak > 1.01 else 1.0
            if self.selected_id and not any(p["id"] == self.selected_id for p in self._placements):
                self.selected_id = None
                self.on_select(None)
        self._apply_fit()
        self._update_region()
        self._schedule_redraw()

    def set_playhead(self, sample: int) -> None:
        self._playhead = max(0, min(int(sample), self._sample_count))
        # Playback updates never reconstruct waveform or action rectangles.
        self._draw_playhead()

    def zoom(self, factor: float) -> None:
        if not math.isfinite(float(factor)) or factor <= 0:
            return
        width = max(1, self.canvas.winfo_width())
        left = self.canvas.canvasx(0)
        play_x = self._sample_x(self._playhead)
        anchor_x = play_x - left if left <= play_x <= left + width else width / 2
        anchor_sample = self._x_sample(left + anchor_x)
        self.pps = max(self.MIN_PPS, min(self.MAX_PPS, self.pps * factor))
        self._fit_pending = False
        self._update_region()
        self.canvas.xview_moveto(max(0., self._sample_x(anchor_sample) - anchor_x) / self._total_width())
        self._schedule_redraw()

    def fit(self) -> None:
        self._fit_pending = True
        self._apply_fit()
        self._update_region()
        self.canvas.xview_moveto(0)
        self._schedule_redraw()

    def get_selection(self) -> dict | None:
        item = next((item for item in self._placements if item["id"] == self.selected_id), None)
        if item is None:
            return None
        result = dict(item)
        start, end = self._bounds(item)
        result.update(name=self._clips.get(int(item["clip_id"]), {}).get("name", "滑行动作"),
                      start_sample=self._samples[start], end_sample=self._samples[end])
        return result

    def delete_selected(self) -> bool:
        if not self.enabled or self.project is None or not self.selected_id:
            return False
        try:
            self.project.remove_clip(self.selected_id)
        except (ValueError, KeyError):
            return False
        self.selected_id = None
        self.on_select(None)
        self.refresh()
        self.on_change()
        return True

    def preview_external_drag(self, clip_id: int, screen_x: float, screen_y: float) -> bool:
        """Show a beat-snapped drop ghost; call from library-card mouse motion."""
        if not self.enabled or self.project is None or not self._inside(screen_x, screen_y):
            self.clear_external_drag()
            return False
        local_x = screen_x - self.canvas.winfo_rootx()
        local_y = screen_y - self.canvas.winfo_rooty()
        start = self._nearest_beat(self._x_sample(self.canvas.canvasx(local_x)))
        self._ghost = self._make_ghost(int(clip_id), start)
        self._last_drag_xy = (local_x, local_y)
        self._set_auto_scroll(local_x)
        self._schedule_redraw()
        return True

    def clear_external_drag(self) -> None:
        if self._drag is None:
            self._ghost = None
            self._stop_auto_scroll()
            self._schedule_redraw()

    def finish_external_drop(self, clip_id: int, screen_x: float, screen_y: float) -> bool:
        if not self.preview_external_drag(clip_id, screen_x, screen_y):
            return False
        ghost = self._ghost
        self._ghost = None
        self._stop_auto_scroll()
        if not ghost or not ghost["valid"]:
            self._schedule_redraw()
            return False
        try:
            result = self.project.place_clip(ghost["clip_id"], ghost["start"], ghost["count"])
        except (ValueError, KeyError, IndexError):
            self._schedule_redraw()
            return False
        self.selected_id = result["id"]
        self.refresh()
        self.on_select(self.selected_id)
        self.on_change()
        return True

    def _configure(self, event: tk.Event) -> None:
        self._apply_fit()
        self._update_region()
        self._schedule_redraw()

    def _apply_fit(self) -> None:
        width = self.canvas.winfo_width()
        if self._fit_pending and self.project is not None and width > 100:
            duration = self._sample_count / self._sample_rate
            self.pps = max(6.0, min(90.0, (width - self.MARGIN * 2) / max(duration, 1)))
            self._fit_pending = False

    def _update_region(self) -> None:
        self.canvas.configure(scrollregion=(0, 0, self._total_width(), self.HEIGHT))

    def _total_width(self) -> float:
        return max(self.canvas.winfo_width(), self.MARGIN * 2 + self._sample_count / self._sample_rate * self.pps)

    def _sample_x(self, sample: float) -> float:
        return self.MARGIN + sample * self.pps / self._sample_rate

    def _x_sample(self, x: float) -> int:
        return max(0, min(self._sample_count, round((x - self.MARGIN) / self.pps * self._sample_rate)))

    def _nearest_beat(self, sample: int) -> int:
        if not self._samples:
            return 0
        index = bisect_left(self._samples, sample)
        if index > 0 and (index >= len(self._samples) or sample - self._samples[index - 1] <= self._samples[index] - sample):
            index -= 1
        first = int(getattr(self.project, "first_beat", 0))
        last = int(getattr(self.project, "last_beat", len(self._samples) - 1))
        return max(first, min(last, index))

    @staticmethod
    def _bounds(item: dict) -> tuple[int, int]:
        first = int(item.get("first_beat", item.get("start_beat", 0)))
        return first, first + int(item["beat_count"])

    def _make_ghost(self, clip: int, start: int, *, count: int | None = None,
                    skip_id: str | None = None) -> dict:
        try:
            allowed = list(self.project.allowed_counts(clip, start))
            chosen = int(self.project.default_count(clip, start)) if count is None else int(count)
            valid = chosen in allowed and start + chosen < len(self._samples)
        except (ValueError, KeyError, IndexError):
            chosen, valid = int(count or self._clips.get(clip, {}).get("beats", 4)), False
        count = max(1, chosen)
        collisions = [p["id"] for p in self._placements
                      if p["id"] != skip_id and self._bounds(p)[0] < start + count and self._bounds(p)[1] > start]
        return {"clip_id": clip, "start": start, "count": count,
                "valid": valid, "collisions": collisions, "skip_id": skip_id}

    def _hit(self, x: float, y: float) -> dict | None:
        if not self.LANE_TOP - 5 <= y <= self.LANE_BOTTOM + 5:
            return None
        sample = self._x_sample(x)
        for item in self._placements:
            start, end = self._bounds(item)
            if self._samples[start] <= sample <= self._samples[end]:
                return item
        return None

    def _press(self, event: tk.Event) -> str:
        self.canvas.focus_set()
        if not self.enabled or self.project is None:
            return "break"
        x = self.canvas.canvasx(event.x)
        item = self._hit(x, event.y)
        if item is not None:
            self.selected_id = item["id"]
            start, end = self._bounds(item)
            mode = "resize" if abs(x - self._sample_x(self._samples[end])) <= 9 else "move"
            self._drag = {"mode": mode, "item": dict(item), "x": event.x, "y": event.y,
                          "offset": self._x_sample(x) - self._samples[start], "moved": False}
            self.on_select(self.selected_id)
            self._schedule_redraw()
        else:
            self.selected_id = None
            self.on_select(None)
            self._drag = {"mode": "scrub"}
            self.on_seek(self._x_sample(x))
            self._schedule_redraw()
        return "break"

    def _motion(self, event: tk.Event) -> str:
        self._last_drag_xy = (event.x, event.y)
        self._update_drag(event.x, event.y)
        return "break"

    def _update_drag(self, x: float, y: float) -> None:
        if not self.enabled or not self._drag:
            return
        world_x = self.canvas.canvasx(x)
        if self._drag["mode"] == "scrub":
            self.on_seek(self._x_sample(world_x))
            self._set_auto_scroll(x)
            return
        if not self._drag["moved"] and abs(x - self._drag["x"]) + abs(y - self._drag["y"]) < 4:
            return
        self._drag["moved"] = True
        item = self._drag["item"]
        first, end = self._bounds(item)
        if self._drag["mode"] == "move":
            start = self._nearest_beat(max(0, self._x_sample(world_x) - self._drag["offset"]))
            self._ghost = self._make_ghost(int(item["clip_id"]), start,
                                            count=int(item["beat_count"]), skip_id=item["id"])
        else:
            nearest_end = self._nearest_beat(self._x_sample(world_x))
            allowed = list(self.project.allowed_counts(int(item["clip_id"]), first))
            if allowed:
                # Valid beat counts avoid truncating a lifted leg halfway through a clip.
                count = min(allowed, key=lambda value: abs(self._samples[first + value] - self._samples[nearest_end]))
            else:
                count = int(item["beat_count"])
            self._ghost = self._make_ghost(int(item["clip_id"]), first, count=count, skip_id=item["id"])
        self._set_auto_scroll(x)
        self._schedule_redraw()

    def _release(self, event: tk.Event) -> str:
        drag, ghost = self._drag, self._ghost
        self._drag = self._ghost = None
        self._stop_auto_scroll()
        if not self.enabled or not drag:
            return "break"
        if drag["mode"] != "scrub" and not drag.get("moved"):
            first, _ = self._bounds(drag["item"])
            self.on_seek(self._samples[first])
        if ghost and ghost["valid"]:
            item = drag["item"]
            first, _ = self._bounds(item)
            changed = (ghost["start"] != first if drag["mode"] == "move"
                       else ghost["count"] != int(item["beat_count"]))
            if changed:
                try:
                    if drag["mode"] == "move":
                        result = self.project.move_clip(item["id"], ghost["start"])
                    else:
                        result = self.project.resize_clip(item["id"], ghost["count"])
                except (ValueError, KeyError, IndexError):
                    result = None
                if result:
                    self.selected_id = result["id"]
                    self.refresh()
                    self.on_select(self.selected_id)
                    self.on_change()
        self._schedule_redraw()
        return "break"

    def _hover(self, event: tk.Event) -> None:
        if self._drag or not self.enabled:
            return
        x = self.canvas.canvasx(event.x)
        item = self._hit(x, event.y)
        if item is None:
            cursor = "crosshair" if self.project is not None else "arrow"
        else:
            _, end = self._bounds(item)
            cursor = "sb_h_double_arrow" if abs(x - self._sample_x(self._samples[end])) <= 9 else "fleur"
        self.canvas.configure(cursor=cursor)

    def _inside(self, screen_x: float, screen_y: float) -> bool:
        x, y = screen_x - self.canvas.winfo_rootx(), screen_y - self.canvas.winfo_rooty()
        return 0 <= x < self.canvas.winfo_width() and 0 <= y < self.canvas.winfo_height()

    def _scroll(self, *args: str) -> None:
        self.canvas.xview(*args)
        self._schedule_redraw()

    def _wheel(self, event: tk.Event) -> str:
        if event.state & 0x0004:
            self.zoom(1.2 if event.delta > 0 else 1 / 1.2)
        else:
            self.canvas.xview_scroll(-int(event.delta / 120) * 3, "units")
            self._schedule_redraw()
        return "break"

    def _wheel_linux(self, event: tk.Event, direction: int) -> str:
        if event.state & 0x0004:
            self.zoom(1.2 if direction < 0 else 1 / 1.2)
        else:
            self.canvas.xview_scroll(direction * 3, "units")
            self._schedule_redraw()
        return "break"

    def _set_auto_scroll(self, local_x: float) -> None:
        if local_x < 28 or local_x > self.canvas.winfo_width() - 28:
            if self._auto_scroll_id is None:
                self._auto_scroll_id = self.after(40, self._auto_scroll)
        else:
            self._stop_auto_scroll()

    def _auto_scroll(self) -> None:
        self._auto_scroll_id = None
        if self._last_drag_xy is None or not (self._drag or self._ghost):
            return
        x, y = self._last_drag_xy
        direction = -1 if x < 28 else 1 if x > self.canvas.winfo_width() - 28 else 0
        if not direction:
            return
        self.canvas.xview_scroll(direction, "units")
        if self._drag:
            self._update_drag(x, y)
        elif self._ghost:
            self._ghost = self._make_ghost(self._ghost["clip_id"], self._nearest_beat(self._x_sample(self.canvas.canvasx(x))))
        self._schedule_redraw()
        if self._auto_scroll_id is None:
            self._auto_scroll_id = self.after(40, self._auto_scroll)

    def _stop_auto_scroll(self) -> None:
        if self._auto_scroll_id is not None:
            self.after_cancel(self._auto_scroll_id)
            self._auto_scroll_id = None
        self._last_drag_xy = None

    def _cancel(self, event: tk.Event | None = None) -> str:
        self._drag = self._ghost = None
        self._stop_auto_scroll()
        self._schedule_redraw()
        return "break"

    def _destroy(self, event: tk.Event) -> None:
        if event.widget is self:
            self._stop_auto_scroll()
            if self._redraw_id is not None:
                self.after_cancel(self._redraw_id)
                self._redraw_id = None

    def _schedule_redraw(self) -> None:
        if self._redraw_id is None:
            self._redraw_id = self.after_idle(self._redraw)

    @staticmethod
    def _clock_text(seconds: float) -> str:
        if seconds < 60:
            return f"{seconds:g}s"
        minutes, remainder = divmod(int(seconds), 60)
        return f"{minutes}:{remainder:02d}"

    def _redraw(self) -> None:
        self._redraw_id = None
        c = self.canvas
        c.delete("scene")
        left, right = c.canvasx(0), c.canvasx(max(1, c.winfo_width()))
        c.create_rectangle(left, self.WAVE_TOP, right, self.WAVE_BOTTOM,
                           fill="#141E27", outline="", tags="scene")
        c.create_rectangle(left, self.LANE_TOP - 3, right, self.LANE_BOTTOM + 3,
                           fill="#111B24", outline="", tags="scene")
        c.create_line(left, self.RULER_BOTTOM, right, self.RULER_BOTTOM, fill=LINE, tags="scene")
        if self.project is None:
            c.create_text((left + right) / 2, 112, text="导入一首音乐，开始编排滑行动作",
                          fill=MUTED, font=("Microsoft YaHei UI", 11), tags="scene")
            return
        sec_left = max(0., (left - self.MARGIN) / self.pps)
        sec_right = min(self._sample_count / self._sample_rate, (right - self.MARGIN) / self.pps)
        tick = next((v for v in (.1, .2, .5, 1, 2, 5, 10, 15, 30, 60, 120, 300)
                     if v * self.pps >= 65), 600)
        seconds = math.ceil(sec_left / tick) * tick
        while seconds <= sec_right:
            x = self._sample_x(seconds * self._sample_rate)
            c.create_line(x, 22, x, self.RULER_BOTTOM, fill=MUTED, tags="scene")
            c.create_text(x + 2, 11, text=self._clock_text(seconds), anchor="w", fill=MUTED,
                          font=("Segoe UI", 9), tags="scene")
            seconds += tick
        self._draw_waveform(left, right)
        last_drum_x = -100.
        for index in range(bisect_left(self._drums, self._x_sample(left)), bisect_right(self._drums, self._x_sample(right))):
            x = self._sample_x(self._drums[index])
            if x - last_drum_x < 4:
                continue
            self.canvas.create_polygon(x-3, self.WAVE_BOTTOM-1, x+3, self.WAVE_BOTTOM-1,
                                       x, self.WAVE_BOTTOM-7, fill="#F1C491", outline="", tags=("scene", "drum"))
            last_drum_x = x
        self._draw_automatic_gaps(left, right)
        lo = max(0, bisect_left(self._samples, self._x_sample(left)) - 1)
        hi = min(len(self._samples), bisect_right(self._samples, self._x_sample(right)) + 1)
        last_grid_x, last_label_x = -100., -100.
        for index in range(lo, hi):
            beat = self._beats[index]
            if int(beat.get("flags", 0)) & 2:
                continue
            x = self._sample_x(self._samples[index])
            strong = bool(int(beat.get("flags", 0)) & 1)
            if x - last_grid_x < (3 if strong else 5):
                continue
            last_grid_x = x
            c.create_line(x, self.BEAT_TOP, x, self.LANE_BOTTOM + 4,
                          fill="#536B7B" if strong else LINE, width=1.4 if strong else 1,
                          dash=() if strong else (2, 4), tags="scene")
            if strong and x - last_label_x >= 55:
                bar = bisect_left(self._downbeats, index) + 1
                c.create_text(x + 3, self.BEAT_TOP + 3, text=f"第 {bar} 小节", anchor="nw", fill=INK,
                              font=("Microsoft YaHei UI", 8), tags="scene")
                last_label_x = x
            elif x - last_label_x >= 38:
                bar_index = bisect_right(self._downbeats, index) - 1
                since = (index - self._downbeats[bar_index] + 1 if bar_index >= 0
                         else index - int(getattr(self.project, "first_beat", 0)) + 1)
                c.create_text(x + 3, self.BEAT_TOP + 3, text=str(since), anchor="nw", fill=MUTED,
                              font=("Segoe UI", 8), tags="scene")
                last_label_x = x
        collisions = set(self._ghost["collisions"]) if self._ghost else set()
        for item in self._placements:
            start, end = self._bounds(item)
            x0, x1 = self._sample_x(self._samples[start]), self._sample_x(self._samples[end])
            if x1 < left - 8 or x0 > right + 8:
                continue
            selected = item["id"] == self.selected_id
            muted = self._ghost and (item["id"] in collisions or item["id"] == self._ghost.get("skip_id"))
            self._draw_clip(item["clip_id"], x0, x1, item["beat_count"], selected=selected,
                            muted=bool(muted), clipped_left=left, clipped_right=right)
        if self._ghost:
            ghost = self._ghost
            end = min(len(self._samples) - 1, ghost["start"] + ghost["count"])
            x0, x1 = self._sample_x(self._samples[ghost["start"]]), self._sample_x(self._samples[end])
            self._draw_clip(ghost["clip_id"], x0, x1, ghost["count"], selected=True,
                            ghost=True, invalid=not ghost["valid"], clipped_left=left, clipped_right=right)
            if not ghost["valid"]:
                hint, color = "剩余拍数不足，请把动作向前移动", "#E78F9D"
            elif ghost["collisions"]:
                hint, color = f"松开后替换 {len(ghost['collisions'])} 段重叠动作 · 吸附到真实拍点", CORAL
            else:
                hint, color = "松开即可放置 · 吸附到真实拍点", INK
        else:
            hint, color = "橙色三角为检测鼓点 · 动作拖入拍点 · 右边缘调整拍数 · Ctrl + 滚轮缩放", MUTED
        c.create_text(left + 12, 215, text=hint, anchor="w", fill=color,
                      font=("Microsoft YaHei UI", 9), tags="scene")
        self._draw_playhead()

    def _draw_automatic_gaps(self, left: float, right: float) -> None:
        for start, end in self._automatic_gaps:
            x0, x1 = self._sample_x(start), self._sample_x(end)
            if x1 < left or x0 > right:
                continue
            x0, x1 = max(x0 + 2, left - 1), min(x1 - 2, right + 1)
            if x1 <= x0:
                continue
            self.canvas.create_rectangle(x0, self.LANE_TOP + 7, x1, self.LANE_BOTTOM - 7,
                                         fill="#1F2C35", outline="#4A5B66", dash=(4, 4),
                                         tags=("scene", "auto-gap"))
            if x1 - x0 >= 78:
                self.canvas.create_text(x0 + 10, (self.LANE_TOP + self.LANE_BOTTOM) / 2,
                                        text="自动滑行", anchor="w", fill="#8396A4",
                                        font=("Microsoft YaHei UI", 9),
                                        tags=("scene", "auto-gap"))

    def _draw_waveform(self, left: float, right: float) -> None:
        c = self.canvas
        center = (self.WAVE_TOP + self.WAVE_BOTTOM) / 2
        amplitude = (self.WAVE_BOTTOM - self.WAVE_TOP) / 2 - 4
        c.create_line(left, center, right, center, fill="#293844", tags="scene")
        n = len(self._waveform)
        if n == 0:
            return
        total = self._sample_count
        lo = max(0, math.floor(self._x_sample(left) / total * n) - 1)
        hi = min(n, math.ceil(self._x_sample(right) / total * n) + 1)
        # At most one min/max line per 2 screen pixels; never create every sample.
        stride = max(1, math.ceil((hi - lo) / max(1, (right - left) / 2)))
        for index in range(lo, hi, stride):
            bucket = self._waveform[index:min(hi, index + stride)]
            low, high = 0., 0.
            for pair in bucket:
                if hasattr(pair, "__len__"):
                    low, high = min(low, float(pair[0])), max(high, float(pair[1]))
                else:
                    value = abs(float(pair))
                    low, high = min(low, -value), max(high, value)
            x = self._sample_x((index + stride / 2) / n * total)
            c.create_line(x, center - max(-1., min(1., high / self._wave_scale)) * amplitude,
                          x, center - max(-1., min(1., low / self._wave_scale)) * amplitude,
                          fill="#598A83", width=max(1., min(2., self._sample_x(total / n * stride) - self.MARGIN)),
                          tags="scene")

    def _draw_clip(self, clip: int, x0: float, x1: float, count: int, *, selected: bool = False,
                   muted: bool = False, ghost: bool = False, invalid: bool = False,
                   clipped_left: float = 0, clipped_right: float = 1e9) -> None:
        c = self.canvas
        color = "#34424D" if muted else CLIP_COLORS[int(clip) % len(CLIP_COLORS)]
        if invalid:
            color = "#A56A70"
        outline = "#E78F9D" if invalid else INK if selected else color
        x0, x1 = max(x0 + 1, clipped_left - 7), min(x1 - 1, clipped_right + 7)
        if x1 < x0:
            x1 = x0 + 1
        y0, y1 = self.LANE_TOP, self.LANE_BOTTOM
        radius = max(0., min(8., (x1 - x0) / 3))
        points = (x0+radius, y0, x1-radius, y0, x1, y0, x1, y0+radius,
                  x1, y1-radius, x1, y1, x1-radius, y1, x0+radius, y1,
                  x0, y1, x0, y1-radius, x0, y0+radius, x0, y0)
        c.create_polygon(points, smooth=True, splinesteps=12, fill=color, outline=outline,
                         width=2 if selected else 1, dash=(5, 3) if ghost else (), tags="scene")
        width = x1 - x0
        if width > 46:
            name = self._clips.get(int(clip), {}).get("name", f"动作 {clip}")
            chars = max(2, int((width - 18) / 11))
            name = name if len(name) <= chars else name[:chars-1] + "…"
            c.create_text(x0 + 9, y0 + 17, text=name, anchor="w", fill=MUTED if muted else CLIP_INK,
                          font=("Microsoft YaHei UI", 10, "bold"), tags="scene")
        if width > 38:
            c.create_text(x0 + 9, y0 + 40, text=f"{count} 拍", anchor="w",
                          fill=MUTED if muted else "#27434F", font=("Microsoft YaHei UI", 8), tags="scene")
        if selected and width > 18:
            c.create_line(x1 - 6, y0 + 20, x1 - 6, y1 - 20, fill="#FFFFFF", width=2, tags="scene")

    def _draw_playhead(self) -> None:
        c = self.canvas
        c.delete("playhead")
        if self.project is None:
            return
        x = self._sample_x(self._playhead)
        left, right = c.canvasx(0), c.canvasx(c.winfo_width())
        if left - 8 <= x <= right + 8:
            c.create_line(x, 27, x, self.LANE_BOTTOM + 8, fill=CORAL, width=2, tags="playhead")
            c.create_polygon(x - 5, 23, x + 5, 23, x, 29, fill=CORAL, outline="", tags="playhead")
