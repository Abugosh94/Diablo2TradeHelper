"""Quick list tooltip poster for Diablo 2 - CustomTkinter GUI."""

import os

import random

import sys

import threading



try:

    import torch  # noqa: F401

except Exception as _e:

    print(f"[startup] WARNING: torch preload failed: {_e}")



import customtkinter as ctk

from tkinter import messagebox



try:

    import pystray

    from PIL import Image, ImageTk

except Exception:

    pystray = None

    Image = None

    ImageTk = None



from config import (load_config, save_config, app_dir, get_trading_url,
                    resource_path, TRADING_SITE_LABELS)

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



APP_TITLE = "D2TradeHelper"

APP_VERSION = "0.2"

# Subtle Diablo-flavored status messages shown while a price check is in flight.
# One is chosen at random each time the user requests a price check.
DIABLO_FLAVOR_MESSAGES = [
    "Consulting the Horadric Cube...",
    "Appraising item stats...",
    "Reading the runes...",
    "Checking the trade stash...",
]

# Map raw backend error messages to clean, actionable user-facing messages.
_ERROR_MESSAGE_MAP = {
    "Diablo II window not found": "Could not detect item stats. Ensure Diablo II is the active window.",
    "No tooltip detected": "Failed to process tooltip. Make sure the item is hovered and try again.",
    "API key is required": "API request timed out. Please check your connection or API key.",
    "timed out": "API request timed out. Please check your connection or API key.",
    "connection": "API request timed out. Please check your connection or API key.",
    "model": "Model error: Invalid response format. Please try a different model.",
    "Invalid response format": "Model error: Invalid response format. Please try a different model.",
    "Empty response from API": "Empty response from API. Please try again.",
}


def _clean_error_message(raw: str) -> str:
    """Return a clean, actionable user-facing message for a raw error string."""
    lower = raw.lower()
    for key, friendly in _ERROR_MESSAGE_MAP.items():
        if key.lower() in lower:
            return friendly
    # Fallback: keep it short and actionable
    return f"Request failed. {raw[:120]}"






def _log_error(text):

    try:

        import datetime

        path = os.path.join(app_dir(), "error_log.txt")

        with open(path, "a", encoding="utf-8") as f:

            f.write(f"\n[{datetime.datetime.now():%Y-%m-%d %H:%M:%S}]\n{text}\n")

    except Exception:

        pass





def format_hotkey(combo):

    """Capitalize hotkey parts for display: 'ctrl+alt+q' -> 'Ctrl+Alt+Q', 'f9' -> 'F9'."""

    if not combo:

        return combo

    parts = combo.split("+")

    return "+".join(p.capitalize() for p in parts)




def _friendly_keybind(name):

    """Return user-friendly name for keybind actions."""

    friendly = {

        "quick_list_item": "Quick List Item",

        "manual_crop_item": "Manual Crop Item",

        "price_check_item": "Price Check Item",

        "quit": "Quit App",

    }

    return friendly.get(name, name)





class CropDialog(ctk.CTkToplevel):

    MAX_W, MAX_H = 1500, 860

    MIN_SEL_W, MIN_SEL_H = 30, 20



    def __init__(self, master, img_bgr, title, on_done):

        super().__init__(master)

        self.title(title or "Select the area to post")

        self.img = img_bgr

        self.on_done = on_done

        self.rect = None

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

        self._photo = ImageTk.PhotoImage(pil)



        self.canvas = ctk.CTkCanvas(self, width=dw, height=dh,

                                    cursor="crosshair", highlightthickness=0, bg=("#FFFFFF", "#18181B"))

        self.canvas.create_image(0, 0, anchor="nw", image=self._photo)

        self.canvas.pack(fill="both", expand=True)



        bar = ctk.CTkFrame(self, fg_color=("#F4F4F5", "#18181B"), border_color=("#E4E4E7", "#27272A"), border_width=1)

        bar.pack(fill="x")

        self.info = ctk.CTkLabel(bar, text="Drag a rectangle around the tooltip. "

                                         "Enter = use, Esc = cancel", text_color=("#18181B", "#F4F4F5"))

        self.info.pack(side="left", padx=6, pady=4)

        self.cancel_btn = ctk.CTkButton(bar, text="Cancel",

                                        command=self._cancel, fg_color=("#27272A", "#27272A"), hover_color=("#27272A", "#27272A"), text_color=("#18181B", "#F4F4F5"))

        self.cancel_btn.pack(side="right", padx=6, pady=4)

        self.use_btn = ctk.CTkButton(bar, text="Use selection",

                                     command=self._ok, state="disabled", fg_color=("#3B82F6", "#3B82F6"), hover_color=("#60A5FA", "#60A5FA"), text_color=("#18181B", "#F4F4F5"))

        self.use_btn.pack(side="right", padx=6, pady=4)



        self.canvas.bind("<ButtonPress-1>", self._press)

        self.canvas.bind("<B1-Motion>", self._motion)

        self.canvas.bind("<ButtonRelease-1>", self._release)

        self.bind("<Escape>", self._cancel)

        self.bind("<Return>", self._ok)

        self.protocol("WM_DELETE_WINDOW", self._cancel)



        self.attributes("-topmost", True)

        self.grab_set()

        self.focus_force()

        self.lift()



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





class ScrollableComboBox(ctk.CTkFrame):
    """A combo box with a scrollable dropdown list for long option lists.

    Behaves like CTkComboBox but the dropdown is a CTkToplevel with a
    CTkScrollableFrame, so many models can be scrolled through.
    """

    def __init__(self, master, variable=None, values=None, width=200, height=28,
                 command=None, fg_color=None, border_color=None, button_color=None,
                 text_color=None, font=None, dropdown_height=220, **kwargs):
        super().__init__(master, fg_color="transparent", **kwargs)

        self._values = list(values or [])
        self._variable = variable or ctk.StringVar()
        self._command = command
        self._dropdown_height = dropdown_height
        self._dropdown = None

        # Configure grid layout: entry takes available space, button fixed
        self.columnconfigure(0, weight=1)
        self.columnconfigure(1, weight=0)

        # Entry/display field
        self.entry = ctk.CTkEntry(
            self,
            textvariable=self._variable,
            height=height,
            fg_color=fg_color,
            border_color=border_color,
            text_color=text_color,
            font=font,
        )
        self.entry.grid(row=0, column=0, sticky="ew", padx=(0, 0), pady=0)

        # Dropdown button
        self.btn = ctk.CTkButton(
            self,
            text="▼",
            width=height,
            height=height,
            command=self._toggle_dropdown,
            fg_color=button_color or ("#3B82F6", "#3B82F6"),
            hover_color=("#60A5FA", "#60A5FA"),
            text_color=("white", "white"),
            font=font or ("Segoe UI", 12),
        )
        self.btn.grid(row=0, column=1, sticky="ns", padx=0, pady=0)

        # Set initial value
        if self._values and not self._variable.get():
            self._variable.set(self._values[0])

    def _toggle_dropdown(self):
        if self._dropdown and self._dropdown.winfo_exists():
            self._close_dropdown()
            return
        self._show_dropdown()

    def _show_dropdown(self):
        self._dropdown = ctk.CTkToplevel(self)
        self._dropdown.overrideredirect(True)
        self._dropdown.attributes("-topmost", True)

        # Position below the entry/button
        x = self.entry.winfo_rootx()
        y = self.entry.winfo_rooty() + self.entry.winfo_height()

        # Outer frame with border
        outer = ctk.CTkFrame(
            self._dropdown,
            fg_color=("#E4E4E7", "#27272A"),
            border_width=0,
        )
        outer.pack(fill="both", expand=True, padx=1, pady=1)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(1, weight=1)  # scroll frame row expands

        # Search box at top of dropdown
        self._search_var = ctk.StringVar()
        self._search_var.trace_add("write", lambda *_: self._filter_dropdown())

        search_entry = ctk.CTkEntry(
            outer,
            textvariable=self._search_var,
            placeholder_text="Search models...",
            height=26,
            fg_color=("#F4F4F5", "#18181B"),
            border_color=("#E4E4E7", "#27272A"),
        )
        search_entry.grid(row=0, column=0, sticky="ew", padx=4, pady=(4, 2))
        search_entry.focus_set()

        # Scrollable frame with options
        scroll_frame = ctk.CTkScrollableFrame(
            outer,
            width=width if hasattr(self, '_dropdown_width') else 200,
            height=self._dropdown_height,
            fg_color=("#F4F4F5", "#18181B"),
            border_color=("#E4E4E7", "#27272A"),
            border_width=1,
        )
        scroll_frame.grid(row=1, column=0, sticky="nsew", padx=0, pady=0)
        scroll_frame.columnconfigure(0, weight=1)

        # Store scroll frame reference and build buttons
        self._dropdown_scroll_frame = scroll_frame
        self._dropdown_all_values = list(self._values)
        self._dropdown_buttons = []
        self._filter_dropdown()

        # Position the dropdown (slightly taller to accommodate search box)
        self._dropdown.geometry(f"{self.winfo_width()}x{self._dropdown_height + 34}+{x}+{y}")

        # Click outside to close
        self._dropdown.bind("<FocusOut>", lambda e: self._dropdown.after(150, self._close_dropdown))
        self._dropdown.focus_set()

    def _filter_dropdown(self):
        """Filter the dropdown model buttons based on the search text."""
        search = self._search_var.get().lower()

        # Remove existing buttons
        for b in self._dropdown_buttons:
            b.destroy()
        self._dropdown_buttons = []

        # Re-add matching buttons
        scroll_frame = self._dropdown_scroll_frame
        for val in self._dropdown_all_values:
            if search in val.lower():
                b = ctk.CTkButton(
                    scroll_frame,
                    text=val,
                    anchor="w",
                    fg_color="transparent",
                    hover_color=("#E5E7EB", "#374151"),
                    text_color=("#18181B", "#F4F4F5"),
                    height=28,
                    command=lambda v=val: self._select(v),
                )
                b.pack(fill="x", padx=3, pady=1)
                self._dropdown_buttons.append(b)


    def _select(self, value):
        self._variable.set(value)
        self._close_dropdown()
        if self._command:
            self._command()

    def _close_dropdown(self):
        if self._dropdown and self._dropdown.winfo_exists():
            self._dropdown.destroy()
        self._dropdown = None

    def configure(self, **kwargs):
        if "values" in kwargs:
            self._values = list(kwargs.pop("values"))
            if self._dropdown and self._dropdown.winfo_exists():
                self._close_dropdown()
                self._show_dropdown()
        if "variable" in kwargs:
            self._variable = kwargs.pop("variable")
            self.entry.configure(textvariable=self._variable)
        super().configure(**kwargs)

    def cget(self, key):
        if key == "values":
            return self._values
        if key == "textvariable":
            return self._variable
        return super().cget(key)

    def grid(self, **kwargs):
        super().grid(**kwargs)

    def pack(self, **kwargs):
        super().pack(**kwargs)

    def place(self, **kwargs):
        super().place(**kwargs)

    def set(self, value):
        self._variable.set(value)

    def get(self):
        return self._variable.get()


class App:

    def __init__(self):

        self.cfg = load_config()

        self.scanner = InventoryScanner()

        self.price_checker = PriceChecker(self.cfg)

        self.scanner.profile = {}

        self.recorder = None

        self.hotkey_mgr = None
        self._bind_listener = None

        self.tray = None

        # Status message cycling during price check (to show user app is working)
        self._status_cycle_after_id = None

        self._build_ui()

        self._start_hotkeys()

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)



    def _apply_theme(self):

        effective_theme = self.theme_var.get()

        if effective_theme in ("System Default (Auto)", "system"):

            effective_theme = self._detect_system_theme()

        ctk.set_appearance_mode(effective_theme.lower())



    def _detect_system_theme(self):

        try:

            import winreg

            key = winreg.OpenKey(

                winreg.HKEY_CURRENT_USER,

                r"Software\\Microsoft\\Windows\\CurrentVersion\\Themes\\Personalize",

            )

            value, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")

            winreg.CloseKey(key)

            return "light" if value == 1 else "dark"

        except Exception:

            return "light"



    def _on_theme_changed(self, selection):

        # Store the internal theme value so config stays consistent
        # ("system" | "light" | "dark") regardless of the display label.
        if selection == "System Default (Auto)":

            self.cfg["theme"] = "system"

            effective_theme = self._detect_system_theme()

        elif selection == "Dark":

            self.cfg["theme"] = "dark"

            effective_theme = "dark"

        else:

            self.cfg["theme"] = "light"

            effective_theme = "light"

        ctk.set_appearance_mode(effective_theme)

        save_config(self.cfg)



    def _build_ui(self):

        self.root = ctk.CTk()

        self.root.title(f"{APP_TITLE} v{APP_VERSION}")

        self.root.configure(fg_color=("#F4F4F5", "#121214"))

        self.root.configure(fg_color=("#F4F4F5", "#121214"))

        self.root.geometry("780x840")

        self.root.minsize(690, 720)

        try:

            self.root.iconbitmap(resource_path("Logo.ico"))

        except Exception:

            pass



        self.pad = {"padx": 10, "pady": 10}



        self.theme_var = ctk.StringVar(value=self.cfg.get("theme", "system"))

        self._apply_theme()



        try:

            _logo_img = Image.open(resource_path("Logo.png")).resize((16, 16), Image.LANCZOS)

            _logo_photo = ctk.CTkImage(light_image=_logo_img, dark_image=_logo_img, size=(16, 16))

            _logo_label = ctk.CTkLabel(self.root, image=_logo_photo, text="", text_color=("#18181B", "#F4F4F5"))

            _logo_label.image = _logo_photo

            _logo_label.place(relx=1.0, x=-30, y=5, anchor="ne")

        except Exception:

            pass

        version_label = ctk.CTkLabel(self.root, text=f"v{APP_VERSION}", font=("Segoe UI", 8), text_color=("#18181B", "#F4F4F5"))

        version_label.place(relx=1.0, x=-10, y=5, anchor="ne")



        self.root.columnconfigure(0, weight=1)
        self.root.columnconfigure(1, weight=0)
        self.root.rowconfigure(0, weight=1)

        self.notebook = ctk.CTkTabview(self.root, fg_color=("#F4F4F5", "#18181B"), border_color=("#E4E4E7", "#27272A"), border_width=1)

        self.notebook.grid(row=0, column=0, sticky="nsew", padx=0, pady=0)



        self.notebook.add("Price Check")

        self.notebook.add("Quick List")

        self.notebook.add("Settings")



        self.pc_tab = self.notebook.tab("Price Check")

        self.pc_tab.columnconfigure(0, weight=1)

        self.pc_tab.rowconfigure(0, weight=1)



        self.pc_scroll = ctk.CTkScrollableFrame(self.pc_tab, fg_color=("#F4F4F5", "#18181B"), border_color=("#E4E4E7", "#27272A"), border_width=1)

        self.pc_scroll.pack(fill="both", expand=True)

        self.pc_scroll.columnconfigure(0, weight=1)



        self.pc_body = ctk.CTkFrame(self.pc_scroll, fg_color=("#F4F4F5", "#18181B"), border_color=("#E4E4E7", "#27272A"), border_width=1)

        self.pc_body.pack(fill="both", expand=True, padx=5, pady=5)

        self.pc_body.columnconfigure(0, weight=1)
        self.pc_body.rowconfigure(3, weight=1)



        self.pc_mode_frame = ctk.CTkFrame(self.pc_body, fg_color=("#F4F4F5", "#18181B"), border_color=("#E4E4E7", "#27272A"), border_width=1)

        self.pc_mode_frame.grid(row=0, column=0, sticky="ew", **self.pad)

        self.pc_mode_frame.columnconfigure(1, weight=1)

        self.pc_mode_frame.columnconfigure(2, weight=1)



        self.pc_mode_var = ctk.StringVar(

            value=self.price_checker.get_config().get("mode", "redirect"))



        self.pc_api_var = ctk.CTkRadioButton(

            self.pc_mode_frame,

            text="Use API key (result appears in this app)",

            variable=self.pc_mode_var,

            value="api",

            command=self._on_pc_mode_changed,

        )

        self.pc_api_var.grid(row=0, column=0, columnspan=3, sticky="w", padx=6, pady=3)



        self.pc_api_key_frame = ctk.CTkFrame(self.pc_mode_frame, fg_color=("#F4F4F5", "#18181B"), border_color=("#E4E4E7", "#27272A"), border_width=1)

        self.pc_api_key_frame.grid(row=1, column=0, columnspan=3, sticky="ew", padx=20, pady=(0, 3))

        self.pc_api_key_frame.columnconfigure(1, weight=1)

        ctk.CTkLabel(self.pc_api_key_frame, text="API key:", text_color=("#18181B", "#F4F4F5")).grid(

            row=0, column=0, sticky="w", padx=(0, 6))

        self.pc_api_key_var = ctk.StringVar()

        # Pre-fill with last successful API key if no current key is saved

        saved_api_key = self.cfg.get("price_check", {}).get("api_key", "")

        last_successful_key = self.cfg.get("price_check", {}).get("last_successful_api_key", "")

        if not saved_api_key and last_successful_key:

            self.pc_api_key_var.set(last_successful_key)

        else:

            self.pc_api_key_var.set(saved_api_key)

        # Auto-fetch models whenever the API key is entered/changed (in API mode)

        self.pc_api_key_var.trace_add(

            "write", lambda *_: self._on_pc_api_key_changed()

        )

        self.pc_api_key_entry = ctk.CTkEntry(

            self.pc_api_key_frame, textvariable=self.pc_api_key_var, width=40, show="*", fg_color=("#F4F4F5", "#18181B"), border_color=("#E4E4E7", "#27272A"))

        self.pc_api_key_entry.grid(row=0, column=1, sticky="ew", padx=(0, 6))

        self.pc_api_show_var = ctk.BooleanVar(value=False)

        self.pc_api_show_cb = ctk.CTkCheckBox(

            self.pc_api_key_frame,

            text="Show", variable=self.pc_api_show_var,

            command=self._on_pc_api_show_toggle,

            text_color=("#18181B", "#F4F4F5"))

        self.pc_api_show_cb.grid(row=0, column=2)

        self.pc_api_fetch_btn = ctk.CTkButton(

            self.pc_api_key_frame,

            text="Fetch Models",

            command=self._on_pc_fetch_models,

        fg_color=("#3B82F6", "#3B82F6"), hover_color=("#60A5FA", "#60A5FA"), text_color=("#18181B", "#F4F4F5"))

        self.pc_api_fetch_btn.grid(row=0, column=3, padx=(6, 0))

        self.pc_api_clear_btn = ctk.CTkButton(

            self.pc_api_key_frame,

            text="Clear",

            command=self._on_pc_api_clear,

        fg_color=("#27272A", "#27272A"), hover_color=("#404040", "#404040"), text_color=("#F4F4F5", "#F4F4F5"))

        self.pc_api_clear_btn.grid(row=0, column=4, padx=(6, 0))

        # API base URL override (for Gemini, DeepSeek, and other OpenAI-compatible APIs)
        # Placed INSIDE the API key frame so it appears directly under the API key entry
        self.pc_api_base_url_frame = ctk.CTkFrame(
            self.pc_api_key_frame, fg_color=("#F4F4F5", "#18181B"),
            border_color=("#E4E4E7", "#27272A"), border_width=1
        )
        self.pc_api_base_url_frame.grid(row=4, column=0, columnspan=7, sticky="ew", padx=6, pady=(8, 0))
        self.pc_api_base_url_frame.columnconfigure(1, weight=1)
        ctk.CTkLabel(
            self.pc_api_base_url_frame, text="API base URL:",
            text_color=("#18181B", "#F4F4F5")
        ).grid(row=0, column=0, sticky="w", padx=(0, 6))
        self.pc_api_base_url_var = ctk.StringVar(
            value=self.cfg.get("price_check", {}).get("api_base_url", "")
        )
        self.pc_api_base_url_entry = ctk.CTkEntry(
            self.pc_api_base_url_frame,
            textvariable=self.pc_api_base_url_var,
            width=60,
            fg_color=("#F4F4F5", "#18181B"),
            border_color=("#E4E4E7", "#27272A"),
        )
        self.pc_api_base_url_entry.grid(row=0, column=1, sticky="ew", padx=(0, 6))
        ctk.CTkLabel(
            self.pc_api_base_url_frame,
            text="(e.g. https://generativelanguage.googleapis.com/v1 - for Gemini etc.)",
            text_color=("#8A8A8A", "#8A8A8A"),
            font=("Segoe UI", 11),
        ).grid(row=0, column=2, sticky="w", padx=(6, 0))

        self.pc_model_var = ctk.StringVar(

            value=self.price_checker.get_config().get("ai_model", "gpt-4o"))

        self.pc_model_label = ctk.CTkLabel(self.pc_api_key_frame, text="Model:", text_color=("#18181B", "#F4F4F5"))

        self.pc_model_label.grid(row=2, column=0, sticky="e", padx=(0, 6))

        self.pc_model_combo = ScrollableComboBox(
            self.pc_api_key_frame,
            variable=self.pc_model_var,
            values=[],
            width=200,
            fg_color=("#F4F4F5", "#18181B"),
            border_color=("#E4E4E7", "#27272A"),
            button_color=("#3B82F6", "#3B82F6"),
            dropdown_height=220,
        )

        self.pc_model_combo.grid(row=2, column=1, columnspan=2, sticky="ew", padx=(0, 6))

        self.pc_model_var.trace_add("write", self._on_pc_model_changed)

        ctk.CTkLabel(
            self.pc_api_key_frame,
            text="Tip: use a model that can process images (e.g. gpt-4o, gemini-1.5-flash).",
            text_color=("#18181B", "#F4F4F5"),
            font=("Segoe UI", 12),
            wraplength=500,
        ).grid(row=1, column=0, columnspan=7, sticky="w", padx=(0, 6), pady=(0, 6))

        self.pc_api_key_frame.grid_remove()



        ctk.CTkRadioButton(

            self.pc_mode_frame,

            text="Redirect to AI tool", text_color=("#18181B", "#F4F4F5"),

            variable=self.pc_mode_var,

            value="redirect",

            command=self._on_pc_mode_changed,

        ).grid(row=2, column=0, columnspan=3, sticky="w", padx=6, pady=3)



        # ---- Custom AI tool / marketplace / status variables ----

        self.pc_custom_ai_name_var = ctk.StringVar()

        self.pc_custom_ai_url_var = ctk.StringVar()

        self.pc_custom_ai_keybind_var = ctk.StringVar(value="ctrl+shift+o")
        self.pc_custom_ai_exe_var = ctk.StringVar(value="")

        self.pc_keybind_var = ctk.StringVar()

        # ---- AI launch mode: browser vs desktop app (exe) ----
        self.pc_ai_launch_var = ctk.StringVar(value="browser")
        self.pc_ai_exe_path_var = ctk.StringVar(value="")

        self.pc_status_var = ctk.StringVar(

            value="Idle. Capture a tooltip and click Price Check."

        )

        # AI Response slide-out panel state

        self.ai_panel = None

        self.ai_panel_expanded = False

        self.ai_panel_width = 400

        self.ai_panel_minimized_width = 40

        self.ai_locked = True

        # User-selectable layout mode: "side" (right drawer) or "bottom" (bottom drawer)
        self.ai_panel_mode = "side"

        # Track panel mode/expanded state before undocking so we can restore it on re-dock
        self.ai_panel_mode_before_undock = "side"
        self.ai_panel_expanded_before_undock = False

        self.ai_bottom_height = 250

        self.ai_bottom_minimized_height = 40

        self.pc_platform_var = ctk.StringVar(value="PC")

        self.pc_ladder_var = ctk.StringVar(

            value=self.price_checker.get_config().get("ladder_status", "Ladder")

        )

        self.pc_hardcore_var = ctk.StringVar(

            value=self.price_checker.get_config().get("hardcore_status", "Softcore")

        )

        self.pc_timeframe_var = ctk.StringVar(

            value=self.price_checker.get_config().get("timeframe", "Last 7 Days")

        )

        self.pc_custom_days_var = ctk.StringVar(

            value=str(self.price_checker.get_config().get("custom_timeframe_days", 7))

        )

        self.pc_mp_buttons = []

        self.pc_custom_mp_var = ctk.StringVar()



        # ---- AI tool selector (next to the redirect option) ----

        ai_options = self.price_checker.get_ai_tool_options()

        ai_labels = [label for _key, label, _url, _kb in ai_options]

        ai_keys = [key for key, _label, _url, _kb in ai_options]

        self._pc_ai_key_by_label = dict(zip(ai_labels, ai_keys))

        stored_key = self.price_checker.get_config().get("ai_tool", "gemini")

        stored_label = "Gemini"

        for label, key in self._pc_ai_key_by_label.items():

            if key == stored_key:

                stored_label = label

                break

        self.pc_ai_tool_var = ctk.StringVar(value=stored_label)

        self.pc_ai_tool_label = ctk.CTkLabel(self.pc_mode_frame, text="AI tool:", text_color=("#18181B", "#F4F4F5"))

        self.pc_ai_tool_label.grid(row=3, column=0, sticky="e", padx=20, pady=3)

        self.pc_ai_tool_combo = ctk.CTkComboBox(

            self.pc_mode_frame,

            variable=self.pc_ai_tool_var,

            values=ai_labels,

            width=200,

            command=lambda _v: self._on_pc_ai_tool_changed(),

        )

        self.pc_ai_tool_combo.grid(

            row=3, column=1, columnspan=2, sticky="ew", padx=20, pady=3

        )

        # ---- AI launch mode: browser vs desktop app (exe) ----
        self.launch_frame = ctk.CTkFrame(self.pc_mode_frame, fg_color="transparent")
        self.launch_frame.grid(row=4, column=0, columnspan=3, sticky="w", padx=20, pady=(8, 2))

        self.pc_ai_launch_browser_btn = ctk.CTkRadioButton(
            self.launch_frame,
            text="Open in browser", text_color=("#18181B", "#F4F4F5"),
            variable=self.pc_ai_launch_var, value="browser",
            command=self._on_pc_ai_launch_changed,
        )
        self.pc_ai_launch_browser_btn.pack(anchor="w", pady=(0, 3))

        self.pc_ai_launch_exe_btn = ctk.CTkRadioButton(
            self.launch_frame,
            text="Open desktop app (recommended)", text_color=("#18181B", "#F4F4F5"),
            variable=self.pc_ai_launch_var, value="exe",
            command=self._on_pc_ai_launch_changed,
        )
        self.pc_ai_launch_exe_btn.pack(anchor="w")

        # Exe path controls on a separate row
        self.exe_row = ctk.CTkFrame(self.launch_frame, fg_color="transparent")
        self.exe_row.pack(anchor="w", pady=(8, 0))

        ctk.CTkLabel(self.exe_row, text="App exe path:", text_color=("#18181B", "#F4F4F5")).pack(side="left", padx=(0, 6))

        self.pc_ai_exe_entry = ctk.CTkEntry(
            self.exe_row, textvariable=self.pc_ai_exe_path_var, width=220,
            fg_color=("#F4F4F5", "#18181B"), border_color=("#E4E4E7", "#27272A"),
        )
        self.pc_ai_exe_entry.pack(side="left", padx=(0, 6))

        self.pc_ai_exe_browse_btn = ctk.CTkButton(
            self.exe_row, text="Browse", width=70,
            command=self._browse_ai_exe,
            fg_color=("#3B82F6", "#3B82F6"), hover_color=("#60A5FA", "#60A5FA"),
            text_color=("#18181B", "#F4F4F5"),
        )
        self.pc_ai_exe_browse_btn.pack(side="left")

        # Set initial radio button states
        self._update_launch_radio_states()



        # New chat behavior

        self.pc_new_chat_var = ctk.StringVar()

        new_chat_each_time = self.price_checker.get_config().get(

            "new_chat_each_time", True

        )

        self.pc_new_chat_var.set(

            "New chat each time" if new_chat_each_time else "Continue same chat"

        )

        self.pc_new_chat_label = ctk.CTkLabel(self.pc_mode_frame, text="New chat behavior:", text_color=("#18181B", "#F4F4F5"))
        self.pc_new_chat_label.grid(row=5, column=0, sticky="e", padx=20, pady=3)

        self.pc_new_chat_combo = ctk.CTkComboBox(

            self.pc_mode_frame,

            variable=self.pc_new_chat_var,

            values=["New chat each time", "Continue same chat"],

            width=200,
            command=lambda _v: self._on_pc_ai_tool_changed(),

        )

        self.pc_new_chat_combo.grid(

            row=5, column=1, columnspan=2, sticky="ew", padx=20, pady=3

        )



        # ---- Item variables (filled into the AI prompt) ----

        var_frame = ctk.CTkFrame(self.pc_body, fg_color=("#F4F4F5", "#18181B"), border_color=("#E4E4E7", "#27272A"), border_width=1)

        var_frame.grid(row=1, column=0, sticky="ew", **self.pad)

        var_frame.columnconfigure(1, weight=1)



        # Platform (always PC)

        ctk.CTkLabel(var_frame, text="Platform:", text_color=("#18181B", "#F4F4F5")).grid(
            row=0, column=0, sticky="w", padx=6, pady=3
        )
        ctk.CTkLabel(var_frame, text="PC", text_color=("#18181B", "#F4F4F5")).grid(
            row=0, column=1, sticky="w", padx=6, pady=3
        )





        # Game Mode (Ladder / Non-Ladder)

        ctk.CTkLabel(var_frame, text="Game Mode:", text_color=("#18181B", "#F4F4F5")).grid(

            row=1, column=0, sticky="w", padx=6, pady=3

        )

        ladder_frame = ctk.CTkFrame(var_frame, fg_color=("#F4F4F5", "#18181B"), border_color=("#E4E4E7", "#27272A"), border_width=1)

        ladder_frame.grid(row=1, column=1, sticky="w", padx=6, pady=3)

        for opt in LADDER_OPTIONS:

            ctk.CTkRadioButton(

                ladder_frame,

                text=opt,

                variable=self.pc_ladder_var,

                value=opt,

            text_color=("#18181B", "#F4F4F5")).pack(side="left", padx=(0, 8))



        # Softcore / Hardcore

        ctk.CTkLabel(var_frame, text="Softcore / Hardcore:", text_color=("#18181B", "#F4F4F5")).grid(

            row=2, column=0, sticky="w", padx=6, pady=3

        )

        hc_frame = ctk.CTkFrame(var_frame, fg_color=("#F4F4F5", "#18181B"), border_color=("#E4E4E7", "#27272A"), border_width=1)

        hc_frame.grid(row=2, column=1, sticky="w", padx=6, pady=3)

        for opt in HARDCORE_OPTIONS:

            ctk.CTkRadioButton(

                hc_frame,

                text=opt,

                variable=self.pc_hardcore_var,

                value=opt,

            text_color=("#18181B", "#F4F4F5")).pack(side="left", padx=(0, 8))



        # Marketplaces (listbox-style, max 5). Click a site to mark it, then

        # click Remove to remove the marked site(s).

        ctk.CTkLabel(var_frame, text="Marketplaces:", text_color=("#18181B", "#F4F4F5")).grid(

            row=3, column=0, sticky="nw", padx=6, pady=3

        )

        mp_frame = ctk.CTkFrame(var_frame, fg_color=("#F4F4F5", "#18181B"), border_color=("#E4E4E7", "#27272A"), border_width=1)

        mp_frame.grid(row=3, column=1, sticky="ew", padx=6, pady=3)

        mp_frame.columnconfigure(0, weight=1)



        self.pc_mp_scroll = ctk.CTkScrollableFrame(mp_frame, height=80, fg_color=("#F4F4F5", "#18181B"), border_color=("#E4E4E7", "#27272A"), border_width=1)

        self.pc_mp_scroll.grid(row=0, column=0, columnspan=3, sticky="ew")

        self.pc_mp_selected = set()

        saved_mps = self.price_checker.get_config().get("marketplaces", [])

        if not saved_mps:

            saved_mps = list(DEFAULT_MARKETPLACES)

        for mp in saved_mps:

            btn = ctk.CTkButton(

                self.pc_mp_scroll,

                text=mp,

                command=lambda m=mp: self._on_pc_toggle_marketplace(m),

            fg_color=("#3B82F6", "#3B82F6"), hover_color=("#60A5FA", "#60A5FA"), text_color=("#18181B", "#F4F4F5"))

            btn.pack(fill="x", padx=2, pady=2)

            self.pc_mp_buttons.append(btn)

        self._refresh_mp_buttons()



        mp_input = ctk.CTkFrame(mp_frame, fg_color=("#F4F4F5", "#18181B"), border_color=("#E4E4E7", "#27272A"), border_width=1)

        mp_input.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(12, 6))

        mp_input.columnconfigure(0, weight=1)

        ctk.CTkEntry(

            mp_input, textvariable=self.pc_custom_mp_var, width=24

        , fg_color=("#F4F4F5", "#18181B"), border_color=("#E4E4E7", "#27272A")).grid(row=0, column=0, sticky="ew", padx=(0, 4))

        ctk.CTkButton(

            mp_input, text="Add", width=70,

            command=self._on_pc_add_marketplace,

        fg_color=("#3B82F6", "#3B82F6"), hover_color=("#60A5FA", "#60A5FA"), text_color=("#18181B", "#F4F4F5")).grid(row=0, column=1, padx=(0, 4))

        ctk.CTkButton(

            mp_input, text="Remove", width=70,

            command=self._on_pc_remove_selected_marketplaces,

        fg_color=("#27272A", "#27272A"), hover_color=("#27272A", "#27272A"), text_color=("#18181B", "#F4F4F5")).grid(row=0, column=2)



        # Price History Window

        ctk.CTkLabel(var_frame, text="Price History Window:", text_color=("#18181B", "#F4F4F5")).grid(

            row=4, column=0, sticky="w", padx=6, pady=3

        )

        tf_frame = ctk.CTkFrame(var_frame, fg_color=("#F4F4F5", "#18181B"), border_color=("#E4E4E7", "#27272A"), border_width=1)

        tf_frame.grid(row=4, column=1, sticky="w", padx=6, pady=3)

        self.pc_timeframe_combo = ctk.CTkComboBox(

            tf_frame,

            variable=self.pc_timeframe_var,

            values=DEFAULT_TIMEFRAMES,

            width=160,

            command=lambda _v: self._on_pc_timeframe_changed(),

        )

        self.pc_timeframe_combo.pack(side="left")

        self.pc_custom_days_entry = ctk.CTkEntry(

            tf_frame, textvariable=self.pc_custom_days_var, width=60

        , fg_color=("#F4F4F5", "#18181B"), border_color=("#E4E4E7", "#27272A"))

        self.pc_custom_days_label = ctk.CTkLabel(tf_frame, text="days", text_color=("#18181B", "#F4F4F5"))

        self._on_pc_timeframe_changed()  # set initial visibility

        self.pc_timeframe_var.trace_add(

            "write", lambda *_: self._on_pc_timeframe_changed()

        )



        # ---- Key Bind Label ----

        self.pc_bind_label_frame = ctk.CTkFrame(self.pc_body, fg_color=("#F4F4F5", "#18181B"), border_color=("#E4E4E7", "#27272A"), border_width=1)

        self.pc_bind_label_frame.grid(row=2, column=0, sticky="ew", **self.pad)

        self.pc_bind_label_frame.columnconfigure(0, weight=1)
        ctk.CTkLabel(
            self.pc_bind_label_frame,
            textvariable=self.pc_keybind_var,
            text_color=("#18181B", "#F4F4F5"),
            font=("Segoe UI", 12),
            justify="center",
        ).grid(row=0, column=0, sticky="ew", padx=6, pady=3)

        ctk.CTkLabel(
            self.pc_bind_label_frame,
            text="Hover an item in Diablo II and press its hotkey (F8 price check / F9 quick list).",
            text_color=("#18181B", "#F4F4F5"),
            font=("Segoe UI", 12, "bold"),
            justify="center",
        ).grid(row=1, column=0, sticky="ew", padx=6, pady=(0, 3))





        # Price Check Result area removed - AI panel handles results display







        # Initialize API key field visibility based on saved mode

        self._on_pc_mode_changed()

        self._on_pc_ai_tool_changed()

        # Create the slide-out AI response panel only in API mode.
        # In redirect mode the drawer is not needed - results appear in the browser.
        if self.pc_mode_var.get() == "api":
            self._create_ai_response_panel()







        self.scanner_tab = self.notebook.tab("Quick List")
        self.scanner_tab.configure(fg_color=("#F4F4F5", "#18181B"), border_color=("#E4E4E7", "#27272A"), border_width=1)


        self.scanner_tab.columnconfigure(0, weight=1)

        self.scanner_tab.rowconfigure(0, weight=1)



        self.scanner_scroll = ctk.CTkScrollableFrame(self.scanner_tab, fg_color=("#F4F4F5", "#18181B"), border_color=("#E4E4E7", "#27272A"), border_width=1)

        self.scanner_scroll.pack(fill="both", expand=True)

        self.scanner_scroll.columnconfigure(0, weight=1)



        self.scanner_body = ctk.CTkFrame(self.scanner_scroll, fg_color=("#F4F4F5", "#18181B"), border_color=("#E4E4E7", "#27272A"), border_width=1)

        self.scanner_body.pack(fill="both", expand=True, padx=5, pady=5)

        self.scanner_body.columnconfigure(0, weight=1)



        self.site_frame = ctk.CTkFrame(self.scanner_body, fg_color=("#F4F4F5", "#18181B"), border_color=("#E4E4E7", "#27272A"), border_width=1)

        self.site_frame.pack(pady=(10, 0), padx=10, fill="x")

        ctk.CTkLabel(self.site_frame, text="Trading website:", font=("Segoe UI", 12, "bold"), text_color=("#18181B", "#F4F4F5")).pack(anchor="w", padx=5, pady=(4, 0))

        self.site_var = ctk.StringVar()

        from config import trading_site_options

        opts = trading_site_options(self.cfg)

        site_labels = [label for _key, label in opts]

        site_keys = [key for key, _label in opts]

        self._site_key_by_label = dict(zip(site_labels, site_keys))

        saved_site = self.cfg.get("trading_site", {}).get("site", "")

        saved_label = None

        for label, key in self._site_key_by_label.items():

            if key == saved_site:

                saved_label = label

                break

        if saved_label is None and site_labels:

            saved_label = site_labels[0]

        self.site_var.set(saved_label or "")

        self.site_combo = ctk.CTkComboBox(self.site_frame,

            variable=self.site_var, values=site_labels, width=300, fg_color=("#F4F4F5", "#18181B"), border_color=("#E4E4E7", "#27272A"), button_color=("#3B82F6", "#3B82F6"),
            command=self.on_site_selected)

        self.site_combo.pack(anchor="w", padx=5, pady=2)

        self.site_combo.bind("<<ComboboxSelected>>", self.on_site_selected)



        self.custom_row = ctk.CTkFrame(self.site_frame, fg_color=("#F4F4F5", "#18181B"), border_color=("#E4E4E7", "#27272A"), border_width=1)

        ctk.CTkLabel(self.custom_row, text="Name:", text_color=("#18181B", "#F4F4F5")).pack(side="left", padx=(8, 0))

        self.custom_name_var = ctk.StringVar()

        ctk.CTkEntry(self.custom_row, textvariable=self.custom_name_var, width=20, fg_color=("#F4F4F5", "#18181B"), border_color=("#E4E4E7", "#27272A")).pack(

            side="left", padx=6)

        ctk.CTkLabel(self.custom_row, text="URL:", text_color=("#18181B", "#F4F4F5")).pack(side="left", padx=(8, 0))

        self.custom_url_var = ctk.StringVar()

        ctk.CTkEntry(self.custom_row, textvariable=self.custom_url_var, width=30, fg_color=("#F4F4F5", "#18181B"), border_color=("#E4E4E7", "#27272A")).pack(

            side="left", padx=6)

        ctk.CTkButton(self.custom_row, text="Save site",

            command=self.on_save_custom_site, fg_color=("#3B82F6", "#3B82F6"), hover_color=("#60A5FA", "#60A5FA"), text_color=("#18181B", "#F4F4F5")).pack(side="left", padx=6)



        self._refresh_site_combo()

        self.open_site_btn = ctk.CTkButton(self.site_frame, text="Open site",

            command=self.on_open_site, fg_color=("#3B82F6", "#3B82F6"), hover_color=("#60A5FA", "#60A5FA"), text_color=("#18181B", "#F4F4F5"))

        self.open_site_btn.pack(side="left", padx=6)

        self.add_site_btn = ctk.CTkButton(self.site_frame, text="Add Custom Site",

            command=self._open_custom_site_dialog, fg_color=("#3B82F6", "#3B82F6"), hover_color=("#60A5FA", "#60A5FA"), text_color=("#18181B", "#F4F4F5"))

        self.add_site_btn.pack(side="left", padx=6)





        # Quick List is key-bind only (F9) - no on-screen button per design.
        mc_key = self.pc_keybind_var.get() or "F10"
        self.manual_crop_btn = ctk.CTkButton(self.scanner_body, text=f"Manual Crop Item ({format_hotkey(mc_key)})",
            command=self.on_manual_crop_item, fg_color=("#3B82F6", "#3B82F6"), hover_color=("#60A5FA", "#60A5FA"), text_color=("#18181B", "#F4F4F5")).pack(pady=4)





        self.ql_instruction_label = ctk.CTkLabel(self.scanner_body,
            text="",
            wraplength=460, text_color=("#18181B", "#F4F4F5"), justify="center")
        self.ql_instruction_label.pack(pady=(10, 0))
        self._update_ql_instruction()



        self.custom_site_dialog = None



        self.settings_tab = self.notebook.tab("Settings")
        self.settings_tab.configure(fg_color=("#F4F4F5", "#18181B"), border_color=("#E4E4E7", "#27272A"), border_width=1)


        self.settings_tab.columnconfigure(0, weight=1)



        self.settings_scroll = ctk.CTkScrollableFrame(self.settings_tab, fg_color=("#F4F4F5", "#18181B"), border_color=("#E4E4E7", "#27272A"), border_width=1)

        self.settings_scroll.pack(fill="both", expand=True)

        self.settings_scroll.columnconfigure(0, weight=1)



        self.settings_body = ctk.CTkFrame(self.settings_scroll, fg_color=("#F4F4F5", "#18181B"), border_color=("#E4E4E7", "#27272A"), border_width=1)

        self.settings_body.pack(fill="both", expand=True, padx=5, pady=5)

        self.settings_body.columnconfigure(0, weight=1)

        for i in range(5):

            self.settings_body.rowconfigure(i, weight=0)

        self.settings_body.rowconfigure(4, weight=1)



        self.kb_frame = ctk.CTkFrame(self.settings_body, fg_color=("#F4F4F5", "#18181B"), border_color=("#E4E4E7", "#27272A"), border_width=1)

        self.kb_frame.grid(row=0, column=0, sticky="ew", **self.pad)

        self.kb_frame.columnconfigure(0, weight=0)

        self.kb_frame.columnconfigure(1, weight=1)

        ctk.CTkLabel(self.kb_frame, text="Keybinds (click a field, then press a key)", text_color=("#18181B", "#F4F4F5"), font=("Segoe UI", 11, "bold")).grid(row=0, column=0, columnspan=2, sticky="w", padx=6, pady=(0, 6))

        self.bind_vars = {}

        self.bind_entries = {}

        row = 1

        for name, label in [("quick_list_item", "Quick List Item"),

                            ("manual_crop_item", "Manual Crop Item"),

                            ("price_check_item", "Price Check Item"),

                            ("quit", "Quit App")]:

            ctk.CTkLabel(self.kb_frame, text=label, text_color=("#18181B", "#F4F4F5")).grid(

                row=row, column=0, sticky="w", padx=6, pady=3)

            var = ctk.StringVar(value=self._current_bind(name))

            self.bind_vars[name] = var

            entry = ctk.CTkEntry(self.kb_frame, textvariable=var, state="readonly", fg_color=("#F4F4F5", "#18181B"), border_color=("#E4E4E7", "#27272A"))

            entry.grid(row=row, column=1, sticky="ew", padx=6, pady=3)

            self.bind_entries[name] = entry

            entry.bind("<Button-1>", lambda e, n=name: self._start_bind_capture(n))

            row += 1

        # Keybinds are applied automatically when a key is captured
        # (on_apply_binds_silent) - no separate Apply button needed.


        self.opt_frame = ctk.CTkFrame(self.settings_body, fg_color=("#F4F4F5", "#18181B"), border_color=("#E4E4E7", "#27272A"), border_width=1)

        self.opt_frame.grid(row=1, column=0, sticky="ew", **self.pad)

        self.close_var = ctk.StringVar(value=self.cfg.get("close_behavior", "minimize"))

        ctk.CTkRadioButton(self.opt_frame, text="On close: exit the app",

            variable=self.close_var, value="exit", text_color=("#18181B", "#F4F4F5")).pack(anchor="w", padx=6, pady=2)

        ctk.CTkRadioButton(self.opt_frame, text="On close: minimize to system tray",

            variable=self.close_var, value="minimize", text_color=("#18181B", "#F4F4F5")).pack(anchor="w", padx=6, pady=2)

        self.startup_var = ctk.BooleanVar(value=self.cfg.get("start_with_windows", False))

        ctk.CTkCheckBox(self.opt_frame, text="Start with Windows",

            variable=self.startup_var, text_color=("#18181B", "#F4F4F5")).pack(anchor="w", padx=6, pady=2)

        self.manual_override_var = ctk.BooleanVar(

            value=self.cfg.get("manual_screenshot_override", False))

        ctk.CTkCheckBox(self.opt_frame, text="Manual screenshot override "
            "(quick-list always opens region selection)", text_color=("#18181B", "#F4F4F5"),
            variable=self.manual_override_var,
            command=self._on_toggle_manual_override).pack(
            anchor="w", padx=6, pady=(2, 6))

        self.save_captures_var = ctk.BooleanVar(
            value=self.cfg.get("scan", {}).get("save_captures", False))

        ctk.CTkCheckBox(self.opt_frame, text="Save screenshot captures "
            "(saves captured tooltips to the captures folder)",
            text_color=("#18181B", "#F4F4F5"),
            variable=self.save_captures_var).pack(
            anchor="w", padx=6, pady=(2, 6))

        ctk.CTkButton(self.opt_frame, text="Apply",

            command=self.on_apply_options, fg_color=("#3B82F6", "#3B82F6"), hover_color=("#60A5FA", "#60A5FA"), text_color=("#18181B", "#F4F4F5")).pack(anchor="w", padx=6, pady=(0, 6))



        self.theme_frame = ctk.CTkFrame(self.settings_body, fg_color=("#F4F4F5", "#18181B"), border_color=("#E4E4E7", "#27272A"), border_width=1)

        self.theme_frame.grid(row=2, column=0, sticky="ew", **self.pad)

        ctk.CTkLabel(self.theme_frame, text="Theme:", font=("Segoe UI", 11), text_color=("#18181B", "#F4F4F5")).grid(row=0, column=0, sticky="w", padx=6, pady=3)

        self.theme_combo_var = ctk.StringVar()

        saved_theme = self.cfg.get("theme", "system")

        # Normalize legacy stored values ("System Default (Auto)") to the
        # internal "system" so the combo always shows system default correctly.
        if saved_theme in ("system", "System Default (Auto)"):

            self.theme_combo_var.set("System Default (Auto)")

        elif saved_theme == "dark":

            self.theme_combo_var.set("Dark")

        else:

            self.theme_combo_var.set("Light")

        self.theme_combo = ctk.CTkOptionMenu(

            self.theme_frame,

            variable=self.theme_combo_var,

            values=["System Default (Auto)", "Light", "Dark"],

            width=200,

            command=self._on_theme_changed,

        )

        self.theme_combo.grid(row=0, column=1, sticky="w", padx=6, pady=3)



        self.src_frame = ctk.CTkFrame(self.settings_body, fg_color=("#F4F4F5", "#18181B"), border_color=("#E4E4E7", "#27272A"), border_width=1)

        self.src_frame.grid(row=3, column=0, sticky="ew", **self.pad)

        ctk.CTkLabel(self.src_frame, text="Window to capture from (matched by title):", text_color=("#18181B", "#F4F4F5"), wraplength=430).pack(anchor="w", padx=6, pady=(4, 0))

        self.source_combo = ctk.CTkComboBox(self.src_frame, width=300, fg_color=("#F4F4F5", "#18181B"), border_color=("#E4E4E7", "#27272A"), button_color=("#3B82F6", "#3B82F6"))

        self.source_combo.pack(anchor="w", padx=6, pady=2)

        self.source_combo.set(self.cfg.get("capture_window_title") or "diablo")

        self.src_btn_frame = ctk.CTkFrame(self.src_frame, fg_color=("#F4F4F5", "#18181B"), border_color=("#E4E4E7", "#27272A"), border_width=1)

        self.src_btn_frame.pack(anchor="w", padx=6, pady=(0, 6))

        ctk.CTkButton(self.src_btn_frame, text="Refresh list",

            command=self.on_refresh_windows, fg_color=("#3B82F6", "#3B82F6"), hover_color=("#60A5FA", "#60A5FA"), text_color=("#18181B", "#F4F4F5")).pack(side="left")

        ctk.CTkButton(self.src_btn_frame, text="Apply Capture Source",

            command=self.on_apply_capture_source, fg_color=("#3B82F6", "#3B82F6"), hover_color=("#60A5FA", "#60A5FA"), text_color=("#18181B", "#F4F4F5")).pack(side="left", padx=6)



    def _on_pc_model_changed(self, *args):

        self.cfg.setdefault("price_check", {})["ai_model"] = self.pc_model_var.get()

        save_config(self.cfg)

    def _on_pc_ai_launch_changed(self):
        """Save launch mode + exe path for the currently selected AI tool."""
        selected = self.pc_ai_tool_var.get()
        selected_key = self._pc_ai_key_by_label.get(selected)
        if not selected_key:
            return
        self.price_checker._save_ai_launch_config(
            selected_key,
            self.pc_ai_launch_var.get(),
            self.pc_ai_exe_path_var.get(),
        )
        save_config(self.cfg)
        self._update_launch_radio_states()

    def _update_launch_radio_states(self):
        """Dim the unselected launch mode radio button (still clickable).
        Highlight the exe path entry in red when exe mode is selected but no
        path is set. The exe path row is always visible."""
        mode = self.pc_ai_launch_var.get()
        active_color = ("#18181B", "#F4F4F5")
        dim_color = ("#808080", "#505050")
        if mode == "browser":
            self.pc_ai_launch_browser_btn.configure(text_color=active_color)
            self.pc_ai_launch_exe_btn.configure(text_color=dim_color)
            # Reset border color when not in exe mode
            self.pc_ai_exe_entry.configure(border_color=("#E4E4E7", "#27272A"))
        else:
            self.pc_ai_launch_browser_btn.configure(text_color=dim_color)
            self.pc_ai_launch_exe_btn.configure(text_color=active_color)
            exe_path = self.pc_ai_exe_path_var.get().strip()
            if not exe_path or not os.path.exists(exe_path):
                self.pc_ai_exe_entry.configure(border_color=("#FF4444", "#FF4444"))
            else:
                self.pc_ai_exe_entry.configure(border_color=("#E4E4E7", "#27272A"))

    def _browse_ai_exe(self):
        """Open a file dialog to select the AI tool desktop app exe."""
        from tkinter import filedialog
        path = filedialog.askopenfilename(
            title="Select AI tool executable",
            filetypes=[("Executables", "*.exe"), ("All files", "*.*")],
        )
        if path:
            self.pc_ai_exe_path_var.set(path)
            self._on_pc_ai_launch_changed()




    def _start_hotkeys(self):

        if self.hotkey_mgr:

            try:

                self.hotkey_mgr.stop()

            except Exception:

                pass

        handlers = {

            "quick_list_item": self.on_quick_list,

            "manual_crop_item": self.on_manual_crop_item,

            "price_check_item": self.on_price_check,

            "quit": self._on_close,

        }

        self.hotkey_mgr = HotkeyManager(handlers)

        bound = self.hotkey_mgr.start()

        self._set_status("Hotkeys: " + ", ".join(f"{format_hotkey(v)}={_friendly_keybind(k)}" for k, v in bound.items()))

        self._update_quick_list_button_text()

        self._refresh_pc_keybind_label()



    def _update_quick_list_button_text(self):

        """Update Quick List button text to show current keybinds."""

        ql_key = self.cfg.get("hotkeys", {}).get("quick_list_item", "f9")

        mc_key = self.cfg.get("hotkeys", {}).get("manual_crop_item", "f10")

        if hasattr(self, "manual_crop_btn") and self.manual_crop_btn:

            self.manual_crop_btn.configure(text=f"Manual Crop Item ({format_hotkey(mc_key)})")

        self._update_ql_instruction()

    def _update_ql_instruction(self):

        """Update the quick-list instruction text to reflect the current keybind."""

        ql_key = format_hotkey(self.cfg.get("hotkeys", {}).get("quick_list_item", "f9"))

        mc_key = format_hotkey(self.cfg.get("hotkeys", {}).get("manual_crop_item", "f10"))

        if hasattr(self, "ql_instruction_label") and self.ql_instruction_label:

            self.ql_instruction_label.configure(

                text=f"Hover an item in game, then press {ql_key} to quick-list.\n"

                     f"The tooltip is cropped, copied, and auto-pasted into the trading site's posting form.\n"

                     f"Or press {mc_key} to drag the region manually.\n"

                     f"When cropping manually: drag a rectangle, then press Enter to confirm.")

    def _on_toggle_manual_override(self):

        self.cfg["manual_screenshot_override"] = bool(self.manual_override_var.get())

        save_config(self.cfg)

        if self.cfg["manual_screenshot_override"]:

            self._set_status("Manual screenshot override ON - quick-list "

                             "always opens region selection")

        else:

            self._set_status("Manual screenshot override OFF - quick-list "

                             "auto-detects (manual selection only on failure)")



    def _minimize_to_tray(self):

        if pystray is None or Image is None:

            self._set_status("Tray icon unavailable (pystray/Pillow not in exe); minimizing to taskbar instead.")

            self.root.iconify()

            return

        try:

            try:

                img = Image.open(resource_path("Logo.png")).convert("RGBA")

                img = img.resize((32, 32), Image.LANCZOS)

            except Exception:

                img = Image.new("RGB", (32, 32), (200, 120, 40))

            self._tray_image = img

            self.root.withdraw()

            if self.tray is None:

                menu = pystray.Menu(

                    pystray.MenuItem("Show", self._show_from_tray, default=True),

                    pystray.MenuItem("Quit", self._quit_from_tray),

                )

                self.tray = pystray.Icon("d2scan", img, APP_TITLE, menu, visible=True)

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

        if not self.cfg.get("close_behavior_prompted", False):

            self._prompt_close_behavior()

            return

        if self.cfg.get("close_behavior", "minimize") == "minimize":

            self._minimize_to_tray()

        else:

            self._shutdown()





    def _shutdown(self):

        if hasattr(self, 'recorder') and self.recorder:

            try:

                self.recorder.stop()

            except Exception as e:

                print(f"[gui] recorder stop failed: {e}")

        if self.hotkey_mgr:

            try:

                self.hotkey_mgr.stop()

            except Exception as e:

                print(f"[gui] hotkey_mgr stop failed: {e}")

        if hasattr(self, 'tray') and self.tray:

            try:

                self.tray.stop()

            except Exception as e:

                print(f"[gui] tray stop failed: {e}")

        try:

            self.root.quit()

            self.root.destroy()

        except tk.TclError:

            pass

        import os

        os._exit(0)



    def run(self):

        self.root.mainloop()





    def _on_pc_mode_changed(self):
        mode = self.pc_mode_var.get()

        is_api = mode == "api"

        if is_api:

            self.pc_api_key_frame.grid(
                row=1, column=0, columnspan=3, sticky="ew", padx=20, pady=(0, 3)
            )
            # Re-show the API base URL field (it was hidden when switching away from API mode)
            if hasattr(self, "pc_api_base_url_frame"):
                self.pc_api_base_url_frame.grid(
                    row=3, column=0, columnspan=7, sticky="ew", padx=6, pady=(8, 0)
                )

            # Hide redirect-only options in API mode
            self.pc_ai_tool_label.grid_remove()
            self.pc_ai_tool_combo.grid_remove()
            if hasattr(self, "launch_frame"):
                self.launch_frame.grid_remove()
            self.pc_new_chat_label.grid_remove()
            self.pc_new_chat_combo.grid_remove()

            # Create the AI drawer if it doesn't exist yet (API mode needs it)
            if not hasattr(self, "ai_panel") or self.ai_panel is None:
                self._create_ai_response_panel()

            # Auto-fetch models when switching to API mode with a key present
            if self.pc_api_key_var.get().strip():
                self._on_pc_fetch_models()

        else:
            self.pc_api_key_frame.grid_remove()
            # Hide the API base URL field too (only relevant in API mode)
            if hasattr(self, "pc_api_base_url_frame"):
                self.pc_api_base_url_frame.grid_remove()
            # Show redirect-only options in redirect mode
            self.pc_ai_tool_label.grid(row=3, column=0, sticky="e", padx=20, pady=3)
            self.pc_ai_tool_combo.grid(row=3, column=1, columnspan=2, sticky="ew", padx=20, pady=3)
            if hasattr(self, "launch_frame"):
                self.launch_frame.grid(row=4, column=0, columnspan=3, sticky="w", padx=20, pady=(8, 2))
            self.pc_new_chat_label.grid(row=5, column=0, sticky="e", padx=20, pady=3)
            self.pc_new_chat_combo.grid(row=5, column=1, columnspan=2, sticky="ew", padx=20, pady=3)
            # Fully hide the AI drawer in redirect mode - results appear in the browser
            if hasattr(self, "ai_panel") and self.ai_panel is not None:
                self.ai_panel.withdraw()

        if hasattr(self, "pc_model_label"):
            self._on_pc_ai_tool_changed()


    def _on_pc_api_key_changed(self):
        """Auto-fetch models when the user types/pastes an API key (API mode)."""

        if self.pc_mode_var.get() != "api":

            return

        api_key = self.pc_api_key_var.get().strip()

        if not api_key:

            self.pc_model_label.grid_remove()

            self.pc_model_combo.grid_remove()

            return

        # Show model field when key is present in API mode

        self.pc_model_label.grid(row=5, column=0, sticky="e", padx=6, pady=3)

        self.pc_model_combo.grid(row=5, column=1, columnspan=2, sticky="ew", padx=6, pady=3)

        self._on_pc_fetch_models()



    def _on_pc_api_show_toggle(self):

        show = self.pc_api_show_var.get()

        self.pc_api_key_entry.configure(show="" if show else "*")

    def _on_pc_api_clear(self):

        """Clear the API key field and hide the model selector."""

        self.pc_api_key_var.set("")

        self.pc_model_label.grid_remove()

        self.pc_model_combo.grid_remove()

        self.pc_status_var.set("API key cleared.")




    def _on_pc_fetch_models(self):

        import urllib.request, urllib.error, json

        # Only fetch models in API mode (any AI tool can have a model field)

        if self.pc_mode_var.get() != "api":

            return

        api_key = self.pc_api_key_var.get().strip().replace(chr(10), "").replace(chr(13), "")

        if not api_key:

            self.pc_status_var.set("Enter an API key first.")

            return

        self.pc_status_var.set("Fetching models...")

        self.root.update_idletasks()

        def _fetch():

            # Detect provider from API key prefix (fully automated, no user input):

            #   sk-proj-* / sk-*  -> OpenAI-compatible (Bearer auth)

            #   nvapi-*           -> NVIDIA NIM (OpenAI-compatible, Bearer auth)

            #   anything else     -> Gemini (query param key)

            #   api_base_url override (if set) -> use custom base URL

            base_url = self.pc_api_base_url_var.get().strip()

            if api_key.startswith("sk-"):

                if base_url:

                    # Use user-specified base URL (e.g. Gemini)

                    url = base_url.rstrip("/") + "/v1/models"

                else:

                    url = "https://api.openai.com/v1/models"

                headers = {"Authorization": f"Bearer {api_key}"}

                parse = lambda data: sorted(

                    m.get("id", "") for m in data.get("data", []) if m.get("id")

                )

            elif api_key.startswith("nvapi-"):

                # NVIDIA NIM API (OpenAI-compatible, Bearer auth)

                url = "https://integrate.api.nvidia.com/v1/models"

                headers = {"Authorization": f"Bearer {api_key}"}

                parse = lambda data: sorted(

                    m.get("id", "") for m in data.get("data", []) if m.get("id")

                )

            else:

                url = f"https://generativelanguage.googleapis.com/v1/models?key={api_key}"

                headers = {}

                parse = lambda data: sorted(

                    m["name"].replace("models/", "")

                    for m in data.get("models", [])

                    if "generateContent" in m.get("supportedGenerationMethods", [])

                )

            try:

                req = urllib.request.Request(url, headers=headers)

                with urllib.request.urlopen(req, timeout=15) as resp:

                    data = json.loads(resp.read().decode("utf-8"))

                models = parse(data)

                if not models:

                    models = []

                # Remember the last successful API key

                self.cfg.setdefault("price_check", {})["last_successful_api_key"] = api_key

                save_config(self.cfg)

                self.root.after(0, lambda: self._on_models_fetched(models, None))

            except urllib.error.HTTPError as e:

                body = e.read().decode("utf-8")

                self.root.after(0, lambda _e=e, _body=body: self._on_models_fetched([], f"HTTP {_e.code}: {_body[:120]}"))

            except Exception as e:

                self.root.after(0, lambda _e=e: self._on_models_fetched([], str(_e)))

        import threading

        t = threading.Thread(target=_fetch, daemon=True)

        t.start()

        self.fetch_thread = t

    def _on_models_fetched(self, models, error):

        if error:

            self.pc_status_var.set(f"Model fetch failed: {error}")

            self.pc_model_combo.configure(values=[])

        else:

            self.pc_model_combo.configure(values=models)

            if models:

                current = self.pc_model_var.get()

                if current not in models:

                    self.pc_model_var.set(models[0])

                    self.cfg.setdefault("price_check", {})["ai_model"] = models[0]

                self.pc_status_var.set(f"Loaded {len(models)} models.")

            else:

                self.pc_model_var.set("")

                self.pc_status_var.set("No vision-capable models found for this API key.")



    def _on_pc_add_marketplace(self):

        name = self.pc_custom_mp_var.get().strip()

        if not name:

            return

        existing = [btn.cget("text") for btn in self.pc_mp_buttons]

        if name in existing:

            self._set_status(f"'{name}' is already in the marketplace list.")

            return

        if len(self.pc_mp_buttons) >= 5:

            self._set_status("Maximum of 5 marketplaces reached.")

            return

        btn = ctk.CTkButton(self.pc_mp_scroll, text=name, width=100,

            command=lambda m=name: self._on_pc_toggle_marketplace(m), fg_color=("#3B82F6", "#3B82F6"), hover_color=("#60A5FA", "#60A5FA"), text_color=("#18181B", "#F4F4F5"))

        btn.pack(fill="x", padx=2, pady=2)

        self.pc_mp_buttons.append(btn)

        self.pc_custom_mp_var.set("")

        self._save_pc_marketplaces()

        self._set_status(f"Added marketplace: {name}")



    def _on_pc_toggle_marketplace(self, name):

        """Mark/unmark a marketplace for removal (visual toggle)."""

        if name in self.pc_mp_selected:

            self.pc_mp_selected.discard(name)

        else:

            self.pc_mp_selected.add(name)

        self._refresh_mp_buttons()



    def _refresh_mp_buttons(self):

        for btn in self.pc_mp_buttons:

            nm = btn.cget("text")

            if nm in self.pc_mp_selected:

                btn.configure(border_width=2, border_color="#ffcc00")

            else:

                btn.configure(border_width=0)



    def _on_pc_remove_selected_marketplaces(self):

        selected = list(self.pc_mp_selected)

        if not selected:

            self._set_status("Select a marketplace to remove.")

            return

        for name in selected:

            for btn in self.pc_mp_buttons[:]:

                if btn.cget("text") == name:

                    btn.destroy()

                    self.pc_mp_buttons.remove(btn)

                    break

            self.pc_mp_selected.discard(name)

        self._save_pc_marketplaces()

        self._set_status("Removed marketplace(s): " + ", ".join(selected))



    def _save_pc_marketplaces(self):

        """Persist the current marketplace buttons to config and save."""

        remaining = [btn.cget("text") for btn in self.pc_mp_buttons]

        self.price_checker.get_config()["marketplaces"] = remaining

        self.price_checker.save()



    def _on_pc_timeframe_changed(self):

        is_custom = self.pc_timeframe_var.get() == "Custom"

        if is_custom:

            self.pc_custom_days_entry.pack(side="left", padx=(6, 0))

            self.pc_custom_days_label.pack(side="left", padx=(0, 4))

        else:

            self.pc_custom_days_entry.pack_forget()

            self.pc_custom_days_label.pack_forget()



    def _on_pc_ai_tool_changed(self):

        # Save the current tool's launch config before switching
        current_key = None
        for label, key in self._pc_ai_key_by_label.items():
            if key == self.pc_ai_tool_var.get():
                current_key = key
                break
        if current_key and current_key != "custom:new":
            self.price_checker._save_ai_launch_config(
                current_key,
                self.pc_ai_launch_var.get(),
                self.pc_ai_exe_path_var.get(),
            )

        selected = self.pc_ai_tool_var.get()

        if selected == "Add custom AI tool...":

            # Reset selection to the current AI tool (look up label from key)

            stored_key = self.price_checker.get_config().get("ai_tool", "gemini")

            stored_label = "Gemini"

            for label, key in self._pc_ai_key_by_label.items():

                if key == stored_key:

                    stored_label = label

                    break

            self.pc_ai_tool_var.set(stored_label)

            self._open_custom_ai_tool_dialog()

            return

        # Load the selected tool's saved launch config (browser vs exe + exe path)
        selected_key = self._pc_ai_key_by_label.get(selected)
        if selected_key:
            launch_mode, exe_path = self.price_checker._get_ai_launch_config(selected_key)
            self.pc_ai_launch_var.set(launch_mode)
            self.pc_ai_exe_path_var.set(exe_path or "")
            self._update_launch_radio_states()

        is_api = self.pc_mode_var.get() == "api"

        if is_api and self.pc_api_key_var.get().strip():

            self.pc_model_label.grid(row=5, column=0, sticky="e", padx=6, pady=3)

            self.pc_model_combo.grid(row=5, column=1, columnspan=2, sticky="ew", padx=6, pady=3)

            self.cfg.setdefault("price_check", {})["ai_model"] = self.pc_model_var.get()

            # Re-fetch models when switching AI tool with a key present

            self._on_pc_fetch_models()

        else:

            self.pc_model_label.grid_remove()

            self.pc_model_combo.grid_remove()



    def _on_pc_model_changed(self, *args):

        self.cfg.setdefault("price_check", {})["ai_model"] = self.pc_model_var.get()

        save_config(self.cfg)



    def _open_custom_ai_tool_dialog(self):

        dialog = ctk.CTkToplevel(self.root, fg_color=("#F4F4F5", "#18181B"))

        dialog.title("Add Custom AI Tool")

        try:

            dialog.iconbitmap(resource_path("Logo.ico"))

        except Exception:

            pass

        dialog.transient(self.root)

        dialog.grab_set()

        dialog.resizable(False, False)

        dialog.update_idletasks()

        x = self.root.winfo_rootx() + 50

        y = self.root.winfo_rooty() + 50

        dialog.geometry(f"450x520+{x}+{y}")

        # Clear any stale values from previous use

        self.pc_custom_ai_name_var.set("")

        self.pc_custom_ai_url_var.set("")

        self.pc_custom_ai_keybind_var.set("ctrl+shift+o")
        self.pc_custom_ai_exe_var.set("")



        frame = ctk.CTkFrame(dialog, fg_color=("#F4F4F5", "#18181B"), border_color=("#E4E4E7", "#27272A"), border_width=1)

        frame.pack(fill="both", expand=True, padx=24, pady=20)

        frame.columnconfigure(0, weight=0)
        frame.columnconfigure(1, weight=1)

        dialog_label_font = ("Segoe UI", 12, "bold")
        entry_font = ("Segoe UI", 11)

        ctk.CTkLabel(frame, text="AI Tool Name:", text_color=("#18181B", "#F4F4F5"), font=dialog_label_font).grid(row=0, column=0, sticky="w", padx=(0, 12), pady=(0, 10))

        name_entry = ctk.CTkEntry(frame, textvariable=self.pc_custom_ai_name_var, width=260, fg_color=("#F4F4F5", "#18181B"), border_color=("#E4E4E7", "#27272A"), font=entry_font, height=32)

        name_entry.grid(row=0, column=1, sticky="ew", pady=(0, 10), padx=(0, 0))

        ctk.CTkLabel(frame, text="URL:", text_color=("#18181B", "#F4F4F5"), font=dialog_label_font).grid(row=1, column=0, sticky="w", padx=(0, 12), pady=10)

        url_entry = ctk.CTkEntry(frame, textvariable=self.pc_custom_ai_url_var, width=260, fg_color=("#F4F4F5", "#18181B"), border_color=("#E4E4E7", "#27272A"), font=entry_font, height=32)

        url_entry.grid(row=1, column=1, sticky="ew", pady=10, padx=(0, 0))

        ctk.CTkLabel(frame, text="New chat keybind:", text_color=("#18181B", "#F4F4F5"), font=dialog_label_font).grid(row=2, column=0, sticky="w", padx=(0, 12), pady=10)

        key_entry = ctk.CTkEntry(frame, textvariable=self.pc_custom_ai_keybind_var, width=260, fg_color=("#F4F4F5", "#18181B"), border_color=("#E4E4E7", "#27272A"), font=entry_font, height=32)

        key_entry.grid(row=2, column=1, sticky="ew", pady=10, padx=(0, 0))


        ctk.CTkLabel(frame, text="Desktop app exe (optional):", text_color=("#18181B", "#F4F4F5"), font=dialog_label_font).grid(row=3, column=0, sticky="w", padx=(0, 12), pady=10)
        exe_frame = ctk.CTkFrame(frame, fg_color="transparent")
        exe_frame.grid(row=3, column=1, sticky="ew", pady=10, padx=(0, 0))
        exe_frame.columnconfigure(0, weight=1)
        exe_entry = ctk.CTkEntry(exe_frame, textvariable=self.pc_custom_ai_exe_var, width=200, fg_color=("#F4F4F5", "#18181B"), border_color=("#E4E4E7", "#27272A"), font=entry_font, height=32)
        exe_entry.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        exe_browse_btn = ctk.CTkButton(exe_frame, text="Browse", width=70, command=self._browse_ai_exe, fg_color=("#3B82F6", "#3B82F6"), hover_color=("#60A5FA", "#60A5FA"), text_color=("#18181B", "#F4F4F5"))
        exe_browse_btn.grid(row=0, column=1)
        ctk.CTkLabel(frame, text="Example: My Gemini - https://gemini.google.com - ctrl+shift+o", text_color=("#18181B", "#F4F4F5"), font=("Segoe UI", 13), wraplength=380).grid(row=4, column=0, columnspan=2, sticky="ew", padx=(0, 0), pady=(0, 14))

        btn_frame = ctk.CTkFrame(frame, fg_color=("#F4F4F5", "#18181B"), border_color=("#E4E4E7", "#27272A"), border_width=1)

        btn_frame.grid(row=5, column=0, columnspan=2, pady=10)



        def _save():

            name = self.pc_custom_ai_name_var.get().strip()

            url = self.pc_custom_ai_url_var.get().strip()

            new_chat_keybind = self.pc_custom_ai_keybind_var.get().strip()

            if not name:

                self._set_status("Give the AI tool a name.")

                return

            if not url:

                self._set_status("Enter a URL for the AI tool.")

                return

            if not url.lower().startswith(("http://", "https://")):

                url = "https://" + url

            try:

                key = self.price_checker.save_custom_ai_tool(name, url, new_chat_keybind, self.pc_custom_ai_exe_var.get())

                save_config(self.cfg)

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



        ctk.CTkButton(btn_frame, text="Save", command=_save, fg_color=("#3B82F6", "#3B82F6"), hover_color=("#60A5FA", "#60A5FA"), text_color=("#18181B", "#F4F4F5")).pack(side="left", padx=(0, 10))

        ctk.CTkButton(btn_frame, text="Cancel", command=dialog.destroy, fg_color=("#27272A", "#27272A"), hover_color=("#27272A", "#27272A"), text_color=("#18181B", "#F4F4F5")).pack(side="left")



        dialog.wait_window(dialog)



    def on_price_check(self):

        # Validate: if redirect mode + desktop app selected but no valid exe path,
        # show error and highlight the entry field in red
        if self.pc_mode_var.get() == "redirect" and self.pc_ai_launch_var.get() == "exe":
            exe_path = self.pc_ai_exe_path_var.get().strip()
            if not exe_path or not os.path.exists(exe_path):
                self.pc_ai_exe_entry.configure(border_color=("#FF4444", "#FF4444"))
                messagebox.showerror(
                    "No exe path set",
                    "Desktop app mode is selected but no valid exe path has been set. "
                    "Please enter or browse for the AI tool's executable path, "
                    "or switch to Open in browser.",
                    parent=self.root,
                )
                return

        pc_cfg = self.price_checker.get_config()

        pc_cfg["mode"] = self.pc_mode_var.get()

        ai_label = self.pc_ai_tool_var.get()

        pc_cfg["ai_tool"] = self._pc_ai_key_by_label.get(ai_label, "gemini")

        pc_cfg["api_key"] = self.pc_api_key_var.get().strip().replace("\n", "").replace("\r", "")

        pc_cfg["api_base_url"] = self.pc_api_base_url_var.get().strip()

        pc_cfg["platform"] = "PC"

        pc_cfg["ladder_status"] = self.pc_ladder_var.get()

        pc_cfg["hardcore_status"] = self.pc_hardcore_var.get()

        pc_cfg["marketplaces"] = [btn.cget("text") for btn in self.pc_mp_buttons]

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

        pc_cfg["new_chat_each_time"] = (self.pc_new_chat_var.get() == "New chat each time")

        self.price_checker.save()



        variables = {

            "PLATFORM": pc_cfg["platform"],
            "GAME_MODE": pc_cfg["ladder_status"],

            "MODE_TYPE": pc_cfg["hardcore_status"],

            "TARGET_MARKETPLACES": ", ".join(pc_cfg["marketplaces"]),

            "PRICE_HISTORY_WINDOW": pc_cfg["timeframe"],

        }



        mode = pc_cfg["mode"]

        ai_tool = pc_cfg["ai_tool"]

        api_key = pc_cfg["api_key"]






        self.pc_status_var.set(random.choice(DIABLO_FLAVOR_MESSAGES))
        # Show a "thinking" indicator in the AI panel so the user knows
        # the request is in flight (AI model calls can take several seconds).
        self._show_ai_response(random.choice(DIABLO_FLAVOR_MESSAGES))
        # Start cycling the status message so the user knows work is in progress
        self._start_status_cycle()




        def _on_result(success, message):

            self.root.after(0, lambda: self._pc_on_result(success, message))



        self.price_checker.run(mode, ai_tool, api_key, variables, _on_result)





    def _start_status_cycle(self):
        """Begin cycling the status message through Diablo-flavored text
        so the user knows a price check is in progress."""
        self._stop_status_cycle()
        self._status_cycle_after_id = self.root.after(3000, self._cycle_status_message)

    def _stop_status_cycle(self):
        """Cancel any pending status-message cycle."""
        if self._status_cycle_after_id is not None:
            try:
                self.root.after_cancel(self._status_cycle_after_id)
            except Exception:
                pass
            self._status_cycle_after_id = None

    def _cycle_status_message(self):
        """Rotate to the next Diablo-flavored status message and reschedule
        itself every 3 seconds while a price check is in flight.
        Never repeats the same message twice in a row so the change is visible."""
        self._stop_status_cycle()
        current = self.pc_status_var.get()
        options = [m for m in DIABLO_FLAVOR_MESSAGES if m != current]
        if not options:
            options = DIABLO_FLAVOR_MESSAGES
        self.pc_status_var.set(random.choice(options))
        self._status_cycle_after_id = self.root.after(3000, self._cycle_status_message)

    def _pc_on_result(self, success, message):

        # Stop cycling the status message now that the request is done
        self._stop_status_cycle()

        if self.pc_mode_var.get() == "api":

            if success:

                # Guard against empty/None response content from the API
                if not message or not message.strip():
                    self._show_ai_response(
                        "Empty response from API. Please try again."
                    )
                    self.pc_status_var.set(
                        "Empty response from API. Please try again."
                    )
                else:
                    self._show_ai_response(message)
                    self.pc_status_var.set("Price check complete.")

            else:


                clean = _clean_error_message(message)
                self.pc_status_var.set(clean)
                self._show_ai_response(f"Error: {clean}")

        else:

            if success:

                self.pc_status_var.set("Opened AI tool in browser - paste prompt + screenshot.")

            else:

                self.pc_status_var.set(_clean_error_message(message))





    def _create_ai_response_panel(self):
        """Create the AI response panel as a separate CTkToplevel window.

        Always a separate window positioned immediately to the right of the
        main application. Defaults to Locked mode: no native title bar or
        frame (overrideredirect=True) so the user cannot drag it
        independently, and its position is tightly coupled to the main
        window's movement via the <Configure> binding so it glides alongside
        instantly with zero drag lag.
        """
        self.ai_locked = True

        self.ai_panel = ctk.CTkToplevel(self.root)
        # Locked mode: remove native title bar/frame - user cannot drag independently
        self.ai_panel.overrideredirect(True)
        self.ai_panel.attributes("-topmost", False)

        # Position at right edge of main window after it is mapped
        self.root.after_idle(self._position_ai_panel_initial)
        # Hide until positioned
        self.ai_panel.withdraw()
        # Track window state changes
        self.root.bind("<Unmap>", self._on_root_unmap)
        self.root.bind("<Map>", self._on_root_map)
        # Tightly couple panel position to main window movement (zero-lag tracking)
        self.root.bind("<Configure>", self._on_root_configure)
        # Apply app logo to drawer
        try:
            self.ai_panel.iconbitmap(resource_path("Logo.ico"))
        except Exception:
            pass
        # Build internal UI
        self._build_ai_panel_ui(self.ai_panel)

        # Start expanded (visible)
        self.ai_panel_expanded = False
        self._show_ai_panel()

    def _build_ai_panel_ui(self, parent):
        """Build the AI panel's internal widgets under the given parent."""
        # Title bar
        self.ai_title_bar = ctk.CTkFrame(
            parent, fg_color=("#F3F4F6", "#27272A"), height=30
        )
        self.ai_title_bar.pack(fill="x", padx=2, pady=2)
        self.ai_title_bar.pack_propagate(False)

        self.ai_toggle_btn = ctk.CTkButton(
            self.ai_title_bar,
            text="←",
            width=40,
            command=self._toggle_ai_panel,
            fg_color=("#3B82F6", "#3B82F6"),
            hover_color=("#60A5FA", "#60A5FA"),
        )
        self.ai_toggle_btn.pack(side="left", padx=5, pady=5)

        self.ai_undock_btn = ctk.CTkButton(
            self.ai_title_bar,
            text="↗ Undock",
            width=70,
            command=self._undock_ai_panel,
            fg_color=("#6B7280", "#6B7280"),
            hover_color=("#9CA3AF", "#9CA3AF"),
        )
        self.ai_undock_btn.pack(side="right", padx=5, pady=5)

        # Mode toggle: Side <-> Bottom drawer (user-selectable)
        self.ai_mode_btn = ctk.CTkButton(
            self.ai_title_bar,
            text="⇄ Bottom" if self.ai_panel_mode == "side" else "⇄ Side",
            width=70,
            command=self._toggle_ai_panel_mode,
            fg_color=("#6B7280", "#6B7280"),
            hover_color=("#9CA3AF", "#9CA3AF"),
        )
        self.ai_mode_btn.pack(side="right", padx=5, pady=5)

        self.ai_title_label = ctk.CTkLabel(
            self.ai_title_bar,
            text="AI Response",
            text_color=("#18181B", "#D9D9D9"),
            font=("Segoe UI", 18, "bold"),
        )
        self.ai_title_label.pack(side="top", padx=5, pady=2)

        # Response text area
        self.ai_response_text = ctk.CTkTextbox(
            parent,
            font=("Consolas", 15),
            fg_color=("#F4F4F5", "#1a1a1a"),
            border_color=("#E4E4E7", "#27272A"),
            border_width=1,
            text_color=("#18181B", "#D9D9D9"),
            wrap="word",
        )
        self.ai_response_text.pack(fill="both", expand=True, padx=5, pady=5)

    def _compute_ai_panel_geometry(self, expanded):
        """Compute the geometry string for the AI panel based on user-selected mode.

        Side drawer: fixed width, 90% of main-window height (5% margin top/bottom),
        positioned at the right edge.
        Bottom drawer: full main-window width, fixed height, positioned at bottom edge.
        When the main window is maximized, the drawer always snaps to the bottom.
        """
        if not self.ai_locked:
            return None

        root_x = self.root.winfo_rootx()
        root_y = self.root.winfo_rooty()
        root_w = self.root.winfo_width()
        root_h = self.root.winfo_height()

        mode = self.ai_panel_mode
        # When the main window is maximized, always snap the drawer to the
        # bottom edge regardless of the user-selected side/bottom mode.
        is_maximized = self.root.state() == "zoomed"
        use_bottom = (mode == "bottom") or is_maximized

        if use_bottom:
            # Bottom drawer mode (user-selected, or maximized snap-to-bottom)
            if expanded:
                w = root_w
                h = self.ai_bottom_height
                x = root_x
                y = root_y + root_h - self.ai_bottom_height
            else:
                w = root_w
                h = self.ai_bottom_minimized_height
                x = root_x
                y = root_y + root_h - self.ai_bottom_minimized_height
        else:
            # Side drawer mode (user-selected, default).
            # Width fixed at 400px; height is 90% of main window height
            # (5% margin top and bottom).
            if expanded:
                w = self.ai_panel_width
                h = int(root_h * 0.90)
                x = root_x + root_w
                y = root_y + int(root_h * 0.05)
            else:
                w = self.ai_panel_minimized_width
                h = int(root_h * 0.90)
                x = root_x + root_w
                y = root_y + int(root_h * 0.05)

        # Note: panel position is computed relative to the main window's
        # position, so it naturally follows the window to any screen.
        # Do not clamp to primary screen bounds, or the panel would snap
        # back to the primary monitor when the app is on a secondary screen.

        return f"{w}x{h}+{x}+{y}"

    def _apply_ai_panel_layout(self, expanded):
        """Apply the responsive layout geometry to the AI panel."""
        if not self.ai_panel or not self.ai_panel.winfo_exists():
            return
        # Only show the panel in API mode - redirect mode keeps it hidden
        if self.pc_mode_var.get() != "api":
            return
        geom = self._compute_ai_panel_geometry(expanded)
        if geom:
            # Ensure the panel is visible and on top (overrideredirect windows
            # can lose visibility during parent window moves on Windows)
            self.ai_panel.deiconify()
            self.ai_panel.geometry(geom)
            self.ai_panel.lift()

    def _toggle_ai_panel(self):
        """Toggle the AI response panel between expanded and hidden (locked mode only)."""
        if self.ai_panel_expanded:
            self._hide_ai_panel()
        else:
            self._show_ai_panel()

    def _toggle_ai_panel_mode(self):
        """Switch the AI panel between Side and Bottom drawer mode (user choice)."""
        if self.ai_panel_mode == "side":
            self.ai_panel_mode = "bottom"
            self.ai_mode_btn.configure(text="⇄ Side")
        else:
            self.ai_panel_mode = "side"
            self.ai_mode_btn.configure(text="⇄ Bottom")

        # Re-apply layout immediately so the panel snaps to the new position
        if self.ai_locked and self.ai_panel and self.ai_panel.winfo_exists():
            self._apply_ai_panel_layout(expanded=self.ai_panel_expanded)

    def _show_ai_panel(self):
        """Show the AI response panel (expanded) with responsive side/bottom layout."""
        if not self.ai_panel:
            return
        # Only show the panel in API mode - redirect mode keeps it hidden
        if self.pc_mode_var.get() != "api":
            return
        self.ai_panel.deiconify()
        self.ai_panel.lift()

        if self.ai_locked:
            self._apply_ai_panel_layout(expanded=True)

        if hasattr(self, "ai_toggle_btn"):
            self.ai_toggle_btn.configure(text="←")
        self.ai_panel_expanded = True

    def _hide_ai_panel(self):
        """Collapse the AI response panel to a slim tab (locked mode) with responsive layout."""
        if not self.ai_panel:
            return

        if self.ai_locked:
            self._apply_ai_panel_layout(expanded=False)
        else:
            # Undocked: withdraw the window entirely
            self.ai_panel.withdraw()

        if hasattr(self, "ai_toggle_btn"):
            self.ai_toggle_btn.configure(text="→")
        self.ai_panel_expanded = False

    def _undock_ai_panel(self):
        """Switch to Undock mode: re-enable window framing so it can be freely dragged."""
        if not self.ai_panel:
            return

        # Save current mode/state so we can restore it on re-dock
        self.ai_panel_mode_before_undock = self.ai_panel_mode
        self.ai_panel_expanded_before_undock = self.ai_panel_expanded

        # Re-enable standard window framing (title bar) so the window can be dragged freely
        self.ai_panel.overrideredirect(False)

        self.ai_locked = False

        # Update buttons: toggle becomes Dock, hide Undock and Mode buttons
        self.ai_toggle_btn.configure(command=self._dock_ai_panel, text="← Dock")
        self.ai_undock_btn.pack_forget()
        self.ai_mode_btn.pack_forget()

        self.ai_panel_expanded = True

        # Make sure it is visible
        self.ai_panel.deiconify()
        self.ai_panel.lift()

    def _dock_ai_panel(self):
        """Switch back to Locked mode: remove frame, snap to side, lock position."""
        if not self.ai_panel:
            return

        # Remove native title bar/frame - lock position to the main window
        self.ai_panel.overrideredirect(True)

        self.ai_locked = True

        # Determine which mode to restore on re-dock:
        # - If app is maximized → force bottom mode
        # - Otherwise restore whatever mode was active before undocking
        #   (side stays side, bottom stays bottom)
        is_maximized = self.root.state() == "zoomed"

        if is_maximized:
            self.ai_panel_mode = "bottom"
            self.ai_panel_expanded = True
        else:
            self.ai_panel_mode = self.ai_panel_mode_before_undock
            self.ai_panel_expanded = self.ai_panel_expanded_before_undock


        self.ai_mode_btn.configure(
            text="⇄ Side" if self.ai_panel_mode == "bottom" else "⇄ Bottom"
        )

        # Reset pack order for all title bar widgets to avoid packer state issues
        # after pack_forget cycles during undock/dock transitions
        self.ai_toggle_btn.pack_forget()
        self.ai_undock_btn.pack_forget()
        self.ai_mode_btn.pack_forget()
        self.ai_title_label.pack_forget()

        self.ai_toggle_btn.pack(side="left", padx=5, pady=5)
        self.ai_undock_btn.pack(side="right", padx=5, pady=5)
        self.ai_mode_btn.pack(side="right", padx=5, pady=5)
        self.ai_title_label.pack(side="top", padx=5, pady=2)

        # Restore button commands
        self.ai_toggle_btn.configure(command=self._toggle_ai_panel, text="←")
        self.ai_undock_btn.configure(command=self._undock_ai_panel)

        # Show panel with restored layout
        self.ai_panel.deiconify()
        self.ai_panel.lift()
        self._apply_ai_panel_layout(expanded=self.ai_panel_expanded)

    def _show_ai_response(self, message):
        """Display the AI response in the panel and expand it."""
        if not self.ai_panel:
            return

        # Guard against empty/None message which causes Tkinter TclError
        if not message or not message.strip():
            message = "Empty response from API. Please try again."

        self.ai_response_text.delete("0.0", "end")
        self.ai_response_text.insert("0.0", message)
        self._show_ai_panel()

    def _position_ai_panel_initial(self):
        """Position the AI panel at the right edge after the root window is mapped."""
        if not self.ai_panel:
            return
        self._show_ai_panel()

    def _on_root_unmap(self, event):
        """Hide the AI panel when the main app window is minimized."""
        if self.ai_panel and self.ai_panel.winfo_exists():
            self.ai_panel.withdraw()

    def _on_root_map(self, event):
        """Show the AI panel again when the main app window is restored.

        Only in API mode - redirect mode keeps the panel hidden since
        results appear in the browser. Force-hide it here in case something
        else showed it while the window was unfocused.
        """
        if self.pc_mode_var.get() != "api":
            # Force-hide the panel in redirect mode regardless of expanded state
            if self.ai_panel and self.ai_panel.winfo_exists():
                self.ai_panel.withdraw()
            return
        if self.ai_panel and self.ai_panel.winfo_exists():
            if self.ai_panel_expanded:
                self._show_ai_panel()
            else:
                self._hide_ai_panel()

    def _on_root_configure(self, event):
        """Tightly couple the locked panel's position to the main window's movement.

        When locked, the panel glides alongside the main window instantly
        (zero lag) because its X/Y is updated on every <Configure> event.
        The side/bottom mode is user-controlled (not automatic).
        """
        if not self.ai_locked:
            return
        if not self.ai_panel or not self.ai_panel.winfo_exists():
            return
        if event.widget is self.root:
            self._apply_ai_panel_layout(expanded=self.ai_panel_expanded)

    def _refresh_site_combo(self):

        from config import trading_site_options

        opts = trading_site_options(self.cfg)

        labels = [label for _key, label in opts]

        keys = [key for key, _label in opts]

        self._site_key_by_label = dict(zip(labels, keys))

        self.site_combo["values"] = labels

        saved_site = self.cfg.get("trading_site", {}).get("site", "")

        saved_label = None

        for label, key in self._site_key_by_label.items():

            if key == saved_site:

                saved_label = label

                break

        if saved_label is None and labels:

            saved_label = labels[0]

        self.site_var.set(saved_label or "")



    def _current_bind(self, name):

        combo = self.cfg["hotkeys"].get(name, "F7")

        return format_hotkey(combo)



    def on_quick_list(self):

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

                    self._set_status("Cancelled - nothing was posted.")

                    return

                if manual:

                    desc = f"Manual crop ({img.shape[1]}x{img.shape[0]}px)"

                else:

                    from capture import last_yolo_capture

                    c = last_yolo_capture

                    desc = (f"Tooltip cropped by YOLO v6 (conf {c['conf']:.2f}, "

                            f"{c.get('class_name', 'item')})")

                self._post_crop(img, desc)

            except Exception as e:

                self._set_status(f"Quick-list failed: {e}")

        threading.Thread(target=_run, daemon=True).start()



    def on_manual_crop_item(self):

        def _run():

            img = self._manual_crop_blocking("Manual crop - drag the tooltip area to post")

            if img is None or img.size == 0:

                self._set_status("Cancelled - nothing was posted.")

                return

            self._post_crop(img, f"Manual crop ({img.shape[1]}x{img.shape[0]}px)")

        threading.Thread(target=_run, daemon=True).start()



    def on_site_selected(self, event=None):

        from config import trading_site_options, add_custom_site, get_trading_url

        label = self.site_var.get()

        opts = trading_site_options(self.cfg)

        selected_key = None

        for key, lbl in opts:

            if lbl == label:

                selected_key = key

                break

        if selected_key:

            # "New custom site..." selection -> open the add dialog

            if selected_key == "custom:new":

                self.site_var.set("")

                self._open_custom_site_dialog()

                return

            if selected_key.startswith("custom:"):

                self.cfg["trading_site"]["site"] = selected_key

                customs = self.cfg.get("trading_site", {}).get("custom_sites", [])

                for cs in customs:

                    if cs.get("key") == selected_key:

                        self.custom_name_var.set(cs.get("name", ""))

                        self.custom_url_var.set(cs.get("url", ""))

                        break

                self.custom_row.pack(fill="x", padx=8, pady=(2, 6))

            else:

                self.cfg["trading_site"]["site"] = selected_key

                self.custom_row.pack_forget()

            _, url = get_trading_url(self.cfg)

            self.cfg.setdefault("diablo2io", {})["posting_url"] = url

            save_config(self.cfg)

            self._set_status(f"Trading site set to {label}")



    def on_save_custom_site(self):

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



    def on_open_site(self):

        from config import get_trading_url

        import os

        _, url = get_trading_url(self.cfg)

        if url:

            os.startfile(url)

            self._set_status(f"Opened {url}")

        else:

            self._set_status("No trading site URL configured")

    def _open_custom_site_dialog(self):

        """Open a dialog to add a new custom trading site.

        Instructs the user to provide a site name and the URL of the trade page.

        """

        dialog = ctk.CTkToplevel(self.root, fg_color=("#F4F4F5", "#18181B"))

        dialog.title("Add Custom Trading Site")

        try:

            dialog.iconbitmap(resource_path("Logo.ico"))

        except Exception:

            pass

        dialog.transient(self.root)

        dialog.grab_set()

        dialog.resizable(False, False)

        dialog.update_idletasks()

        x = self.root.winfo_rootx() + 50

        y = self.root.winfo_rooty() + 50

        dialog.geometry(f"420x400+{x}+{y}")

        frame = ctk.CTkFrame(dialog, fg_color=("#F4F4F5", "#18181B"), border_color=("#E4E4E7", "#27272A"), border_width=1)

        frame.pack(fill="both", expand=True, padx=24, pady=20)

        frame.columnconfigure(0, weight=0)
        frame.columnconfigure(1, weight=1)

        dialog_label_font = ("Segoe UI", 12, "bold")
        entry_font = ("Segoe UI", 11)

        ctk.CTkLabel(frame, text="Site Name:", text_color=("#18181B", "#F4F4F5"), font=dialog_label_font).grid(row=0, column=0, sticky="w", padx=(0, 12), pady=(0, 10))

        name_var = ctk.StringVar()

        name_entry = ctk.CTkEntry(frame, textvariable=name_var, width=260, fg_color=("#F4F4F5", "#18181B"), border_color=("#E4E4E7", "#27272A"), font=entry_font, height=32)

        name_entry.grid(row=0, column=1, sticky="ew", pady=(0, 10), padx=(0, 0))

        ctk.CTkLabel(frame, text="Trade Page URL:", text_color=("#18181B", "#F4F4F5"), font=dialog_label_font).grid(row=1, column=0, sticky="w", padx=(0, 12), pady=10)

        url_var = ctk.StringVar()

        url_entry = ctk.CTkEntry(frame, textvariable=url_var, width=260, fg_color=("#F4F4F5", "#18181B"), border_color=("#E4E4E7", "#27272A"), font=entry_font, height=32)

        url_entry.grid(row=1, column=1, sticky="ew", pady=10, padx=(0, 0))

        ctk.CTkLabel(frame, text="Example: Diablo2.io - https://diablo2.io/posting.php?f=16&mode=post", text_color=("#18181B", "#F4F4F5"), font=("Segoe UI", 13), wraplength=350).grid(row=2, column=0, columnspan=2, sticky="ew", padx=(0, 0), pady=(0, 14))

        btn_frame = ctk.CTkFrame(frame, fg_color=("#F4F4F5", "#18181B"), border_color=("#E4E4E7", "#27272A"), border_width=1)

        btn_frame.grid(row=3, column=0, columnspan=2, pady=10)

        def _save():

            name = name_var.get().strip()

            url = url_var.get().strip()

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

                dialog.destroy()

            except Exception as e:

                messagebox.showerror("Error", f"Failed to save site: {e}")

        ctk.CTkButton(btn_frame, text="Save", command=_save, fg_color=("#3B82F6", "#3B82F6"), hover_color=("#60A5FA", "#60A5FA"), text_color=("#18181B", "#F4F4F5")).pack(side="left", padx=6)

        ctk.CTkButton(btn_frame, text="Cancel", command=dialog.destroy, fg_color=("#27272A", "#27272A"), hover_color=("#27272A", "#27272A"), text_color=("#18181B", "#F4F4F5")).pack(side="left", padx=6)

        name_entry.focus_set()



    def on_apply_binds(self):

        from hotkeys import validate_combo

        for name, var in self.bind_vars.items():

            combo = var.get().strip().lower()

            if not combo:

                messagebox.showerror("Keybinds", f"Empty keybind for '{name}'")

                return

            ok, err = validate_combo(combo)

            if not ok:

                messagebox.showerror("Keybinds",

                    f"Invalid combo '{combo}' for '{name}':\\n{err}")

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

        self.cfg["close_behavior_prompted"] = True

        self.cfg["start_with_windows"] = bool(self.startup_var.get())

        self.cfg["manual_screenshot_override"] = bool(self.manual_override_var.get())

        self.cfg.setdefault("scan", {})["save_captures"] = bool(self.save_captures_var.get())

        save_config(self.cfg)

        if not set_start_with_windows(self.startup_var.get()):

            messagebox.showwarning("Options",

                "Could not update the Windows startup entry.")

        self._set_status("Options saved.")



    def on_refresh_windows(self):

        self.source_combo.set(self.cfg.get("capture_window_title") or "diablo")

        self._set_status("Capture source refreshed.")



    def on_apply_capture_source(self):

        self.cfg["capture_window_title"] = self.source_combo.get()

        save_config(self.cfg)

        self._set_status(f"Capture source set to: {self.cfg['capture_window_title']}")



    def _start_bind_capture(self, name):

        if self._bind_listener is not None:

            try:

                self._bind_listener.stop()

            except Exception:

                pass

            self._bind_listener = None

        var = self.bind_vars[name]

        entry = self.bind_entries[name]

        var.set("Press a key... (Esc to Cancel)")

        pressed_mods = set()

        finished = [False]



        from pynput import keyboard



        def _finish(combo):

            finished[0] = True

            listener.stop()

            self._bind_listener = None



            def _apply():

                formatted = format_hotkey(combo)
                var.set(formatted)

                self.on_apply_binds_silent(name, formatted)

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

            base = kname.replace("_l", "").replace("_r", "").replace("_gr", "")

            if base in ("ctrl", "control", "alt", "shift", "cmd", "win"):

                pressed_mods.add({"control": "ctrl"}.get(base, base))

                return

            if kname == "escape":

                listener.stop()

                self._bind_listener = None



                def _restore():

                    var.set(self._current_bind(name))

                try:

                    self.root.after(0, _restore)

                except RuntimeError:

                    pass

                return

            key_part = kname if len(kname) == 1 else kname

            mods = [m for m in ("ctrl", "alt", "shift") if m in pressed_mods]

            combo = "+".join(mods + [key_part])

            _finish(combo)



        listener = keyboard.Listener(on_press=on_press)

        self._bind_listener = listener

        listener.daemon = True

        listener.start()



    def on_apply_binds_silent(self, name, combo):

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

        self._update_ql_instruction()

        self._set_status(f"Keybind set: {name} = {combo}")



    def _open_crop_dialog(self, img_bgr, title, on_done):

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

                if not self.root.winfo_exists():

                    return None

            except RuntimeError:

                return None

        return holder.get("crop")



    def _post_crop(self, img, source_desc):

        import cv2

        from PIL import Image

        from config import get_trading_url

        _, url = get_trading_url(self.cfg)

        os.startfile(url)

        focus_browser_window(url)

        pil = Image.fromarray(cv2.cvtColor(img, cv2.COLOR_BGR2RGB))

        self._set_status(f"{source_desc} - posting page opened")



        def _clipboard_then_paste():

            try:

                if not copy_image_to_clipboard(pil):

                    self._set_status(f"Opened {url} - clipboard copy failed; "

                                     "check that pywin32 is installed")

                    return

                from hotkeys import focus_and_paste

                focus_and_paste(url)

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



    def _on_toggle_manual_override(self):

        self.cfg["manual_screenshot_override"] = bool(self.manual_override_var.get())

        save_config(self.cfg)

        if self.cfg["manual_screenshot_override"]:

            self._set_status("Manual screenshot override ON - quick-list "

                             "always opens region selection")

        else:

            self._set_status("Manual screenshot override OFF - quick-list "

                             "auto-detects (manual selection only on failure)")



    def _minimize_to_tray(self):

        if pystray is None or Image is None:

            self._set_status("Tray icon unavailable (pystray/Pillow not in exe); minimizing to taskbar instead.")

            self.root.iconify()

            return

        try:

            try:

                img = Image.open(resource_path("Logo.png")).convert("RGBA")

                img = img.resize((32, 32), Image.LANCZOS)

            except Exception:

                img = Image.new("RGB", (32, 32), (200, 120, 40))

            self._tray_image = img

            self.root.withdraw()

            if self.tray is None:

                menu = pystray.Menu(

                    pystray.MenuItem("Show", self._show_from_tray, default=True),

                    pystray.MenuItem("Quit", self._quit_from_tray),

                )

                self.tray = pystray.Icon("d2scan", img, APP_TITLE, menu, visible=True)

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

        if not self.cfg.get("close_behavior_prompted", False):

            self._prompt_close_behavior()

            return

        if self.cfg.get("close_behavior", "minimize") == "minimize":

            self._minimize_to_tray()

        else:

            self._shutdown()



    def _prompt_close_behavior(self):

        # Ensure the dialog appears in front of the AI response panel
        # and any other windows by temporarily making the root topmost.
        was_topmost = self.root.attributes("-topmost")
        self.root.attributes("-topmost", True)

        try:
            choice = messagebox.askyesnocancel(

                "D2TradeHelper - Close behavior",

                "When you close the window, would you like to keep the app open?\n\n"

                "  Yes → Minimize to system tray (app keeps running in background)\n"

                "  No   → Exit the app completely\n"

                "  Cancel → Keep the window open",

                icon="question", parent=self.root,

            )
        finally:
            self.root.attributes("-topmost", was_topmost)

        if choice is None:

            return

        if choice:

            self.cfg["close_behavior"] = "minimize"

        else:

            self.cfg["close_behavior"] = "exit"

        self.cfg["close_behavior_prompted"] = True

        self.close_var.set(self.cfg["close_behavior"])

        save_config(self.cfg)

        if self.cfg["close_behavior"] == "minimize":

            self._minimize_to_tray()

        else:

            self._shutdown()



    def _shutdown(self):

        if hasattr(self, 'recorder') and self.recorder:

            try:

                self.recorder.stop()

            except Exception as e:

                print(f"[gui] recorder stop failed: {e}")

        if self.hotkey_mgr:

            try:

                self.hotkey_mgr.stop()

            except Exception as e:

                print(f"[gui] hotkey_mgr stop failed: {e}")

        if hasattr(self, 'tray') and self.tray:

            try:

                self.tray.stop()

            except Exception as e:

                print(f"[gui] tray stop failed: {e}")

        try:

            self.root.quit()

            self.root.destroy()

        except tk.TclError:

            pass

        import os

        os._exit(0)



    def run(self):

        self.root.mainloop()





    def _refresh_pc_keybind_label(self):

        if self.hotkey_mgr and self.hotkey_mgr.listener:

            bound = {k: v for k, v in self.cfg.get("hotkeys", {}).items()

                     if k in self.hotkey_mgr.handlers}

            binds = ", ".join(f"{format_hotkey(v)}={_friendly_keybind(k)}" for k, v in bound.items())

            self.pc_keybind_var.set(binds)

            # Update button text to reflect current keybinds

            self._update_quick_list_button_text()

        else:

            self.pc_keybind_var.set("Hotkeys not loaded")



    def _set_status(self, text):

        def _update():

            self.pc_status_var.set(text)

            if hasattr(self, "status_var"):

                self.status_var.set(text)

        try:

            self.root.after(0, _update)

        except RuntimeError:

            pass





def main():

    ctk.set_appearance_mode("System")

    ctk.set_default_color_theme("blue")

    app = App()

    app.run()





if __name__ == "__main__":

    main()



