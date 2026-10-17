"""Quick list tooltip poster for Diablo 2.

Hover an item, press the hotkey: the YOLO tooltip detector crops the
tooltip, copies it to the clipboard, and opens your chosen trading site
where the image is auto-pasted into the posting form.
"""
import os
import subprocess
import sys
import threading

# torch must be in the process before winocr (imported by ocr.py) - see
# main.py's CRITICAL IMPORT ORDER note. When launched via main.py this is
# an instant no-op (torch is already loaded); it matters when gui.py is
# run directly.
try:
    import torch  # noqa: F401
except Exception as _e:  # pragma: no cover
    print(f"[startup] WARNING: torch preload failed: {_e}")

import tkinter as tk
from tkinter import ttk, messagebox

# pystray + Pillow imported at module level so PyInstaller bundles them
# into the onefile exe (used lazily in _minimize_to_tray).
try:
    import pystray
    from PIL import Image, ImageTk
except Exception:
    pystray = None
    Image = None
    ImageTk = None

from config import (load_config, save_config, app_dir, get_trading_url,
                    TRADING_SITE_LABELS)
from hotkeys import HotkeyManager, copy_image_to_clipboard, focus_browser_window
from scanner import InventoryScanner
from pricecheck import (
    PriceChecker,
    DEFAULT_AI_TOOLS,
    PLATFORM_OPTIONS,
    LADDER_OPTIONS,
    HARDCORE_OPTIONS,
    DEFAULT_TIMEFRAMES,
    DEFAULT_MARKETPLACES,
)

# ------------------------------------------------------------------ RoundedButton
# tkinter.ttk buttons use native OS drawing and CANNOT render rounded corners.
# This custom canvas-based button provides true rounded/pill-shaped buttons
# with hover and pressed feedback, working in both light and dark themes.
class RoundedButton(tk.Canvas):
    """A round-cornered button drawn on a Canvas.
    
    Parameters
    ----------
    parent : tk.Widget
        Parent widget.
    text : str
        Button label text.
    command : callable, optional
        Function to call on click.
    width : int, optional
        Fixed width in pixels (auto-sized if 0).
    height : int, optional
        Fixed height in pixels (auto-sized if 0).
    corner_radius : int, optional
        Corner radius in pixels. Use a large value for pill shape.
    bg_color : str, optional
        Background color (default: accent color).
    fg_color : str, optional
        Text color (default: white).
    hover_color : str, optional
        Hover background color.
    pressed_color : str, optional
        Pressed background color.
    disabled_color : str, optional
        Disabled background color.
    disabled_fg_color : str, optional
        Disabled text color.
    font : tuple, optional
        Font tuple (family, size, style).
    """

    # Theme colors — separate palettes for primary and secondary buttons
    theme_primary_bg = None
    theme_primary_fg = None
    theme_primary_hover = None
    theme_primary_pressed = None
    theme_primary_disabled = None
    theme_primary_disabled_fg = None
    theme_secondary_bg = None
    theme_secondary_fg = None
    theme_secondary_hover = None
    theme_secondary_pressed = None
    theme_secondary_border = None
    theme_secondary_disabled = None
    theme_secondary_disabled_fg = None

    # Default secondary button colors (when theme not yet applied)
    secondary_bg_default = "#2a2e3d"
    secondary_fg_default = "#ffffff"
    secondary_disabled_default = "#3a3a42"
    secondary_disabled_fg_default = "#8a8a9a"

    # Track all instances for theme updates
    _instances = set()

    @classmethod
    def update_theme(cls, primary_bg=None, primary_fg=None, primary_hover=None,
                     primary_pressed=None, primary_disabled=None,
                     primary_disabled_fg=None,
                     secondary_bg=None, secondary_fg=None,
                     secondary_hover=None, secondary_pressed=None,
                     secondary_border=None, secondary_disabled=None,
                     secondary_disabled_fg=None):
        """Update theme colors for all RoundedButton instances."""
        if primary_bg is not None:
            cls.theme_primary_bg = primary_bg
        if primary_fg is not None:
            cls.theme_primary_fg = primary_fg
        if primary_hover is not None:
            cls.theme_primary_hover = primary_hover
        if primary_pressed is not None:
            cls.theme_primary_pressed = primary_pressed
        if primary_disabled is not None:
            cls.theme_primary_disabled = primary_disabled
        if primary_disabled_fg is not None:
            cls.theme_primary_disabled_fg = primary_disabled_fg
        if secondary_bg is not None:
            cls.theme_secondary_bg = secondary_bg
        if secondary_fg is not None:
            cls.theme_secondary_fg = secondary_fg
        if secondary_hover is not None:
            cls.theme_secondary_hover = secondary_hover
        if secondary_pressed is not None:
            cls.theme_secondary_pressed = secondary_pressed
        if secondary_border is not None:
            cls.theme_secondary_border = secondary_border
        if secondary_disabled is not None:
            cls.theme_secondary_disabled = secondary_disabled
        if secondary_disabled_fg is not None:
            cls.theme_secondary_disabled_fg = secondary_disabled_fg
        # Redraw all existing buttons
        for inst in list(cls._instances):
            try:
                inst._draw()
            except Exception:
                pass

    def __init__(self, parent, text="", command=None, width=0, height=32,
                 corner_radius=16, bg_color=None, fg_color="#ffffff",
                 hover_color=None, pressed_color=None,
                 disabled_color="#5a5a5a", disabled_fg_color="#aaaaaa",
                 font=("Segoe UI", 9, "normal"), state="normal",
                 style="primary"):
        super().__init__(parent, highlightthickness=0, bd=0, cursor="hand2")

        self.command = command
        self.text = text
        self.font = font
        self.corner_radius = corner_radius
        self.enabled = True
        self.pressed = False
        self.hovered = False
        self.style = style  # "primary" or "secondary"

        # Colors - instance overrides take priority, then theme, then defaults
        self.bg_color = bg_color
        self.fg_color = fg_color
        self.hover_color = hover_color
        self.pressed_color = pressed_color
        self.disabled_color = disabled_color
        self.disabled_fg_color = disabled_fg_color

        # Track this instance for theme updates
        RoundedButton._instances.add(self)

        if state == "disabled":
            self.enabled = False

        # Calculate button size from text
        self._min_width, self._min_height = self._measure_text(font)
        if width == 0:
            width = self._min_width
        if height == 32:
            height = self._min_height
        self.configure(width=width, height=height)

        # Bind events
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<ButtonPress-1>", self._on_press)
        self.bind("<ButtonRelease-1>", self._on_release)
        self.bind("<Configure>", self._on_resize)

        self._draw()

    def _measure_text(self, font):
        """Calculate button size from text."""
        import tkinter.font as tkfont
        font_obj = tkfont.Font(font=font)
        text_width = font_obj.measure(self.text) if self.text else 50
        min_width = max(text_width + 32, 100)  # padding + min width
        min_height = max(font_obj.metrics("linespace") + 16, 32)
        return min_width, min_height

    def _resolve(self, instance_val, theme_val, default_val):
        """Resolve color: instance override > theme > default."""
        if instance_val is not None:
            return instance_val
        if theme_val is not None:
            return theme_val
        return default_val

    def _current_bg(self):
        if self.style == "secondary":
            if not self.enabled:
                return self._resolve(self.disabled_color,
                                     self.theme_secondary_disabled,
                                     secondary_disabled_default)
            if self.pressed:
                return self._resolve(self.pressed_color,
                                     self.theme_secondary_pressed,
                                     self.bg_color)
            if self.hovered:
                return self._resolve(self.hover_color,
                                     self.theme_secondary_hover,
                                     self.bg_color)
            return self._resolve(self.bg_color,
                                 self.theme_secondary_bg,
                                 self.secondary_bg_default)
        else:
            if not self.enabled:
                return self._resolve(self.disabled_color,
                                     self.theme_primary_disabled,
                                     "#5a5a5a")
            if self.pressed:
                return self._resolve(self.pressed_color,
                                     self.theme_primary_pressed,
                                     self.bg_color)
            if self.hovered:
                return self._resolve(self.hover_color,
                                     self.theme_primary_hover,
                                     self.bg_color)
            return self._resolve(self.bg_color,
                                 self.theme_primary_bg,
                                 "#4f46e5")

    def _current_fg(self):
        if self.style == "secondary":
            if not self.enabled:
                return self._resolve(self.disabled_fg_color,
                                     self.theme_secondary_disabled_fg,
                                     self.secondary_disabled_fg_default)
            return self._resolve(self.fg_color,
                                 self.theme_secondary_fg,
                                 self.secondary_fg_default)
        else:
            if not self.enabled:
                return self._resolve(self.disabled_fg_color,
                                     self.theme_primary_disabled_fg,
                                     "#aaaaaa")
            return self._resolve(self.fg_color,
                                 self.theme_primary_fg,
                                 "#ffffff")

    def _draw(self):
        self.delete("all")
        w = self.winfo_width()
        h = self.winfo_height()
        if w < 1 or h < 1:
            return

        r = min(self.corner_radius, h // 2)
        bg = self._current_bg()
        fg = self._current_fg()

        # Draw rounded rectangle
        self.create_rounded_rect(0, 0, w, h, r, fill=bg, outline="")

        # Draw text centered
        if self.text:
            self.create_text(w // 2, h // 2, text=self.text,
                             fill=fg, font=self.font, justify="center")

    def create_rounded_rect(self, x1, y1, x2, y2, r, **kwargs):
        """Draw a rounded rectangle."""
        points = [
            x1 + r, y1,
            x2 - r, y1,
            x2, y1,
            x2, y1 + r,
            x2, y2 - r,
            x2, y2,
            x2 - r, y2,
            x1 + r, y2,
            x1, y2,
            x1, y2 - r,
            x1, y1 + r,
            x1, y1,
        ]
        return self.create_polygon(points, smooth=True, **kwargs)

    def _on_enter(self, event):
        if self.enabled:
            self.hovered = True
            self._draw()

    def _on_leave(self, event):
        self.hovered = False
        self.pressed = False
        self._draw()

    def _on_press(self, event):
        if self.enabled:
            self.pressed = True
            self._draw()

    def _on_release(self, event):
        if self.enabled and self.pressed:
            self.pressed = False
            self._draw()
            if self.command:
                self.command()
        self.pressed = False

    def _on_resize(self, event):
        self._draw()

    def configure(self, **kwargs):
        if "text" in kwargs:
            self.text = kwargs.pop("text")
        if "command" in kwargs:
            self.command = kwargs.pop("command")
        if "state" in kwargs:
            state = kwargs.pop("state")
            if state == "disabled":
                self.enabled = False
            elif state == "normal":
                self.enabled = True
        if "fg_color" in kwargs:
            self.fg_color = kwargs.pop("fg_color")
        if "bg_color" in kwargs:
            self.bg_color = kwargs.pop("bg_color")
        super().configure(**kwargs)
        self._draw()

    def configure_command(self, command):
        self.command = command

# Keep a reference to the single-instance mutex so it isn't garbage-collected
# and released while the app is running.
if getattr(sys, "frozen", False):
    try:
        import win32event
        _SINGLE_INSTANCE_MUTEX = win32event.CreateMutex(None, False, "D2TradeHelper_SingleInstanceMutex")
    except Exception:
        pass

APP_TITLE = "D2TradeHelper"
APP_VERSION = "0.1"


def resource_path(relative_path):
    """Get the absolute path to a resource, working for both source and
    PyInstaller onefile exe. In a frozen exe, data files bundled with
    --add-data live under sys._MEIPASS; otherwise they're next to the
    script/exe.
    """
    if getattr(sys, "frozen", False):
        base = getattr(sys, "_MEIPASS", None)
        if base and os.path.exists(os.path.join(base, relative_path)):
            return os.path.join(base, relative_path)
        return os.path.join(os.path.dirname(sys.executable), relative_path)
    return os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        relative_path)


class CropDialog(tk.Toplevel):
    """Drag-a-rectangle selector over a screenshot.

    Shows the given BGR image scaled to fit, lets the user drag the region
    they want to post, and calls on_done(crop_bgr) - or on_done(None) on
    cancel (Esc / close / Cancel button). Must run on the Tk main thread.
    """

    MAX_W, MAX_H = 1500, 860      # display size cap
    MIN_SEL_W, MIN_SEL_H = 30, 20  # minimum acceptable selection

    def __init__(self, master, img_bgr, title, on_done):
        super().__init__(master)
        self.title(title or "Select the area to post")
        self.img = img_bgr
        self.on_done = on_done
        self.rect = None           # (x1, y1, x2, y2) in DISPLAY coords
        self._drag_start = None
        self._temp_id = None

        import cv2
        from PIL import Image, ImageTk
        scale = min(self.MAX_W / img_bgr.shape[1],
                    self.MAX_H / img_bgr.shape[0], 1.0)
        self.scale = scale
        dw = max(1, int(img_bgr.shape[1] * scale))
        dh = max(1, int(img_bgr.shape[0] * scale))
        rgb = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB)
        pil = Image.fromarray(rgb).resize((dw, dh))
        self._photo = ImageTk.PhotoImage(pil)  # keep a reference

        self.canvas = tk.Canvas(self, width=dw, height=dh,
                                cursor="crosshair", highlightthickness=0)
        self.canvas.create_image(0, 0, anchor="nw", image=self._photo)
        self.canvas.pack(fill="both", expand=True)

        bar = ttk.Frame(self)
        bar.pack(fill="x")
        self.info = ttk.Label(bar, text="Drag a rectangle around the tooltip. "
                                     "Enter = use, Esc = cancel")
        self.info.pack(side="left", padx=6, pady=4)
        RoundedButton(bar, text="Cancel", command=self._cancel).pack(
            side="right", padx=6, pady=4)
        self.use_btn = RoundedButton(bar, text="Use selection", command=self._ok,
                                   state="disabled")
        self.use_btn.pack(side="right", padx=6, pady=4)

        self.canvas.bind("<ButtonPress-1>", self._press)
        self.canvas.bind("<B1-Motion>", self._motion)
        self.canvas.bind("<ButtonRelease-1>", self._release)
        self.bind("<Escape>", self._cancel)
        self.bind("<Return>", self._ok)
        self.protocol("WM_DELETE_WINDOW", self._cancel)

        self.attributes("-topmost", True)  # stay on top so it gets focus
        self.grab_set()      # modal while open
        self.focus_force()   # force focus to this dialog (Windows)
        self.lift()

    # -- drag handling -------------------------------------------------------
    def _press(self, e):
        self._drag_start = (e.x, e.y)
        if self._temp_id is not None:
            self.canvas.delete(self._temp_id)
            self._temp_id = None

    def _motion(self, e):
        if not self._drag_start:
            return
        x0, y0 = self._drag_start
        if self._temp_id is not None:
            self.canvas.delete(self._temp_id)
        self._temp_id = self.canvas.create_rectangle(
            min(x0, e.x), min(y0, e.y), max(x0, e.x), max(y0, e.y),
            outline="#00ff00", width=2)

    def _release(self, e):
        if not self._drag_start:
            return
        x0, y0 = self._drag_start
        self._drag_start = None
        x1, y1 = min(x0, e.x), min(y0, e.y)
        x2, y2 = max(x0, e.x), max(y0, e.y)
        if (x2 - x1) >= self.MIN_SEL_W and (y2 - y1) >= self.MIN_SEL_H:
            self.rect = (x1, y1, x2, y2)
            self.use_btn.configure(state="normal")
            self.info.configure(
                text=f"Selection: {int((x2 - x1) / self.scale)}x"
                     f"{int((y2 - y1) / self.scale)}px "
                     "(Enter = use, drag again to redo)")
        else:
            self.rect = None
            self.use_btn.configure(state="disabled")
            self.info.configure(text="Selection too small - drag a bigger rectangle.")

    # -- finish --------------------------------------------------------------
    def _ok(self, _e=None):
        if not self.rect:
            return
        dx1, dy1, dx2, dy2 = self.rect
        s = 1.0 / self.scale
        h, w = self.img.shape[:2]
        x1 = max(0, min(int(dx1 * s), w - 1))
        y1 = max(0, min(int(dy1 * s), h - 1))
        x2 = max(x1 + 1, min(int(dx2 * s), w))
        y2 = max(y1 + 1, min(int(dy2 * s), h))
        crop = self.img[y1:y2, x1:x2].copy()
        cb = self.on_done
        self.destroy()
        if cb:
            cb(crop)

    def _cancel(self, _e=None):
        cb = self.on_done
        self.destroy()
        if cb:
            cb(None)


def _log_error(text):
    """Append an error to error_log.txt next to the app - crucial in the
    windowless exe where there is no console to see exceptions."""
    try:
        import datetime
        path = os.path.join(app_dir(),
                            "error_log.txt")
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"\n[{datetime.datetime.now():%Y-%m-%d %H:%M:%S}]\n{text}\n")
    except Exception:
        pass


class App:
    def __init__(self):
        self.cfg = load_config()
        self.scanner = InventoryScanner()
        self.price_checker = PriceChecker(self.cfg)
        self.scanner.profile = {}
        self.recorder = None
        self.hotkey_mgr = None
        self.tray = None
        self._build_ui()
        self._start_hotkeys()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------------------------------------------------ Theme

    def _apply_theme(self):
        """Apply light/dark theme colors to all ttk and tk widgets."""
        style = ttk.Style()
        style.theme_use("clam")

        # Resolve "system" to actual light/dark based on Windows setting
        effective_theme = self.theme_var.get()
        if effective_theme == "system":
            effective_theme = self._detect_system_theme()

        # ------------------------------------------------------------------ Palette (centralized)
        # Dark mode colors
        DARK_PALETTE = {
            "bg": "#1e1e26",
            "fg": "#e0e0e0",
            "panel_bg": "#252526",
            "select_bg": "#3c3c45",
            "select_fg": "#ffffff",
            "accent": "#4f46e5",
            "accent_hover": "#6366f1",
            "accent_pressed": "#4338ca",
            "entry_bg": "#333333",
            "entry_fg": "#e0e0e0",
            "disabled_fg": "#9aa0a6",
            "disabled_bg": "#2a2a2d",
            "border": "#4a4a4a",
            "hover_bg": "#333333",
            "secondary_bg": "#2a2e3d",
            "secondary_hover": "#dc2626",
            "secondary_pressed": "#b91c1c",
            "secondary_text": "#ffffff",
            "secondary_border": "#3f3f46",
            "secondary_disabled_bg": "#3a3a42",
            "secondary_disabled_fg": "#8a8a9a",
            "primary_disabled_bg": "#5a5a5a",
            "primary_disabled_fg": "#aaaaaa",
        }
        # Light mode colors
        LIGHT_PALETTE = {
            "bg": "#fafafa",
            "fg": "#202020",
            "panel_bg": "#f0f0f0",
            "select_bg": "#e8f1fe",
            "select_fg": "#000000",
            "accent": "#4f46e5",
            "accent_hover": "#4338ca",
            "accent_pressed": "#3730a3",
            "entry_bg": "#ffffff",
            "entry_fg": "#202020",
            "disabled_fg": "#999999",
            "disabled_bg": "#e5e5e5",
            "border": "#cccccc",
            "hover_bg": "#f0f0f0",
            "secondary_bg": "#e2e8f0",
            "secondary_hover": "#ef4444",
            "secondary_pressed": "#dc2626",
            "secondary_text": "#1e293b",
            "secondary_border": "#cbd5e1",
            "secondary_disabled_bg": "#f1f5f9",
            "secondary_disabled_fg": "#94a3b8",
            "primary_disabled_bg": "#e5e5e5",
            "primary_disabled_fg": "#999999",
        }

        if effective_theme == "dark":
            p = DARK_PALETTE
        else:
            p = LIGHT_PALETTE

        bg = p["bg"]
        fg = p["fg"]
        panel_bg = p["panel_bg"]
        select_bg = p["select_bg"]
        select_fg = p["select_fg"]
        accent = p["accent"]
        accent_hover = p["accent_hover"]
        accent_pressed = p["accent_pressed"]
        entry_bg = p["entry_bg"]
        entry_fg = p["entry_fg"]
        disabled_fg = p["disabled_fg"]
        disabled_bg = p["disabled_bg"]
        border = p["border"]
        hover_bg = p["hover_bg"]
        secondary_bg = p["secondary_bg"]
        secondary_hover = p["secondary_hover"]
        secondary_pressed = p["secondary_pressed"]
        secondary_text = p["secondary_text"]
        secondary_border = p["secondary_border"]
        secondary_disabled_bg = p["secondary_disabled_bg"]
        secondary_disabled_fg = p["secondary_disabled_fg"]
        primary_disabled_bg = p["primary_disabled_bg"]
        primary_disabled_fg = p["primary_disabled_fg"]
        self.root.configure(bg=bg)

        # Common widget styles
        style.configure("TFrame", background=bg)
        style.configure("TLabel", background=bg, foreground=fg)
        style.configure("TLabelframe", background=panel_bg, foreground=fg, bordercolor=border, borderwidth=1)
        style.configure("TLabelframe.Label", background=panel_bg, foreground=fg, font=("Segoe UI", 9, "bold"))
        style.configure(
            "TEntry",
            fieldbackground=entry_bg,
            foreground=entry_fg,
            bordercolor=border,
            insertcolor=fg,
            borderwidth=1,
            relief="flat",
        )
        style.map(
            "TEntry",
            fieldbackground=[("disabled", disabled_bg)],
            foreground=[("disabled", disabled_fg)],
        )
        style.configure(
            "TCombobox",
            fieldbackground=entry_bg,
            foreground=entry_fg,
            bordercolor=border,
            insertcolor=fg,
            borderwidth=1,
            relief="flat",
        )
        style.map(
            "TCombobox",
            fieldbackground=[("disabled", disabled_bg)],
            foreground=[("disabled", disabled_fg)],
            selectbackground=[("readonly", entry_bg)],
            selectforeground=[("readonly", entry_fg)],
        )

        # Primary action button - solid accent color, pill-shaped via generous
        # padding, hover/pressed states, bold text
        style.configure(
            "TButton",
            background=accent,
            foreground="#ffffff",
            bordercolor=accent,
            focuscolor=accent,
            borderwidth=0,
            relief="flat",
            padding=(16, 8),
            font=("Segoe UI", 9, "bold"),
        )
        style.map(
            "TButton",
            background=[
                ("active", accent_hover),
                ("pressed", accent_pressed),
                ("disabled", primary_disabled_bg),
            ],
            foreground=[("disabled", primary_disabled_fg)],
        )

        # Secondary button - subtle outline style for secondary actions
        style.configure(
            "Secondary.TButton",
            background=secondary_bg,
            foreground=secondary_text,
            bordercolor=secondary_border,
            focuscolor=border,
            borderwidth=1,
            relief="flat",
            padding=(16, 8),
            font=("Segoe UI", 9, "bold"),
        )
        style.map(
            "Secondary.TButton",
            background=[("active", secondary_hover), ("pressed", secondary_pressed)],
            foreground=[("disabled", secondary_disabled_fg)],
            bordercolor=[("active", accent), ("pressed", accent)],
        )

        style.configure(
            "TRadiobutton",
            background=bg,
            foreground=fg,
            focuscolor=bg,
            borderwidth=0,
        )
        style.map("TRadiobutton", background=[("active", bg), ("selected", bg)])

        style.configure(
            "TCheckbutton",
            background=bg,
            foreground=fg,
            focuscolor=bg,
            borderwidth=0,
        )
        style.map("TCheckbutton", background=[("active", bg)])

        style.configure("TNotebook", background=bg, bordercolor=border)
        style.configure(
            "TNotebook.Tab",
            background=panel_bg,
            foreground=fg,
            bordercolor=border,
            borderwidth=1,
            padding=(10, 5),
        )
        style.map(
            "TNotebook.Tab",
            background=[("selected", bg), ("active", hover_bg)],
            foreground=[("selected", accent), ("active", fg)],
        )

        # Remove dotted focus rings from all widgets
        style.configure("TEntry", focuscolor=entry_bg, borderwidth=1, relief="flat")
        style.configure("TCombobox", focuscolor=entry_bg, borderwidth=1, relief="flat")
        style.configure("TCheckbutton", focuscolor=bg)
        style.configure("TRadiobutton", focuscolor=bg)

        # Text widget (result area) - configure manually since ttk doesn't cover it
        self.root.option_add("*Text.background", entry_bg)
        self.root.option_add("*Text.foreground", entry_fg)
        self.root.option_add("*Text.insertBackground", fg)
        self.root.option_add("*Text.selectBackground", select_bg)
        self.root.option_add("*Text.selectForeground", select_fg)
        self.root.option_add("*Text.disabledForeground", disabled_fg)
        self.root.option_add("*Text.disabledBackground", disabled_bg)

        # tk.Listbox (item log, etc.)
        self.root.option_add("*Listbox.background", entry_bg)
        self.root.option_add("*Listbox.foreground", entry_fg)
        self.root.option_add("*Listbox.selectBackground", select_bg)
        self.root.option_add("*Listbox.selectForeground", select_fg)
        self.root.option_add("*Listbox.disabledForeground", disabled_fg)
        self.root.option_add("*Listbox.disabledBackground", disabled_bg)

        # tk.Scrollbar - match theme
        self.root.option_add("*Scrollbar.background", panel_bg)
        self.root.option_add("*Scrollbar.troughColor", bg)
        self.root.option_add("*Scrollbar.activeBackground", border)
        self.root.option_add("*Scrollbar.arrowColor", fg)

        # tk.Menu / tk.OptionMenu dropdown
        self.root.option_add("*Menu.background", panel_bg)
        self.root.option_add("*Menu.foreground", fg)
        self.root.option_add("*Menu.borderColor", border)
        self.root.option_add("*Menu.selectColor", accent)
        self.root.option_add("*Menu.selectForeground", "#ffffff")
        self.root.option_add("*Menu.activeBackground", accent_hover)
        self.root.option_add("*Menu.activeForeground", "#ffffff")
        self.root.option_add("*Menu.disabledForeground", disabled_fg)
        self.root.option_add("*OptionMenu.background", entry_bg)
        self.root.option_add("*OptionMenu.foreground", entry_fg)

        # Explicitly set canvas background to match root so no white dead space
        self.root.option_add("*Canvas.background", bg)
        self.root.option_add("*Canvas.highlightThickness", 0)

        # Update scrollable canvases explicitly (option_add doesn't affect existing widgets)
        if hasattr(self, "pc_canvas"):
            self.pc_canvas.configure(bg=bg)
        if hasattr(self, "settings_canvas"):
            self.settings_canvas.configure(bg=bg)

        # Update all RoundedButton instances with theme colors
        RoundedButton.update_theme(
            primary_bg=accent,
            primary_fg="#ffffff",
            primary_hover=accent_hover,
            primary_pressed=accent_pressed,
            primary_disabled=primary_disabled_bg,
            primary_disabled_fg=primary_disabled_fg,
            secondary_bg=secondary_bg,
            secondary_fg=secondary_text,
            secondary_hover=secondary_hover,
            secondary_pressed=secondary_pressed,
            secondary_border=secondary_border,
            secondary_disabled=secondary_disabled_bg,
            secondary_disabled_fg=secondary_disabled_fg,
        )

    def _toggle_theme(self):
        """Toggle between light and dark theme."""
        new_theme = "dark" if self.theme_var.get() == "light" else "light"
        self.theme_var.set(new_theme)
        self.cfg["theme"] = new_theme
        save_config(self.cfg)
        self._apply_theme()

    # ------------------------------------------------------------------ UI

    def _build_ui(self):
        self.root = tk.Tk()
        # Apply Logo.ico to the window title bar and taskbar icon
        try:
            self.root.iconbitmap(resource_path("Logo.ico"))
        except Exception:
            pass
        self.root.title(f"{APP_TITLE} v{APP_VERSION}")
        self.root.geometry("780x840")
        self.root.minsize(690, 720)

        self.pad = {"padx": 10, "pady": 5}

        # --- Dark mode support ---
        self.theme_var = tk.StringVar(value=self.cfg.get("theme", "system"))
        self._apply_theme()

        # App icon + version label at top right
        try:
            _logo_img = Image.open(resource_path("Logo.png")).resize((16, 16), Image.LANCZOS)
            _logo_photo = ImageTk.PhotoImage(_logo_img)
            _logo_label = ttk.Label(self.root, image=_logo_photo)
            _logo_label.image = _logo_photo  # keep reference
            _logo_label.place(relx=1.0, x=-30, y=5, anchor="ne")
        except Exception:
            pass
        version_label = ttk.Label(self.root, text=f"v{APP_VERSION}", font=("Segoe UI", 8))
        version_label.place(relx=1.0, x=-10, y=5, anchor="ne")

        # Top-level tab bar: Price Check | Quick List | Settings
        notebook = ttk.Notebook(self.root)
        notebook.pack(fill="both", expand=True, padx=0, pady=0)

        # ---- Tab 1: Price Check (scrollable) ----
        pc_tab = ttk.Frame(notebook)
        notebook.add(pc_tab, text="Price Check")
        pc_container = ttk.Frame(pc_tab)
        pc_container.pack(fill="both", expand=True)
        self.pc_canvas = tk.Canvas(pc_container, highlightthickness=0)
        pc_vbar = ttk.Scrollbar(pc_container, orient="vertical", command=self.pc_canvas.yview)
        self.pc_canvas.configure(yscrollcommand=pc_vbar.set)
        pc_vbar.pack(side="right", fill="y")
        self.pc_canvas.pack(side="left", fill="both", expand=True)
        pc_body = ttk.Frame(self.pc_canvas)
        pc_body.bind(
            "<Configure>",
            lambda e: self.pc_canvas.configure(scrollregion=self.pc_canvas.bbox("all")),
        )
        self.pc_canvas.create_window((0, 0), window=pc_body, anchor="nw", tags="pcbody")
        self.pc_canvas.bind(
            "<Configure>",
            lambda e: self.pc_canvas.itemconfigure("pcbody", width=e.width),
        )
        # Mouse-wheel scrolling on the Price Check tab only
        def _pc_wheel(e):
            if notebook.index(notebook.select()) == 0:
                self.pc_canvas.yview_scroll(-1 * (e.delta // 120), "units")
        self.pc_canvas.bind_all("<MouseWheel>", _pc_wheel)
        pc_body.columnconfigure(0, weight=1)
        pc_body.rowconfigure(3, weight=1)

        # ---- Mode selection ----
        self.pc_mode_frame = ttk.LabelFrame(pc_body, text="How to get the price check")
        self.pc_mode_frame.grid(row=0, column=0, sticky="ew", **self.pad)
        self.pc_mode_frame.columnconfigure(1, weight=1)
        self.pc_mode_frame.columnconfigure(2, weight=1)

        self.pc_mode_var = tk.StringVar(
            value=self.price_checker.get_config().get("mode", "redirect")
        )
        ttk.Radiobutton(
            self.pc_mode_frame,
            text="Use API key (result appears in this app)",
            variable=self.pc_mode_var,
            value="api",
            command=self._on_pc_mode_changed,
        ).grid(row=0, column=0, columnspan=3, sticky="w", padx=6, pady=3)

        # API key entry (shown only in API mode)
        self.pc_api_key_frame = ttk.Frame(self.pc_mode_frame)
        self.pc_api_key_frame.grid(row=1, column=0, columnspan=3, sticky="ew", padx=20, pady=(0, 3))
        self.pc_api_key_frame.columnconfigure(1, weight=1)
        ttk.Label(self.pc_api_key_frame, text="API key:").grid(row=0, column=0, sticky="w", padx=(0, 6))
        self.pc_api_key_var = tk.StringVar(value=self.cfg.get("price_check", {}).get("api_key", ""))
        self.pc_api_key_entry = ttk.Entry(
            self.pc_api_key_frame, textvariable=self.pc_api_key_var, width=40, show="*"
        )
        self.pc_api_key_entry.grid(row=0, column=1, sticky="ew", padx=(0, 6))
        self.pc_api_show_var = tk.BooleanVar(value=False)
        self.pc_api_show_cb = ttk.Checkbutton(
            self.pc_api_key_frame,
            text="Show",
            variable=self.pc_api_show_var,
            command=self._on_pc_api_show_toggle,
        )
        self.pc_api_show_cb.grid(row=0, column=2)
        self.pc_api_fetch_btn = RoundedButton(
            self.pc_api_key_frame,
            text="Fetch Models",
            style="secondary",
            command=self._on_pc_fetch_models,
        )
        self.pc_api_fetch_btn.grid(row=0, column=3, padx=(6, 0))

        # Gemini model row (shown only in API mode + Gemini selected)
        self.pc_model_var = tk.StringVar(
            value=self.price_checker.get_config().get("gemini_model", "gemini-1.5-pro")
        )
        self.pc_model_label = ttk.Label(self.pc_api_key_frame, text="Gemini model:")
        self.pc_model_label.grid(row=1, column=0, sticky="e", padx=(0, 6))
        self.pc_model_combo = ttk.Combobox(
            self.pc_api_key_frame,
            textvariable=self.pc_model_var,
            values=[],  # populated by fetching from API
            width=18,
            state="readonly",
        )
        self.pc_model_combo.grid(row=1, column=1, columnspan=2, sticky="ew", padx=(0, 6))
        # Will be gridded/removed dynamically in _on_pc_mode_changed and _on_pc_ai_tool_changed
        self.pc_api_key_frame.grid_remove()  # hidden until API mode selected

        ttk.Radiobutton(
            self.pc_mode_frame,
            text="Redirect to AI tool (open in browser)",
            variable=self.pc_mode_var,
            value="redirect",
            command=self._on_pc_mode_changed,
        ).grid(row=2, column=0, columnspan=3, sticky="w", padx=6, pady=3)

        # AI tool selector (next to redirect option)
        self.pc_ai_tool_var = tk.StringVar()
        ai_options = self.price_checker.get_ai_tool_options()
        ai_labels = [label for _key, label, _url, _kb in ai_options]
        ai_keys = [key for key, _label, _url, _kb in ai_options]
        self._pc_ai_key_by_label = dict(zip(ai_labels, ai_keys))
        # Map stored key back to label for display
        stored_key = self.price_checker.get_config().get("ai_tool", "gemini")
        stored_label = "Gemini"
        for label, key in self._pc_ai_key_by_label.items():
            if key == stored_key:
                stored_label = label
                break
        self.pc_ai_tool_var.set(stored_label)
        self.pc_ai_tool_label = ttk.Label(self.pc_mode_frame, text="AI tool:")
        self.pc_ai_tool_label.grid(row=3, column=0, sticky="e", padx=6, pady=3)
        self.pc_ai_tool_combo = ttk.Combobox(
            self.pc_mode_frame,
            textvariable=self.pc_ai_tool_var,
            values=ai_labels,
            width=18,
            state="readonly",
        )
        self.pc_ai_tool_combo.grid(row=3, column=1, columnspan=2, sticky="ew", padx=6, pady=3)
        self.pc_ai_tool_combo.bind(
            "<<ComboboxSelected>>", lambda e: self._on_pc_ai_tool_changed()
        )

        # Custom AI tool dialog is opened when "Add custom AI tool..." is selected
        self.pc_custom_ai_name_var = tk.StringVar()
        self.pc_custom_ai_url_var = tk.StringVar()
        self.pc_custom_ai_keybind_var = tk.StringVar(value="ctrl+shift+o")
        # set initial visibility (guard: pc_model_label may not exist yet)
        if hasattr(self, "pc_model_label"):
            self._on_pc_ai_tool_changed()

        # New chat behavior
        self.pc_new_chat_var = tk.StringVar()
        new_chat_each_time = self.price_checker.get_config().get("new_chat_each_time", True)
        self.pc_new_chat_var.set(
            "New chat each time" if new_chat_each_time else "Continue same chat"
        )
        ttk.Label(self.pc_mode_frame, text="New chat behavior:").grid(
            row=4, column=0, sticky="e", padx=6, pady=3
        )
        self.pc_new_chat_combo = ttk.Combobox(
            self.pc_mode_frame,
            textvariable=self.pc_new_chat_var,
            values=["New chat each time", "Continue same chat"],
            width=18,
            state="readonly",
        )
        self.pc_new_chat_combo.grid(row=4, column=1, columnspan=2, sticky="ew", padx=6, pady=3)

        # Key bind display
        self.pc_keybind_var = tk.StringVar()
        self.pc_keybind_label = ttk.Label(
            self.pc_mode_frame,
            textvariable=self.pc_keybind_var,
            foreground="#666",
        )
        self.pc_keybind_label.grid(row=5, column=0, columnspan=3, sticky="w", padx=6, pady=3)
        self._refresh_pc_keybind_label()

        # Instructions
        ttk.Label(
            self.pc_mode_frame,
            text="Hover an item in game and press F8 to price check.",
            foreground="#666",
            font=("Segoe UI", 8),
        ).grid(row=6, column=0, columnspan=3, sticky="w", padx=6, pady=(0, 3))

        # ---- Item variables ----
        var_frame = ttk.LabelFrame(pc_body, text="Item variables (filled into the AI prompt)")
        var_frame.grid(row=1, column=0, sticky="ew", **self.pad)
        var_frame.columnconfigure(1, weight=1)

        # Platform (fixed to PC)
        ttk.Label(var_frame, text="Platform:").grid(
            row=0, column=0, sticky="w", padx=6, pady=3
        )
        self.pc_platform_var = tk.StringVar(value="PC")
        ttk.Label(var_frame, text="PC").grid(
            row=0, column=1, sticky="w", padx=6, pady=3
        )

        # Game Mode (Ladder / Non-Ladder)
        ttk.Label(var_frame, text="Game Mode:").grid(
            row=1, column=0, sticky="w", padx=6, pady=3
        )
        self.pc_ladder_var = tk.StringVar(
            value=self.price_checker.get_config().get("ladder_status", "Ladder")
        )
        ladder_frame = ttk.Frame(var_frame)
        ladder_frame.grid(row=1, column=1, sticky="w", padx=6, pady=3)
        for opt in LADDER_OPTIONS:
            ttk.Radiobutton(
                ladder_frame,
                text=opt,
                variable=self.pc_ladder_var,
                value=opt,
            ).pack(side="left", padx=(0, 8))

        # Softcore / Hardcore
        ttk.Label(var_frame, text="Softcore / Hardcore:").grid(
            row=2, column=0, sticky="w", padx=6, pady=3
        )
        self.pc_hardcore_var = tk.StringVar(
            value=self.price_checker.get_config().get("hardcore_status", "Softcore")
        )
        hc_frame = ttk.Frame(var_frame)
        hc_frame.grid(row=2, column=1, sticky="w", padx=6, pady=3)
        for opt in HARDCORE_OPTIONS:
            ttk.Radiobutton(
                hc_frame,
                text=opt,
                variable=self.pc_hardcore_var,
                value=opt,
            ).pack(side="left", padx=(0, 8))

        # Marketplaces (max 5, add/remove only - no clicking to select)
        ttk.Label(var_frame, text="Marketplaces:").grid(
            row=3, column=0, sticky="nw", padx=6, pady=3
        )
        mp_frame = ttk.Frame(var_frame)
        mp_frame.grid(row=3, column=1, sticky="ew", padx=6, pady=3)
        mp_frame.columnconfigure(0, weight=1)
        self.pc_marketplace_listbox = tk.Listbox(
            mp_frame,
            selectmode="none",
            height=4,
            exportselection=False,
        )
        self.pc_marketplace_listbox.grid(row=0, column=0, sticky="ew")
        saved_mps = self.price_checker.get_config().get("marketplaces", [])
        if not saved_mps:
            saved_mps = list(DEFAULT_MARKETPLACES)
        for mp in saved_mps:
            self.pc_marketplace_listbox.insert(tk.END, mp)
        mp_scroll = ttk.Scrollbar(
            mp_frame, orient="vertical", command=self.pc_marketplace_listbox.yview
        )
        mp_scroll.grid(row=0, column=1, sticky="ns")
        self.pc_marketplace_listbox.configure(yscrollcommand=mp_scroll.set)

        mp_btn_frame = ttk.Frame(mp_frame)
        mp_btn_frame.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(4, 0))
        self.pc_custom_marketplace_var = tk.StringVar()
        ttk.Entry(
            mp_btn_frame, textvariable=self.pc_custom_marketplace_var, width=24
        ).pack(side="left", padx=(0, 4))
        RoundedButton(
            mp_btn_frame,
            text="Add",
            command=self._on_pc_add_marketplace,
        ).pack(side="left", padx=(0, 4))
        RoundedButton(
            mp_btn_frame,
            text="Remove",
            command=self._on_pc_remove_marketplace,
        ).pack(side="left")

        # Price History Window
        ttk.Label(var_frame, text="Price History Window:").grid(
            row=4, column=0, sticky="w", padx=6, pady=3
        )
        self.pc_timeframe_var = tk.StringVar(
            value=self.price_checker.get_config().get("timeframe", "Last 7 Days")
        )
        tf_frame = ttk.Frame(var_frame)
        tf_frame.grid(row=4, column=1, sticky="w", padx=6, pady=3)
        self.pc_timeframe_combo = ttk.Combobox(
            tf_frame,
            textvariable=self.pc_timeframe_var,
            values=DEFAULT_TIMEFRAMES,
            width=18,
            state="readonly",
        )
        self.pc_timeframe_combo.pack(side="left")
        # Custom days entry (shown only when Custom is selected)
        self.pc_custom_days_var = tk.StringVar(
            value=str(self.price_checker.get_config().get("custom_timeframe_days", 7))
        )
        self.pc_custom_days_entry = ttk.Entry(
            tf_frame, textvariable=self.pc_custom_days_var, width=6
        )
        self.pc_custom_days_label = ttk.Label(tf_frame, text="days")
        self._on_pc_timeframe_changed()  # set initial visibility
        self.pc_timeframe_var.trace_add("write", lambda *_: self._on_pc_timeframe_changed())

        # ---- Key Bind Label (use hotkey to trigger price check) ----
        self.pc_bind_label_frame = ttk.Frame(pc_body)
        self.pc_bind_label_frame.grid(row=2, column=0, sticky="ew", **self.pad)
        self.pc_bind_label_frame.columnconfigure(0, weight=1)
        ttk.Label(
            self.pc_bind_label_frame,
            textvariable=self.pc_keybind_var,
            foreground="#666",
            font=("Segoe UI", 9, "bold"),
        ).grid(row=0, column=0, sticky="w", padx=6, pady=3)
        ttk.Label(
            self.pc_bind_label_frame,
            text="Hover an item in game and press the hotkey to price check.",
            foreground="#666",
            font=("Segoe UI", 8),
        ).grid(row=1, column=0, sticky="w", padx=6, pady=(0, 3))

        # ---- Result area (shown only in API mode) ----
        self.pc_result_frame = ttk.LabelFrame(pc_body, text="Price Check Result")
        self.pc_result_frame.grid(row=3, column=0, sticky="nsew", **self.pad)
        self.pc_result_frame.columnconfigure(0, weight=1)
        self.pc_result_frame.rowconfigure(0, weight=1)
        self.pc_result_text = tk.Text(
            self.pc_result_frame, height=10, width=52, wrap="word", font=("Consolas", 9)
        )
        self.pc_result_text.grid(row=0, column=0, sticky="nsew", padx=6, pady=6)
        pc_result_scroll = ttk.Scrollbar(
            self.pc_result_frame, orient="vertical", command=self.pc_result_text.yview
        )
        pc_result_scroll.grid(row=0, column=1, sticky="ns")
        self.pc_result_text.configure(yscrollcommand=pc_result_scroll.set)

        # Initially hide result area if not in API mode
        if self.pc_mode_var.get() != "api":
            self.pc_result_frame.grid_forget()

        # Initialize API key field visibility based on saved mode
        self._on_pc_mode_changed()

        self.pc_status_var = tk.StringVar(
            value="Idle. Capture a tooltip and click Price Check."
        )
        ttk.Label(
            pc_body,
            textvariable=self.pc_status_var,
            wraplength=460,
            foreground="#444",
        ).grid(row=4, column=0, sticky="ew", **self.pad)

        scanner_tab = ttk.Frame(notebook)
        notebook.add(scanner_tab, text="Quick List")
        container = ttk.Frame(scanner_tab)
        container.pack(fill="both", expand=True)
        canvas = tk.Canvas(container, highlightthickness=0)
        vbar = ttk.Scrollbar(container, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vbar.set)
        vbar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        body = ttk.Frame(canvas)
        body.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=body, anchor="nw", tags="body")
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure("body", width=e.width))
        # Mouse-wheel scrolling (scanner tab only) - now tab index 1
        def _wheel(e):
            if notebook.index(notebook.select()) == 1:
                canvas.yview_scroll(-1 * (e.delta // 120), "units")
        canvas.bind_all("<MouseWheel>", _wheel)





        self.status_var = tk.StringVar(value="Idle. Hover item and press hotkey to post.")
        ttk.Label(body, textvariable=self.status_var, wraplength=460,
                  foreground="#444").pack(fill="x", **self.pad)

        # ---- Trading site section (Quick List tab) ----
        site_frame = ttk.LabelFrame(body, text="Trading Site")
        site_frame.pack(fill="x", **self.pad)

        row1 = ttk.Frame(site_frame)
        row1.pack(fill="x", padx=8, pady=(6, 2))
        ttk.Label(row1, text="Post to:").pack(side="left")
        self.site_var = tk.StringVar()
        self.site_combo = ttk.Combobox(row1, textvariable=self.site_var, width=34, state="readonly")
        self.site_combo.pack(side="left", padx=6)

        # Inline custom site fields (shown when custom site selected)
        self.custom_row = ttk.Frame(site_frame)
        self.custom_name_var = tk.StringVar()
        self.custom_url_var = tk.StringVar()
        ttk.Label(self.custom_row, text="Name:").pack(side="left")
        ttk.Entry(self.custom_row, textvariable=self.custom_name_var, width=20).pack(side="left", padx=6)
        ttk.Label(self.custom_row, text="URL:").pack(side="left", padx=(8, 0))
        ttk.Entry(self.custom_row, textvariable=self.custom_url_var, width=30).pack(side="left", padx=6)
        RoundedButton(self.custom_row, text="Save site", command=self.on_save_custom_site).pack(side="left", padx=6)

        self._refresh_site_combo()
        self.site_combo.bind("<<ComboboxSelected>>", self.on_site_selected)
        self.open_site_btn = RoundedButton(row1, text="Open site", command=self.on_open_site)
        self.open_site_btn.pack(side="left", padx=6)

        # Main action buttons
        btn_frame = ttk.Frame(body)
        btn_frame.pack(pady=14)
        RoundedButton(btn_frame, text="Quick List Hovered Item (F9)",
                   command=self.on_quick_list).pack(pady=4)
        RoundedButton(btn_frame, text="Manual Crop + Post (F10)",
                   command=self.on_manual_crop_item).pack(pady=4)

        # Instructions
        instr = ttk.Label(body,
            text="Hover an item in game, then press F9 (or the button).\n"
                 "The tooltip is cropped, copied, and auto-pasted into the trading site's posting form.\n"
                 "Or press F10 to drag the region manually.\n"
                 "When cropping manually: drag a rectangle, then press Enter to confirm.",
            wraplength=460, foreground="#444", justify="center")
        instr.pack(pady=(10, 0))

        # Hotkeys
        hk = tk.StringVar()
        def _update_hk(*_):
            hk.set("Hotkeys: " + ", ".join(
                f"{v}={k}" for k, v in {
                    "F9": "quick_list_item",
                    "F10": "manual_crop_item",
                    "Ctrl+Alt+Q": "quit",
                }.items()))
        _update_hk()
        ttk.Label(body, textvariable=hk, wraplength=460,
                  foreground="#666", font=("Segoe UI", 8)).pack(pady=(6, 4))



        # ---- Tab 3: Settings (scrollable) ----
        settings_outer = ttk.Frame(notebook)
        notebook.add(settings_outer, text="Settings")
        settings_outer.columnconfigure(0, weight=1)
        settings_outer.rowconfigure(0, weight=1)
        self.settings_canvas = tk.Canvas(settings_outer, highlightthickness=0)
        s_vbar = ttk.Scrollbar(settings_outer, orient="vertical", command=self.settings_canvas.yview)
        self.settings_canvas.configure(yscrollcommand=s_vbar.set)
        s_vbar.pack(side="right", fill="y")
        self.settings_canvas.pack(side="left", fill="both", expand=True)
        settings_tab = ttk.Frame(self.settings_canvas)
        settings_tab.bind("<Configure>",
                          lambda e: self.settings_canvas.configure(scrollregion=self.settings_canvas.bbox("all")))
        self.settings_canvas.create_window((0, 0), window=settings_tab, anchor="nw", tags="sbody")
        self.settings_canvas.bind("<Configure>",
                      lambda e: self.settings_canvas.itemconfigure("sbody", width=e.width))
        # Mouse-wheel scrolling on the Settings tab only - now tab index 2
        def _s_wheel(e):
            if notebook.index(notebook.select()) == 2:
                self.settings_canvas.yview_scroll(-1 * (e.delta // 120), "units")
        self.settings_canvas.bind("<MouseWheel>", _s_wheel)
        settings_tab.columnconfigure(0, weight=1)
        settings_tab.rowconfigure(0, weight=0)
        settings_tab.rowconfigure(1, weight=0)
        settings_tab.rowconfigure(2, weight=0)
        settings_tab.rowconfigure(3, weight=0)
        settings_tab.rowconfigure(4, weight=1)

        # Keybinds: click a field, then press the desired key combo
        kb = ttk.LabelFrame(settings_tab, text="Keybinds (click a field, then press a key)")
        kb.grid(row=0, column=0, sticky="ew", **self.pad)
        self.bind_vars = {}
        self.bind_entries = {}
        self._bind_listener = None
        row = 0
        for name, label in [("quick_list_item", "Quick-list hovered item"),
                            ("manual_crop_item", "Manual crop + post"),
                            ("price_check_item", "Price check item"),
                            ("quit", "Quit app")]:
            ttk.Label(kb, text=label).grid(row=row, column=0, sticky="w", padx=6, pady=3)
            var = tk.StringVar(value=self._current_bind(name))
            self.bind_vars[name] = var
            entry = ttk.Entry(kb, textvariable=var, width=14, state="readonly")
            entry.grid(row=row, column=1, padx=6, pady=3)
            self.bind_entries[name] = entry
            entry.bind("<Button-1>", lambda e, n=name: self._start_bind_capture(n))
            row += 1
        RoundedButton(kb, text="Apply Keybinds", command=self.on_apply_binds).grid(
            row=row, column=0, columnspan=2, pady=6, sticky="w", padx=6)

        # Options: close behavior + start with windows
        opt = ttk.LabelFrame(settings_tab, text="App behavior")
        opt.grid(row=1, column=0, sticky="ew", **self.pad)
        self.close_var = tk.StringVar(
            value=self.cfg.get("close_behavior", "minimize"))
        ttk.Radiobutton(opt, text="On close: exit the app",
                        variable=self.close_var, value="exit").pack(anchor="w", padx=6, pady=2)
        ttk.Radiobutton(opt, text="On close: minimize to system tray",
                        variable=self.close_var, value="minimize").pack(anchor="w", padx=6, pady=2)
        self.startup_var = tk.BooleanVar(value=self.cfg.get("start_with_windows", False))
        ttk.Checkbutton(opt, text="Start with Windows",
                        variable=self.startup_var).pack(anchor="w", padx=6, pady=2)
        self.manual_override_var = tk.BooleanVar(
            value=self.cfg.get("manual_screenshot_override", False))
        ttk.Checkbutton(opt, text="Manual screenshot override "
                        "(quick-list always opens region selection)",
                        variable=self.manual_override_var,
                        command=self._on_toggle_manual_override
                        ).pack(anchor="w", padx=6, pady=(2, 6))
        RoundedButton(opt, text="Apply", command=self.on_apply_options).pack(
            anchor="w", padx=6, pady=(0, 6))

        # Theme: light / dark / system default
        theme_frame = ttk.LabelFrame(settings_tab, text="Appearance")
        theme_frame.grid(row=3, column=0, sticky="ew", **self.pad)
        self.theme_combo_var = tk.StringVar()
        saved_theme = self.cfg.get("theme", "system")
        if saved_theme == "system":
            self.theme_combo_var.set("System Default (Auto)")
        elif saved_theme == "dark":
            self.theme_combo_var.set("Dark")
        else:
            self.theme_combo_var.set("Light")
        ttk.Label(theme_frame, text="Theme:").grid(row=0, column=0, sticky="w", padx=6, pady=3)
        ttk.OptionMenu(
            theme_frame,
            self.theme_combo_var,
            self.theme_combo_var.get(),
            "System Default (Auto)",
            "Light",
            "Dark",
            command=self._on_theme_combo_changed,
        ).grid(row=0, column=1, sticky="w", padx=6, pady=3)

        # Capture source: which window/screen the tool captures from
        src_frame = ttk.LabelFrame(settings_tab, text="Capture source")
        src_frame.grid(row=2, column=0, sticky="ew", **self.pad)
        ttk.Label(src_frame, text="Window to capture from (matched by title):",
                  wraplength=430).pack(anchor="w", padx=6, pady=(4, 0))
        self.source_combo = ttk.Combobox(src_frame, width=44)
        self.source_combo.pack(anchor="w", padx=6, pady=2)
        self.source_combo.set(self.cfg.get("capture_window_title") or "diablo")
        btn_row = ttk.Frame(src_frame)
        btn_row.pack(anchor="w", padx=6, pady=(0, 6))
        RoundedButton(btn_row, text="Refresh list",
                   command=self.on_refresh_windows).pack(side="left")
        RoundedButton(btn_row, text="Apply Capture Source", command=self.on_apply_capture_source).pack(side="left", padx=6)

    def on_refresh_windows(self):
        from capture import list_windows
        titles = [w["title"] for w in list_windows()]
        self.source_combo["values"] = titles
        self._set_status(f"Found {len(titles)} open windows - pick one and Apply.")

    def on_apply_capture_source(self):
        choice = self.source_combo.get().strip()
        self.cfg["capture_window_title"] = choice or "diablo"
        save_config(self.cfg)
        import capture
        capture._cfg = load_config()  # reload so the matcher sees the change
        found = capture.game_window_found()
        self._set_status(f"Capture source set to '{self.cfg['capture_window_title']}'"
                         + ("" if found else " (no matching window open right now)"))


    def _refresh_site_combo(self):
        """Refresh the trading site combobox with built-in and custom sites."""
        from config import trading_site_options
        opts = trading_site_options(self.cfg)
        self.site_combo['values'] = [label for key, label in opts]
        
        # Set current selection
        from config import get_trading_url
        key, url = get_trading_url(self.cfg)
        # Find the label for this key
        found = False
        for k, label in opts:
            if k == key:
                self.site_var.set(label)
                found = True
                # Show/hide custom row based on selection
                if k.startswith("custom:"):
                    customs = self.cfg.get("trading_site", {}).get("custom_sites", [])
                    for cs in customs:
                        if cs.get("key") == k:
                            self.custom_name_var.set(cs.get("name", ""))
                            self.custom_url_var.set(cs.get("url", ""))
                            break
                    self.custom_row.pack(fill="x", padx=8, pady=(2, 6))
                else:
                    self.custom_row.pack_forget()
                break
        if not found:
            # Fallback to first item if not found
            if opts:
                self.site_var.set(opts[0][1])

    def on_site_selected(self, event=None):
        """Handle trading site selection change."""
        from config import trading_site_options, add_custom_site, get_trading_url
        label = self.site_var.get()
        opts = trading_site_options(self.cfg)
        
        # Find the key for this label
        selected_key = None
        for key, lbl in opts:
            if lbl == label:
                selected_key = key
                break
        
        if selected_key:
            if selected_key.startswith("custom:"):
                # Custom site selected - show inline fields and populate
                self.cfg["trading_site"]["site"] = selected_key
                customs = self.cfg.get("trading_site", {}).get("custom_sites", [])
                for cs in customs:
                    if cs.get("key") == selected_key:
                        self.custom_name_var.set(cs.get("name", ""))
                        self.custom_url_var.set(cs.get("url", ""))
                        break
                self.custom_row.pack(fill="x", padx=8, pady=(2, 6))
            else:
                # Built-in site
                self.cfg["trading_site"]["site"] = selected_key
                self.custom_row.pack_forget()
            # Sync the selected site's URL into cfg["diablo2io"]["posting_url"]
            # so downstream code can still read it if needed.
            _, url = get_trading_url(self.cfg)
            self.cfg.setdefault("diablo2io", {})["posting_url"] = url
            save_config(self.cfg)
            self._set_status(f"Trading site set to {label}")

    def on_open_site(self):
        """Open the currently selected trading site URL in the default browser."""
        from config import get_trading_url
        import os
        _, url = get_trading_url(self.cfg)
        if url:
            os.startfile(url)
            self._set_status(f"Opened {url}")
        else:
            self._set_status("No trading site URL configured")

    def on_save_custom_site(self):
        """Save the custom site name/URL from the inline fields."""
        name = self.custom_name_var.get().strip()
        url = self.custom_url_var.get().strip()
        if not name:
            messagebox.showerror("Error", "Please enter a site name")
            return
        if not url:
            messagebox.showerror("Error", "Please enter a URL")
            return
        if not url.lower().startswith(("http://", "https://")):
            url = "https://" + url
        try:
            from config import add_custom_site, get_trading_url
            key = add_custom_site(self.cfg, name, url)
            save_config(self.cfg)
            self._refresh_site_combo()
            self.cfg["trading_site"]["site"] = key
            _, resolved_url = get_trading_url(self.cfg)
            self.cfg.setdefault("diablo2io", {})["posting_url"] = resolved_url
            save_config(self.cfg)
            self.custom_row.pack_forget()
            self._set_status(f"Custom site '{name}' saved and selected")
        except Exception as e:
            messagebox.showerror("Error", f"Failed to save site: {e}")

    def on_manage_custom_sites(self):
        """Open dialog to manage custom trading sites."""
        self._custom_site_dialog()

    def _custom_site_dialog(self):
        """Dialog to add/edit custom trading sites."""
        dialog = tk.Toplevel(self.root)
        dialog.title("Manage Custom Sites")
        dialog.geometry("400x300")
        dialog.transient(self.root)
        dialog.grab_set()
        
        # Center dialog on parent
        dialog.geometry("+%d+%d" % (self.root.winfo_rootx() + 50,
                                   self.root.winfo_rooty() + 50))
        
        # Main frame
        main_frame = ttk.Frame(dialog)
        main_frame.pack(fill="both", expand=True, padx=10, pady=10)
        
        # Instructions
        ttk.Label(main_frame, text="Custom Trading Sites:").pack(anchor="w", pady=(0, 5))
        
        # Listbox with scrollbar
        list_frame = ttk.Frame(main_frame)
        list_frame.pack(fill="both", expand=True, pady=(0, 10))
        
        self.custom_sites_listbox = tk.Listbox(list_frame, height=8)
        self.custom_sites_listbox.pack(side="left", fill="both", expand=True)
        
        scrollbar = ttk.Scrollbar(list_frame, orient="vertical", command=self.custom_sites_listbox.yview)
        scrollbar.pack(side="right", fill="y")
        self.custom_sites_listbox.config(yscrollcommand=scrollbar.set)
        
        # Buttons
        btn_frame = ttk.Frame(main_frame)
        btn_frame.pack(fill="x", pady=(0, 10))
        
        RoundedButton(btn_frame, text="Add", command=lambda: self._edit_custom_site(dialog, None)).pack(side="left", padx=(0, 5))
        RoundedButton(btn_frame, text="Edit", command=lambda: self._edit_custom_site(dialog, "edit")).pack(side="left", padx=5)
        RoundedButton(btn_frame, text="Delete", command=self._delete_custom_site).pack(side="left", padx=5)
        
        # Close button
        RoundedButton(main_frame, text="Close", command=dialog.destroy).pack(pady=10)
        
        # Load initial data
        self._refresh_custom_sites_list()


    def _refresh_custom_sites_list(self):
        """Refresh the custom sites listbox."""
        self.custom_sites_listbox.delete(0, tk.END)
        customs = self.cfg.get("trading_site", {}).get("custom_sites", [])
        for site in customs:
            name = site.get("name", "Unnamed")
            url = site.get("url", "No URL")
            display = f"{name}: {url}"
            self.custom_sites_listbox.insert(tk.END, display)

    def _edit_custom_site(self, dialog, action=None):
        """Dialog to add or edit a custom site."""
        selection = self.custom_sites_listbox.curselection()
        index = selection[0] if selection else None
        
        # Get existing data if editing
        existing = {}
        if index is not None and action == "edit":
            customs = self.cfg.get("trading_site", {}).get("custom_sites", [])
            if 0 <= index < len(customs):
                existing = customs[index]
        
        # Create edit dialog
        edit_dialog = tk.Toplevel(dialog)
        edit_dialog.title("Add Custom Site" if index is None else "Edit Custom Site")
        edit_dialog.geometry("350x150")
        edit_dialog.transient(dialog)
        edit_dialog.grab_set()
        
        # Center on parent
        edit_dialog.geometry("+%d+%d" % (dialog.winfo_rootx() + 50,
                                        dialog.winfo_rooty() + 50))
        
        # Form
        form_frame = ttk.Frame(edit_dialog)
        form_frame.pack(fill="both", expand=True, padx=20, pady=20)
        
        ttk.Label(form_frame, text="Site Name:").grid(row=0, column=0, sticky="w", pady=5)
        name_var = tk.StringVar(value=existing.get("name", ""))
        ttk.Entry(form_frame, textvariable=name_var, width=30).grid(row=0, column=1, sticky="ew", pady=5, padx=(10, 0))
        
        ttk.Label(form_frame, text="URL:").grid(row=1, column=0, sticky="w", pady=5)
        url_var = tk.StringVar(value=existing.get("url", ""))
        ttk.Entry(form_frame, textvariable=url_var, width=30).grid(row=1, column=1, sticky="ew", pady=5, padx=(10, 0))
        
        # Button frame
        btn_frame = ttk.Frame(form_frame)
        btn_frame.grid(row=2, column=0, columnspan=2, pady=20)
        
        def save_site():
            name = name_var.get().strip()
            url = url_var.get().strip()
            
            if not name:
                messagebox.showerror("Error", "Please enter a site name", parent=edit_dialog)
                return
            if not url:
                messagebox.showerror("Error", "Please enter a URL", parent=edit_dialog)
                return
            
            if not url.lower().startswith(("http://", "https://")):
                url = "https://" + url
            
            try:
                from config import add_custom_site
                key = add_custom_site(self.cfg, name, url)
                save_config(self.cfg)
                self._refresh_site_combo()
                self._refresh_custom_sites_list()
                messagebox.showinfo("Success", f"Site '{name}' saved successfully", parent=edit_dialog)
                edit_dialog.destroy()
            except Exception as e:
                messagebox.showerror("Error", f"Failed to save site: {e}", parent=edit_dialog)
        
        RoundedButton(btn_frame, text="Save", command=save_site).pack(side="left", padx=(0, 10))
        RoundedButton(btn_frame, text="Cancel", command=edit_dialog.destroy).pack(side="left")
        
        # Configure grid weights
        form_frame.columnconfigure(1, weight=1)

    def _delete_custom_site(self):
        """Delete the selected custom site."""
        selection = self.custom_sites_listbox.curselection()
        if not selection:
            messagebox.showwarning("Warning", "Please select a site to delete")
            return
        
        index = selection[0]
        customs = self.cfg.get("trading_site", {}).get("custom_sites", [])
        if 0 <= index < len(customs):
            site = customs[index]
            name = site.get("name", "Unnamed")
            
            if messagebox.askyesno("Confirm Delete", f"Delete custom site '{name}'?"):
                del customs[index]
                save_config(self.cfg)
                self._refresh_site_combo()
                self._refresh_custom_sites_list()


    # ------------------------------------------------------------- Actions
    # ------------------------------------------------------------- Actions

    # ------------------------------------------------------------- Actions

    def _current_bind(self, name):
        combo = self.cfg["hotkeys"].get(name, "F7")
        return combo.title() if "+" not in combo else combo

    def _start_bind_capture(self, name):
        """Click-to-capture: wait for the user's next keystroke and record it."""
        if self._bind_listener is not None:
            try:
                self._bind_listener.stop()
            except Exception:
                pass
            self._bind_listener = None
        var = self.bind_vars[name]
        entry = self.bind_entries[name]
        var.set("Press a key... (Esc cancels)")
        pressed_mods = set()
        finished = [False]

        from pynput import keyboard

        def _finish(combo):
            finished[0] = True
            listener.stop()
            self._bind_listener = None

            def _apply():
                var.set(combo)
                self.on_apply_binds_silent(name, combo)
            try:
                self.root.after(0, _apply)
            except RuntimeError:
                pass

        def on_press(key):
            try:
                kname = key.name or ""
            except AttributeError:
                kname = key.char or ""
            kname = kname.lower()
            # Normalize modifier variants
            base = kname.replace("_l", "").replace("_r", "").replace("_gr", "")
            if base in ("ctrl", "control", "alt", "shift", "cmd", "win"):
                pressed_mods.add({"control": "ctrl"}.get(base, base))
                return
            if kname == "escape":
                # cancel: restore old value
                listener.stop()
                self._bind_listener = None

                def _restore():
                    var.set(self._current_bind(name))
                try:
                    self.root.after(0, _restore)
                except RuntimeError:
                    pass
                return
            # A non-modifier key was pressed -> build the combo
            key_part = kname if len(kname) == 1 else kname
            mods = [m for m in ("ctrl", "alt", "shift") if m in pressed_mods]
            combo = "+".join(mods + [key_part])
            _finish(combo)

        listener = keyboard.Listener(on_press=on_press)
        self._bind_listener = listener
        listener.daemon = True
        listener.start()

    def on_apply_binds_silent(self, name, combo):
        """Apply a single captured keybind immediately (no dialogs)."""
        from hotkeys import validate_combo
        ok, err = validate_combo(combo)
        if not ok:
            messagebox.showerror("Keybinds", f"Cannot use '{combo}': {err}")
            var = self.bind_vars[name]
            var.set(self._current_bind(name))
            return
        self.cfg["hotkeys"][name] = combo
        save_config(self.cfg)
        self._start_hotkeys()
        self._set_status(f"Keybind set: {name} = {combo}")

    # ------------------------------------------------ Character profiles

    def _refresh_char_list(self):
        import profiles
        names = [c["name"] for c in profiles.list_characters()]
        self.char_combo["values"] = names

    def on_select_character(self, event=None):
        import profiles
        name = self.char_combo.get()
        if not name:
            return
        profiles.set_active(name)
        char = profiles.get_active()
        if char:
            self.scanner.start_character_session(char)
            self._refresh_item_log()
            self._set_status(f"Active character: {char['name']}"
                             + (f" (L{char['level']} {char['class']})"
                                if char.get("level") else ""))

    def _character_dialog(self, title, name="", klass="", on_ok=None):
        """Shared name/class dialog (level doesn't matter for trading)."""
        dlg = tk.Toplevel(self.root)
        dlg.title(title)
        dlg.transient(self.root)
        dlg.grab_set()
        frm = ttk.Frame(dlg)
        frm.pack(padx=10, pady=10)
        ttk.Label(frm, text="Name:").grid(row=0, column=0, sticky="w", pady=3)
        name_var = tk.StringVar(value=name)
        ttk.Entry(frm, textvariable=name_var, width=24).grid(row=0, column=1, pady=3)
        ttk.Label(frm, text="Class:").grid(row=1, column=0, sticky="w", pady=3)
        class_var = tk.StringVar(value=klass)
        ttk.Combobox(frm, textvariable=class_var, width=22, state="readonly",
                     values=["Amazon", "Sorceress", "Necromancer", "Paladin",
                             "Barbarian", "Druid", "Assassin",
                             "Warlock"]).grid(row=1, column=1, pady=3)

        def _ok():
            new_name = name_var.get().strip()
            if not new_name:
                return
            if on_ok:
                on_ok(new_name, class_var.get().strip() or None)
            dlg.destroy()

        RoundedButton(frm, text="OK", command=_ok).grid(row=3, column=0,
                                                      columnspan=2, pady=8)

    def on_new_character(self):
        def _create(name, klass):
            import profiles
            profiles.create_character(name, klass)
            self._refresh_char_list()
            self.char_combo.set(name)
            self.on_select_character()
        self._character_dialog("New Character", on_ok=_create)

    def on_edit_character(self):
        import profiles
        name = self.char_combo.get().strip()
        if not name:
            self._set_status("Select a character to edit first.")
            return
        current = next((c for c in profiles.list_characters()
                        if c["name"] == name), None)
        if not current:
            self._set_status("Selected character has no profile to edit.")
            return

        def _update(new_name, klass):
            updated = profiles.update_character(name, new_name, klass)
            if updated is None:
                self._set_status(f"Could not update '{name}' - is '{new_name}' "
                                "already used by another character?")
                return
            self._refresh_char_list()
            self.char_combo.set(updated["name"])
            self.on_select_character()

        self._character_dialog("Edit Character", name=current["name"],
                               klass=current.get("class") or "", on_ok=_update)

    # ------------------------------------------------ Debug capture








    def on_apply_binds(self):
        from hotkeys import validate_combo
        # Validate ALL combos before saving anything
        for name, var in self.bind_vars.items():
            combo = var.get().strip().lower()
            if not combo:
                messagebox.showerror("Keybinds", f"Empty keybind for '{name}'")
                return
            ok, err = validate_combo(combo)
            if not ok:
                messagebox.showerror("Keybinds",
                                     f"Invalid combo '{combo}' for '{name}':\n{err}")
                return
        for name, var in self.bind_vars.items():
            self.cfg["hotkeys"][name] = var.get().strip().lower()
        save_config(self.cfg)
        self._start_hotkeys()
        self._set_status("Keybinds saved and applied.")

    def on_apply_options(self):
        from hotkeys import set_start_with_windows
        self.cfg["close_behavior"] = self.close_var.get()
        self.cfg["minimize_to_tray"] = self.close_var.get() == "minimize"
        # User explicitly set close behavior, so the first-time prompt
        # should not re-ask on next window close.
        self.cfg["close_behavior_prompted"] = True
        self.cfg["start_with_windows"] = bool(self.startup_var.get())
        self.cfg["manual_screenshot_override"] = bool(self.manual_override_var.get())
        save_config(self.cfg)
        if not set_start_with_windows(self.startup_var.get()):
            messagebox.showwarning("Options",
                                   "Could not update the Windows startup entry.")
        self._set_status("Options saved.")

    # -- Manual crop helpers ---------------------------------------------------

    # -- Manual crop helpers ---------------------------------------------------

    def _open_crop_dialog(self, img_bgr, title, on_done):
        """Schedule the region-selection dialog on the Tk main thread."""
        def _build():
            try:
                CropDialog(self.root, img_bgr, title, on_done)
            except Exception as e:
                _log_error(f"CropDialog failed: {e}")
                try:
                    on_done(None)
                except Exception:
                    pass
        try:
            self.root.after(0, _build)
        except RuntimeError:
            on_done(None)

    def _manual_crop_blocking(self, title):
        """Fresh game screenshot -> user drags the region -> crop (or None).

        Blocks the calling worker thread until the dialog is answered; the
        dialog itself runs on the Tk main thread (Tk is not thread-safe)."""
        from capture import capture_full_window
        img, _rect = capture_full_window()
        if img is None:
            self._set_status("Diablo window not found - open the game first")
            return None
        done = threading.Event()
        holder = {}

        def _done(crop):
            holder["crop"] = crop
            done.set()

        self._open_crop_dialog(img, title, _done)
        while not done.wait(timeout=0.5):
            try:
                if not self.root.winfo_exists():  # app closed under us
                    return None
            except RuntimeError:
                return None
        return holder.get("crop")

    def _post_crop(self, img, source_desc):
        """Open the posting page IMMEDIATELY, then encode the clipboard and
        auto-paste in the background. The browser needs seconds to load the
        page, so the clipboard work (a few hundred ms) is completely hidden
        off the critical path - the page opens as early as possible."""
        import cv2
        from PIL import Image
        from config import get_trading_url
        _, url = get_trading_url(self.cfg)
        os.startfile(url)                       # browser starts NOW
        focus_browser_window(url)               # bring the tab to the foreground
        pil = Image.fromarray(cv2.cvtColor(img, cv2.COLOR_BGR2RGB))
        self._set_status(f"{source_desc} - posting page opened")

        def _clipboard_then_paste():
            try:
                if not copy_image_to_clipboard(pil):
                    self._set_status(f"Opened {url} - clipboard copy failed; "
                                     "check that pywin32 is installed")
                    return
                from hotkeys import focus_and_paste
                focus_and_paste(url)            # waits for the page, then pastes
            except Exception as e:
                self._set_status(f"Quick-list failed: {e}")

        threading.Thread(target=_clipboard_then_paste, daemon=True).start()

        def _ocr_later():
            try:
                item = self.scanner.parse_item_from_image(img)
                if item.get("name") and item["name"] != "Unknown":
                    self.recorder._merge_item(item)
                    self._refresh_item_log()
            except Exception:
                pass
        threading.Thread(target=_ocr_later, daemon=True).start()

    def on_quick_list(self):
        """Hotkey: capture hovered tooltip, copy image, open diablo2.io posting.
        The screenshot is captured and copied BEFORE the site opens; OCR runs
        afterwards only to update the local item list."""
        def _run():
            try:
                manual = False
                if self.cfg.get("manual_screenshot_override"):
                    img = self._manual_crop_blocking(
                        "Manual override - drag the tooltip area to post")
                    manual = True
                else:
                    img = self.scanner.capture_item_for_posting()
                    if img is None or img.size == 0:
                        from capture import game_window_found, last_yolo_capture
                        if not game_window_found():
                            self._set_status("Diablo window not found - open the game "
                                            "first (the tool only captures from D2)")
                            return
                        why = last_yolo_capture.get("reason", "no tooltip found")
                        self._set_status(f"Auto-capture failed ({why}) - "
                                         "select the area manually...")
                        img = self._manual_crop_blocking(
                            f"Auto-detect failed ({why}) - drag the tooltip area")
                        manual = True
                if img is None or img.size == 0:
                    self._set_status("Cancelled - nothing was posted. Tip: run "
                                     "collect_missed_tooltip.py to teach the model "
                                     "this item type.")
                    return
                if manual:
                    desc = f"Manual crop ({img.shape[1]}x{img.shape[0]}px)"
                else:
                    from capture import last_yolo_capture
                    c = last_yolo_capture
                    desc = (f"Tooltip cropped by YOLO v6 (conf {c['conf']:.2f}, "
                            f"{c['shape'][0]}x{c['shape'][1]}px)"
                            if c.get("conf") else "Tooltip captured")
                self._post_crop(img, desc)
            except Exception as e:
                self._set_status(f"Quick-list failed: {e}")
        threading.Thread(target=_run, daemon=True).start()

    def on_manual_crop_item(self):
        """Hotkey (default F10): manual crop & post - the user selects the
        region on a fresh screenshot; no auto-detection involved."""
        def _run():
            try:
                img = self._manual_crop_blocking(
                    "Manual crop - drag the area to post")
                if img is None or img.size == 0:
                    self._set_status("Manual crop cancelled - nothing was posted")
                    return
                self._post_crop(img, f"Manual crop ({img.shape[1]}x{img.shape[0]}px)")
            except Exception as e:
                self._set_status(f"Manual crop failed: {e}")
        threading.Thread(target=_run, daemon=True).start()

    # ------------------------------------------------ Price Check handlers

    def _refresh_pc_keybind_label(self):
        """Show the current price check key bind next to the button."""
        bind = self.cfg.get("hotkeys", {}).get("price_check_item", "f8")
        self.pc_keybind_var.set(f"Key bind: {bind.upper()}")

    def _on_theme_combo_changed(self, selection):
        """Handle theme dropdown change in Settings."""
        if selection == "System Default (Auto)":
            # Save "system" so it stays dynamic across restarts
            new_theme = "system"
            self.theme_var.set("system")
        else:
            new_theme = "dark" if selection == "Dark" else "light"
            self.theme_var.set(new_theme)
        self.cfg["theme"] = new_theme
        save_config(self.cfg)
        self._apply_theme()

    def _detect_system_theme(self):
        """Detect Windows system theme preference. Falls back to 'light'."""
        try:
            import winreg
            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
            )
            value, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
            winreg.CloseKey(key)
            return "light" if value == 1 else "dark"
        except Exception:
            return "light"

    def _on_pc_mode_changed(self):
        """Show/hide API key field and result area based on selected mode."""
        mode = self.pc_mode_var.get()
        is_api = mode == "api"
        if is_api:
            self.pc_api_key_frame.grid(row=1, column=0, columnspan=3, sticky="ew", padx=20, pady=(0, 3))
            self.pc_result_frame.grid(row=3, column=0, sticky="ew", **self.pad)
        else:
            self.pc_api_key_frame.grid_remove()
            self.pc_result_frame.grid_forget()
        # Model selector only valid in API mode (guard for init-time call
        # before pc_model_label is created)
        if hasattr(self, "pc_model_label"):
            self._on_pc_ai_tool_changed()

    def _on_pc_api_show_toggle(self):
        """Toggle API key visibility in the Price Check entry."""
        show = self.pc_api_show_var.get()
        self.pc_api_key_entry.configure(show="" if show else "*")

    def _on_pc_fetch_models(self):
        """Fetch available Gemini models using the entered API key."""
        import urllib.request, urllib.error, json
        api_key = self.pc_api_key_var.get().strip()
        if not api_key:
            self.pc_status_var.set("Enter an API key first.")
            return
        self.pc_status_var.set("Fetching models from Gemini API...")
        self.root.update_idletasks()

        def _fetch():
            url = f"https://generativelanguage.googleapis.com/v1/models?key={api_key}"
            try:
                req = urllib.request.Request(url)
                with urllib.request.urlopen(req, timeout=15) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                models = [m["name"].replace("models/", "") for m in data.get("models", []) if "generateContent" in m.get("supportedGenerationMethods", [])]
                models.sort()
                if not models:
                    models = ["gemini-1.5-pro", "gemini-1.5-flash"]
                self.root.after(0, lambda: self._on_models_fetched(models, None))
            except urllib.error.HTTPError as e:
                body = e.read().decode("utf-8")
                self.root.after(0, lambda: self._on_models_fetched([], f"HTTP {e.code}: {body[:120]}"))
            except Exception as e:
                self.root.after(0, lambda: self._on_models_fetched([], str(e)))

        import threading
        threading.Thread(target=_fetch, daemon=True).start()

    def _on_models_fetched(self, models, error):
        """Callback after model fetch completes."""
        if error:
            self.pc_status_var.set(f"Model fetch failed: {error}")
            self.pc_model_combo["values"] = []
        else:
            # Always ensure vision-capable models are available as defaults.
            # The API may return models that don't support image input (e.g.
            # antigravity-preview-*), so prepend known-working ones.
            safe_defaults = ["gemini-1.5-pro", "gemini-1.5-flash"]
            deduped = []
            for m in safe_defaults + models:
                if m not in deduped:
                    deduped.append(m)
            self.pc_model_combo["values"] = deduped
            # Default to gemini-1.5-pro (known to support images)
            current = self.pc_model_var.get()
            if current not in deduped and deduped:
                self.pc_model_var.set(deduped[0])
                self.cfg.setdefault("price_check", {})["gemini_model"] = deduped[0]
            self.pc_status_var.set(f"Loaded {len(deduped)} models.")

    def _on_pc_add_marketplace(self):
        """Add a user-typed marketplace to the listbox (max 5)."""
        name = self.pc_custom_marketplace_var.get().strip()
        if not name:
            return
        # Avoid duplicates
        existing = self.pc_marketplace_listbox.get(0, tk.END)
        if name in existing:
            self._set_status(f"'{name}' is already in the marketplace list.")
            return
        # Limit to 5
        if self.pc_marketplace_listbox.size() >= 5:
            self._set_status("Maximum of 5 marketplaces reached.")
            return
        self.pc_marketplace_listbox.insert(tk.END, name)
        self.pc_custom_marketplace_var.set("")
        self._set_status(f"Added marketplace: {name}")

    def _on_pc_remove_marketplace(self):
        """Remove the currently selected marketplace from the listbox."""
        sel = self.pc_marketplace_listbox.curselection()
        if not sel:
            self._set_status("Select a marketplace to remove.")
            return
        name = self.pc_marketplace_listbox.get(sel[0])
        self.pc_marketplace_listbox.delete(sel[0])
        self._set_status(f"Removed marketplace: {name}")

    def _on_pc_timeframe_changed(self):
        """Show/hide the custom days entry based on timeframe selection."""
        is_custom = self.pc_timeframe_var.get() == "Custom"
        if is_custom:
            self.pc_custom_days_entry.pack(side="left", padx=(6, 0))
            self.pc_custom_days_label.pack(side="left", padx=(0, 4))
        else:
            self.pc_custom_days_entry.pack_forget()
            self.pc_custom_days_label.pack_forget()

    def _on_pc_ai_tool_changed(self):
        """Open a dialog to add a custom AI tool when selected from dropdown.
        Also show/hide the Gemini model selector based on the AI tool choice."""
        selected = self.pc_ai_tool_var.get()
        if selected == "Add custom AI tool...":
            # Reset selection so user can pick a real tool after adding custom one
            self.pc_ai_tool_var.set(self.price_checker.get_config().get("ai_tool", "gemini"))
            self._open_custom_ai_tool_dialog()
            return

        # Model selector only shown in API mode + Gemini selected
        is_api = self.pc_mode_var.get() == "api"
        is_gemini = selected == "Gemini"
        if is_api and is_gemini:
            self.pc_model_label.grid(row=5, column=0, sticky="e", padx=6, pady=3)
            self.pc_model_combo.grid(row=5, column=1, columnspan=2, sticky="ew", padx=6, pady=3)
            # Save the selected model to config
            self.cfg.setdefault("price_check", {})["gemini_model"] = self.pc_model_var.get()
        else:
            self.pc_model_label.grid_remove()
            self.pc_model_combo.grid_remove()

    def _on_pc_model_changed(self, *args):
        """Save the selected Gemini model to config."""
        self.cfg.setdefault("price_check", {})["gemini_model"] = self.pc_model_var.get()
        save_config(self.cfg)

    def _open_custom_ai_tool_dialog(self):
        """Open a modal dialog to add a custom AI tool (name + URL + keybind)."""
        dialog = tk.Toplevel(self.root)
        dialog.title("Add Custom AI Tool")
        dialog.transient(self.root)
        dialog.grab_set()
        dialog.resizable(False, False)

        # Center on parent
        dialog.update_idletasks()
        x = self.root.winfo_rootx() + 50
        y = self.root.winfo_rooty() + 50
        dialog.geometry(f"+{x}+{y}")

        frame = ttk.Frame(dialog, padding=20)
        frame.pack(fill="both", expand=True)

        ttk.Label(frame, text="AI Tool Name:").grid(row=0, column=0, sticky="w", pady=(0, 5))
        name_entry = ttk.Entry(frame, textvariable=self.pc_custom_ai_name_var, width=40)
        name_entry.grid(row=1, column=0, sticky="ew", pady=(0, 10))
        name_entry.focus_set()

        ttk.Label(frame, text="URL (e.g. https://example.com):").grid(row=2, column=0, sticky="w", pady=(0, 5))
        url_entry = ttk.Entry(frame, textvariable=self.pc_custom_ai_url_var, width=40)
        url_entry.grid(row=3, column=0, sticky="ew", pady=(0, 10))

        ttk.Label(frame, text="New chat keybind (e.g. ctrl+shift+o):").grid(row=4, column=0, sticky="w", pady=(0, 5))
        keybind_entry = ttk.Entry(frame, textvariable=self.pc_custom_ai_keybind_var, width=40)
        keybind_entry.grid(row=5, column=0, sticky="ew", pady=(0, 20))

        frame.columnconfigure(0, weight=1)

        btn_frame = ttk.Frame(frame)
        btn_frame.grid(row=6, column=0, sticky="e")

        def _save():
            name = self.pc_custom_ai_name_var.get().strip()
            url = self.pc_custom_ai_url_var.get().strip()
            keybind = self.pc_custom_ai_keybind_var.get().strip()
            if not name or not url:
                self._set_status("Please enter both name and URL.")
                return
            try:
                key = self.price_checker.save_custom_ai_tool(name, url, keybind)
                # Refresh dropdown
                ai_options = self.price_checker.get_ai_tool_options()
                ai_labels = [label for _key, label, _url, _kb in ai_options]
                ai_keys = [key for key, _label, _url, _kb in ai_options]
                self.pc_ai_tool_combo["values"] = ai_labels
                self._pc_ai_key_by_label = dict(zip(ai_labels, ai_keys))
                for lbl, k in zip(ai_labels, ai_keys):
                    if k == key:
                        self.pc_ai_tool_var.set(lbl)
                        break
                dialog.destroy()
                self._set_status(f"Custom AI tool '{name}' saved.")
            except ValueError as e:
                self._set_status(str(e))

        RoundedButton(btn_frame, text="Save", command=_save).pack(side="left", padx=(0, 10))
        RoundedButton(btn_frame, text="Cancel", command=dialog.destroy).pack(side="left")

        dialog.wait_window(dialog)

    def on_price_check(self):
        """Gather user selections, fill the prompt, capture the tooltip,
        and run the price check (API or redirect mode)."""
        pc_cfg = self.price_checker.get_config()

        # Persist current selections to config
        pc_cfg["mode"] = self.pc_mode_var.get()
        ai_label = self.pc_ai_tool_var.get()
        pc_cfg["ai_tool"] = self._pc_ai_key_by_label.get(ai_label, "gemini")
        pc_cfg["api_key"] = self.pc_api_key_var.get().strip()
        pc_cfg["platform"] = "PC"  # always fixed to PC
        pc_cfg["ladder_status"] = self.pc_ladder_var.get()
        pc_cfg["hardcore_status"] = self.pc_hardcore_var.get()
        # All items in the listbox (no selection-based filtering)
        pc_cfg["marketplaces"] = list(self.pc_marketplace_listbox.get(0, tk.END))
        if not pc_cfg["marketplaces"]:
            pc_cfg["marketplaces"] = list(DEFAULT_MARKETPLACES)
        tf = self.pc_timeframe_var.get()
        pc_cfg["timeframe"] = tf
        if tf == "Custom":
            try:
                days = int(self.pc_custom_days_var.get().strip())
                if days < 1:
                    days = 1
            except ValueError:
                days = 7
            pc_cfg["custom_timeframe_days"] = days
            pc_cfg["timeframe"] = f"Last {days} Days"
        # Save new chat behavior preference
        pc_cfg["new_chat_each_time"] = (
            self.pc_new_chat_var.get() == "New chat each time"
        )
        self.price_checker.save()

        # Build variables dict for prompt substitution
        variables = {
            "Game Mode": pc_cfg["ladder_status"],
            "Softcore / Hardcore": pc_cfg["hardcore_status"],
            "selected_trading_websites": ", ".join(pc_cfg["marketplaces"]),
            "Price History Window": pc_cfg["timeframe"],
        }

        mode = pc_cfg["mode"]
        ai_tool = pc_cfg["ai_tool"]
        api_key = pc_cfg["api_key"]

        # Only show result text in API mode; redirect mode just opens the browser
        if mode == "api":
            self.pc_result_text.delete("1.0", tk.END)
            self.pc_result_text.insert(tk.END, "Running price check...\n")
        self.pc_status_var.set("Capturing tooltip and contacting AI...")

        def _on_result(success, message):
            self.root.after(0, lambda: self._pc_on_result(success, message))

        self.price_checker.run(mode, ai_tool, api_key, variables, _on_result)

    def _pc_on_result(self, success, message):
        """Callback invoked on the main thread when the price check finishes."""
        # Only display result text in API mode; redirect mode handled by browser
        if self.pc_mode_var.get() == "api":
            self.pc_result_text.delete("1.0", tk.END)
            if success:
                self.pc_result_text.insert(tk.END, message)
                self.pc_status_var.set("Price check complete.")
            else:
                self.pc_result_text.insert(tk.END, f"Error: {message}")
                self.pc_status_var.set("Price check failed.")
        else:
            # Redirect mode: the browser was already opened by pricecheck.py
            if success:
                self.pc_status_var.set("Opened AI tool in browser - paste prompt + screenshot.")
            else:
                self.pc_status_var.set(f"Redirect failed: {message}")

    def _on_toggle_manual_override(self):
        """Settings checkbox: apply + save immediately."""
        self.cfg["manual_screenshot_override"] = bool(self.manual_override_var.get())
        save_config(self.cfg)
        if self.cfg["manual_screenshot_override"]:
            self._set_status("Manual screenshot override ON - quick-list "
                             "always opens region selection")
        else:
            self._set_status("Manual screenshot override OFF - quick-list "
                             "auto-detects (manual selection only on failure)")

    # ------------------------------------------------------------ Helpers

    def _refresh_item_log(self):
        self.listbox.delete(0, tk.END)
        for it in self.scanner.profile.get("inventory", []):
            label = it.get("full_name") or it.get("name", "?")
            self.listbox.insert(tk.END, f"{label} - {it.get('rarity', '?')}")

    def _set_status(self, text):
        def _update():
            self.status_var.set(text)
            if text.startswith("[") and "]" in text:
                entry = text.split("]", 1)[1].strip()
                self.listbox.insert(tk.END, entry)
                self.listbox.see(tk.END)
        try:
            self.root.after(0, _update)
        except RuntimeError:
            pass

    def _start_hotkeys(self):
        if self.hotkey_mgr:
            self.hotkey_mgr.stop()
        handlers = {
            "quick_list_item": self.on_quick_list,
            "manual_crop_item": self.on_manual_crop_item,
            "price_check_item": self.on_price_check,
            "quit": self._on_close,
        }
        self.hotkey_mgr = HotkeyManager(handlers)
        bound = self.hotkey_mgr.start()
        self._set_status("Hotkeys: " + ", ".join(f"{v}={k}" for k, v in bound.items()))





    # ------------------------------------------------------------- Tray

    def _minimize_to_tray(self):
        if pystray is None or Image is None:
            self._set_status("Tray icon unavailable (pystray/Pillow not in exe); minimizing to taskbar instead.")
            self.root.iconify()
            return
        try:
            # Load Logo.png as the tray icon; fall back to a solid-color
            # image if the file can't be found (source runs) or bundled.
            try:
                img = Image.open(resource_path("Logo.png")).convert("RGBA")
                img = img.resize((32, 32), Image.LANCZOS)
            except Exception:
                img = Image.new("RGB", (32, 32), (200, 120, 40))
            self._tray_image = img  # keep reference so it isn't GC'd
            self.root.withdraw()
            if self.tray is None:
                menu = pystray.Menu(
                    pystray.MenuItem("Show", self._show_from_tray, default=True),
                    pystray.MenuItem("Quit", self._quit_from_tray),
                )
                # visible=True in the constructor so pystray registers the
                # icon when its Win32 window handle is created inside
                # tray.run(). Setting it as a property afterward is too late.
                self.tray = pystray.Icon("d2scan", img, APP_TITLE, menu,
                                         visible=True)
                threading.Thread(target=self.tray.run, daemon=True).start()
            else:
                self.tray.visible = True
        except Exception as e:
            _log_error("tray failed: " + str(e))
            self._set_status("Tray unavailable (" + str(e) + "); minimizing to taskbar.")
            self.root.iconify()

    def _show_from_tray(self, icon=None, item=None):
        self.root.after(0, lambda: self.root.deiconify())

    def _quit_from_tray(self, icon=None, item=None):
        if self.tray:
            self.tray.stop()
        self.root.after(0, self._shutdown)

    def _on_close(self):
        # First-time close: ask the user whether to minimize to tray or exit.
        if not self.cfg.get("close_behavior_prompted", False):
            self._prompt_close_behavior()
            return  # the prompt callback acts on the user's choice

        if self.cfg.get("close_behavior", "minimize") == "minimize":
            self._minimize_to_tray()
        else:
            self._shutdown()

    def _prompt_close_behavior(self):
        """First-time exit: prompt the user to choose minimize-to-tray or exit."""
        choice = messagebox.askyesnocancel(
            "D2TradeHelper - Close behavior",
            "When you close the window, what would you like to do?\n\n"
            "  Yes  → Minimize to system tray (app keeps running in background)\n"
            "  No   → Exit the app completely\n"
            "  Cancel → Keep the window open",
            icon="question",
            parent=self.root,
        )
        if choice is None:
            # Cancel - keep the window open, do nothing
            return
        if choice:  # Yes → minimize to tray
            self.cfg["close_behavior"] = "minimize"
        else:  # No → exit
            self.cfg["close_behavior"] = "exit"
        self.cfg["close_behavior_prompted"] = True
        # Keep the settings radio buttons in sync with the applied behavior
        self.close_var.set(self.cfg["close_behavior"])
        save_config(self.cfg)
        # Act on the user's choice
        if self.cfg["close_behavior"] == "minimize":
            self._minimize_to_tray()
        else:
            self._shutdown()

    def _shutdown(self):
        
        # Stop recorder
        if hasattr(self, 'recorder') and self.recorder:
            try:
                self.recorder.stop()
            except Exception as e:
                print(f"[gui] recorder stop failed: {e}")
        
        # Stop hotkey manager
        if self.hotkey_mgr:
            try:
                self.hotkey_mgr.stop()
            except Exception as e:
                print(f"[gui] hotkey_mgr stop failed: {e}")
        
        # Stop pystray if running
        if hasattr(self, 'tray') and self.tray:
            try:
                self.tray.stop()
            except Exception as e:
                print(f"[gui] tray stop failed: {e}")
        
        # Force destroy root window
        try:
            self.root.quit()
            self.root.destroy()
        except tk.TclError:
            pass
        
        # Force exit if still hanging
        import os
        os._exit(0)

    def run(self):
        self.root.mainloop()

