"""Screen capture utilities using mss (fast, per-monitor).

Regions are defined in config as normalized fractions (0..1) of the game
window so the tool works at any resolution/window size.
"""
def _hide_cursor():
    """Hide the Windows cursor. Returns a function to restore it."""
    import win32gui
    import ctypes
    user32 = ctypes.windll.user32
    old_count = user32.ShowCursor(False)
    return lambda: user32.ShowCursor(old_count >= 0)

def _show_cursor():
    """Show the Windows cursor."""
    import win32gui
    win32gui.ShowCursor(True)
import json
import os
import sys
import threading
import time
import subprocess
import tempfile

import cv2
import numpy as np
import mss

from config import load_config, save_config, app_dir

_cfg = load_config()

# ---------------------------------------------------------------------------
# Persistent subprocess worker management
# ---------------------------------------------------------------------------
_worker_process = None          # subprocess.Popen handle (or None)
_LAST_REQUEST_TIME = 0.0        # monotonic time of last capture request


def get_worker_idle_timeout() -> float:
    """Return the configured worker idle timeout in seconds.

    Reads from config['capture']['worker_idle_timeout'] with a 300s default.
    """
    return float(_cfg.get("capture", {}).get("worker_idle_timeout", 180))


def set_worker_idle_timeout(seconds: int):
    """Persist a new worker idle timeout (seconds) to config."""
    _cfg.setdefault("capture", {})["worker_idle_timeout"] = int(seconds)
    save_config(_cfg)


def _match_title():
    """Window-title substring to capture from. Defaults to 'diablo'; the
    user can point the tool at ANY window via Settings (config key
    'capture_window_title')."""
    return (_cfg.get("capture_window_title") or "diablo").lower()

def list_windows():
    """Visible top-level windows (title, left, top, width, height) for the
    Settings capture-source dropdown."""
    out = []
    try:
        import win32gui

        def _enum(hwnd, acc):
            if win32gui.IsWindowVisible(hwnd):
                title = win32gui.GetWindowText(hwnd)
                if title and len(title) > 2:
                    l, t, r, b = win32gui.GetWindowRect(hwnd)
                    if r - l > 200 and b - t > 150:
                        acc.append({"title": title, "rect": (l, t, r - l, b - t)})
            return True

        win32gui.EnumWindows(_enum, out)
    except ImportError:
        pass
    # Largest windows first
    out.sort(key=lambda w: w["rect"][2] * w["rect"][3], reverse=True)
    return out


def get_game_window_rect(require_game=True):
    """Find the capture-source window (the Diablo window by default, or any
    window matching the user-configured title). Returns (left, top, width,
    height) or None when no matching window is found and require_game is
    True.

    We only ever capture from this window - never the whole desktop - so
    other windows' tooltips/content can't leak in.
    """
    needle = _match_title()
    candidates = []
    try:
        import win32gui

        def _enum(hwnd, acc):
            if win32gui.IsWindowVisible(hwnd):
                title = win32gui.GetWindowText(hwnd)
                if title and needle in title.lower():
                    acc.append(hwnd)
            return True

        hwnds = []
        win32gui.EnumWindows(_enum, hwnds)
        for hwnd in hwnds:
            l, t, r, b = win32gui.GetWindowRect(hwnd)
            candidates.append((l, t, r - l, b - t))
    except ImportError:
        try:
            import pygetwindow as gw
            for w in gw.getAllWindows():
                if w.title and needle in w.title.lower() and w.width > 200:
                    candidates.append((w.left, w.top, w.width, w.height))
        except Exception:
            pass

    if candidates:
        # Largest area wins (browser tabs titled "diablo2.io" are smaller)
        candidates.sort(key=lambda c: c[2] * c[3], reverse=True)
        l, t, w, h = candidates[0]
        if w > 200 and h > 150:
            return l, t, w, h

    if require_game:
        return None
    # Legacy fallback for tests / explicit non-game use
    with mss.mss() as sct:
        mon = sct.monitors[1]
        return mon["left"], mon["top"], mon["width"], mon["height"]


def game_window_found():
    return get_game_window_rect() is not None


def capture_full_window():
    """Grab a full screenshot of the game window.

    Returns (bgr_img, (left, top, w, h)) or (None, None) when the window is
    not found. Full screenshots are NOT saved - only cropped tooltips are
    kept under captures/.
    """
    rect = get_game_window_rect()
    if rect is None:
        print("[capture] Diablo window not found - not capturing")
        return None, None
    l, t, w, h = rect
    with mss.mss() as sct:
        shot = sct.grab({"left": l, "top": t, "width": w, "height": h})
    img = np.array(shot)[:, :, :3]
    return img, rect


def region_to_pixels(region, win_rect):
    """Convert a normalized region config into absolute pixel coords."""
    l, t, w, h = win_rect
    x = int(l + region["x"] * w)
    y = int(t + region["y"] * h)
    rw = max(1, int(region["w"] * w))
    rh = max(1, int(region["h"] * h))
    return x, y, rw, rh

def capture_region(region_name):
    """Capture a named region from config. Returns a BGR numpy image or
    None when the Diablo window is not found (we never capture the desktop)."""
    region = _cfg["regions"][region_name]
    win = get_game_window_rect()
    if win is None:
        print("[capture] Diablo window not found - not capturing")
        return None
    x, y, rw, rh = region_to_pixels(region, win)
    with mss.mss() as sct:
        shot = sct.grab({"left": x, "top": y, "width": rw, "height": rh})
    img = np.array(shot)[:, :, :3]  # BGRA -> BGR
    if _cfg["scan"].get("save_captures"):
        save_capture(img, region_name, max_size_mb=1)
    return img


def get_cursor_pos():
    """Current mouse cursor position in screen coordinates."""
    try:
        import win32api
        pt = win32api.GetCursorPos()
        return int(pt[0]), int(pt[1])
    except ImportError:
        try:
            from pynput.mouse import Controller
            p = Controller().position
            return int(p[0]), int(p[1])
        except Exception:
            return 0, 0


def image_is_blank(img):
    """True when a capture came back (nearly) black/empty - happens when the
    game blocks screen capture. Reported clearly instead of silently
    feeding garbage to OCR."""
    if img is None or img.size == 0:
        return True
    import cv2 as _cv
    gray = _cv.cvtColor(img, _cv.COLOR_BGR2GRAY)
    return float(gray.mean()) < 4.0


# ---------------------------------------------------------------------------
# ONNX tooltip detector (v6) - the PRIMARY capture path for quick-list.
# No legacy fallback - if ONNX fails, callers show an error and open manual crop.
# ---------------------------------------------------------------------------

# Info about the most recent capture_tooltip() result, for UI status
# lines and diagnostics: {"conf": float, "box": (x1,y1,x2,y2), "shape": (w,h)}
last_capture_result = {}
# Backward-compat alias: gui_ctk.py and pricecheck.py import last_yolo_capture
last_yolo_capture = last_capture_result








def _find_worker_path():
    """Locate capture_worker.py for subprocess mode."""
    src_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "capture_worker.py")
    if os.path.exists(src_path):
        return src_path
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        exe_path = os.path.join(meipass, "capture_worker.py")
        if os.path.exists(exe_path):
            return exe_path
    return None


class SubprocessError(Exception):
    """Raised when the capture subprocess itself fails."""
    pass


def _log_capture_error(text: str):
    """Append an error message to error_log.txt."""
    try:
        from datetime import datetime
        from config import app_dir
        path = os.path.join(app_dir(), "error_log.txt")
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"\n[{datetime.now():%Y-%m-%d %H:%M:%S}] {text}\n")
    except Exception:
        pass


def _kill_worker():
    """Terminate the capture worker subprocess if it is running."""
    global _worker_process
    if _worker_process is not None:
        poll = _worker_process.poll()
        if poll is None:
            try:
                _worker_process.terminate()
                _worker_process.wait(timeout=5)
            except Exception:
                try:
                    _worker_process.kill()
                except Exception:
                    pass
        _worker_process = None


def _ensure_worker_running():
    """Start a fresh capture worker (killing any existing one first).

    The worker is killed after every capture so each capture starts from a
    clean state with a freshly loaded ONNX model.
    """
    global _worker_process
    _kill_worker()

    worker_path = _find_worker_path()
    if worker_path is None:
        raise SubprocessError("capture_worker.py not found")

    python_exe = sys.executable
    if getattr(sys, "frozen", False):
        worker_args = [python_exe, "--capture-worker"]
    else:
        worker_args = [python_exe, worker_path, "--capture-worker"]

    try:
        _worker_process = subprocess.Popen(
            worker_args,
            stdout=subprocess.DEVNULL,
            stderr=None,
        )
        print(f"[capture] Worker subprocess started (PID={_worker_process.pid})")
        return _worker_process
    except Exception as e:
        _worker_process = None
        raise SubprocessError(f"Failed to start worker: {e}")


def _write_capture_request(output_path: str, conf_path: str,
                           conf_thres: float, timeout: float = 30.0):
    """Write a capture request file and wait for the worker to respond.

    Returns (img, conf) or (None, 0.0).
    """
    import uuid

    req_dir = os.path.join(tempfile.gettempdir(), "d2_capture_requests")
    os.makedirs(req_dir, exist_ok=True)

    req_id = uuid.uuid4().hex
    req_file = os.path.join(req_dir, f"d2_req_{req_id}.req")
    request = {
        "output_path": output_path,
        "conf_path": conf_path,
        "conf_thres": conf_thres,
    }

    tmp_req = req_file + ".tmp"
    with open(tmp_req, "w") as f:
        json.dump(request, f)
    os.replace(tmp_req, req_file)

    t0 = time.time()
    while time.time() - t0 < timeout:
        if not os.path.exists(req_file):
            if os.path.exists(output_path) and os.path.getsize(output_path) > 0:
                img = cv2.imread(output_path)
                conf = 0.0
                if os.path.exists(conf_path):
                    try:
                        with open(conf_path, "r") as f:
                            conf = float(f.read().strip())
                    except Exception:
                        pass
                return img, conf
            return None, 0.0
        time.sleep(0.1)

    try:
        os.remove(req_file)
    except Exception:
        pass
    raise SubprocessError("worker did not respond within timeout")


def capture_tooltip_via_subprocess(conf_thres=0.5):
    """Capture the tooltip using the subprocess worker.

    A fresh worker is started for each capture and killed immediately after,
    so every capture runs against a freshly loaded ONNX model.

    Returns (crop_bgr, confidence) or (None, 0.0).
    """
    _ensure_worker_running()

    tmpdir = tempfile.gettempdir()
    ts = int(time.time() * 1000)
    img_path = os.path.join(tmpdir, f"d2_capture_{os.getpid()}_{ts}.png")
    conf_path = img_path + ".conf"

    global _LAST_REQUEST_TIME
    _LAST_REQUEST_TIME = time.time()

    try:
        return _write_capture_request(img_path, conf_path, conf_thres)
    finally:
        for p in (img_path, conf_path):
            try:
                if os.path.exists(p):
                    os.remove(p)
            except Exception:
                pass
        # Kill the worker after every capture so the next one starts fresh
        _kill_worker()


def capture_tooltip(conf_thres=0.5):
    """Capture the hovered item tooltip using the ONNX subprocess worker.

    Starts the worker on first call if it isn't already running. The worker
    loads the ONNX model once and stays alive between captures (until the
    configured idle timeout expires).

    Returns (crop_bgr, confidence) or (None, 0.0) when no tooltip is found
    or the worker fails. Callers should handle None by showing an error and
    opening the manual crop window.
    """
    try:
        img, conf = capture_tooltip_via_subprocess(conf_thres)
    except SubprocessError as e:
        _log_capture_error(f"subprocess error: {e}")
        print(f"[capture] subprocess error: {e}")
        return None, 0.0

    if img is not None:
        print(f"[capture] ONNX tooltip found (conf={conf:.2f})")
        last_capture_result.update({"conf": float(conf), "box": None,
                                    "shape": img.shape,
                                    "source": "subprocess"})
        return img, conf

    # Worker ran but found no tooltip
    last_capture_result.clear()
    return None, 0.0


def shutdown_worker():
    """Terminate the capture worker subprocess if it is running.

    Called on app exit to ensure the worker process doesn't linger.
    """
    _kill_worker()



def save_capture(img, name, max_size_mb=1):
    """
    Save image with compression to keep file size under max_size_mb.
    Uses JPEG compression with adaptive quality to stay under max_size_mb.
    """
    out_dir = os.path.join(
        app_dir(),
        _cfg["scan"].get("captures_dir", "captures"),
    )
    os.makedirs(out_dir, exist_ok=True)
    
    base_path = os.path.join(out_dir, f"{name}_{int(time.time())}")
    
    # Try JPEG with adaptive quality first (usually smaller than PNG for photos)
    # Start with quality 85 and reduce if needed
    for quality in [85, 75, 65, 55, 45, 35, 25, 15]:
        path = f"{base_path}.jpg"
        cv2.imwrite(path, img, [cv2.IMWRITE_JPEG_QUALITY, quality])
        if os.path.getsize(path) <= max_size_mb * 1024 * 1024:
            return path
    
    # If still too large, try PNG with compression
    for compression in [9, 7, 5, 3, 1]:
        path = f"{base_path}.png"
        cv2.imwrite(path, img, [cv2.IMWRITE_PNG_COMPRESSION, compression])
        if os.path.getsize(path) <= max_size_mb * 1024 * 1024:
            return path
    
    # Last resort: resize image and try again
    h, w = img.shape[:2]
    for scale in [0.9, 0.8, 0.7, 0.6, 0.5]:
        new_w, new_h = int(w * scale), int(h * scale)
        resized = cv2.resize(img, (new_w, new_h), interpolation=cv2.INTER_AREA)
        for quality in [85, 75, 65, 55, 45, 35, 25, 15]:
            path = f"{base_path}.jpg"
            cv2.imwrite(path, resized, [cv2.IMWRITE_JPEG_QUALITY, quality])
            if os.path.getsize(path) <= max_size_mb * 1024 * 1024:
                return path
    
    # Fallback - save anyway (will be over limit)
    path = f"{base_path}.jpg"
    cv2.imwrite(path, img, [cv2.IMWRITE_JPEG_QUALITY, 85])
    return path
