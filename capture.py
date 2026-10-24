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
WORKER_IDLE_TIMEOUT = 300.0     # 5 minutes – worker self-kills after this


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
# YOLO tooltip detector (v6) - the PRIMARY capture path for quick-list.
# The legacy color-threshold detector (capture_tooltip_near_cursor /
# detect_tooltip_in_band below) remains only as a fallback.
# ---------------------------------------------------------------------------

# Info about the most recent capture_tooltip_yolo() result, for UI status
# lines and diagnostics: {"conf": float, "box": (x1,y1,x2,y2), "shape": (w,h)}
last_yolo_capture = {}

_TOOLTIP_MODEL = None
_TOOLTIP_MODEL_FAILED = False
_TOOLTIP_MODEL_LOCK = threading.Lock()
# In a PyInstaller onefile exe, sys._MEIPASS is the temp extraction folder
# where bundled data files (like tooltip_best_v6.pt) live at runtime.
# Fallback: also check next to the exe (in case --add-data didn't bundle it).
if getattr(sys, "frozen", False):
    _BASE_DIR = sys._MEIPASS
    _exe_dir = os.path.dirname(sys.executable)
    if not os.path.exists(os.path.join(_BASE_DIR, "tooltip_best_v6.pt")):
        if os.path.exists(os.path.join(_exe_dir, "tooltip_best_v6.pt")):
            _BASE_DIR = _exe_dir
else:
    _BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_TOOLTIP_MODEL_PATH = os.path.join(_BASE_DIR, "tooltip_best_v6.pt")


def get_tooltip_model():
    """Load tooltip_best_v6.pt once and cache it. Returns None if the file
    is missing or loading failed (callers then use the legacy detector).
    A failure is latched: we do not retry the failing import on every F9."""
    global _TOOLTIP_MODEL, _TOOLTIP_MODEL_FAILED
    if _TOOLTIP_MODEL is not None or _TOOLTIP_MODEL_FAILED:
        return _TOOLTIP_MODEL
    with _TOOLTIP_MODEL_LOCK:
        if _TOOLTIP_MODEL is not None or _TOOLTIP_MODEL_FAILED:
            return _TOOLTIP_MODEL
        if not os.path.exists(_TOOLTIP_MODEL_PATH):
            print("[yolo] tooltip_best_v6.pt not found - run train_tooltip.py")
            _TOOLTIP_MODEL_FAILED = True
            return None
        try:
            from ultralytics import YOLO
            _TOOLTIP_MODEL = YOLO(_TOOLTIP_MODEL_PATH)
            trained = time.strftime("%Y-%m-%d %H:%M",
                                    time.localtime(os.path.getmtime(_TOOLTIP_MODEL_PATH)))
            print(f"[yolo] model loaded: {_TOOLTIP_MODEL_PATH} (trained {trained})")
            try:
                # warm-up: the first inference allocates memory and selects
                # kernels; doing it here keeps the first F9 press at
                # steady-state speed instead of paying a cold-start penalty
                import numpy as _np
                _TOOLTIP_MODEL(_np.zeros((640, 640, 3), _np.uint8),
                               verbose=False, conf=0.99)
            except Exception:
                pass  # warm-up is best-effort
        except Exception as e:
            _TOOLTIP_MODEL_FAILED = True
            msg = f"[yolo] model load FAILED ({e}) - using legacy detector"
            print(msg)
            try:
                from gui_ctk import _log_error
                _log_error(msg)
            except Exception:
                pass
    return _TOOLTIP_MODEL


def unload_tooltip_model():
    """Release the cached YOLO model to free RAM after a capture.

    The next call to get_tooltip_model() will reload the model
    (one-time cold start). Python and torch may hold onto freed memory
    in their internal allocators for reuse, so Windows Task Manager may
    not show an immediate drop, but the memory is released for reuse by
    this process and will not grow unboundedly.
    """
    global _TOOLTIP_MODEL, _TOOLTIP_MODEL_FAILED
    with _TOOLTIP_MODEL_LOCK:
        _TOOLTIP_MODEL = None
        _TOOLTIP_MODEL_FAILED = False
    # Force Python garbage collection to reclaim circular/ref cycles
    import gc
    gc.collect()
    # Clear torch's caching allocator (CPU and CUDA) so held memory is
    # released back to the OS as much as possible.
    try:
        import torch
        if hasattr(torch, "cuda") and torch.cuda.is_available():
            torch.cuda.empty_cache()
        if hasattr(torch, "_C") and hasattr(torch._C, "_cuda_clearCublasWorkspaces"):
            try:
                torch._C._cuda_clearCublasWorkspaces()
            except Exception:
                pass
    except Exception:
        pass
    # Ask Windows to trim the process working set so Task Manager reflects
    # the freed memory sooner. Python/torch may still hold it internally,
    # but this nudges the OS to page out unused pages.
    try:
        import ctypes
        psapi = ctypes.windll.psapi
        psapi.EmptyWorkingSet(ctypes.windll.kernel32.GetCurrentProcess())
    except Exception:
        pass
    print("[yolo] model unloaded - RAM freed for reuse")


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


def _ensure_worker_running():
    """Start the persistent capture worker if it isn't already alive."""
    global _worker_process
    if _worker_process is not None:
        poll = _worker_process.poll()
        if poll is None:
            return _worker_process
        _worker_process = None

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
            stderr=subprocess.DEVNULL,
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
    """Capture the tooltip using the persistent subprocess worker.

    The worker loads torch + YOLO once and stays alive between captures,
    self-terminating after WORKER_IDLE_TIMEOUT seconds of inactivity.
    The parent app never loads torch itself.

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


def capture_tooltip(conf_thres=0.5):
    """Route tooltip capture based on config.

    If config['capture']['use_subprocess'] is True, runs the detector in a
    persistent subprocess so the parent never loads torch. Falls back to
    in-process detection ONLY if the subprocess errors out.

    Returns (crop_bgr, confidence) or (None, 0.0).
    """
    use_subprocess = _cfg.get("capture", {}).get("use_subprocess", False)
    if use_subprocess:
        try:
            img, conf = capture_tooltip_via_subprocess(conf_thres)
        except SubprocessError as e:
            _log_capture_error(
                f"subprocess error ({e}) - falling back to in-process "
                f"(torch will load)"
            )
            print(f"[capture] subprocess error ({e}) - falling back to in-process")
            return capture_tooltip_yolo(conf_thres)
        if img is not None:
            return img, conf
        print("[capture] subprocess found no tooltip - no torch loaded in parent")
        return None, 0.0
    return capture_tooltip_yolo(conf_thres)


def shutdown_worker():
    """Terminate the persistent capture worker if it is running.

    Called on app exit to ensure the worker process doesn't linger.
    """
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


# Sanity gates for detection boxes. Real D2R tooltips range from ~350x72 up
# to very wide name-only unique tooltips (1045x98, aspect 13:1 - trained on
# the UserCV_* crops). Fragments of unrecognized tooltips (170x220 pieces)
# must not become the posted crop.
MIN_BOX_W = 250
MIN_BOX_H = 55
MAX_BOX_ASPECT = 13.5

# Border-aligned trimming of the posted crop: the crop should sit exactly
# on the tooltip's near-black border - no game-scene margin, and never
# cutting into the tooltip itself.
#   - EXPAND a side (up to BOX_EXPAND px) while the strip just OUTSIDE the
#     box edge is border-dark: includes the full black border when the
#     model's box sits just inside it (the "cut off tooltip" fix).
#   - SHAVE a side (up to BOX_INSET px) while the strip just INSIDE the box
#     edge is bright margin (>=45 gray): removes the inherited crop margin.
#     Dark strips (border/panel) always stop the shave, so the tooltip's
#     content is never clipped.
BOX_INSET = 6     # max margin shaved off each side
BOX_EXPAND = 3    # max px added to each side to include the black border
CROP_PAD = 8      # unconditional safety padding added top/bottom AFTER
                  # border-alignment, so model boxes sitting 1-2px inside the
                  # tooltip never clip text/border details. Clamped to image.
CROP_PAD_X = 20       # left/right total (text runs closest to the side borders)


def _align_to_border(img, box):
    """Align a detection box to the tooltip's near-black border.

    Returns a box (x1, y1, x2, y2) that hugs the tooltip border:
    expanded up to BOX_EXPAND px per side to include the full black border,
    then shaved up to BOX_INSET px per side to drop bright game-scene
    margin. Dark strips (border/panel) always stop the shave, so tooltip
    content is never cut off. Falls back to the original box when the
    alignment would break the size gates.
    """
    import cv2 as _cv
    x1, y1, x2, y2 = box
    h, w = img.shape[:2]

    def _strip_mean(xa, ya, xb, yb):
        xa, ya = max(0, xa), max(0, ya)
        xb, yb = min(w, xb), min(h, yb)
        if xb <= xa or yb <= ya:
            return -1.0
        return float(_cv.cvtColor(img[ya:yb, xa:xb],
                                  _cv.COLOR_BGR2GRAY).mean())

    # EXPAND: include dark (border) strips just outside the box edges
    for _ in range(BOX_EXPAND):
        if 0 <= _strip_mean(x1 - 1, y1, x1, y2) < 30:
            x1 -= 1
        if 0 <= _strip_mean(x1, y1 - 1, x2, y1) < 30:
            y1 -= 1
        if 0 <= _strip_mean(x2, y1, x2 + 1, y2) < 30:
            x2 += 1
        if 0 <= _strip_mean(x1, y2, x2, y2 + 1) < 30:
            y2 += 1
    # SHAVE: drop bright (margin) strips just inside the box edges
    for _ in range(BOX_INSET):
        if _strip_mean(x1, y1, x1 + 1, y2) >= 45:
            x1 += 1
        if _strip_mean(x1, y1, x2, y1 + 1) >= 45:
            y1 += 1
        if _strip_mean(x2 - 1, y1, x2, y2) >= 45:
            x2 -= 1
        if _strip_mean(x1, y2 - 1, x2, y2) >= 45:
            y2 -= 1
    # clamp + sanity: never return a degenerate/undersized box
    x1, y1 = max(0, x1), max(0, y1)
    x2, y2 = min(w, x2), min(h, y2)
    if (x2 - x1) >= MIN_BOX_W and (y2 - y1) >= MIN_BOX_H:
        return (x1, y1, x2, y2)
    return box


def _pad_box(img, box, pad=CROP_PAD, pad_x=CROP_PAD_X):
    """Grow a box by `pad` px top/bottom and `pad_x` px left/right, then
    re-center the crop window on the tooltip.

    Plain clamping clips the padded side at screen edges, leaving the
    tooltip off-center in the crop. Instead: grow to the full padded size
    first, then shift the window back so the tooltip stays centered -
    stealing space from the roomy side when the tight side is clipped.
    Falls back to plain clamping when the padded window is bigger than
    the image itself.
    """
    h, w = img.shape[:2]
    x1, y1, x2, y2 = box
    cx1, cx2 = _center_span(w, x1 - pad_x, x2 + pad_x)
    cy1, cy2 = _center_span(h, y1 - pad, y2 + pad)
    return (cx1, cy1, cx2, cy2)


def _center_span(limit, lo, hi):
    """Clamp a [lo, hi) span into [0, limit), shifting (not just clipping)
    so the span keeps its size and stays centered on its original midpoint.
    """
    size = hi - lo
    if size >= limit:
        return (0, limit)
    # shift so the span is centered on its midpoint, clamped into bounds
    mid = (lo + hi) / 2.0
    lo = int(round(mid - size / 2.0))
    lo = max(0, min(limit - size, lo))
    return (lo, lo + size)


def capture_tooltip_yolo(conf_thres=0.5):
    """Capture the item tooltip with the YOLO detector.

    Takes a full screenshot of the game window, runs tooltip_best_v6.pt,
    and returns (crop_bgr, confidence) of the best tooltip box, or
    (None, 0.0) if the window is missing, the model is missing, or nothing
    was detected above conf_thres.

    conf_thres is 0.5 (not 0.25): dark game UI panels score 0.2-0.6 and
    must NOT be cropped as tooltips; real tooltips score ~0.9+.
    Among the boxes above the threshold, the one closest to the mouse
    cursor is preferred (the hovered tooltip is always near the cursor,
    big panels usually are not).
    """
    last_yolo_capture.clear()
    rect = get_game_window_rect()
    if rect is None:
        last_yolo_capture["reason"] = "game window not found"
        print("[yolo] Diablo window not found")
        return None, 0.0
    l, t, w, h = rect
    model = get_tooltip_model()
    if model is None:
        last_yolo_capture["reason"] = "model unavailable/failed to load"
        return None, 0.0
    with mss.mss() as sct:
        shot = sct.grab({"left": l, "top": t, "width": w, "height": h})
    full = np.array(shot)[:, :, :3]  # BGR

    results = model(full, verbose=False, conf=conf_thres)
    cx, cy = get_cursor_pos()
    rel_cx, rel_cy = cx - l, cy - t  # cursor position in window coordinates

    best, best_conf, best_box, best_score = None, 0.0, None, -1e9
    n_seen = 0
    for result in results:
        if result.boxes is None:
            continue
        for box in result.boxes:
            conf = float(box.conf[0])
            if conf <= conf_thres:
                continue
            n_seen += 1
            x1, y1, x2, y2 = map(int, box.xyxy[0])
            x1 = max(0, min(x1, full.shape[1] - 1))
            y1 = max(0, min(y1, full.shape[0] - 1))
            x2 = max(0, min(x2, full.shape[1]))
            y2 = max(0, min(y2, full.shape[0]))
            bw, bh = x2 - x1, y2 - y1
            if bw <= 0 or bh <= 0:
                continue
            if bw < MIN_BOX_W or bh < MIN_BOX_H or bw / bh > MAX_BOX_ASPECT:
                continue  # fragment/strip, not a whole tooltip
            # Prefer boxes near the mouse: 1000px from the cursor costs 0.40
            # conf. A tooltip by the cursor beats a distant panel; a panel
            # right under the cursor can still win only on raw confidence.
            dist = (((x1 + x2) / 2 - rel_cx) ** 2 +
                    ((y1 + y2) / 2 - rel_cy) ** 2) ** 0.5
            score = conf - 0.4 * dist / 1000.0
            if score > best_score:
                best = full[y1:y2, x1:x2]
                best_conf, best_box, best_score = conf, (x1, y1, x2, y2), score

    if best is None:
        if n_seen:
            last_yolo_capture["reason"] = (
                f"{n_seen} detection(s) rejected by size sanity "
                f"(min {MIN_BOX_W}x{MIN_BOX_H}, aspect<={MAX_BOX_ASPECT})")
            print(f"[yolo] {n_seen} box(es) found but all rejected as fragments")
        else:
            last_yolo_capture["reason"] = f"no detection above conf {conf_thres}"
            print(f"[yolo] no tooltip detected (conf>{conf_thres})")
        unload_tooltip_model()
        return None, 0.0
    # Border-align the box: include the full black border (expand) and drop
    # bright game-scene margin (shave) - never cutting tooltip content.
    # Then add the unconditional safety padding so no detail is cut off.
    best_box = _pad_box(full, _align_to_border(full, best_box))
    best = full[best_box[1]:best_box[3], best_box[0]:best_box[2]]
    last_yolo_capture.update(conf=best_conf, box=best_box,
                             shape=(best.shape[1], best.shape[0]))
    print(f"[yolo] tooltip detected: conf={best_conf:.2f} "
          f"box={best_box} size={best.shape[1]}x{best.shape[0]}")
    if _cfg["scan"].get("save_captures"):
        # async, off the critical path
        _crop_snap = best.copy()
        threading.Thread(
            target=lambda im=_crop_snap: save_capture(im, "tooltip_yolo", max_size_mb=1),
            daemon=True).start()
    unload_tooltip_model()
    return best, best_conf


def detect_tooltip_yolo_frame(frame, cur_x=None, cur_y=None, conf_thres=0.5):
    """Run the tooltip detector on an arbitrary image (e.g. a video frame).

    Returns the cropped tooltip (BGR) or None. Same size gates and
    cursor-proximity selection as the live quick-list capture, plus safety
    padding so no tooltip detail is cut off at the crop edge.
    """
    model = get_tooltip_model()
    if model is None:
        return None
    results = model(frame, verbose=False, conf=conf_thres)
    best, best_score = None, -1e9
    for result in results:
        if result.boxes is None:
            continue
        for box in result.boxes:
            conf = float(box.conf[0])
            if conf <= conf_thres:
                continue
            x1, y1, x2, y2 = map(int, box.xyxy[0])
            x1 = max(0, min(x1, frame.shape[1] - 1))
            y1 = max(0, min(y1, frame.shape[0] - 1))
            x2 = max(0, min(x2, frame.shape[1]))
            y2 = max(0, min(y2, frame.shape[0]))
            bw, bh = x2 - x1, y2 - y1
            if bw <= 0 or bh <= 0:
                continue
            if bw < MIN_BOX_W or bh < MIN_BOX_H or bw / bh > MAX_BOX_ASPECT:
                continue
            score = conf
            if cur_x is not None and cur_y is not None:
                dist = (((x1 + x2) / 2 - cur_x) ** 2 +
                        ((y1 + y2) / 2 - cur_y) ** 2) ** 0.5
                score = conf - 0.4 * dist / 1000.0
            if score > best_score:
                x1, y1, x2, y2 = _pad_box(frame, (x1, y1, x2, y2))
                best = frame[y1:y2, x1:x2].copy()
                best_score = score
    unload_tooltip_model()
    return best


def calibrate_tooltip_offset():
    """Capture the tooltip near the cursor NOW and remember its exact offset
    relative to the cursor. All future captures use this exact region, so
    detection guesswork is eliminated for this user's setup.

    Returns (offset_dict, image) or (None, None).
    """
    img, origin = capture_tooltip_near_cursor(use_stored=False)
    if img is None or origin is None:
        return None, None
    cx, cy = get_cursor_pos()
    offset = {"dx": origin[0] - cx, "dy": origin[1] - cy,
              "w": int(img.shape[1]), "h": int(img.shape[0])}
    cfg = load_config()
    cfg["tooltip_offset"] = offset
    save_config(cfg)
    return offset, img

def capture_tooltip_near_cursor(max_w=560, max_h=460, use_stored=True):
    """Capture the item tooltip near the mouse cursor.

    Strategy: Capture a vertical strip at the cursor's X position spanning the
    full height of the game window (width = half screen), then detect and crop
    the tooltip from that vertical strip. If tooltip is near edge, expand capture
    area to ensure full tooltip is captured.
    """
    import cv2
    import numpy as np
    rect = get_game_window_rect()
    if rect is None:
        print("[capture] Diablo window not found - not capturing")
        return None, None
    l, t, w, h = rect
    cx, cy = get_cursor_pos()

    # Calibrated offset: grab the exact region that worked before
    if use_stored:
        off = _cfg.get("tooltip_offset")
        if off:
            x0 = max(l, cx + int(off["dx"]))
            y0 = max(t, cy + int(off["dy"]))
            cw = int(off["w"]); ch = int(off["h"])
            if x0 + cw <= l + w and y0 + ch <= t + h and cw > 20 and ch > 10:
                with mss.mss() as sct:
                    shot = sct.grab({"left": x0, "top": y0,
                                     "width": cw, "height": ch})
                img = np.array(shot)[:, :, :3]
                if not image_is_blank(img):
                    if _cfg["scan"].get("save_captures"):
                        save_capture(img, "tooltip_near_cursor", max_size_mb=1)
                    return img, (x0, y0)
                # stored region came back blank (tooltip gone) -> auto-detect

    # IMPROVED STRATEGY: Capture vertical strip at cursor X, full window height
    # Width = half the window width, centered on cursor X
    # If tooltip is near edge, expand capture area to ensure full tooltip is captured
    strip_w = w // 2
    half_strip = strip_w // 2
    
    # Calculate ideal strip position centered on cursor
    ideal_x0 = cx - half_strip
    ideal_x1 = cx + half_strip
    
    # If tooltip would be near left edge, shift strip right
    if ideal_x0 < l:
        x0 = l
        x1 = min(l + w, l + strip_w)
    # If tooltip would be near right edge, shift strip left
    elif ideal_x1 > l + w:
        x1 = l + w
        x0 = max(l, x1 - strip_w)
    else:
        x0 = ideal_x0
        x1 = ideal_x1

    # Ensure minimum width
    if x1 - x0 < strip_w:
        if x0 == l:
            x1 = min(l + w, x0 + strip_w)
        else:
            x0 = max(l, x1 - strip_w)

    y0 = t
    y1 = t + h
    cw = x1 - x0
    ch = y1 - y0

    if cw < 60 or ch < 60:
        return None, None

    with mss.mss() as sct:
        shot = sct.grab({"left": x0, "top": y0, "width": cw, "height": ch})
    img = np.array(shot)[:, :, :3]

    rel_cx = cx - x0
    rel_cy = cy - y0

    crop = detect_tooltip_in_band(img, rel_cx, rel_cy)
    if crop is None:
        return None, None
    if _cfg["scan"].get("save_captures"):
        save_capture(crop, "tooltip_near_cursor", max_size_mb=1)
    return crop, (x0, y0)

def _create_color_masks(img):
    """Create color masks for all Diablo 2 text colors per specification."""
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)

    # Rarity color ranges in HSV (matching specification colors)
    # Gold/Tan for Unique/Runeword: #C7B377 / #D8B41D -> HSV approx
    gold_lower = np.array([15, 40, 120])
    gold_upper = np.array([35, 255, 255])

    # Green for Set items: #00FF00
    green_lower = np.array([35, 80, 80])
    green_upper = np.array([85, 255, 255])

    # Blue for Magic: #6969FF
    blue_lower = np.array([100, 80, 100])
    blue_upper = np.array([130, 255, 255])

    # Yellow for Rare: #FFFF64
    yellow_lower = np.array([20, 80, 100])
    yellow_upper = np.array([40, 255, 255])

    # Orange for Crafted/Loose Runes: #FFA800
    orange_lower = np.array([10, 80, 100])
    orange_upper = np.array([25, 255, 255])

    # White for Normal/Sockets: #FFFFFF
    white_lower = np.array([0, 0, 180])
    white_upper = np.array([180, 40, 255])

    # Grey for Sockets/Bases: #696969
    gray_lower = np.array([0, 0, 60])
    gray_upper = np.array([180, 30, 180])

    gold_mask = cv2.inRange(hsv, gold_lower, gold_upper)
    green_mask = cv2.inRange(hsv, green_lower, green_upper)
    blue_mask = cv2.inRange(hsv, blue_lower, blue_upper)
    yellow_mask = cv2.inRange(hsv, yellow_lower, yellow_upper)
    orange_mask = cv2.inRange(hsv, orange_lower, orange_upper)
    white_mask = cv2.inRange(hsv, white_lower, white_upper)
    gray_mask = cv2.inRange(hsv, gray_lower, gray_upper)

    text_mask = gold_mask | green_mask | blue_mask | yellow_mask | orange_mask | white_mask | gray_mask

    # Morphology to connect text characters
    kernel = np.ones((3, 3), np.uint8)
    text_mask = cv2.morphologyEx(text_mask, cv2.MORPH_CLOSE, kernel)
    text_mask = cv2.morphologyEx(text_mask, cv2.MORPH_OPEN, kernel)

    return text_mask

def _find_text_components(text_mask):
    """Find all text components from a color mask."""
    num_labels, labels, stats, centroids = cv2.connectedComponentsWithStats(text_mask, connectivity=8)
    text_components = []
    for i in range(1, len(stats)):
        x, y, w, h, area = stats[i]
        if area >= 15 and w >= 5 and h >= 3:
            cx, cy = centroids[i]
            text_components.append({
                "x": x, "y": y, "w": w, "h": h, "area": area,
                "cx": x + w / 2, "cy": y + h / 2, "x2": x + w, "y2": y + h
            })
    return text_components


def _find_title_line(img, cur_x=None, cur_y=None):
    """
    Multi-Color Header Detection (Top Anchor):
    Scan the entire screenshot for item name headers in rarity colors.
    Returns (x, y, w, h) of the title line bounding box, or None.
    """
    import cv2
    import numpy as np
    h, w = img.shape[:2]

    # Use text mask to find ALL text, then project horizontally
    text_mask = _create_color_masks(img)

    # Horizontal projection to find text rows
    h_proj = np.sum(text_mask > 0, axis=1)

    # Find text rows in top portion (top 50% of image)
    search_limit = min(h, int(h * 0.5))
    text_rows = np.where(h_proj[:search_limit] > 10)[0]

    if len(text_rows) == 0:
        return None

    # Group consecutive rows into lines
    lines = []
    current = [text_rows[0]]
    for y in text_rows[1:]:
        if y - current[-1] <= 3:
            current.append(y)
        else:
            lines.append((current[0], current[-1]))
            current = [y]
    lines.append((current[0], current[-1]))

    if not lines:
        return None

    # Find first SUBSTANTIAL line that's NOT full-width border noise
    for title_y1, title_y2 in lines:
        line_mask = text_mask[title_y1:title_y2+1, :]
        h_proj_line = np.sum(line_mask > 0, axis=0)
        text_cols = np.where(h_proj_line > 5)[0]

        if len(text_cols) == 0:
            continue

        left = text_cols[0]
        right = text_cols[-1]
        line_width = right - left + 1

        # Skip full-width lines (likely border noise)
        if line_width >= w * 0.9:
            continue

        # Title line should be substantial (at least 50px wide)
        if line_width >= 50:
            return (left, title_y1, line_width, title_y2 - title_y1 + 1)

    # Fallback: use first non-full-width line
    for title_y1, title_y2 in lines:
        line_mask = text_mask[title_y1:title_y2+1, :]
        h_proj_line = np.sum(line_mask > 0, axis=0)
        text_cols = np.where(h_proj_line > 5)[0]

        if len(text_cols) == 0:
            continue

        left = text_cols[0]
        right = text_cols[-1]
        line_width = right - left + 1

        if line_width < w * 0.9:
            return (left, title_y1, line_width, title_y2 - title_y1 + 1)

    return None

def _expand_from_title(img, tx, ty, tw, th):
    """
    Dynamic Text Clustering (Vertical Grouping):
    From title line, group all vertically stacked text lines associated with that item.
    Find the lowest valid text line or prompt to establish the bottom boundary (Y_max).
    """
    import cv2
    import numpy as np
    h, w = img.shape[:2]

    # Use text-based expansion: find ALL text components below title
    text_mask = _create_color_masks(img)
    text_components = _find_text_components(text_mask)

    # Filter to components at or below title
    below = [c for c in text_components if c["y"] >= ty]

    if len(below) >= 3:
        min_x = min(c["x"] for c in below)
        max_x2 = max(c["x2"] for c in below)
        min_y = min(c["y"] for c in below)
        max_y2 = max(c["y2"] for c in below)

        # Include title in bbox
        min_x = min(min_x, tx)
        max_x2 = max(max_x2, tx + tw)
        min_y = min(min_y, ty)

        return (min_x, min_y, max_x2 - min_x, max_y2 - min_y)

    # Fallback: use full text bbox
    all_comps = _find_text_components(_create_color_masks(img))
    if all_comps:
        min_x = min(c["x"] for c in all_comps)
        max_x2 = max(c["x2"] for c in all_comps)
        min_y = min(c["y"] for c in all_comps)
        max_y2 = max(c["y2"] for c in all_comps)
        return (min_x, min_y, max_x2 - min_x, max_y2 - min_y)

    return (tx, ty, tw, th + 100)

def _find_all_dark_panels(img):
    """Find all dark rectangular panels in the image that could be tooltips.

    Tooltips have a distinct dark semi-transparent background with:
    - Colored title at the top (gold/green/blue/white/red/orange)
    - Multi-line text below
    - Aspect ratio typically 1.5-6.0
    """
    import cv2
    import numpy as np
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    adaptive = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_MEAN_C, 
                                      cv2.THRESH_BINARY_INV, 15, 5)
    panel_contours, _ = cv2.findContours(adaptive, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    panels = []
    for cnt in panel_contours:
        x, y, cw, ch = cv2.boundingRect(cnt)
        area = cv2.contourArea(cnt)
        if area < 500 or cw < 50 or ch < 30:
            continue
        aspect = cw / ch if ch > 0 else 0
        if aspect < 1.2 or aspect > 8.0:
            continue

        # Quick check: panel should have a colored title at the top
        panel_region = img[y:y+ch, x:x+cw]
        if _panel_has_tooltip_title(panel_region):
            panels.append({"x": x, "y": y, "cw": cw, "ch": ch, "area": area})

    # Sort by area descending (largest first)
    panels.sort(key=lambda p: p["area"], reverse=True)
    return panels

def _panel_has_tooltip_title(panel_img):
    """Check if panel has a tooltip-like title at the top.

    Tooltip title should be:
    - Near the top of the panel (first 30 rows)
    - Colored (gold/green/blue/white/red/orange)
    - Horizontal text spanning significant width
    """
    if panel_img is None or panel_img.size == 0:
        return False
    h, w = panel_img.shape[:2]
    if h < 30 or w < 50:
        return False

    # Check the top portion of the panel (first 30 rows) for colored title
    # Title is often several pixels down from the very top due to panel border
    title_h = min(30, h)
    title_region = panel_img[:title_h, :]
    gray = cv2.cvtColor(title_region, cv2.COLOR_BGR2GRAY)
    bright = gray > 90  # Slightly lower threshold to catch title text
    if not bright.any():
        return False

    # Check for rarity colors in the top rows
    import numpy as _np
    mean_bgr = _np.array([title_region[:, :, c][bright].mean() for c in range(3)])
    # Stricter distance for tooltip title
    best_dist = min(
        sum((int(mean_bgr[i]) - ref[i]) ** 2 for i in range(3))
        for ref in [(60, 160, 255), (60, 200, 60), (60, 160, 255), (255, 160, 60), (200, 200, 200), (30, 150, 255)]
    )
    if best_dist > 12000:  # Tolerance for tooltip title
        return False

    # Title should span significant width (not just a few pixels)
    bright_cols = np.where(bright.any(axis=0))[0]
    if len(bright_cols) < 20:  # Title should be at least 20px wide
        return False

    return True

def _looks_like_tooltip_panel(panel_img):
    """Check if a panel region looks like a tooltip (colored title + multi-line text)."""
    if panel_img is None or panel_img.size == 0:
        return False

    h, w = panel_img.shape[:2]
    if h < 30 or w < 50:
        return False

    # Check top portion for colored title line (rarity colors)
    # Title is typically in the first 15-20% of panel height
    title_h = max(1, min(15, h // 5))
    title_region = panel_img[:title_h, :]
    gray = cv2.cvtColor(title_region, cv2.COLOR_BGR2GRAY)
    bright = gray > 90
    if not bright.any():
        return False

    # Mean color of bright pixels only
    import numpy as _np
    mean_bgr = _np.array([title_region[:, :, c][bright].mean() for c in range(3)])

    # Diablo 2 rarity colors (BGR) - matches specification
    rarity_colors = {
        'unique': (60, 160, 255),     # gold/tan #C7B377/#D8B41D
        'set': (60, 200, 60),         # green #00FF00
        'rare': (60, 160, 255),       # yellow #FFFF64
        'magic': (255, 160, 60),      # blue #6969FF
        'normal': (200, 200, 200),    # white #FFFFFF
        'crafted': (30, 150, 255),    # orange #FFA800
    }

    best_dist = min(
        sum((int(mean_bgr[i]) - ref[i]) ** 2 for i in range(3))
        for ref in rarity_colors.values()
    )
    if best_dist > 12000:  # Tolerance for color match
        return False

    # Multi-line check using text mask (more reliable than adaptive threshold)
    text_mask = _create_color_masks(panel_img)
    h_proj = np.sum(text_mask > 0, axis=1)

    # Count text lines (group consecutive rows with text)
    text_rows = np.where(h_proj > 10)[0]
    if len(text_rows) == 0:
        return False

    # Group consecutive rows into lines
    lines = 0
    in_line = False
    for r in (h_proj > 10):
        if r and not in_line:
            lines += 1
            in_line = True
        elif not r:
            in_line = False

    return lines >= 2

def detect_tooltip_in_band(band_img, cur_x=None, cur_y=None):
    """Find the item tooltip inside a band image using spec-compliant detection.

    Multi-Color Header Detection (Top Anchor):
        Scan the entire screenshot for item name headers in rarity colors:
        Unique/Runeword: Gold/Tan (#C7B377 / #D8B41D)
        Set: Bright Green (#00FF00)
        Magic: Light Blue (#6969FF)
        Rare: Bright Yellow (#FFFF64)
        Crafted/Loose Runes: Orange (#FFA800)
        Sockets/Bases/Normal: White (#FFFFFF) or Grey (#696969)

    Dynamic Text Clustering (Vertical Grouping):
        Once a title color is detected, treat it as the top boundary (Y_min).
        Group all vertically stacked text lines associated with that item
        (sub-headers like item types, stats, requirements, socket counts,
        and bottom action hints like Ctrl + Left Click).
        Find the lowest valid text line or prompt to establish the bottom boundary (Y_max).

    Adaptive Bounding and Width Expansion:
        Determine the left (X_min) and right (X_max) bounds by finding
        the widest line of text in that specific tooltip stack.
        Handle variable sizing gracefully - small items like rings have
        compact tooltips, while multi-stat runewords or weapons are tall and wide.

    Padding and Clean Output:
        Add a uniform 15-20 pixel padding around all four sides of the final
        calculated bounding box to ensure the dark semi-transparent panel
        background is fully captured without clipping the edges.

    Strategy 1 (Primary): Multi-color header detection + vertical text clustering
    Strategy 2 (Fallback): Cursor-aware text clustering near cursor position
    Strategy 3 (Last resort): Dark panel detection with validation
    """
    import cv2
    import numpy as np
    img = band_img
    h, w = img.shape[:2]

    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)

    gold_lower = np.array([15, 40, 120])
    gold_upper = np.array([35, 255, 255])
    green_lower = np.array([35, 80, 80])
    green_upper = np.array([85, 255, 255])
    blue_lower = np.array([100, 80, 100])
    blue_upper = np.array([130, 255, 255])
    yellow_lower = np.array([20, 80, 100])
    yellow_upper = np.array([40, 255, 255])
    orange_lower = np.array([10, 80, 100])
    orange_upper = np.array([25, 255, 255])
    white_lower = np.array([0, 0, 180])
    white_upper = np.array([180, 40, 255])
    gray_lower = np.array([0, 0, 60])
    gray_upper = np.array([180, 30, 180])

    gold_mask = cv2.inRange(hsv, gold_lower, gold_upper)
    green_mask = cv2.inRange(hsv, green_lower, green_upper)
    blue_mask = cv2.inRange(hsv, blue_lower, blue_upper)
    yellow_mask = cv2.inRange(hsv, yellow_lower, yellow_upper)
    orange_mask = cv2.inRange(hsv, orange_lower, orange_upper)
    white_mask = cv2.inRange(hsv, white_lower, white_upper)
    gray_mask = cv2.inRange(hsv, gray_lower, gray_upper)

    rarity_mask = gold_mask | green_mask | blue_mask | yellow_mask | orange_mask
    text_mask = rarity_mask | white_mask | gray_mask

    kernel = np.ones((3, 3), np.uint8)
    text_mask = cv2.morphologyEx(text_mask, cv2.MORPH_CLOSE, kernel)
    text_mask = cv2.morphologyEx(text_mask, cv2.MORPH_OPEN, kernel)
    rarity_mask = cv2.morphologyEx(rarity_mask, cv2.MORPH_CLOSE, kernel)
    rarity_mask = cv2.morphologyEx(rarity_mask, cv2.MORPH_OPEN, kernel)

    h_proj_rarity = np.sum(rarity_mask > 0, axis=1)
    search_limit = min(h, int(h * 0.4))
    title_rows = np.where(h_proj_rarity[:search_limit] > 5)[0]  # Lower threshold to catch sparse colored highlights

    if len(title_rows) == 0:
        h_proj_text = np.sum(text_mask > 0, axis=1)
        title_rows = np.where(h_proj_text[:search_limit] > 30)[0]

    if len(title_rows) == 0:
        pass
    else:
        title_lines = []
        current = [title_rows[0]]
        for y in title_rows[1:]:
            if y - current[-1] <= 3:
                current.append(y)
            else:
                title_lines.append((current[0], current[-1]))
                current = [y]
        title_lines.append((current[0], current[-1]))

        # Filter title lines by cursor position if available
        # Tooltip is typically below and near the cursor
        if cur_y is not None:
            # Only consider title lines within reasonable distance below cursor
            # Tooltip usually appears within 300px below cursor
            filtered_lines = []
            for y1, y2 in title_lines:
                # Title should be at or below cursor, within ~300px
                if y1 >= cur_y - 20 and y1 <= cur_y + 300:
                    filtered_lines.append((y1, y2))
            if filtered_lines:
                title_lines = filtered_lines

        best_title_bbox = None
        best_score = -1

        for y1, y2 in title_lines:
            # Find the rarity-colored portion of the title to get horizontal position
            line_rarity = rarity_mask[y1:y2+1, :]
            rarity_pixels = cv2.countNonZero(line_rarity)
            if rarity_pixels < 10:  # Must have some colored pixels
                continue

            h_proj_rarity_line = np.sum(line_rarity > 0, axis=0)
            rarity_cols = np.where(h_proj_rarity_line > 1)[0]
            if len(rarity_cols) == 0:
                continue
            
            rarity_left = rarity_cols[0]
            rarity_right = rarity_cols[-1]
            rarity_width = rarity_right - rarity_left + 1
            rarity_cx = rarity_left + rarity_width // 2

            # Now find text components aligned with this rarity center
            # Use text_mask components to measure full title width
            num_labels, labels, stats, centroids = cv2.connectedComponentsWithStats(text_mask, connectivity=8)
            
            # Find text components on this title line (similar Y position)
            title_line_components = []
            for i in range(1, len(stats)):
                x, y, cw, ch, area = stats[i]
                if area >= 15 and cw >= 5 and ch >= 3:
                    cx, cy = centroids[i]
                    # Check if component is on this title line (within 10px vertically)
                    if y1 - 5 <= cy <= y2 + 5:
                        # Check horizontal alignment with rarity center
                        horiz_tolerance = max(rarity_width, cw) * 1.0  # Wider tolerance for title
                        if abs(cx - rarity_cx) < horiz_tolerance:
                            title_line_components.append({'x': x, 'x2': x + cw, 'cx': cx, 'cy': cy})
            
            if len(title_line_components) < 2:
                continue
            
            # Measure full title width from aligned text components
            left = min(c['x'] for c in title_line_components)
            right = max(c['x2'] for c in title_line_components)
            line_width = right - left + 1
            title_cx = (left + right) // 2

            # Skip full-width lines (likely border noise)
            if line_width >= w * 0.85:
                continue

            # Title should be substantial
            if line_width < 30:
                continue

            tx, ty, tw, th = left, y1, line_width, y2 - y1 + 1

            # Score by aligned components below
            aligned_count = 0
            for i in range(1, len(stats)):
                x, y, cw, ch, area = stats[i]
                if area >= 15 and cw >= 5 and ch >= 3:
                    cx, cy = centroids[i]
                    if y >= ty - 5:
                        horiz_tolerance = max(tw, cw) * 0.7
                        if abs(cx - title_cx) < horiz_tolerance:
                            aligned_count += 1

            if aligned_count > best_score:
                best_score = aligned_count
                best_title_bbox = (tx, ty, tw, th)

        if best_title_bbox is None:
            # Fallback: use first valid line with text_mask width
            for y1, y2 in title_lines:
                line_text = text_mask[y1:y2+1, :]
                h_proj_line = np.sum(line_text > 0, axis=0)
                text_cols = np.where(h_proj_line > 5)[0]

                if len(text_cols) == 0:
                    continue

                left = text_cols[0]
                right = text_cols[-1]
                line_width = right - left + 1

                if line_width < w * 0.85 and line_width >= 30:
                    best_title_bbox = (left, y1, line_width, y2 - y1 + 1)
                    break

        if best_title_bbox is not None:
            tx, ty, tw, th = best_title_bbox
            title_cx = tx + tw // 2

            num_labels, labels, stats, centroids = cv2.connectedComponentsWithStats(text_mask, connectivity=8)
            text_components = []
            for i in range(1, len(stats)):
                x, y, cw, ch, area = stats[i]
                if area >= 15 and cw >= 5 and ch >= 3:
                    cx, cy = centroids[i]
                    text_components.append({
                        'x': x, 'y': y, 'w': cw, 'h': ch, 'area': area,
                        'cx': x + cw / 2, 'cy': y + ch / 2, 'x2': x + cw, 'y2': y + ch
                    })

            tooltip_components = []
            for c in text_components:
                if c['y'] >= ty - 5:
                    horiz_tolerance = max(tw, c['w']) * 0.7
                    if abs(c['cx'] - title_cx) < horiz_tolerance:
                        tooltip_components.append(c)

            if len(tooltip_components) >= 3:
                tooltip_components.sort(key=lambda c: c['cy'])
                y_min = ty
                y_max = max(c['y2'] for c in tooltip_components)

                lines = []
                current_line = [tooltip_components[0]]
                for c in tooltip_components[1:]:
                    if abs(c['cy'] - current_line[-1]['cy']) <= 10:
                        current_line.append(c)
                    else:
                        lines.append(current_line)
                        current_line = [c]
                lines.append(current_line)

                max_width = 0
                best_line_left = tx
                best_line_right = tx + tw

                for line in lines:
                    line_left = min(c['x'] for c in line)
                    line_right = max(c['x2'] for c in line)
                    line_width = line_right - line_left
                    if line_width > max_width:
                        max_width = line_width
                        best_line_left = line_left
                        best_line_right = line_right

                x_min = min(best_line_left, tx)
                x_max = max(best_line_right, tx + tw)

                final_w = x_max - x_min
                final_h = y_max - y_min

                # Adaptive padding based on tooltip size - less padding for small tooltips
                tooltip_area = final_w * final_h
                if tooltip_area < 50000:  # Small tooltip (< ~220x220)
                    pad = 15
                elif tooltip_area < 150000:  # Medium tooltip
                    pad = 30
                else:  # Large tooltip
                    pad = 50

                x_min = max(0, x_min - pad)
                y_min = max(0, y_min - pad)
                x_max = min(w, x_max + pad)
                y_max = min(h, y_max + pad)

                final_w = x_max - x_min
                final_h = y_max - y_min

                if final_w >= 50 and final_h >= 30:                # Optionally use tighter crop for even more precise cropping
                    tight_crop = crop_tooltip_tight(img, (x_min, y_min, final_w, final_h))
                    if tight_crop is not None and tight_crop.shape[0] >= 30 and tight_crop.shape[1] >= 30:
                        return tight_crop
                    return img[y_min:y_max, x_min:x_max]

def crop_tooltip_tight(img, tooltip_bbox=None):
    """
    Tighter crop that finds the exact text bounds using color masks.
    Crops as close as possible to the actual text content.
    """
    if tooltip_bbox is None:
        return None
    
    tx, ty, tw, th = tooltip_bbox
    h, w = img.shape[:2]
    
    # Use color masks to find exact text boundaries
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    
    # Text colors (white, gold, green, blue, yellow, orange, gray)
    gold_mask = cv2.inRange(hsv, np.array([15, 40, 120]), np.array([35, 255, 255]))
    green_mask = cv2.inRange(hsv, np.array([35, 80, 80]), np.array([85, 255, 255]))
    blue_mask = cv2.inRange(hsv, np.array([100, 80, 100]), np.array([130, 255, 255]))
    yellow_mask = cv2.inRange(hsv, np.array([20, 80, 100]), np.array([40, 255, 255]))
    orange_mask = cv2.inRange(hsv, np.array([10, 80, 100]), np.array([25, 255, 255]))
    white_mask = cv2.inRange(hsv, np.array([0, 0, 180]), np.array([180, 40, 255]))
    gray_mask = cv2.inRange(hsv, np.array([0, 0, 60]), np.array([180, 30, 180]))
    
    text_mask = gold_mask | green_mask | blue_mask | yellow_mask | orange_mask | white_mask | gray_mask
    
    # Morphology to connect text
    kernel = np.ones((3, 3), np.uint8)
    text_mask = cv2.morphologyEx(text_mask, cv2.MORPH_CLOSE, kernel)
    text_mask = cv2.morphologyEx(text_mask, cv2.MORPH_OPEN, kernel)
    
    # Find text bounds within tooltip region
    roi = text_mask[ty:ty+th, tx:tx+tw]
    if roi.size == 0:
        return None
    
    coords = cv2.findNonZero(roi)
    if coords is None:
        return None
    
    coords = coords.reshape(-1, 2)
    x_coords = coords[:, 0]
    y_coords = coords[:, 1]
    
    # Very tight padding (5px)
    tight_pad = 5
    x1 = max(0, tx + x_coords.min() - tight_pad)
    y1 = max(0, ty + y_coords.min() - tight_pad)
    x2 = min(w, tx + x_coords.max() + tight_pad)
    y2 = min(h, ty + y_coords.max() + tight_pad)
    
    if x2 <= x1 or y2 <= y1:
        return None
    
    return img[y1:y2, x1:x2]

    text_mask = _create_color_masks(img)
    text_components = _find_text_components(text_mask)

    if text_components:
        if cur_x is not None and cur_y is not None:
            search_radius_x = 300
            search_radius_y = 250
            nearby = [c for c in text_components
                      if abs(c["cx"] - cur_x) <= search_radius_x
                      and abs(c["cy"] - cur_y) <= search_radius_y]

            if len(nearby) >= 3:
                text_components = nearby

        if len(text_components) >= 3:
            min_y = min(c["y"] for c in text_components)
            max_y2 = max(c["y2"] for c in text_components)
            min_x = min(c["x"] for c in text_components)
            max_x2 = max(c["x2"] for c in text_components)

            pad = 20
            fx = max(0, min_x - pad)
            fy = max(0, min_y - pad)
            fw = min(w, max_x2 + pad) - fx
            fh = min(h, max_y2 + pad) - fy

            return img[fy:fy+fh, fx:fx+fw]

    panels = _find_all_dark_panels(img)

    for panel in panels:
        px, py, pw, ph = panel["x"], panel["y"], panel["cw"], panel["ch"]
        panel_region = img[py:py+ph, px:px+pw]

        if _looks_like_tooltip_panel(panel_region):
            pad = 20
            fx = max(0, px - pad)
            fy = max(0, py - pad)
            fw = min(w, px + pw + pad) - fx
            fh = min(h, py + ph + pad) - fy
            return img[fy:fy+fh, fx:fx+fw]

    return None

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
