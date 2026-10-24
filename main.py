"""D2TradeHelper - tooltip crop + auto-post to trading sites.

Hover an item in game, press F9: the YOLO tooltip detector crops the
tooltip, copies it to the clipboard, and opens your chosen trading site
(LootLens / Loot Goblin / Runewizard / Diablo2.io / Custom) where the
image is auto-pasted into the posting form.

Usage:
  python main.py              launch the GUI control panel
  python main.py --console    run in console hotkey mode (no GUI)
  python main.py --capture-worker  run YOLO capture worker (subprocess mode)
"""
import argparse
import os
import subprocess
import sys
import threading
import time

# If launched as a capture-worker subprocess (PyInstaller exe or source),
# run the YOLO capture worker and exit. This lets the parent process stay
# lightweight (no torch/cv2 loaded) while the worker handles detection.
if "--capture-worker" in sys.argv:
    # In exe mode, bundled modules live in _MEIPASS - add to sys.path so
    # capture_worker can be imported. In source mode, an optional positional
    # arg gives the worker script's directory.
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass and meipass not in sys.path:
        sys.path.insert(0, meipass)
    # Optional: worker_path passed in source mode to add its dir to sys.path
    args = sys.argv[1:]
    if "--capture-worker" in args:
        args.remove("--capture-worker")
    for extra in args:
        if not extra:
            continue
        # extra could be a file path (worker_path in source mode) or an image path
        d = extra if os.path.isdir(extra) else os.path.dirname(extra)
        if d and d not in sys.path:
            sys.path.insert(0, d)
    from capture_worker import main as worker_main
    worker_main()
    sys.exit(0)

# PyInstaller splash screen: dismiss after 3 seconds.
# The bootloader shows this before Python starts; we close it from Python.
# Try immediate close first (works in most cases), with a timer backup.
try:
    import pyi_splash  # type: ignore
    pyi_splash.close()  # immediate close
    print("[startup] splash closed immediately", flush=True)
except Exception as _e:
    print(f"[startup] pyi_splash not available or close failed: {_e}", flush=True)
    # Fallback: timer-based close as backup
    def _close_splash():
        try:
            import pyi_splash  # type: ignore
            pyi_splash.close()
            print("[startup] splash closed via timer", flush=True)
        except Exception:
            pass
    _splash_timer = threading.Timer(1.0, _close_splash)
    _splash_timer.daemon = True
    _splash_timer.start()

# torch and cv2 are imported lazily (background warmup thread + capture.py)
# so the GUI window appears immediately instead of waiting for heavy DLLs.

from config import load_config, get_trading_url
from hotkeys import HotkeyManager, copy_image_to_clipboard
from scanner import InventoryScanner


# Tooltip detector (v6): the canonical loader + capture logic live in
# capture.py so the console app and the GUI share ONE cached model instance.
def get_tooltip_model():
    from capture import get_tooltip_model as _load
    return _load()


def open_trading_site(cfg, browser=None):
    """Open the user's selected trading site in a new browser tab."""
    from config import CREATE_NO_WINDOW
    _key, url = get_trading_url(cfg)
    print(f"[list] Opening {url}")
    browser = browser or cfg.get("browser", "default")
    if browser == "default":
        # Opens in a NEW TAB of the default browser; os.startfile uses
        # ShellExecute so no console window can flash.
        os.startfile(url)
    else:
        subprocess.Popen([browser, "--new-tab", url], shell=False,
                         creationflags=CREATE_NO_WINDOW)
    try:
        import threading as _th
        from hotkeys import focus_browser_window
        # background thread: never block the caller while the browser spawns
        _th.Thread(target=focus_browser_window, args=(url,), daemon=True).start()
    except Exception:
        pass
    return url


class App:
    def __init__(self):
        self.cfg = load_config()
        self.scanner = InventoryScanner()
        self.overlay_visible = True
        self.running = True

    # -- Hotkey actions --------------------------------------------------------

    def on_quick_list_item(self):
        print("[list] Capturing hovered item with YOLO detection...")
        def _run():
            try:
                # 1+2. Capture the hovered tooltip with the YOLO detector
                from capture import capture_tooltip
                best_crop, best_conf = capture_tooltip()

                if best_crop is None:
                    print("[list] No item tooltip detected - hover an item and try again")
                    return

                print(f"[list] Tooltip detected with confidence: {best_conf:.2f}")

                # 3. Copy to clipboard (PNG + DIB, sized for trade sites)
                import cv2
                from PIL import Image
                pil_img = Image.fromarray(cv2.cvtColor(best_crop, cv2.COLOR_BGR2RGB))
                copied = copy_image_to_clipboard(pil_img)

                # 4. Open the selected trading site and auto-paste.
                # Baseline MUST be captured before the browser opens, else
                # focus_and_paste could see the browser as its baseline and
                # wait the full timeout for a "change" that already happened.
                import ctypes as _ct
                baseline = _ct.windll.user32.GetForegroundWindow()  # the game
                open_trading_site(self.cfg)

                if copied:
                    from hotkeys import focus_and_paste
                    threading.Thread(target=focus_and_paste,
                                     kwargs={"baseline_hwnd": baseline},
                                     daemon=True).start()
            except Exception as e:
                print(f"[list] Failed: {e}")
                import traceback
                traceback.print_exc()

        threading.Thread(target=_run, daemon=True).start()

    def on_manual_crop_item(self):
        print("[list] Manual crop requested - use the GUI (F10) for region selection.")

    def on_quit(self):
        print("[app] Quitting...")
        self.running = False

    # -- Helpers ---------------------------------------------------------------

    def _open_browser(self, url):
        open_trading_site(self.cfg)

    def run(self):
        handlers = {
            "quick_list_item": self.on_quick_list_item,
            "manual_crop_item": self.on_manual_crop_item,
            "quit": self.on_quit,
        }
        hk = HotkeyManager(handlers)
        bound = hk.start()
        print("[app] Hotkeys: " + ", ".join(f"{v}={k}" for k, v in bound.items()))
        print("[app] Hover an item and press F9 to quick-list. Ctrl+Alt+Q to quit.")
        try:
            while self.running:
                time.sleep(0.2)
        except KeyboardInterrupt:
            pass
        finally:
            hk.stop()


def main():
    # --- Single-instance enforcement -----------------------------------------
    # Prevent multiple copies of the exe from running at the same time.
    import win32event
    import win32api

    MUTEX_NAME = "D2TradeHelper_SingleInstanceMutex"
    mutex = win32event.CreateMutex(None, False, MUTEX_NAME)
    if win32api.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        import tkinter as tk
        from tkinter import messagebox
        root = tk.Tk()
        root.withdraw()
        messagebox.showinfo("D2TradeHelper",
                            "Another instance of D2TradeHelper is already running.")
        root.destroy()
        return 1

    ap = argparse.ArgumentParser(description="D2TradeHelper")
    ap.add_argument("--console", action="store_true",
                    help="run in console hotkey mode instead of the GUI")
    args = ap.parse_args()

    # NOTE: the YOLO / torch model is intentionally NOT preloaded at startup.
    # It is loaded on first use by capture.get_tooltip_model() and unloaded
    # after each capture (unload_tooltip_model) so idle memory stays low.
    # The first F9 / F8 press pays a one-time cold-start load.

    if args.console:
        app = App()
        app.run()  # console hotkey mode
        return 0
    # Default: GUI control panel
    from gui_ctk import App as GuiApp, _log_error
    # Crash hooks: in a windowless exe, unhandled exceptions (especially in
    # threads) are invisible - log them to error_log.txt so issues are diagnosable.
    import sys as _sys
    import threading as _threading
    import traceback as _traceback

    def _sys_hook(t, v, tb):
        _log_error("UNHANDLED: %s: %s\n%s"
                   % (getattr(t, "__name__", t), v,
                      "".join(_traceback.format_exception(t, v, tb))))

    def _thread_hook(args):
        _log_error("THREAD CRASH in %s: %s: %s\n%s"
                   % (args.thread.name, args.exc_type.__name__, args.exc_value,
                      "".join(_traceback.format_exception(
                          args.exc_type, args.exc_value, args.exc_traceback))))

    _sys.excepthook = _sys_hook
    _threading.excepthook = _thread_hook
    gui = GuiApp()
    gui.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
