"""Price check feature: capture the hovered item tooltip and get an AI
price valuation either via a direct API call or by redirecting the user
to their AI tool of choice (Gemini / ChatGPT).

The prompt template is loaded from Prompt.txt next to the app and its
variables ({ladder_status}, {hardcore_status}, {platform},
{trading_platforms}, {timeframe}) are filled from the user's selections
in the Price Check tab.
"""
import base64
import io
import json
import sys
import threading
import urllib.error
import urllib.request

import cv2
import webbrowser

from PIL import Image

from config import resource_path, load_config, save_config

# Set to True to log raw API request/response bodies to stdout / error_log
DEBUG_API = False

def _log_api(label, body):
    """Print a truncated API response body for debugging API connectivity."""
    if not DEBUG_API:
        return
    preview = body[:500].replace("\\n", " ") if isinstance(body, str) else str(body)[:500]
    print(f"[pricecheck] {label} response: {preview}...")

PROMPT_FILE = "Prompt.txt"

DEFAULT_AI_TOOLS = [
    # (key, label, url, new_chat_keybind)
    ("gemini", "Gemini", "https://gemini.google.com", "ctrl+shift+o"),
    ("chatgpt", "ChatGPT", "https://chat.openai.com", "ctrl+shift+o"),
]

DEFAULT_MARKETPLACES = [
    "Traderie",
    "Diablo2.io",
    "Diablo.Trade",
]

DEFAULT_TIMEFRAMES = ["Last 3 Days", "Last 7 Days", "Last 30 Days", "Lifetime", "Custom"]

PLATFORM_OPTIONS = ["PC", "PlayStation", "Xbox", "Switch"]

LADDER_OPTIONS = ["Non-Ladder", "Ladder"]

HARDCORE_OPTIONS = ["Softcore", "Hardcore"]


def load_prompt():
    """Load the AI prompt template from Prompt.txt next to the app.

    Returns the template string, or None if the file cannot be read.
    """
    path = resource_path(PROMPT_FILE)
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except OSError as e:
        print(f"[pricecheck] Failed to load prompt file ({path}): {e}")
        return None


def fill_prompt(template, variables):
    """Replace {{variable}} placeholders in the prompt template with the
    user-selected values. Unknown placeholders are left untouched."""
    result = template
    for key, value in variables.items():
        result = result.replace("{{" + key + "}}", str(value))
    return result


def _image_to_base64(pil_img):
    """Convert a PIL image to a base64-encoded PNG data-URL string."""
    buf = io.BytesIO()
    pil_img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("utf-8")


def image_to_clipboard(pil_image):
    """Copy a PIL image to the Windows clipboard (DIB format)."""
    try:
        import win32clipboard
        import win32con

        output = io.BytesIO()
        pil_image.convert("RGB").save(output, format="BMP")
        data = output.getvalue()[14:]  # strip BMP file header
        output.close()
        win32clipboard.OpenClipboard()
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardData(win32con.CF_DIB, data)
        win32clipboard.CloseClipboard()
        return True
    except Exception as e:
        print(f"[pricecheck] Image clipboard copy failed: {e}")
        return False


def text_to_clipboard(text):
    """Copy plain text to the Windows clipboard."""
    try:
        import win32clipboard
        import win32con

        win32clipboard.OpenClipboard()
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardData(win32con.CF_UNICODETEXT, text)
        win32clipboard.CloseClipboard()
        return True
    except Exception as e:
        print(f"[pricecheck] Text clipboard copy failed: {e}")
        return False


def capture_tooltip():
    """Capture the hovered item tooltip using the YOLO detector.

    Returns (pil_image, None) on success, or (None, error_message) on
    failure. The returned image is a PIL RGB image.
    """
    from capture import capture_tooltip_yolo, game_window_found, last_yolo_capture

    if not game_window_found():
        return None, "Diablo II window not found - open the game first."

    img, _conf = capture_tooltip_yolo()
    if img is None:
        why = last_yolo_capture.get("reason", "no tooltip detected")
        return None, (
            f"No tooltip detected ({why}) - hover an item in game and try again."
        )

    # Convert BGR numpy array -> PIL RGB image
    rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
    return Image.fromarray(rgb), None


class PriceChecker:
    """Orchestrates a price check: capture tooltip, fill prompt, then either
    call the AI API directly or redirect the user to their AI tool."""

    def __init__(self, cfg):
        self.cfg = cfg
        pc = cfg.setdefault("price_check", {})
        pc.setdefault("mode", "redirect")
        pc.setdefault("ai_tool", "gemini")
        pc.setdefault("api_key", "")
        pc.setdefault("platform", "PC")
        pc.setdefault("ladder_status", "Ladder")
        pc.setdefault("hardcore_status", "Softcore")
        pc.setdefault("marketplaces", list(DEFAULT_MARKETPLACES))
        pc.setdefault("timeframe", "Last 7 Days")
        pc.setdefault("custom_timeframe_days", 7)
        pc.setdefault("custom_ai_tools", [])
        pc.setdefault("new_chat_each_time", True)

    def get_config(self):
        return self.cfg.setdefault("price_check", {})

    def save(self):
        save_config(self.cfg)

    # ------------------------------------------------------------------ #
    # Public entry point
    # ------------------------------------------------------------------ #
    def run(self, mode, ai_tool, api_key, variables, on_result):
        """Start a price check on a background thread.

        on_result(success: bool, message: str) is called on completion.
        """
        threading.Thread(
            target=self._run_thread,
            args=(mode, ai_tool, api_key, variables, on_result),
            daemon=True,
        ).start()

    # ------------------------------------------------------------------ #
    # Internal
    # ------------------------------------------------------------------ #
    def _run_thread(self, mode, ai_tool, api_key, variables, on_result):
        template = load_prompt()
        if template is None:
            on_result(False, "Failed to load prompt template. Check Prompt.txt exists.")
            return

        filled_prompt = fill_prompt(template, variables)

        # Always capture the tooltip image first
        pil_img, err = capture_tooltip()
        if err:
            on_result(False, err)
            return

        # Copy the screenshot to the clipboard so the user can paste it
        # into any AI tool manually if needed.
        image_to_clipboard(pil_img)

        if mode == "redirect":
            self._run_redirect(ai_tool, filled_prompt, pil_img, on_result)
        else:
            self._run_api(ai_tool, api_key, filled_prompt, pil_img, on_result)

    def _run_redirect(self, ai_tool, filled_prompt, pil_img, on_result):
        """Open the AI tool in the browser and paste the filled prompt
        + tooltip screenshot into it.

        If the AI tool tab is already open in an existing browser window:
          - If new_chat_each_time is True: press the tool's new-chat keybind
            (e.g. Ctrl+Shift+O) to start a fresh conversation, then paste.
          - If new_chat_each_time is False: paste into the current chat.

        Otherwise open a new tab and fully automate paste + submit.
        """
        url = self._ai_tool_url(ai_tool)
        new_chat_kb_str = self._ai_tool_new_chat_keybind(ai_tool)
        new_chat_kb = self._parse_keybind(new_chat_kb_str) if new_chat_kb_str else None

        from hotkeys import focus_browser_window
        import ctypes
        import time

        user32 = ctypes.windll.user32
        VK_CONTROL, VK_V = 0x11, 0x56
        VK_RETURN = 0x0D
        KEYUP = 0x0002

        def _press(key_down, key_up=None):
            """Press and release a single key."""
            user32.keybd_event(key_down, 0, 0, 0)
            time.sleep(0.05)
            user32.keybd_event(key_down, 0, KEYUP, 0)
            time.sleep(0.05)

        def _press_combo(*keys):
            """Press and release a key combination (e.g. Ctrl+Shift+O)."""
            for k in keys:
                user32.keybd_event(k, 0, 0, 0)
                time.sleep(0.05)
            for k in reversed(keys):
                user32.keybd_event(k, 0, KEYUP, 0)
                time.sleep(0.05)

        def _paste_clipboard():
            """Paste clipboard contents (Ctrl+V)."""
            _press_combo(VK_CONTROL, VK_V)

        def _submit():
            """Press Enter to submit the pasted content."""
            _press(VK_RETURN)

        # Read user preference for new-chat behavior
        new_chat_each_time = self.cfg.setdefault("price_check", {}).get(
            "new_chat_each_time", True
        )

        # Always copy the prompt to clipboard first
        text_to_clipboard(filled_prompt)

        # Try to focus an already-open AI tool tab first
        already_open = focus_browser_window(url)

        if already_open:
            # Give the window a moment to settle as foreground
            time.sleep(0.3)
            if new_chat_each_time and new_chat_kb:
                # Wait for the AI tool page to finish loading before starting
                # a new chat, so Ctrl+Shift+O lands on a ready input box.
                self._wait_for_page_ready()
                # Start a new chat in the existing tab
                _press_combo(*new_chat_kb)
                # Give the new chat UI a moment to appear
                time.sleep(2.0)
            # If new_chat_each_time is False, we paste into the current chat
        else:
            # No existing window found - open a new tab
            webbrowser.open(url)
            # Give the browser a moment to open the new tab, then focus it
            time.sleep(2.0)
            focused = focus_browser_window(url)
            if focused:
                time.sleep(0.3)
            # New tab opens on the homepage - start a fresh chat so the
            # conversation doesn't get named after the item
            if new_chat_each_time and new_chat_kb:
                # Wait for the browser to actually open/load the AI tool tab
                # before sending the new-chat keybind.
                self._wait_for_page_ready()
                _press_combo(*new_chat_kb)
                time.sleep(2.0)

        # Paste the prompt text (Ctrl+V)
        _paste_clipboard()
        time.sleep(0.5)

        # Paste the screenshot image (Ctrl+V) - both prompt and image
        # are now in the input box waiting to be submitted together
        time.sleep(1.0)
        image_to_clipboard(pil_img)
        time.sleep(0.5)
        _paste_clipboard()

        # Wait for the image to finish uploading before submitting
        time.sleep(3.0)
        _submit()

        preview = filled_prompt.strip()
        if len(preview) > 200:
            preview = preview[:200] + "..."
        if already_open and new_chat_each_time:
            msg = (
                f"Focused existing {ai_tool.capitalize()} tab, started a new "
                f"chat ({new_chat_kb_str}), and pasted the price check prompt "
                f"+ screenshot (submitted).\n\n--- Prompt ---\n{preview}"
            )
        elif already_open:
            msg = (
                f"Focused existing {ai_tool.capitalize()} tab and pasted into "
                f"the current chat.\n\n--- Prompt ---\n{preview}"
            )
        else:
            if new_chat_each_time:
                msg = (
                    f"Opened {ai_tool.capitalize()} in a new tab, started a "
                    f"new chat ({new_chat_kb_str}), and pasted the price "
                    f"check prompt + screenshot (submitted).\n\n"
                    f"--- Prompt ---\n{preview}"
                )
            else:
                msg = (
                    f"Opened {ai_tool.capitalize()} in a new tab and pasted "
                    f"the price check prompt + screenshot (submitted).\n\n"
                    f"--- Prompt ---\n{preview}"
                )
        on_result(True, msg)

    @staticmethod
    def _wait_for_page_ready(max_wait=8.0, stable_seconds=0.4):
        """Wait until the foreground (browser) window shows a stable,
        non-empty page title, indicating the AI tool page has finished
        loading and its input/new-chat handler is ready.

        Returns True when the title stabilized, False on timeout.
        """
        import ctypes
        import time

        user32 = ctypes.windll.user32

        def fg_title():
            hwnd = user32.GetForegroundWindow()
            if not hwnd:
                return ""
            buf = ctypes.create_unicode_buffer(512)
            user32.GetWindowTextW(hwnd, buf, 512)
            return buf.value.strip()

        t0 = time.time()
        last_title, stable_since = None, None
        while time.time() - t0 < max_wait:
            title = fg_title()
            if title and title == last_title:
                if stable_since is None:
                    stable_since = time.time()
                if time.time() - stable_since >= stable_seconds:
                    return True
            else:
                last_title, stable_since = title, None
            time.sleep(0.1)
        return False

    def _run_api(self, ai_tool, api_key, filled_prompt, pil_img, on_result):
        """Call the selected AI API with the filled prompt + tooltip image.

        Works for any AI tool: built-in (gemini, chatgpt) or custom.
        The provider is detected from the API key format (sk-* = OpenAI,
        anything else = Gemini), independent of the AI tool selected for
        redirect mode. The model comes from the AI model dropdown.
        """
        if not api_key or not api_key.strip():
            on_result(False, "API request timed out. Please check your connection or API key.")
            return

        api_key = api_key.strip()

        try:
            # Custom AI tool - use its configured URL
            if ai_tool and ai_tool.startswith("custom:"):
                url = self._ai_tool_url(ai_tool)
                ok, text = self._call_custom(
                    ai_tool, url, api_key, filled_prompt, pil_img
                )
            # Detect provider from API key format, not redirect AI tool selection
            elif api_key.startswith("sk-"):
                # OpenAI-compatible API (sk-proj-*, sk-*)
                ok, text = self._call_openai(api_key, filled_prompt, pil_img)
            elif api_key.startswith("nvapi-"):
                # NVIDIA NIM API (OpenAI-compatible, Bearer auth)
                ok, text = self._call_nvidia(api_key, filled_prompt, pil_img)
            else:
                # Gemini API (query param key)
                ok, text = self._call_gemini(api_key, filled_prompt, pil_img)
            on_result(ok, text)
        except Exception as e:  # noqa: BLE001
            on_result(False, "API request timed out. Please check your connection or API key.")

    # ------------------------------------------------------------------ #
    # AI API clients (stdlib urllib only - no new dependencies)
    # ------------------------------------------------------------------ #
    def get_ai_tool_options(self):
        """Return list of (key, label, url, new_chat_keybind) for all AI
        tools including customs."""
        opts = list(DEFAULT_AI_TOOLS)
        for c in self.cfg.setdefault("price_check", {}).get("custom_ai_tools", []):
            key = "custom:" + (c.get("name", "").strip().lower())
            label = c.get("name", "Custom")
            url = c.get("url", "")
            keybind = c.get("new_chat_keybind", "ctrl+shift+o")
            opts.append((key, label, url, keybind))
        opts.append(("custom:new", "Add custom AI tool...", "", ""))
        return opts

    def _ai_tool_url(self, ai_tool):
        for key, _label, url, _kb in DEFAULT_AI_TOOLS:
            if key == ai_tool:
                return url
        # Check custom AI tools
        for c in self.cfg.setdefault("price_check", {}).get("custom_ai_tools", []):
            if "custom:" + (c.get("name", "").strip().lower()) == ai_tool:
                return c.get("url", "")
        return DEFAULT_AI_TOOLS[0][2]

    def _ai_tool_new_chat_keybind(self, ai_tool):
        """Return the new-chat keybind string for the given AI tool
        (e.g. 'ctrl+shift+o'), or None if unknown."""
        for key, _label, _url, kb in DEFAULT_AI_TOOLS:
            if key == ai_tool:
                return kb
        for c in self.cfg.setdefault("price_check", {}).get("custom_ai_tools", []):
            if "custom:" + (c.get("name", "").strip().lower()) == ai_tool:
                return c.get("new_chat_keybind", "ctrl+shift+o")
        return None

    @staticmethod
    def _parse_keybind(keybind_str):
        """Parse a keybind string like 'ctrl+shift+o' into a tuple of
        virtual key codes. Returns None if unparseable."""
        if not keybind_str:
            return None
        parts = [p.strip().lower() for p in keybind_str.split("+")]
        vk_map = {
            "ctrl": 0x11, "control": 0x11,
            "shift": 0x10,
            "alt": 0x12,
            "cmd": 0x5B, "win": 0x5B, "super": 0x5B,
        }
        keys = []
        for p in parts:
            if p in vk_map:
                keys.append(vk_map[p])
            elif len(p) == 1 and p.isalpha():
                keys.append(ord(p.upper()))
            elif len(p) == 1 and p.isdigit():
                keys.append(ord(p))
            else:
                return None
        return tuple(keys)

    def save_custom_ai_tool(self, name, url, new_chat_keybind="ctrl+shift+o"):
        """Save or update a custom AI tool. Returns its key."""
        name = (name or "").strip()
        url = (url or "").strip()
        new_chat_keybind = (new_chat_keybind or "ctrl+shift+o").strip()
        if not name:
            raise ValueError("Give the AI tool a name.")
        if not url:
            raise ValueError("Enter a URL for the AI tool.")
        if not url.lower().startswith(("http://", "https://")):
            url = "https://" + url
        pc = self.cfg.setdefault("price_check", {})
        customs = pc.setdefault("custom_ai_tools", [])
        for c in customs:
            if (c.get("name", "").strip().lower() == name.lower()):
                c["name"], c["url"], c["new_chat_keybind"] = name, url, new_chat_keybind
                self.save()
                return "custom:" + name.lower()
        customs.append({"name": name, "url": url, "new_chat_keybind": new_chat_keybind})
        self.save()
        return "custom:" + name.lower()

    def _call_gemini(self, api_key, prompt, pil_img):
        """Call the Gemini API (generativelanguage.googleapis.com)."""
        img_b64 = _image_to_base64(pil_img)
        payload = {
            "contents": [
                {
                    "parts": [
                        {"text": prompt},
                        {
                            "inline_data": {
                                "mime_type": "image/png",
                                "data": img_b64,
                            }
                        },
                    ]
                }
            ]
        }
        model = self.cfg.setdefault("price_check", {}).get("ai_model", "")

        if not model:
            return False, "Model error: Invalid response format. Please try a different model."

        def _try_call(model_name):
            url = (
                "https://generativelanguage.googleapis.com/v1/models/"
                + model_name + ":generateContent?key=" + api_key
            )
            data = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(
                url, data=data, headers={"Content-Type": "application/json"}
            )
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    body = resp.read().decode("utf-8")
                _log_api("_call_gemini", body)
            except urllib.error.HTTPError as e:
                body = e.read().decode("utf-8")
                _log_api("_call_gemini", body)
                return e.code, body
            return None, body

        code, body = _try_call(model)

        if code is not None:
            return False, "API request timed out. Please check your connection or API key."

        result = json.loads(body)
        try:
            text = result["candidates"][0]["content"]["parts"][0]["text"]
            return True, text
        except (KeyError, IndexError, TypeError):
            return True, body  # return raw if we can't parse the expected shape

    def _call_openai(self, api_key, prompt, pil_img):
        """Call the OpenAI API using the model selected in the dropdown."""
        img_b64 = _image_to_base64(pil_img)
        model = self.cfg.setdefault("price_check", {}).get("ai_model", "gpt-4o")
        payload = {
            "model": model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/png;base64,{img_b64}"
                            },
                        },
                    ],
                }
            ],
            "max_tokens": 1000,
        }
        url = "https://api.openai.com/v1/chat/completions"
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer " + api_key,
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                body = resp.read().decode("utf-8")
                _log_api("_call_openai", body)
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8")
            _log_api("_call_openai", body)
            return False, "API request timed out. Please check your connection or API key."
        result = json.loads(body)
        try:
            text = result["choices"][0]["message"]["content"]
            return True, text
        except (KeyError, IndexError, TypeError):
            return True, body

    def _call_nvidia(self, api_key, prompt, pil_img):
        """Call the NVIDIA NIM API using the model selected in the dropdown.

        NVIDIA NIM is OpenAI-compatible; only the base URL differs.
        """
        img_b64 = _image_to_base64(pil_img)
        model = self.cfg.setdefault("price_check", {}).get("ai_model", "")
        payload = {
            "model": model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/png;base64,{img_b64}"
                            },
                        },
                    ],
                }
            ],
            "max_tokens": 1000,
        }
        url = "https://integrate.api.nvidia.com/v1/chat/completions"
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer " + api_key,
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                body = resp.read().decode("utf-8")
                _log_api("_call_nvidia", body)
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8")
            _log_api("_call_nvidia", body)
            return False, "API request timed out. Please check your connection or API key."
        result = json.loads(body)
        try:
            text = result["choices"][0]["message"]["content"]
            return True, text
        except (KeyError, IndexError, TypeError):
            return True, body

    def _call_custom(self, tool_key, url, api_key, prompt, pil_img):
        """Call a custom AI tool API with the filled prompt + tooltip image.

        Uses a generic OpenAI-compatible request format:
          POST <url>
          Headers: Authorization: Bearer <api_key>, Content-Type: application/json
          Body:  {"model": <model>, "messages": [{"role": "user", "content": [...] }]}

        Works with any AI tool that exposes an OpenAI-compatible chat API.
        """
        img_b64 = _image_to_base64(pil_img)
        model = self.cfg.setdefault("price_check", {}).get("ai_model", "")

        if not model:
            return False, "Model error: Invalid response format. Please try a different model."

        payload = {
            "model": model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/png;base64,{img_b64}"
                            },
                        },
                    ],
                }
            ],
            "max_tokens": 1000,
        }
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer " + api_key,
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                body = resp.read().decode("utf-8")
                _log_api("_call_custom", body)
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8")
            _log_api("_call_custom", body)
            return False, "API request timed out. Please check your connection or API key."
        except Exception as e:
            return False, "API request timed out. Please check your connection or API key."
        result = json.loads(body)
        try:
            # Try OpenAI-compatible response format first
            text = result["choices"][0]["message"]["content"]
            return True, text
        except (KeyError, IndexError, TypeError):
            # Fall back to Gemini-compatible format
            try:
                text = result["candidates"][0]["content"]["parts"][0]["text"]
                return True, text
            except (KeyError, IndexError, TypeError):
                return True, body