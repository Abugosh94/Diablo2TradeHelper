"""Subprocess worker for YOLO tooltip capture.

Runs in a separate Python process so that torch + YOLO (+ cv2) memory is
fully reclaimed by the operating system after each capture. The main app
never loads torch itself.

Communication:
  argv[1] = output image path (PNG)
  argv[2] = confidence sidecar path (text file with float)

Exits 0 on success (image may be empty if no tooltip found).
Exits 1 on unexpected error.
"""
import sys
import os
import cv2


def main():
    if len(sys.argv) < 3:
        print("Usage: capture_worker.py <output_image_path> <confidence_path>",
              file=sys.stderr)
        sys.exit(1)

    output_path = sys.argv[1]
    conf_path = sys.argv[2]

    try:
        # Import inside worker process so torch is never loaded in the parent.
        from capture import get_tooltip_model, capture_tooltip_yolo

        model = get_tooltip_model()
        if model is None:
            # Signal failure: empty image, zero confidence.
            open(output_path, "wb").close()
            with open(conf_path, "w") as f:
                f.write("0.0")
            sys.exit(0)

        img, conf = capture_tooltip_yolo()
        if img is not None:
            cv2.imwrite(output_path, img)
            with open(conf_path, "w") as f:
                f.write(str(float(conf)))
        else:
            open(output_path, "wb").close()
            with open(conf_path, "w") as f:
                f.write("0.0")

    except Exception as e:
        print(f"[capture_worker] Error: {e}", file=sys.stderr)
        try:
            open(output_path, "wb").close()
            with open(conf_path, "w") as f:
                f.write("0.0")
        except Exception:
            pass
        sys.exit(1)

    sys.exit(0)


if __name__ == "__main__":
    main()