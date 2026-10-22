"""Global hotkey handling via pynput + clipboard + Windows startup."""
import ctypes
import os
import threading
import time
from ctypes import wintypes

from pynput import keyboard, mouse

from config import load_config


def parse_combo(combo_str):
    """'ctrl+alt+q' -> '<ctrl>+<alt>+q' for pynput GlobalHotKeys.

    pynput requires angle brackets around all multi-char key names
    (modifiers and special keys like <f8>); single characters stay bare.
    """
    parts = [p.strip().lower() for p in combo_str.split("+")]
    normalized = []
    for p in parts:
        if p in ("ctrl", "control"):
            normalized.append("<ctrl>")
        elif p == "alt":
            normalized.append("<alt>")
        elif p == "shift":
            normalized.append("<shift>")
        elif p in ("cmd", "super", "win"):
            normalized.append("<cmd>")
        elif len(p) == 1:
            normalized.append(p)
        else:
            normalized.append(f"<{p}>")
    return "+".join(normalized)


def validate_combo(combo_str):
    """Return (ok, error_message) for a user-entered hotkey combo."""
    try:
        from pynput import keyboard
        # GlobalHotKeys uses HotKey.parse internally
        HotKey = keyboard.HotKey
        HotKey.parse(parse_combo(combo_str))
        return True, None
    except Exception as e:
        return False, str(e) or "invalid key combination"


class HotkeyManager:
    def __init__(self, handlers):
        """handlers: dict of hotkey name -> callable (run in a thread)."""
        self.cfg = load_config()
        self.handlers = handlers
        self.listener = None

    def start(self):
        mapping = {}
        for name, combo in self.cfg["hotkeys"].items():
            if name not in self.handlers:
                continue
            fn = self.handlers[name]

            def _make(f):
                def _run():
                    threading.Thread(target=f, daemon=True).start()
                return _run

            mapping[parse_combo(combo)] = _make(fn)
        self.listener = keyboard.GlobalHotKeys(mapping)
        self.listener.daemon = True
        self.listener.start()
        return {k: v for k, v in self.cfg["hotkeys"].items() if k in self.handlers}

    def stop(self):
        if self.listener:
            self.listener.stop()


def copy_image_to_clipboard(pil_image, max_width=640, max_height=720):
    """Copy a PIL image to the Windows clipboard for browser pasting.

    BOTH formats are placed on the clipboard (EmptyClipboard clears the
    rest), because not every browser reads the "PNG" clipboard format:
      - "PNG" (compressed; Chromium browsers prefer it)
      - CF_DIB bitmap (Firefox & co read this; the browser re-encodes it
        to PNG when uploading)
    Earlier attempts to ship only ONE format broke pasting in browsers
    that don't read the other. The image carried by BOTH is identical and
    already downscaled, so any re-encode stays small.

    Sizing (forums reject oversized attachments):
      - scaled to fit max_width x max_height (720p-height compliant)
      - tier 1: full-color PNG when it fits the byte cap
      - tier 2: 256-color palette PNG (halves the size, no visible loss)
      - tier 3: progressive downscale until it fits
      - cap: config key 'paste_image_max_kb', default 250 KB
    """
    try:
        import win32clipboard
        import io
        from PIL import Image as _PILImage
        from config import load_config
        max_bytes = int(load_config().get("paste_image_max_kb", 250)) * 1024

        img = pil_image
        if img.mode != "RGB":
            img = img.convert("RGB")
        scale = min(max_width / img.width, max_height / img.height, 1.0)
        if scale < 1.0:
            img = img.resize((max(1, int(img.width * scale)),
                              max(1, int(img.height * scale))),
                             _PILImage.BILINEAR)   # fast; visually identical here

        def _png_bytes(image, palette=False):
            out = (image.convert("P", palette=_PILImage.ADAPTIVE, colors=256)
                   if palette else image)
            buf = io.BytesIO()
            out.save(buf, "PNG")   # no optimize=True: 3-5x faster encode,
            data = buf.getvalue()  # ~10% bigger - the size loop still
            buf.close()           # enforces the byte cap via tier 2/3
            return data

        png = _png_bytes(img)                     # tier 1: full color
        if len(png) > max_bytes:
            pal = _png_bytes(img, palette=True)  # tier 2: palettized
            if len(pal) <= max_bytes:
                png = pal
            else:                                # tier 3: shrink until fits
                while True:
                    png = _png_bytes(img, palette=True)
                    if len(png) <= max_bytes or img.width <= 320:
                        break
                    new_w = int(img.width * 0.85)
                    img = img.resize(
                        (new_w, max(1, int(img.height * new_w / img.width))),
                        _PILImage.BILINEAR)

        # CF_DIB at the SAME final size (Firefox-style browsers read this
        # and re-encode to PNG on upload; the source pixels are already
        # downscaled so the re-encode stays small)
        output = io.BytesIO()
        img.save(output, "BMP")
        dib = output.getvalue()[14:]  # strip BMP file header for CF_DIB
        output.close()

        win32clipboard.OpenClipboard()
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardData(
            win32clipboard.RegisterClipboardFormat("PNG"), png)
        win32clipboard.SetClipboardData(win32clipboard.CF_DIB, dib)
        win32clipboard.CloseClipboard()
        print(f"[hotkeys] clipboard PNG: {len(png) / 1024:.0f} KB "
              f"at {img.width}x{img.height} (+ DIB {len(dib) / 1024:.0f} KB)")
        return True
    except Exception as e:
        print(f"[hotkeys] Clipboard image copy failed: {e}")
        return False





RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
APP_NAME = "D2TradeHelper"


def set_start_with_windows(enabled, exe_path=None):
    """Register/unregister the app in the HKCU Run key."""
    try:
        import winreg
        import sys
        if exe_path is None:
            if getattr(sys, "frozen", False):
                exe_path = sys.executable
            else:
                exe_path = f'"{sys.executable}" "{os.path.join(os.path.dirname(os.path.abspath(__file__)), "main.py")}"'
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE)
        try:
            if enabled:
                winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, exe_path)
            else:
                try:
                    winreg.DeleteValue(key, APP_NAME)
                except FileNotFoundError:
                    pass
        finally:
            winreg.CloseKey(key)
        return True
    except Exception as e:
        print(f"[hotkeys] Start-with-Windows failed: {e}")
        return False


def get_start_with_windows():
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ)
        try:
            winreg.QueryValueEx(key, APP_NAME)
            return True
        except FileNotFoundError:
            return False
        finally:
            winreg.CloseKey(key)
    except Exception:
        return False


def _build_title_hints(url_hint=None, include_tool_hints=True):
    """Build a list of case-insensitive substring hints used to match a
    browser window title against the selected AI tool URL.

    Only AI-tool-specific hints are used - trading sites are never matched
    here, so a bookmarks page or trading tab won't be mistaken for the AI tool.

    Args:
        url_hint: A URL (e.g. "gemini.google.com") or a tool name (e.g. "gemini").
        include_tool_hints: When False (used for browser mode), only domain-based
            hints are generated so desktop app windows are NOT matched.
            When True (used for exe/desktop app mode), tool name hints are
            included so desktop app windows CAN be matched by title.

    Returns a list of lowercase hint strings.
    """
    hints = []
    if not url_hint:
        return hints
    low = url_hint.lower()
    # If the hint looks like a URL, parse domain-based hints
    if "://" in low or "." in low:
        try:
            from urllib.parse import urlparse
            netloc = urlparse(low if "://" in low else "https://" + low).netloc
            if netloc:
                domain = netloc.replace("www.", "").split(":")[0]
                hints.append(domain)
                # Also add the domain without subdomain (e.g. "google.com"
                # from "gemini.google.com", "openai.com" from "chat.openai.com")
                parts = domain.split(".")
                if len(parts) >= 2:
                    hints.append(".".join(parts[-2:]))
                # AI-tool-specific short hints (window titles show page
                # names, not full domains) - only for desktop app matching
                if include_tool_hints:
                    if "gemini" in low:
                        hints += ["gemini", "google gemini"]
                    elif "chatgpt" in low or "openai" in low:
                        hints += ["chatgpt", "chat gpt", "openai", "chat.openai.com"]
        except Exception:
            pass
    else:
        # Treat as a tool name hint (e.g. "gemini", "chatgpt") for
        # focusing desktop app windows by title.
        hints.append(low)
        if include_tool_hints:
            if "gemini" in low:
                hints += ["gemini", "google gemini"]
            elif "chatgpt" in low or "openai" in low:
                hints += ["chatgpt", "chat gpt", "openai", "chat.openai.com"]
    return hints


def foreground_title_matches(url_hint=None):
    """Return True if the current foreground window's title matches the
    hints derived from url_hint. Used to verify that a browser window was
    actually brought to the front before sending keys to it.
    """
    import ctypes
    user32 = ctypes.windll.user32
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return False
    buf = ctypes.create_unicode_buffer(512)
    user32.GetWindowTextW(hwnd, buf, 512)
    title = buf.value.strip().lower()
    if not title:
        return False
    hints = _build_title_hints(url_hint)
    return any(h in title for h in hints)


def _find_matching_window(user32, hints, current_pid):
    """Enum visible windows and return the first hwnd whose title matches
    any of the hints (and belongs to a different process)."""
    found = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def _enum_cb(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length == 0:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        title = buf.value.strip().lower()
        if title and any(h in title for h in hints):
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value != current_pid:
                found.append(hwnd)
        return True

    try:
        user32.EnumWindows(_enum_cb, 0)
    except Exception:
        pass
    return found[0] if found else None





def _focus_window(user32, hwnd, timeout):
    """Foreground the given hwnd.  Returns True when it is actually the
    foreground window within timeout seconds.

    Uses AllowSetForegroundWindow + AttachThreadInput to work around
    fullscreen games (Diablo II) that normally block SetForegroundWindow
    from other processes.  The window is NOT resized or moved - it stays
    on whatever screen it is already on.
    """
    try:
        # Allow this process to set foreground - critical when a fullscreen
        # game (Diablo II) is the current foreground window and would
        # otherwise block SetForegroundWindow calls from other processes.
        try:
            kernel32 = ctypes.windll.kernel32
            cur_thread = kernel32.GetCurrentThreadId()
            user32.AllowSetForegroundWindow(cur_thread)
        except Exception:
            pass

        fg_thread = user32.GetWindowThreadProcessId(
            user32.GetForegroundWindow(), None)
        cur_thread = ctypes.windll.kernel32.GetCurrentThreadId()
        attached = False
        try:
            if fg_thread != cur_thread:
                user32.AttachThreadInput(fg_thread, cur_thread, True)
                attached = True
            user32.SetForegroundWindow(hwnd)
            user32.BringWindowToTop(hwnd)
        finally:
            if attached:
                user32.AttachThreadInput(fg_thread, cur_thread, False)
        t0 = time.time()
        while time.time() - t0 < timeout:
            if user32.GetForegroundWindow() == hwnd:
                return True
            time.sleep(0.05)

        # Allow this process to set foreground - critical when a fullscreen
        # game (Diablo II) is the current foreground window and would
        # otherwise block SetForegroundWindow calls from other processes.
        try:
            kernel32 = ctypes.windll.kernel32
            cur_thread = kernel32.GetCurrentThreadId()
            user32.AllowSetForegroundWindow(cur_thread)
        except Exception:
            pass

        fg_thread = user32.GetWindowThreadProcessId(
            user32.GetForegroundWindow(), None)
        cur_thread = ctypes.windll.kernel32.GetCurrentThreadId()
        attached = False
        try:
            if fg_thread != cur_thread:
                user32.AttachThreadInput(fg_thread, cur_thread, True)
                attached = True
            user32.SetForegroundWindow(hwnd)
            user32.BringWindowToTop(hwnd)
        finally:
            if attached:
                user32.AttachThreadInput(fg_thread, cur_thread, False)
        t0 = time.time()
        while time.time() - t0 < timeout:
            if user32.GetForegroundWindow() == hwnd:
                return True
            time.sleep(0.05)
        return user32.GetForegroundWindow() == hwnd
    except Exception as e:
        print(f"[hotkeys] focus_browser_window failed: {e}")
        return False


def focus_browser_window(url_hint=None, timeout=3.0):
    """Bring the trading-site / AI-tool browser tab to the foreground.

    webbrowser.open() opens the URL in a NEW TAB, but if the browser window
    was minimized / behind the game, the tab never becomes visible and the
    auto-paste lands nowhere. This finds a visible window whose title
    contains a hint from the URL (domain or page title) and restores +
    focuses it, so the page is actually visible.

    Best effort: returns True when a window was focused, False otherwise
    (the paste logic still runs - if the user focuses the browser in time,
    the paste lands).
    """
    # Browser mode: only match domain hints (e.g. "gemini.google.com",
    # "google.com"), not tool name hints (e.g. "gemini", "google gemini").
    # This prevents desktop app windows from being matched when the user
    # selected "Open in browser".
    hints = _build_title_hints(url_hint, include_tool_hints=False)

    try:
        user32 = ctypes.windll.user32
    except Exception as e:
        print(f"[hotkeys] focus_browser_window unavailable: {e}")
        return False

    current_pid = os.getpid()

    # Try multiple times: a fullscreen game (Diablo II) can briefly block
    # SetForegroundWindow or hide the browser window from enumeration on the
    # first attempt.  After webbrowser.open() opens the URL, we need a few
    # retries for the browser to create the tab and update its window title.
    # Use short per-attempt timeouts so a blocked SetForegroundWindow
    # doesn't stall on every retry.
    for _attempt in range(2):
        hwnd = _find_matching_window(user32, hints, current_pid)
        if hwnd:
            if _focus_window(user32, hwnd, 1.5):
                return True
            time.sleep(0.3)
            continue
        time.sleep(0.3)

    return False



def focus_exe_window(ai_tool=None, timeout=3.0):
    """Bring an AI desktop app window to the foreground WITHOUT moving
    it between monitors.

    Unlike focus_browser_window(), this does NOT call BringWindowToTop()
    (which can reposition windows across monitors). It restores the window
    if minimized (keeping it on the same monitor), uses AllowSetForegroundWindow
    + AttachThreadInput to work around fullscreen games (Diablo II) that
    block SetForegroundWindow, then calls SetForegroundWindow alone to
    activate it without changing its monitor position.

    Returns True when a window was focused, False otherwise.
    """
    hints = _build_title_hints(ai_tool, include_tool_hints=True)
    if not hints:
        print(f"[focus_exe_window] No hints for ai_tool={ai_tool!r}")
        return False

    try:
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
    except Exception as e:
        print(f"[hotkeys] focus_exe_window unavailable: {e}")
        return False

    current_pid = os.getpid()
    cur_thread = kernel32.GetCurrentThreadId()

    def _allow_foreground():
        try:
            user32.AllowSetForegroundWindow(cur_thread)
        except Exception:
            pass

    def _attached_foreground(hwnd):
        """SetForegroundWindow with AttachThreadInput, no BringWindowToTop."""
        fg_thread = user32.GetWindowThreadProcessId(
            user32.GetForegroundWindow(), None
        )
        attached = False
        try:
            if fg_thread != cur_thread:
                user32.AttachThreadInput(fg_thread, cur_thread, True)
                attached = True
            user32.SetForegroundWindow(hwnd)
        finally:
            if attached:
                user32.AttachThreadInput(fg_thread, cur_thread, False)

    for _attempt in range(2):
        hwnd = _find_matching_window(user32, hints, current_pid)
        if hwnd:
            # Skip Windows Explorer / folder windows - we want the actual
            # AI desktop app, not an Explorer window showing its folder.
            cls_buf = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(hwnd, cls_buf, 256)
            win_class = cls_buf.value.lower()
            if win_class in ("cabinetwclass", "explorewclass", "progman", "shelldll_defview"):
                print(f"[focus_exe_window] Skipping folder/Explorer window class={win_class} for {ai_tool!r}")
                time.sleep(0.3)
                continue
            print(f"[focus_exe_window] Found app window hwnd={hwnd} class={win_class} for {ai_tool!r}")
            # Restore if minimized - keeps window on the same monitor
            if user32.IsIconic(hwnd):
                user32.ShowWindow(hwnd, 9)  # SW_RESTORE
                time.sleep(0.3)
            # Try SetForegroundWindow twice (like _focus_window) to work
            # around fullscreen games that briefly block it.
            for _try in range(2):
                _allow_foreground()
                _attached_foreground(hwnd)
                t0 = time.time()
                while time.time() - t0 < timeout:
                    if user32.GetForegroundWindow() == hwnd:
                        print(f"[focus_exe_window] Foreground achieved for {ai_tool!r}")
                        return True
                    time.sleep(0.05)
            result = user32.GetForegroundWindow() == hwnd
            print(f"[focus_exe_window] Foreground result={result} for {ai_tool!r}")
            return result
        print(f"[focus_exe_window] No matching window found (attempt {_attempt + 1})")
        time.sleep(0.3)

    return False



def wait_for_left_click(timeout=None):
    """Block until the user clicks the left mouse button.

    Returns True if a click was received, False if timeout elapsed.
    """
    clicked = threading.Event()

    def on_click(x, y, button, pressed):
        if button == mouse.Button.left and pressed:
            clicked.set()
            return False  # stop listener

    with mouse.Listener(on_click=on_click) as listener:
        if timeout is not None:
            if not clicked.wait(timeout):
                return False
        else:
            clicked.wait()
        return True


def focus_and_paste(url=None, max_wait=4.0, min_settle=0.2, baseline_hwnd=None):
    """Send Ctrl+V to the foreground window (the browser tab we just opened),
    so the clipboard image is pasted into the site's posting form.

    Two-stage wait:
      1. Wait until the foreground is NOT the baseline window (the game at
         quick-list time). Callers pass the baseline captured BEFORE the
         browser opened; if focus_browser_window() already brought the tab
         front, this breaks immediately instead of waiting a full timeout.
      2. Once the browser window is in front, wait for its PAGE to finish
         loading: browsers set the window title from the loaded page, so a
         non-empty title that stays UNCHANGED for 0.4s means the page (and
         its paste handler) is ready.

    Timings are deliberately tight (0.1s polls, 0.4s stability, 0.2s
    settle) - the original 1.0+1.0s scheme added ~2s of dead time on
    every quick-list.

    Best effort: if the browser never takes the foreground the paste fires
    after max_wait anyway.

    Runs in a background thread.
    """
    import ctypes
    import time

    VK_CONTROL, VK_V = 0x11, 0x56
    KEYUP = 0x0002
    try:
        user32 = ctypes.windll.user32

        def fg_hwnd():
            return user32.GetForegroundWindow()

        def fg_title():
            hwnd = fg_hwnd()
            if not hwnd:
                return ""
            buf = ctypes.create_unicode_buffer(512)
            user32.GetWindowTextW(hwnd, buf, 512)
            return buf.value.strip()

        # 1) foreground switch (browser window opening)
        start_hwnd = baseline_hwnd or fg_hwnd()   # the game, at quick-list time
        t0 = time.time()
        while time.time() - t0 < max_wait:
            cur = fg_hwnd()
            if cur != start_hwnd and cur != 0:
                break
            time.sleep(0.1)

        # 2) page readiness: stable, non-empty foreground window title
        deadline = time.time() + max_wait
        last_title, stable_since = None, None
        while time.time() < deadline:
            title = fg_title()
            if title and title == last_title:
                if stable_since is None:
                    stable_since = time.time()
                if time.time() - stable_since >= 0.4:
                    break               # page loaded, paste handler ready
            else:
                last_title, stable_since = title, None
            time.sleep(0.1)

        # Only d2r.diablo.trade/marketplace/create requires the user to
        # click "Sell" or "Buy" before the paste handler is active. Wait
        # until the user clicks (no time limit), then paste.
        if url and "d2r.diablo.trade/marketplace/create" in url.lower():
            print("[hotkeys] Waiting for user to click Sell/Buy before pasting...")
            wait_for_left_click(timeout=None)

        time.sleep(min_settle)           # final grace for the paste handler
        user32.keybd_event(VK_CONTROL, 0, 0, 0)
        user32.keybd_event(VK_V, 0, 0, 0)
        user32.keybd_event(VK_V, 0, KEYUP, 0)
        user32.keybd_event(VK_CONTROL, 0, KEYUP, 0)
        time.sleep(0.2)          # extra delay for slow PCs
        return True
    except Exception as e:
        print(f"[hotkeys] Auto-paste failed: {e}")
        return False
