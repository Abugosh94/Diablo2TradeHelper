"""D2TradeHelper - tooltip crop + auto-post to trading sites.

Hover an item in game, press F9: the ONNX tooltip detector crops the
tooltip, copies it to the clipboard, and opens your chosen trading site
(LootLens / Loot Goblin / Runewizard / Diablo2.io / Custom) where the
image is auto-pasted into the posting form.

Usage:
  python main.py              launch the GUI control panel
  python main.py --console    run in console hotkey mode (no GUI)
  python main.py --capture-worker  run ONNX capture worker (subprocess mode)
"""
import argparse
import os
import subprocess
import sys
import tempfile
import threading
import time

# If launched as a capture-worker subprocess (PyInstaller exe or source),
# run the ONNX capture worker and exit. This lets the parent process stay
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
from capture import shutdown_worker


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
        # Start the ONNX capture worker immediately so the model is loaded
        # and ready when the user presses F9. The worker stays in a separate
        # process; the parent never loads ONNX/torch itself.
        from capture import _ensure_worker_running
        try:
            _ensure_worker_running()
        except Exception as e:
            print(f"[app] Warning: could not preload capture worker: {e}", flush=True)

    # -- Hotkey actions --------------------------------------------------------

    def on_quick_list_item(self):
        print("[list] on_quick_list_item called", flush=True)
        def _run():
            try:
                print("[list] _run() started", flush=True)
                # 1+2. Capture the hovered tooltip with the ONNX detector
                from capture import capture_tooltip
                best_crop, best_conf = capture_tooltip()
                print(f"[list] capture_tooltip returned: img={best_crop is not None}, conf={best_conf}", flush=True)

                if best_crop is None:
                    # Capture full window first (in RAM) so manual crop can use it
                    from capture import capture_full_window
                    full_img, _rect = capture_full_window()

                    # ONNX failed or found no tooltip - show error and open manual crop
                    print("[list] No item tooltip detected - showing error and manual crop", flush=True)
                    from tkinter import messagebox
                    messagebox.showerror(
                        "Tooltip not detected",
                        "The ONNX detector did not find an item tooltip.\n\n"
                        "Make sure you are hovering an item in Diablo II and try again.\n"
                        "You can also use Manual Crop (F10) to select the area."
                    )

                    # Open manual crop with the pre-captured image
                    if full_img is not None:
                        self._manual_crop_blocking(
                            "Auto-detect failed - drag the tooltip area", full_img)
                    else:
                        self._set_status("Diablo window not found - open the game first")
                    return

                print(f"[list] Tooltip detected conf={best_conf:.2f} shape={best_crop.shape}", flush=True)

                # 3. Copy to clipboard (PNG + DIB, sized for trade sites)
                print("[list] Converting crop to PIL image...", flush=True)
                import cv2
                from PIL import Image
                pil_img = Image.fromarray(cv2.cvtColor(best_crop, cv2.COLOR_BGR2RGB))
                print(f"[list] PIL image: {pil_img.size} mode={pil_img.mode}", flush=True)

                print("[list] Copying to clipboard...", flush=True)
                copied = copy_image_to_clipboard(pil_img)
                print(f"[list] Clipboard copy: {'success' if copied else 'FAILED'}", flush=True)

                # 4. Open the selected trading site and auto-paste.
                import ctypes as _ct
                baseline = _ct.windll.user32.GetForegroundWindow()  # the game
                print(f"[list] Baseline window: {baseline}", flush=True)
                print("[list] Opening trading site...", flush=True)
                open_trading_site(self.cfg)
                print("[list] Trading site opened", flush=True)

                if copied:
                    from hotkeys import focus_and_paste
                    print("[list] Starting focus_and_paste thread...", flush=True)
                    threading.Thread(target=focus_and_paste,
                                     kwargs={"baseline_hwnd": baseline},
                                     daemon=True).start()
                else:
                    print("[list] Clipboard copy failed - paste skipped", flush=True)
            except Exception as e:
                print(f"[list] EXCEPTION: {e}", flush=True)
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
        print("[app] Hotkeys bound:", {k: v for k, v in bound.items()}, flush=True)
        print("[app] Press F9 for quick-list, F10 for manual crop, Ctrl+Alt+Q to quit.", flush=True)
        print("[app] NOTE: If Diablo II is focused, it may intercept F9 before this app sees it.", flush=True)
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

    # NOTE: tooltip capture runs either in a subprocess (no torch in parent)
    # or via the legacy color detector (no torch at all). No ONNX/torch is
    # loaded in the parent process at any point.

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
    shutdown_worker()
    return 0


if __name__ == "__main__":
    sys.exit(main())
