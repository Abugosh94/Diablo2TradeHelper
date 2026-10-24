"""Quick list tooltip capture: ONNX tooltip crop for posting to trade sites.

No parsing, no profiles, no OCR - just capture the hovered tooltip image
so it can be copied to the clipboard and pasted into a trading site.
"""
from config import load_config


class InventoryScanner:
    def __init__(self):
        self.cfg = load_config()

    def capture_item_for_posting(self):
        """Capture ONLY the tightly-cropped tooltip with the ONNX detector.

        Returns the crop image, or None when no valid tooltip is found -
        we must never post an empty/garbage crop to the website.
        """
        from capture import capture_tooltip
        try:
            img, _conf = capture_tooltip()
            if img is not None:
                return img
            # ONNX returned no crop - do not fall back to legacy detector.
            # The GUI will open manual crop selection instead.
            print("[capture] ONNX returned no crop - manual crop required")
        except Exception as e:
            print(f"[capture] ONNX path failed ({e}) - manual crop required")
        return None

