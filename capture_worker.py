"""Long-running subprocess worker for YOLO tooltip capture.

Loads torch + YOLO once at startup, then processes capture requests via
temp files. Exits after IDLE_TIMEOUT seconds of inactivity so the OS
reclaims all worker memory.

Communication:
  Request file:  <tempdir>/d2_capture_requests/d2_req_<id>.json
    {"output_path": "...", "conf_path": "...", "conf_thres": 0.5}
  Response: worker writes result PNG to output_path, confidence to conf_path,
  then deletes the request file.

Idle timeout: 300 seconds (5 minutes).
"""
import sys
import os
import time
import json
import tempfile
import glob

import cv2
import numpy as np
import mss

_meipass = getattr(sys, "_MEIPASS", None)
if _meipass and _meipass not in sys.path:
    sys.path.insert(0, _meipass)

REQUEST_DIR = os.path.join(tempfile.gettempdir(), "d2_capture_requests")


def _read_idle_timeout() -> int:
    """Read the configured idle timeout from config.json.

    Reads on every call so GUI changes take effect without restarting the worker.
    Falls back to 300 seconds (5 min) if config is missing/unreadable.
    """
    try:
        import json
        config_path = os.path.join(app_dir(), "config.json")
        with open(config_path, "r") as f:
            cfg = json.load(f)
        return int(cfg.get("capture", {}).get("worker_idle_timeout", 300))
    except Exception:
        return 300


def app_dir():
    """Directory where config.json lives (exe folder when frozen)."""
    import sys
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))

MIN_BOX_W = 250
MIN_BOX_H = 55
MAX_BOX_ASPECT = 13.5
BOX_INSET = 6
BOX_EXPAND = 3
CROP_PAD = 8
CROP_PAD_X = 20


def _strip_mean(img, xa, ya, xb, yb):
    h, w = img.shape[:2]
    xa, ya = max(0, xa), max(0, ya)
    xb, yb = min(w, xb), min(h, yb)
    if xb <= xa or yb <= ya:
        return -1.0
    return float(cv2.cvtColor(img[ya:yb, xa:xb], cv2.COLOR_BGR2GRAY).mean())


def _align_to_border(img, box):
    x1, y1, x2, y2 = box
    h, w = img.shape[:2]
    for _ in range(BOX_EXPAND):
        if 0 <= _strip_mean(img, x1 - 1, y1, x1, y2) < 30:
            x1 -= 1
        if 0 <= _strip_mean(img, x1, y1 - 1, x2, y1) < 30:
            y1 -= 1
        if 0 <= _strip_mean(img, x2, y1, x2 + 1, y2) < 30:
            x2 += 1
        if 0 <= _strip_mean(img, x1, y2, x2, y2 + 1) < 30:
            y2 += 1
    for _ in range(BOX_INSET):
        if _strip_mean(img, x1, y1, x1 + 1, y2) >= 45:
            x1 += 1
        if _strip_mean(img, x1, y1, x2, y1 + 1) >= 45:
            y1 += 1
        if _strip_mean(img, x2 - 1, y1, x2, y2) >= 45:
            x2 -= 1
        if _strip_mean(img, x1, y2 - 1, x2, y2) >= 45:
            y2 -= 1
    x1, y1 = max(0, x1), max(0, y1)
    x2, y2 = min(w, x2), min(h, y2)
    if (x2 - x1) >= MIN_BOX_W and (y2 - y1) >= MIN_BOX_H:
        return (x1, y1, x2, y2)
    return box


def _center_span(limit, lo, hi):
    size = hi - lo
    if size >= limit:
        return (0, limit)
    mid = (lo + hi) / 2.0
    lo = int(round(mid - size / 2.0))
    lo = max(0, min(limit - size, lo))
    return (lo, lo + size)


def _pad_box(img, box, pad=CROP_PAD, pad_x=CROP_PAD_X):
    h, w = img.shape[:2]
    x1, y1, x2, y2 = box
    cx1, cx2 = _center_span(w, x1 - pad_x, x2 + pad_x)
    cy1, cy2 = _center_span(h, y1 - pad, y2 + pad)
    return (cx1, cy1, cx2, cy2)


def _load_model():
    import torch
    from ultralytics import YOLO
    model_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "tooltip_best_v6.pt")
    if not os.path.exists(model_path):
        for candidate in ["tooltip_best_v6.pt",
                          os.path.join(tempfile.gettempdir(), "_MEI*",
                                       "tooltip_best_v6.pt")]:
            matches = glob.glob(candidate)
            if matches:
                model_path = matches[0]
                break
    return YOLO(model_path)


def _detect_tooltip(model, full, conf_thres=0.5):
    h, w = full.shape[:2]
    results = model(full, verbose=False, conf=conf_thres)

    best, best_conf, best_score = None, 0.0, -1e9
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
                continue
            dist = (((x1 + x2) / 2 - w / 2) ** 2 +
                    ((y1 + y2) / 2 - h / 2) ** 2) ** 0.5
            score = conf - 0.4 * dist / 1000.0
            if score > best_score:
                best = full[y1:y2, x1:x2]
                best_conf, best_score = conf, score

    return best, best_conf, n_seen


def _get_game_window_rect():
    import win32gui
    needle = "diablo"
    candidates = []

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

    if candidates:
        candidates.sort(key=lambda c: c[2] * c[3], reverse=True)
        l, t, w, h = candidates[0]
        if w > 200 and h > 150:
            return (l, t, w, h)
    return None


def main():
    os.makedirs(REQUEST_DIR, exist_ok=True)

    print(f"[capture_worker] Started (PID={os.getpid()})",
          file=sys.stderr, flush=True)

    try:
        model = _load_model()
        print(f"[capture_worker] Model loaded",
              file=sys.stderr, flush=True)
    except Exception as e:
        print(f"[capture_worker] Failed to load model: {e}",
              file=sys.stderr, flush=True)
        sys.exit(1)

    last_activity = time.time()

    while True:
        now = time.time()
        idle_timeout = _read_idle_timeout()
        if now - last_activity > idle_timeout:
            print(f"[capture_worker] Idle timeout ({idle_timeout}s) - exiting",
                  file=sys.stderr, flush=True)
            break

        try:
            entries = os.listdir(REQUEST_DIR)
        except Exception:
            entries = []
        request_files = sorted(
            f for f in entries if f.endswith(".req")
        )

        if not request_files:
            time.sleep(0.2)
            continue

        req_file = os.path.join(REQUEST_DIR, request_files[0])
        try:
            with open(req_file, "r") as f:
                req = json.load(f)

            output_path = req["output_path"]
            conf_path = req["conf_path"]
            conf_thres = req.get("conf_thres", 0.5)

            rect = _get_game_window_rect()
            if rect is None:
                open(output_path, "wb").close()
                with open(conf_path, "w") as f:
                    f.write("0.0")
            else:
                l, t, w, h = rect
                with mss.mss() as sct:
                    shot = sct.grab({"left": l, "top": t,
                                     "width": w, "height": h})
                full = np.array(shot)[:, :, :3]

                img, conf, n_seen = _detect_tooltip(model, full, conf_thres)

                if img is not None:
                    cv2.imwrite(output_path, img)
                    with open(conf_path, "w") as f:
                        f.write(str(float(conf)))
                else:
                    open(output_path, "wb").close()
                    with open(conf_path, "w") as f:
                        f.write("0.0")

            last_activity = time.time()
        except Exception as e:
            print(f"[capture_worker] Error processing request: {e}",
                  file=sys.stderr, flush=True)
        finally:
            try:
                os.remove(req_file)
            except Exception:
                pass

    sys.exit(0)


if __name__ == "__main__":
    main()