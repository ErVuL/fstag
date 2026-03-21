"""Tkinter-based file browser UI with tag support and virtual scrolling."""

from __future__ import annotations

import os
import platform
import subprocess
import tkinter as tk
import tkinter.font
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from tkinter import messagebox, simpledialog, ttk

from .store import TagStore

# ── visual constants ─────────────────────────────────────────────

PRESET_COLORS = [
    "#ef4444", "#f97316", "#eab308", "#22c55e",
    "#06b6d4", "#3b82f6", "#8b5cf6", "#ec4899",
    "#78716c", "#1e293b", "#14b8a6", "#a3e635",
]

ROW_H = 28
SELECT_BG = "#cde4f7"
HOVER_BG = "#e8f0fe"
ROW_BG_EVEN = "#ffffff"
ROW_BG_ODD = "#f7f7f7"
DIR_FG = "#1d4ed8"
TEXT_FG = "#111111"
TAG_H = 18
TAG_PAD = 4
TAG_GAP = 5
TAG_FONT = ("TkDefaultFont", 8, "bold")
NAME_FONT = ("TkDefaultFont", 10)
NAME_FONT_BOLD = ("TkDefaultFont", 10, "bold")
HEADER_BG = "#e8e8e8"
HEADER_H = 26
SEARCH_DEBOUNCE_MS = 250


def _contrast_fg(hex_color: str) -> str:
    """Return black or white foreground for readability on the given background."""
    h = hex_color.lstrip("#")
    if len(h) != 6:
        return "#000000"
    r, g, b = int(h[:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return "#000000" if 0.299 * r + 0.587 * g + 0.114 * b > 140 else "#ffffff"


def _open_file(path: Path) -> None:
    """Open a file with the OS default handler."""
    system = platform.system()
    try:
        if system == "Darwin":
            subprocess.Popen(["open", str(path)])
        elif system == "Windows":
            os.startfile(str(path))  # type: ignore[attr-defined]
        else:
            subprocess.Popen(["xdg-open", str(path)])
    except OSError:
        pass


@dataclass
class _Row:
    """One row in the file list."""
    id: str
    is_dir: bool
    display: str
    tags: list


# ── colour picker dialog ────────────────────────────────────────

class ColorPickerDialog(tk.Toplevel):
    def __init__(self, parent, title="Pick a colour", current=None):
        super().__init__(parent)
        self.withdraw()
        self.title(title)
        self.resizable(False, False)
        self.transient(parent)
        self.result = None

        frame = ttk.Frame(self, padding=10)
        frame.pack()

        grid = ttk.Frame(frame)
        grid.pack()

        for i, color in enumerate(PRESET_COLORS):
            row, col = divmod(i, 4)
            is_current = current and color.lower() == current.lower()
            btn = tk.Button(
                grid, bg=color, activebackground=color,
                width=4, height=2, bd=3,
                relief=tk.SUNKEN if is_current else tk.RAISED,
                cursor="hand2",
                command=lambda c=color: self._pick(c),
            )
            btn.grid(row=row, column=col, padx=3, pady=3)

        ttk.Button(frame, text="Cancel", command=self.destroy).pack(pady=(10, 0))

        self.update_idletasks()
        px = parent.winfo_rootx() + (parent.winfo_width() - self.winfo_width()) // 2
        py = parent.winfo_rooty() + (parent.winfo_height() - self.winfo_height()) // 2
        self.geometry(f"+{px}+{py}")
        self.deiconify()
        self.grab_set()
        self.wait_window()

    def _pick(self, color: str):
        self.result = color
        self.destroy()


# ── canvas-based file list with virtual scrolling ────────────────

class FileListCanvas:
    """Scrollable file list drawn on a canvas.

    Only rows visible in the viewport are rendered.  On scroll, off-screen
    items are removed and newly visible ones are created, keeping work
    proportional to the viewport height rather than total row count.
    """

    def __init__(self, parent, *, on_select, on_double_click, on_right_click):
        self._on_select_cb = on_select
        self._on_double_click_cb = on_double_click
        self._on_right_click_cb = on_right_click

        self._frame = ttk.Frame(parent)

        self._canvas = tk.Canvas(self._frame, highlightthickness=0, bg=ROW_BG_EVEN)
        self._vsb = ttk.Scrollbar(self._frame, orient=tk.VERTICAL, command=self._canvas.yview)
        self._canvas.configure(yscrollcommand=self._on_yscroll)
        self._canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self._vsb.pack(side=tk.RIGHT, fill=tk.Y)

        self._canvas.bind("<Button-1>", self._on_click)
        self._canvas.bind("<Double-1>", self._on_dbl_click)
        self._canvas.bind("<Button-3>", self._on_rclick)
        self._canvas.bind("<Button-2>", self._on_rclick)
        self._canvas.bind("<Motion>", self._on_motion)
        self._canvas.bind("<Enter>", self._on_enter)
        self._canvas.bind("<Leave>", self._on_leave)
        self._canvas.bind("<Configure>", self._on_resize)

        # Linux: bind mousewheel directly to canvas (scoped, no global capture)
        if platform.system() == "Linux":
            self._canvas.bind("<Button-4>",
                              lambda e: self._canvas.yview_scroll(-3, "units"))
            self._canvas.bind("<Button-5>",
                              lambda e: self._canvas.yview_scroll(3, "units"))

        # Data
        self._rows: list[_Row] = []
        self._row_id_to_idx: dict[str, int] = {}
        self._all_tags: dict = {}
        self._selection: set[str] = set()
        self._hover_idx: int = -1
        self._canvas_width: int = 600

        # Virtual scrolling state
        self._row_bg_ids: dict[int, int] = {}  # visible row idx -> bg rect canvas id
        self._scroll_after_id: int | None = None
        self._resize_after_id: int | None = None

        # Font measurement
        self._tag_font = tkinter.font.Font(font=TAG_FONT)
        self._name_font = tkinter.font.Font(font=NAME_FONT)

    @property
    def widget(self):
        return self._frame

    # ── scroll / resize ──────────────────────────────────────────

    def _on_yscroll(self, first, last):
        """Intercept scroll-position updates to trigger visible-row redraw."""
        self._vsb.set(first, last)
        if self._scroll_after_id is not None:
            self._canvas.after_cancel(self._scroll_after_id)
        self._scroll_after_id = self._canvas.after(8, self._draw_visible)

    def _on_resize(self, event):
        new_w = event.width
        if new_w == self._canvas_width:
            return
        self._canvas_width = new_w
        if self._resize_after_id is not None:
            self._canvas.after_cancel(self._resize_after_id)
        self._resize_after_id = self._canvas.after(60, self._full_redraw)

    def _on_enter(self, event):
        system = platform.system()
        if system == "Darwin":
            self._canvas.bind_all(
                "<MouseWheel>",
                lambda e: self._canvas.yview_scroll(-e.delta, "units"))
        elif system != "Linux":
            self._canvas.bind_all(
                "<MouseWheel>",
                lambda e: self._canvas.yview_scroll(-(e.delta // 120), "units"))

    def _on_leave(self, event):
        if self._hover_idx != -1:
            old = self._hover_idx
            self._hover_idx = -1
            self._update_row_bg(old)
        if platform.system() != "Linux":
            self._canvas.unbind_all("<MouseWheel>")

    # ── data ─────────────────────────────────────────────────────

    def get_selection(self) -> list[str]:
        return [rid for rid in self._selection if not rid.startswith("dir:")]

    def set_data(self, rows: list[_Row], all_tags: dict) -> None:
        """Set the list contents and redraw."""
        self._rows = rows
        self._row_id_to_idx = {row.id: i for i, row in enumerate(rows)}
        self._all_tags = all_tags
        valid = {row.id for row in rows}
        self._selection &= valid
        self._hover_idx = -1
        self._full_redraw()

    # ── drawing ──────────────────────────────────────────────────

    def _full_redraw(self) -> None:
        """Clear everything and redraw header + visible rows."""
        self._resize_after_id = None
        self._canvas.delete("all")
        self._row_bg_ids.clear()
        w = self._canvas_width
        total_h = HEADER_H + len(self._rows) * ROW_H
        self._canvas.configure(scrollregion=(0, 0, w, max(total_h, 1)))

        # Header (tagged separately so _draw_visible doesn't touch it)
        self._canvas.create_rectangle(
            0, 0, w, HEADER_H, fill=HEADER_BG, outline="", tags="hdr")
        self._canvas.create_text(
            10, HEADER_H // 2, text="Name", anchor=tk.W,
            font=NAME_FONT_BOLD, fill=TEXT_FG, tags="hdr")

        self._draw_visible()

    def _draw_visible(self) -> None:
        """Redraw only the rows currently visible in the viewport."""
        self._scroll_after_id = None
        self._canvas.delete("row")
        self._row_bg_ids.clear()

        if not self._rows:
            return

        w = self._canvas_width
        top_y = self._canvas.canvasy(0)
        bot_y = self._canvas.canvasy(self._canvas.winfo_height())

        start = max(0, int((top_y - HEADER_H) / ROW_H))
        end = min(len(self._rows), int((bot_y - HEADER_H) / ROW_H) + 2)

        for i in range(start, end):
            row = self._rows[i]
            y = HEADER_H + i * ROW_H
            bg = self._row_bg(i, row.id)

            bg_id = self._canvas.create_rectangle(
                0, y, w, y + ROW_H, fill=bg, outline="", tags="row")
            self._row_bg_ids[i] = bg_id

            self._canvas.create_line(
                0, y + ROW_H - 1, w, y + ROW_H - 1, fill="#e0e0e0", tags="row")

            if row.is_dir:
                self._canvas.create_text(
                    12, y + ROW_H // 2,
                    text=f"\U0001f4c1  {row.display}",
                    anchor=tk.W, font=NAME_FONT_BOLD, fill=DIR_FG, tags="row")
            else:
                self._canvas.create_text(
                    12, y + ROW_H // 2,
                    text=f"\U0001f4c4  {row.display}",
                    anchor=tk.W, font=NAME_FONT, fill=TEXT_FG, tags="row")
                if row.tags:
                    self._draw_tags(y, row.tags, w)

    def _draw_tags(self, row_y: int, file_tags: list[str], canvas_w: int) -> None:
        """Draw colored tag pills right-aligned in the row."""
        measurements = [
            (t, self._tag_font.measure(t) + TAG_PAD * 2) for t in file_tags
        ]
        total_w = sum(tw for _, tw in measurements) + TAG_GAP * (len(measurements) - 1)
        x = canvas_w - total_w - 10
        tag_y = row_y + (ROW_H - TAG_H) // 2

        for tname, tw in measurements:
            color = self._all_tags.get(tname, {}).get("color", "#999999")
            fg = _contrast_fg(color)
            self._canvas.create_rectangle(
                x, tag_y, x + tw, tag_y + TAG_H,
                fill=color, outline=color, tags="row")
            self._canvas.create_text(
                x + tw // 2, tag_y + TAG_H // 2,
                text=tname, fill=fg, font=TAG_FONT, tags="row")
            x += tw + TAG_GAP

    def _row_bg(self, idx: int, row_id: str) -> str:
        if row_id in self._selection:
            return SELECT_BG
        if idx == self._hover_idx:
            return HOVER_BG
        return ROW_BG_EVEN if idx % 2 == 0 else ROW_BG_ODD

    def _update_row_bg(self, idx: int) -> None:
        """Update a single row's background color without full redraw."""
        if idx >= 0 and idx in self._row_bg_ids:
            bg = self._row_bg(idx, self._rows[idx].id)
            self._canvas.itemconfigure(self._row_bg_ids[idx], fill=bg)

    def _row_at_y(self, canvas_y: float) -> int:
        """Return row index for a canvas y coordinate, or -1."""
        y = canvas_y - HEADER_H
        if y < 0:
            return -1
        idx = int(y // ROW_H)
        return idx if 0 <= idx < len(self._rows) else -1

    # ── events ───────────────────────────────────────────────────

    def _on_click(self, event):
        cy = self._canvas.canvasy(event.y)
        idx = self._row_at_y(cy)
        if idx < 0:
            return
        row_id = self._rows[idx].id
        ctrl = event.state & 0x4

        old = set(self._selection)
        if ctrl:
            self._selection.symmetric_difference_update({row_id})
        else:
            self._selection.clear()
            self._selection.add(row_id)

        for rid in old.symmetric_difference(self._selection):
            self._update_row_bg(self._row_id_to_idx.get(rid, -1))
        self._on_select_cb()

    def _on_dbl_click(self, event):
        cy = self._canvas.canvasy(event.y)
        idx = self._row_at_y(cy)
        if idx < 0:
            return
        row = self._rows[idx]
        self._on_double_click_cb(row.id, row.is_dir)

    def _on_rclick(self, event):
        cy = self._canvas.canvasy(event.y)
        idx = self._row_at_y(cy)
        if idx < 0:
            return
        row_id = self._rows[idx].id
        if row_id not in self._selection:
            old = set(self._selection)
            self._selection.clear()
            self._selection.add(row_id)
            for rid in old.symmetric_difference(self._selection):
                self._update_row_bg(self._row_id_to_idx.get(rid, -1))
            self._on_select_cb()
        self._on_right_click_cb(event)

    def _on_motion(self, event):
        cy = self._canvas.canvasy(event.y)
        idx = self._row_at_y(cy)
        if idx != self._hover_idx:
            old = self._hover_idx
            self._hover_idx = idx
            self._update_row_bg(old)
            self._update_row_bg(idx)


# ── main application ─────────────────────────────────────────────

class App:
    def __init__(self, store: TagStore):
        self.store = store
        self._current_dir = ""

        self.root = tk.Tk()
        self.root.title("fstag")
        self.root.geometry("1050x660")
        self.root.minsize(750, 450)

        # State
        self._active_tag_filters: set[str] = set()
        self._filter_mode = tk.StringVar(value="any")
        self._search_var = tk.StringVar()
        self._search_after_id: str | None = None
        self._search_var.trace_add("write", lambda *_: self._on_search_typed())

        self._tag_buttons: dict[str, tk.Button] = {}
        self._action_tag_buttons: dict[str, tk.Button] = {}
        self._action_label: ttk.Label | None = None
        self._build_ui()
        self._refresh_tag_panel()
        self._do_refresh()

        # Auto-reconcile only when the app window is activated from outside
        self._app_has_focus = True
        self._in_dialog = False
        self.root.bind("<FocusIn>", self._on_focus_in)
        self.root.bind("<FocusOut>", self._on_focus_out)

    def _on_focus_out(self, event):
        if event.widget is self.root:
            self._app_has_focus = False

    def _on_focus_in(self, event):
        if event.widget is not self.root or self._app_has_focus:
            return
        self._app_has_focus = True
        if self._in_dialog:
            return
        self.store.refresh()
        self._refresh_tag_panel()
        self._do_refresh()

    def _on_search_typed(self):
        """Debounce: schedule refresh after user stops typing."""
        if self._search_after_id is not None:
            self.root.after_cancel(self._search_after_id)
        self._search_after_id = self.root.after(SEARCH_DEBOUNCE_MS, self._do_search)

    def _do_search(self):
        self._search_after_id = None
        self._do_refresh()

    # ── UI construction ──────────────────────────────────────────

    def _build_ui(self):
        # ── path bar ─────────────────────────────────────────────
        path_bar = ttk.Frame(self.root, padding=(4, 4, 4, 0))
        path_bar.pack(fill=tk.X)

        self._up_btn = ttk.Button(path_bar, text="\u2191 Up", width=6, command=self._go_up)
        self._up_btn.pack(side=tk.LEFT, padx=(0, 6))

        self._breadcrumb_frame = ttk.Frame(path_bar)
        self._breadcrumb_frame.pack(side=tk.LEFT, fill=tk.X, expand=True)

        ttk.Button(path_bar, text="Refresh", command=self._on_refresh).pack(side=tk.RIGHT)

        # ── search bar ───────────────────────────────────────────
        top = ttk.Frame(self.root, padding=(4, 2))
        top.pack(fill=tk.X)
        ttk.Label(top, text="Search:").pack(side=tk.LEFT)
        ttk.Entry(top, textvariable=self._search_var).pack(
            side=tk.LEFT, fill=tk.X, expand=True, padx=(4, 0))

        # ── status bar (bottom) ──────────────────────────────────
        self._status_var = tk.StringVar()
        ttk.Label(self.root, textvariable=self._status_var, relief=tk.SUNKEN,
                  padding=(4, 2)).pack(fill=tk.X, side=tk.BOTTOM)

        # ── action bar (above status, horizontally scrollable) ────
        action_outer = ttk.Frame(self.root)
        action_outer.pack(fill=tk.X, side=tk.BOTTOM)
        self._action_hsb = ttk.Scrollbar(action_outer, orient=tk.HORIZONTAL)
        self._action_canvas = tk.Canvas(
            action_outer, highlightthickness=0, height=34,
            xscrollcommand=self._action_hsb.set)
        self._action_hsb.configure(command=self._action_canvas.xview)
        self._action_canvas.pack(fill=tk.X, side=tk.TOP, padx=4, pady=(4, 0))
        self._action_frame = ttk.Frame(self._action_canvas)
        self._action_canvas_win = self._action_canvas.create_window(
            (0, 0), window=self._action_frame, anchor=tk.NW)
        self._action_frame.bind("<Configure>", self._on_action_frame_configure)
        self._action_canvas.bind("<Configure>",
            lambda e: self._action_canvas.itemconfig(self._action_canvas_win, height=e.height))

        # ── main paned ───────────────────────────────────────────
        paned = ttk.PanedWindow(self.root, orient=tk.HORIZONTAL)
        paned.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)

        # ── left: tag panel ──────────────────────────────────────
        left = ttk.Frame(paned, width=200)
        paned.add(left, weight=0)

        ttk.Label(left, text="Tags", font=("", 11, "bold")).pack(
            anchor=tk.W, padx=4, pady=(4, 0))

        btn_row = ttk.Frame(left)
        btn_row.pack(fill=tk.X, padx=4, pady=2)
        ttk.Button(btn_row, text="+ New", command=self._on_new_tag).pack(side=tk.LEFT)
        ttk.Button(btn_row, text="Clear", command=self._clear_all).pack(
            side=tk.LEFT, padx=(4, 0))

        mode_row = ttk.Frame(left)
        mode_row.pack(fill=tk.X, padx=4, pady=(0, 2))
        ttk.Label(mode_row, text="Match:", font=("", 8)).pack(side=tk.LEFT)
        ttk.Radiobutton(mode_row, text="Any", variable=self._filter_mode, value="any",
                         command=self._do_refresh).pack(side=tk.LEFT, padx=(4, 0))
        ttk.Radiobutton(mode_row, text="All", variable=self._filter_mode, value="all",
                         command=self._do_refresh).pack(side=tk.LEFT)

        self._tag_canvas = tk.Canvas(left, highlightthickness=0, width=190)
        tag_scroll = ttk.Scrollbar(left, orient=tk.VERTICAL, command=self._tag_canvas.yview)
        self._tag_inner = ttk.Frame(self._tag_canvas)
        self._tag_inner.bind(
            "<Configure>",
            lambda e: self._tag_canvas.configure(
                scrollregion=self._tag_canvas.bbox("all")))
        self._tag_canvas_win = self._tag_canvas.create_window(
            (0, 0), window=self._tag_inner, anchor=tk.NW)
        self._tag_canvas.configure(yscrollcommand=tag_scroll.set)
        self._tag_canvas.bind(
            "<Configure>",
            lambda e: self._tag_canvas.itemconfig(self._tag_canvas_win, width=e.width))
        self._tag_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        tag_scroll.pack(side=tk.RIGHT, fill=tk.Y)

        # Tag panel mousewheel (scoped to widget, not global)
        if platform.system() == "Linux":
            self._tag_canvas.bind(
                "<Button-4>",
                lambda e: self._tag_canvas.yview_scroll(-3, "units"))
            self._tag_canvas.bind(
                "<Button-5>",
                lambda e: self._tag_canvas.yview_scroll(3, "units"))

        # ── right: file list (canvas-based) ──────────────────────
        self._file_list = FileListCanvas(
            paned,
            on_select=self._update_action_bar,
            on_double_click=self._on_double_click,
            on_right_click=self._on_right_click,
        )
        paned.add(self._file_list.widget, weight=1)

    # ── path / navigation ────────────────────────────────────────

    @property
    def _is_global_search(self) -> bool:
        return bool(self._active_tag_filters) or bool(self._search_var.get().strip())

    def _update_path_bar(self):
        for w in self._breadcrumb_frame.winfo_children():
            w.destroy()

        if self._is_global_search:
            parts = []
            if self._active_tag_filters:
                parts.append("tags: " + ", ".join(sorted(self._active_tag_filters)))
            search = self._search_var.get().strip()
            if search:
                parts.append(f'"{search}"')
            ttk.Label(self._breadcrumb_frame,
                      text=f"{self.store.root.name}  [searching all: {' \u2014 '.join(parts)}]",
                      font=("", 10, "bold")).pack(side=tk.LEFT)
            self._up_btn.configure(state=tk.DISABLED)
            return

        root_btn = tk.Label(self._breadcrumb_frame, text=self.store.root.name,
                            font=("", 10, "bold"), fg="#1d4ed8", cursor="hand2")
        root_btn.pack(side=tk.LEFT)
        root_btn.bind("<Button-1>", lambda e: self._navigate_to(""))

        if self._current_dir:
            segs = self._current_dir.split("/")
            for i, seg in enumerate(segs):
                ttk.Label(self._breadcrumb_frame, text="  /  ", font=("", 10)).pack(
                    side=tk.LEFT)
                target = "/".join(segs[:i + 1])
                is_last = (i == len(segs) - 1)
                crumb = tk.Label(
                    self._breadcrumb_frame, text=seg,
                    font=("", 10, "bold") if is_last else ("", 10),
                    fg="#333333" if is_last else "#1d4ed8",
                    cursor="arrow" if is_last else "hand2",
                )
                crumb.pack(side=tk.LEFT)
                if not is_last:
                    crumb.bind("<Button-1>", lambda e, t=target: self._navigate_to(t))

        self._up_btn.configure(state=tk.NORMAL if self._current_dir else tk.DISABLED)

    def _navigate_to(self, rel_dir: str):
        self._current_dir = rel_dir
        self._file_list._selection.clear()
        self._do_refresh()

    def _go_up(self):
        if not self._current_dir:
            return
        parent = str(PurePosixPath(self._current_dir).parent)
        self._navigate_to("" if parent == "." else parent)

    def _enter_dir(self, dirname: str):
        new_dir = f"{self._current_dir}/{dirname}" if self._current_dir else dirname
        self._navigate_to(new_dir)

    # ── tag panel ────────────────────────────────────────────────

    def _refresh_tag_panel(self):
        """Full rebuild of the tag panel (only on tag create/rename/delete/color)."""
        for w in self._tag_inner.winfo_children():
            w.destroy()
        self._tag_buttons = {}

        tags = self.store.get_all_tags()
        if not tags:
            ttk.Label(self._tag_inner, text="No tags yet.\nClick '+ New' to create one.",
                      wraplength=170).pack(padx=8, pady=12)
            return

        for name, info in sorted(tags.items()):
            color = info.get("color", "#3b82f6")
            row = tk.Frame(self._tag_inner)
            row.pack(fill=tk.X, padx=4, pady=1)

            active = name in self._active_tag_filters
            btn = tk.Button(
                row, text=f"  {name}  ", bg=color, fg=_contrast_fg(color),
                relief=tk.SUNKEN if active else tk.RAISED,
                bd=2, padx=4, pady=2,
                font=("", 9, "bold") if active else ("", 9),
                cursor="hand2",
                command=lambda n=name: self._toggle_tag_filter(n),
            )
            btn.pack(side=tk.LEFT, fill=tk.X, expand=True)
            self._tag_buttons[name] = btn

            tk.Button(row, text="...", width=3, cursor="hand2",
                      command=lambda n=name: self._on_edit_tag(n)).pack(
                          side=tk.RIGHT, padx=(2, 0))

    def _update_tag_button(self, name: str):
        """Update a single tag button's active/inactive appearance."""
        btn = self._tag_buttons.get(name)
        if btn is None:
            return
        active = name in self._active_tag_filters
        btn.configure(
            relief=tk.SUNKEN if active else tk.RAISED,
            font=("", 9, "bold") if active else ("", 9),
        )

    def _toggle_tag_filter(self, name: str):
        if name in self._active_tag_filters:
            self._active_tag_filters.discard(name)
        else:
            self._active_tag_filters.add(name)
        self._update_tag_button(name)
        self._do_refresh()

    def _clear_all(self):
        old_active = set(self._active_tag_filters)
        self._active_tag_filters.clear()
        for name in old_active:
            self._update_tag_button(name)
        self._search_var.set("")
        # Cancel any pending debounced search since we refresh immediately
        if self._search_after_id is not None:
            self.root.after_cancel(self._search_after_id)
            self._search_after_id = None
        self._do_refresh()

    # ── file list data ───────────────────────────────────────────

    def _get_current_entries(self, all_files: dict) -> list[_Row]:
        """Build the row list for the current view (directory or search)."""
        search = self._search_var.get().lower().strip()
        active = self._active_tag_filters
        mode = self._filter_mode.get()

        if active or search:
            rows: list[_Row] = []
            for relpath, meta in sorted(all_files.items()):
                file_tags = meta.get("tags", [])
                filename = PurePosixPath(relpath).name

                if active:
                    ftset = set(file_tags)
                    if mode == "all" and not active.issubset(ftset):
                        continue
                    if mode == "any" and not active.intersection(ftset):
                        continue

                if search:
                    haystack = f"{filename.lower()} {relpath.lower()} {' '.join(file_tags).lower()}"
                    if not all(t in haystack for t in search.split()):
                        continue

                rows.append(_Row(id=relpath, is_dir=False, display=relpath, tags=file_tags))
            return rows

        prefix = f"{self._current_dir}/" if self._current_dir else ""
        dirs_seen: set[str] = set()
        files_here: list[_Row] = []

        for relpath, meta in sorted(all_files.items()):
            if prefix and not relpath.startswith(prefix):
                continue
            rest = relpath[len(prefix):]
            parts = rest.split("/")
            if len(parts) == 1:
                files_here.append(
                    _Row(id=relpath, is_dir=False, display=parts[0],
                         tags=meta.get("tags", [])))
            else:
                dirs_seen.add(parts[0])

        dir_rows = [
            _Row(id=f"dir:{d}", is_dir=True, display=d, tags=[])
            for d in sorted(dirs_seen)
        ]
        return dir_rows + files_here

    def _do_refresh(self, *, update_path_bar: bool = True) -> None:
        """Refresh file list, status bar, and action bar."""
        if update_path_bar:
            self._update_path_bar()
        all_files = self.store.get_files()
        all_tags = self.store.get_all_tags()
        rows = self._get_current_entries(all_files)
        self._file_list.set_data(rows, all_tags)

        n_dirs = sum(1 for r in rows if r.is_dir)
        n_files = len(rows) - n_dirs
        self._status_var.set(
            f"{n_dirs} folder{'s' if n_dirs != 1 else ''}  \u00b7  "
            f"{n_files} file{'s' if n_files != 1 else ''} shown  \u00b7  "
            f"{len(all_files)} total  \u00b7  {len(all_tags)} tags"
        )
        self._update_action_bar()

    # ── interactions ─────────────────────────────────────────────

    def _on_double_click(self, row_id: str, is_dir: bool):
        if is_dir:
            self._enter_dir(row_id[4:])  # strip "dir:" prefix
        else:
            path = self.store.root / row_id
            if path.exists():
                _open_file(path)
            else:
                messagebox.showwarning(
                    "File not found",
                    f"'{row_id}' no longer exists on disk.\nClick Refresh to update.",
                    parent=self.root,
                )

    def _on_right_click(self, event):
        selected = self._file_list.get_selection()
        if not selected:
            return

        menu = tk.Menu(self.root, tearoff=0)
        tags = self.store.get_all_tags()

        if tags:
            add_menu = tk.Menu(menu, tearoff=0)
            rm_menu = tk.Menu(menu, tearoff=0)
            for tag_name in sorted(tags):
                add_menu.add_command(
                    label=f"  {tag_name}",
                    command=lambda t=tag_name, s=list(selected):
                        self._batch_tag(s, t, add=True))
                rm_menu.add_command(
                    label=f"  {tag_name}",
                    command=lambda t=tag_name, s=list(selected):
                        self._batch_tag(s, t, add=False))
            menu.add_cascade(label="Add tag", menu=add_menu)
            menu.add_cascade(label="Remove tag", menu=rm_menu)
            menu.add_separator()

        menu.add_command(
            label="Open file",
            command=lambda: _open_file(self.store.root / selected[0]))
        menu.add_command(
            label="Open folder",
            command=lambda: _open_file((self.store.root / selected[0]).parent))
        menu.tk_popup(event.x_root, event.y_root)

    def _batch_tag(self, relpaths: list[str], tag_name: str, add: bool):
        with self.store.batch():
            for rp in relpaths:
                if add:
                    self.store.add_tag_to_file(rp, tag_name)
                else:
                    self.store.remove_tag_from_file(rp, tag_name)
        self._do_refresh(update_path_bar=False)

    # ── action bar ───────────────────────────────────────────────

    def _on_action_frame_configure(self, event):
        self._action_canvas.configure(scrollregion=self._action_canvas.bbox("all"))
        if event.width > self._action_canvas.winfo_width():
            self._action_hsb.pack(fill=tk.X, side=tk.TOP, padx=4)
        else:
            self._action_hsb.pack_forget()

    def _build_action_bar(self):
        """Full rebuild of action bar widgets."""
        for w in self._action_frame.winfo_children():
            w.destroy()
        self._action_tag_buttons = {}
        self._action_label = None

        selected = self._file_list.get_selection()
        tags = self.store.get_all_tags()

        if not selected:
            ttk.Label(self._action_frame,
                      text="Select files, then click a tag to assign it.",
                      foreground="gray").pack(side=tk.LEFT)
            self._action_canvas.xview_moveto(0)
            return

        n = len(selected)
        self._action_label = ttk.Label(
            self._action_frame,
            text=f"{n} file{'s' if n > 1 else ''} selected \u2014 click to toggle:",
            font=("", 9, "bold"))
        self._action_label.pack(side=tk.LEFT, padx=(0, 8))

        if not tags:
            ttk.Label(self._action_frame, text="(create a tag first)",
                      foreground="gray").pack(side=tk.LEFT)
            return

        sel_tags_list = [set(self.store.get_file_tags(rp)) for rp in selected]
        common_tags = set(sel_tags_list[0])
        for s in sel_tags_list[1:]:
            common_tags &= s

        for tag_name in sorted(tags):
            color = tags[tag_name].get("color", "#3b82f6")
            has_tag = tag_name in common_tags
            btn = tk.Button(
                self._action_frame,
                text=f" {tag_name} \u2713 " if has_tag else f" {tag_name} ",
                bg=color, fg=_contrast_fg(color),
                relief=tk.SUNKEN if has_tag else tk.RAISED,
                bd=2, padx=6, pady=1,
                font=("", 9, "bold") if has_tag else ("", 9),
                cursor="hand2",
                command=lambda t=tag_name: self._toggle_tag_on_selection(t),
            )
            btn.pack(side=tk.LEFT, padx=2)
            self._action_tag_buttons[tag_name] = btn
        self._action_canvas.xview_moveto(0)

    def _refresh_action_buttons(self):
        """Update only the check/uncheck state of existing action bar buttons."""
        selected = self._file_list.get_selection()
        if not selected or not self._action_tag_buttons:
            return

        sel_tags_list = [set(self.store.get_file_tags(rp)) for rp in selected]
        common_tags = set(sel_tags_list[0])
        for s in sel_tags_list[1:]:
            common_tags &= s

        tags = self.store.get_all_tags()
        for tag_name, btn in self._action_tag_buttons.items():
            color = tags.get(tag_name, {}).get("color", "#3b82f6")
            has_tag = tag_name in common_tags
            btn.configure(
                text=f" {tag_name} \u2713 " if has_tag else f" {tag_name} ",
                bg=color, fg=_contrast_fg(color),
                relief=tk.SUNKEN if has_tag else tk.RAISED,
                font=("", 9, "bold") if has_tag else ("", 9),
            )

    def _update_action_bar(self):
        """Decide whether to rebuild or just refresh the action bar."""
        selected = self._file_list.get_selection()
        new_n = len(selected)
        tag_keys = set(self.store.get_all_tags().keys())

        prev_had_sel = bool(self._action_tag_buttons) or (
            self._action_label is not None and self._action_label.winfo_exists())
        if (new_n == 0) != (not prev_had_sel) or tag_keys != set(self._action_tag_buttons.keys()):
            self._build_action_bar()
        elif new_n > 0:
            if self._action_label and self._action_label.winfo_exists():
                self._action_label.configure(
                    text=f"{new_n} file{'s' if new_n > 1 else ''} selected \u2014 click to toggle:")
            self._refresh_action_buttons()

    def _toggle_tag_on_selection(self, tag_name: str):
        selected = self._file_list.get_selection()
        if not selected:
            return

        all_have = all(tag_name in self.store.get_file_tags(rp) for rp in selected)
        with self.store.batch():
            for rp in selected:
                if all_have:
                    self.store.remove_tag_from_file(rp, tag_name)
                else:
                    self.store.add_tag_to_file(rp, tag_name)

        # Redraw file list (tag pills changed) and update action buttons
        all_files = self.store.get_files()
        all_tags = self.store.get_all_tags()
        rows = self._get_current_entries(all_files)
        self._file_list.set_data(rows, all_tags)
        self._refresh_action_buttons()

    # ── tag management ───────────────────────────────────────────

    def _on_new_tag(self):
        self._in_dialog = True
        try:
            name = simpledialog.askstring("New tag", "Tag name:", parent=self.root)
            if not name or not name.strip():
                return
            name = name.strip()
            if name in self.store.get_all_tags():
                messagebox.showwarning("Duplicate", f"Tag '{name}' already exists.",
                                       parent=self.root)
                return
            dlg = ColorPickerDialog(self.root, title=f"Colour for '{name}'")
            if dlg.result is None:
                return

            self.store.create_tag(name, dlg.result)

            selected = self._file_list.get_selection()
            if selected and messagebox.askyesno(
                    "Assign tag",
                    f"Apply '{name}' to {len(selected)} selected "
                    f"file{'s' if len(selected) > 1 else ''}?",
                    parent=self.root):
                with self.store.batch():
                    for rp in selected:
                        self.store.add_tag_to_file(rp, name)

            self._refresh_tag_panel()
            self._do_refresh(update_path_bar=False)
        finally:
            self._in_dialog = False

    def _on_edit_tag(self, name: str):
        menu = tk.Menu(self.root, tearoff=0)
        menu.add_command(label="Change colour",
                         command=lambda: self._change_tag_color(name))
        menu.add_command(label="Rename", command=lambda: self._rename_tag(name))
        menu.add_separator()
        menu.add_command(label="Delete", command=lambda: self._delete_tag(name))
        menu.tk_popup(self.root.winfo_pointerx(), self.root.winfo_pointery())

    def _change_tag_color(self, name: str):
        self._in_dialog = True
        try:
            current = self.store.get_all_tags().get(name, {}).get("color", "#3b82f6")
            dlg = ColorPickerDialog(self.root, title=f"Colour for '{name}'", current=current)
            if dlg.result is not None:
                self.store.update_tag_color(name, dlg.result)
                self._refresh_tag_panel()
                self._do_refresh(update_path_bar=False)
        finally:
            self._in_dialog = False

    def _rename_tag(self, old_name: str):
        self._in_dialog = True
        try:
            new_name = simpledialog.askstring("Rename tag",
                                              f"New name for '{old_name}':",
                                              parent=self.root)
            if not new_name or not new_name.strip():
                return
            new_name = new_name.strip()
            if new_name in self.store.get_all_tags():
                messagebox.showwarning("Duplicate", f"Tag '{new_name}' already exists.",
                                       parent=self.root)
                return
            if old_name in self._active_tag_filters:
                self._active_tag_filters.discard(old_name)
                self._active_tag_filters.add(new_name)
            self.store.rename_tag(old_name, new_name)
            self._refresh_tag_panel()
            self._do_refresh(update_path_bar=False)
        finally:
            self._in_dialog = False

    def _delete_tag(self, name: str):
        self._in_dialog = True
        try:
            if messagebox.askyesno(
                    "Delete tag",
                    f"Delete tag '{name}'?\nThis removes it from all files.",
                    parent=self.root):
                self._active_tag_filters.discard(name)
                self.store.delete_tag(name)
                self._refresh_tag_panel()
                self._do_refresh(update_path_bar=False)
        finally:
            self._in_dialog = False

    # ── misc ─────────────────────────────────────────────────────

    def _on_refresh(self):
        self.store.refresh()
        self._refresh_tag_panel()
        self._do_refresh()

    def run(self):
        self.root.mainloop()
