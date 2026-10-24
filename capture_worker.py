"""Long-running subprocess worker for ONNX tooltip capture.

Loads ONNX model once at startup, then processes capture requests via
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

# Safe print: when running as --noconsole exe, sys.stderr/sys.stdout may be
# invalid and print() raises OSError: [Errno 22] Invalid argument.
def safe_print(*args, **kwargs):
    try:
        print(*args, **kwargs)
    except Exception:
        pass

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
        return int(cfg.get("capture", {}).get("worker_idle_timeout", 180))
    except Exception:
        return 180


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

# ONNX model settings
MODEL_IMG_SIZE = 640  # ONNX input size
CONF_THRES_DEFAULT = 0.5
IOU_THRES = 0.45  # NMS IoU threshold


def _letterbox(img, new_shape=(MODEL_IMG_SIZE, MODEL_IMG_SIZE), color=(114, 114, 114)):
    """Resize image to new_shape with letterbox padding."""
    shape = img.shape[:2]
    if isinstance(new_shape, int):
        new_shape = (new_shape, new_shape)
    r = min(new_shape[0] / shape[0], new_shape[1] / shape[1])
    new_unpad = int(round(shape[1] * r)), int(round(shape[0] * r))
    dw, dh = new_shape[1] - new_unpad[0], new_shape[0] - new_unpad[1]
    dw /= 2
    dh /= 2
    if shape[::-1] != new_unpad:
        img = cv2.resize(img, new_unpad, interpolation=cv2.INTER_LINEAR)
    top, bottom = int(round(dh - 0.1)), int(round(dh + 0.1))
    left, right = int(round(dw - 0.1)), int(round(dw + 0.1))
    img = cv2.copyMakeBorder(img, top, bottom, left, right, cv2.BORDER_CONSTANT, value=color)
    return img, r, (dw, dh)


def _preprocess(img):
    """Letterbox + normalize to 0-1 + BGR->RGB + HWC->NCHW + add batch."""
    img_resized, ratio, pad = _letterbox(img, (MODEL_IMG_SIZE, MODEL_IMG_SIZE))
    img_rgb = cv2.cvtColor(img_resized, cv2.COLOR_BGR2RGB)
    img_norm = img_rgb.astype(np.float32) / 255.0
    img_chw = np.transpose(img_norm, (2, 0, 1))
    img_nchw = np.expand_dims(img_chw, axis=0)
    return img_nchw, ratio, pad


def _xywh2xyxy(x):
    """Convert [x_center, y_center, w, h] to [x1, y1, x2, y2]."""
    y = np.copy(x)
    y[..., 0] = x[..., 0] - x[..., 2] / 2
    y[..., 1] = x[..., 1] - x[..., 3] / 2
    y[..., 2] = x[..., 0] + x[..., 2] / 2
    y[..., 3] = x[..., 1] + x[..., 3] / 2
    return y


def _nms(boxes, scores, iou_thres):
    """Non-Maximum Suppression."""
    if len(boxes) == 0:
        return []
    x1 = boxes[:, 0]
    y1 = boxes[:, 1]
    x2 = boxes[:, 2]
    y2 = boxes[:, 3]
    areas = (x2 - x1) * (y2 - y1)
    order = scores.argsort()[::-1]
    keep = []
    while order.size > 0:
        i = order[0]
        keep.append(i)
        xx1 = np.maximum(x1[i], x1[order[1:]])
        yy1 = np.maximum(y1[i], y1[order[1:]])
        xx2 = np.minimum(x2[i], x2[order[1:]])
        yy2 = np.minimum(y2[i], y2[order[1:]])
        w = np.maximum(0.0, xx2 - xx1)
        h = np.maximum(0.0, yy2 - yy1)
        inter = w * h
        ovr = inter / (areas[i] + areas[order[1:]] - inter)
        inds = np.where(ovr <= iou_thres)[0]
        order = order[inds + 1]
    return keep


def _scale_coords(img1_shape, coords, img0_shape):
    """Rescale coords from letterboxed shape back to original."""
    gain = min(img1_shape[0] / img0_shape[0], img1_shape[1] / img0_shape[1])
    pad = (img1_shape[1] - img0_shape[1] * gain) / 2, (img1_shape[0] - img0_shape[0] * gain) / 2
    coords[:, [0, 2]] -= pad[0]
    coords[:, [1, 3]] -= pad[1]
    coords[:, :4] /= gain
    coords[:, 0] = np.clip(coords[:, 0], 0, img0_shape[1])
    coords[:, 1] = np.clip(coords[:, 1], 0, img0_shape[0])
    coords[:, 2] = np.clip(coords[:, 2], 0, img0_shape[1])
    coords[:, 3] = np.clip(coords[:, 3], 0, img0_shape[0])
    return coords


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
    import onnxruntime as ort
    model_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "tooltip_best_v6.onnx")
    if not os.path.exists(model_path):
        import glob
        for candidate in ["tooltip_best_v6.onnx",
                          os.path.join(tempfile.gettempdir(), "_MEI*", "tooltip_best_v6.onnx")]:
            matches = glob.glob(candidate)
            if matches:
                model_path = matches[0]
                break
    sess = ort.InferenceSession(model_path)
    return sess


def _detect_tooltip(sess, full, conf_thres=CONF_THRES_DEFAULT):
    """Run ONNX inference and return best tooltip crop."""
    orig_shape = full.shape[:2]
    input_tensor, ratio, pad = _preprocess(full)
    input_name = sess.get_inputs()[0].name
    outputs = sess.run(None, {input_name: input_tensor})
    outputs = outputs[0]
    predictions = outputs[0].T
    boxes = predictions[:, 0:4]
    scores = predictions[:, 4]
    mask = scores > conf_thres
    if not mask.any():
        return None, 0.0, 0
    boxes = boxes[mask]
    scores = scores[mask]
    boxes_xyxy = _xywh2xyxy(boxes)
    boxes_xyxy = _scale_coords((MODEL_IMG_SIZE, MODEL_IMG_SIZE), boxes_xyxy, orig_shape)
    keep = _nms(boxes_xyxy, scores, IOU_THRES)
    boxes_xyxy = boxes_xyxy[keep]
    scores = scores[keep]
    n_seen = len(boxes_xyxy)
    valid_mask = np.ones(len(boxes_xyxy), dtype=bool)
    for i, box in enumerate(boxes_xyxy):
        bw = box[2] - box[0]
        bh = box[3] - box[1]
        if bw < MIN_BOX_W or bh < MIN_BOX_H or bw / bh > MAX_BOX_ASPECT:
            valid_mask[i] = False
    boxes_xyxy = boxes_xyxy[valid_mask]
    scores = scores[valid_mask]
    if len(boxes_xyxy) == 0:
        return None, 0.0, n_seen
    best_idx = np.argmax(scores)
    best_box = boxes_xyxy[best_idx]
    best_conf = scores[best_idx]
    x1, y1, x2, y2 = [int(v) for v in best_box]
    crop = full[y1:y2, x1:x2]
    crop_box = _align_to_border(full, (x1, y1, x2, y2))
    crop_box = _pad_box(full, crop_box)
    crop = full[crop_box[1]:crop_box[3], crop_box[0]:crop_box[2]]
    return crop, float(best_conf), len(boxes_xyxy)


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

    safe_print(f"[capture_worker] Started (PID={os.getpid()})", flush=True)

    try:
        model = _load_model()
        safe_print(f"[capture_worker] Model loaded", flush=True)
    except Exception as e:
        safe_print(f"[capture_worker] Failed to load model: {e}", flush=True)
        sys.exit(1)

    last_activity = time.time()

    while True:
        now = time.time()
        idle_timeout = _read_idle_timeout()
        if idle_timeout > 0 and now - last_activity > idle_timeout:
            safe_print(f"[capture_worker] Idle timeout ({idle_timeout}s) - exiting", flush=True)
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
            safe_print(f"[capture_worker] Error processing request: {e}", flush=True)
        finally:
            try:
                os.remove(req_file)
            except Exception:
                pass

    sys.exit(0)


if __name__ == "__main__":
    main()