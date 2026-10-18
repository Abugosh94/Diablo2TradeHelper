"""Configuration loader for the D2 Inventory Scanner.

Creates a default config.json next to this file on first run.
"""
import json
import os
import sys

# Flag to keep child processes (tesseract, cmd) from flashing console
# windows in a windowless (PyInstaller --noconsole) app.
CREATE_NO_WINDOW = 0x08000000


def app_dir():
    """Directory where the app stores its data (config, output, captures).

    CRITICAL for the PyInstaller onefile exe: __file__ points into a
    TEMPORARY extraction folder (_MEIxxxx) that is DELETED when the app
    closes - anything written there vanishes. For frozen builds we must
    use the exe's own folder instead.
    """
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


# Config lives NEXT TO the app (exe folder when frozen), never inside the
# _MEI temp dir - see app_dir() above.
CONFIG_PATH = os.path.join(app_dir(), "config.json")


def resource_path(relative_path):
    """Get the absolute path to a resource, working for both source and
    PyInstaller onefile exe. In a frozen exe, data files bundled with
    --add-data live under sys._MEIPASS; otherwise they're next to the
    script/exe."""
    if getattr(sys, "frozen", False):
        base = getattr(sys, "_MEIPASS", None)
        if base and os.path.exists(os.path.join(base, relative_path)):
            return os.path.join(base, relative_path)
        return os.path.join(os.path.dirname(sys.executable), relative_path)
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), relative_path)

DEFAULT_CONFIG = {
    "capture_window_title": "Diablo II",  # capture only from the selected app window (Diablo II by default)
    "hotkeys": {
        "quick_list_item": "f9",
        "manual_crop_item": "f10",
        "price_check_item": "f8",
        "quit": "ctrl+alt+q",
    },
    "browser": "default",
    "trading_site": {
        "site": "diablo2io",
        "custom_sites": [],
        "sites": {
            "diablo2io": "https://diablo2.io/posting.php?f=16&mode=post",
            "diablotrade": "https://d2r.diablo.trade/marketplace/create"
        }
    },
    "scan": {
        "save_captures": True,
        "captures_dir": "captures",
    },
    "manual_screenshot_override": False,  # quick-list always opens manual region selection
    "paste_image_max_kb": 250,  # max size of the clipboard PNG pasted to trade sites
    "minimize_to_tray": True,  # GUI: close button hides to system tray
    "close_behavior": "exit",  # 'exit' | 'minimize'
    "close_behavior_prompted": False,  # first-close: ask minimize vs exit
    "start_with_windows": False,  # register in HKCU Run key
    "regions": {
        "_comment": "Pixel regions as normalized 0..1 fractions of the game window. Tune for your resolution.",
        "tooltip": {"x": 0.30, "y": 0.10, "w": 0.40, "h": 0.50},
    },
    "price_check": {
        "mode": "redirect",  # 'api' or 'redirect'
        "ai_tool": "gemini",  # 'gemini', 'chatgpt', or 'custom:<name>'
        "api_key": "",
        "platform": "PC",
        "ladder_status": "Ladder",
        "hardcore_status": "Softcore",
        "marketplaces": ["Traderie", "Diablo2.io", "Diablo.Trade"],
        "timeframe": "Last 7 Days",
        "custom_timeframe_days": 7,
        "new_chat_each_time": True,  # True = new chat each price check, False = same chat
        "custom_ai_tools": [],
    },
    "theme": "system",  # 'system' | 'light' | 'dark' - defaults to system default
}
TRADING_SITE = DEFAULT_CONFIG["trading_site"]


def load_config():
    if not os.path.exists(CONFIG_PATH):
        save_config(DEFAULT_CONFIG)
        print(f"[config] Created default config at {CONFIG_PATH}")
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f"[config] Failed to read config ({e}); using defaults.", file=sys.stderr)
        cfg = dict(DEFAULT_CONFIG)
    # Shallow-merge missing keys from defaults
    merged = dict(DEFAULT_CONFIG)
    for k, v in cfg.items():
        if isinstance(v, dict) and isinstance(merged.get(k), dict):
            merged[k] = {**merged[k], **v}
        else:
            merged[k] = v
    # Migrate old configs: diablo2io.posting_url -> trading_site.sites.diablo2io
    if "diablo2io" in cfg and "trading_site" not in cfg:
        old_url = (cfg.get("diablo2io") or {}).get("posting_url", "")
        if old_url:
            merged["trading_site"] = dict(DEFAULT_CONFIG["trading_site"])
            merged["trading_site"]["sites"] = dict(DEFAULT_CONFIG["trading_site"]["sites"])
            merged["trading_site"]["sites"]["diablo2io"] = old_url
            merged["trading_site"]["site"] = "diablo2io"
    # Migrate to the marketplace-only scheme (v0.03): rebuild the sites map
    # (drop lootlens/lootgoblin/runewizard, ensure diablo.trade), convert the
    # legacy single custom_url into a named custom site.
    ts = merged.get("trading_site") or {}
    defaults = DEFAULT_CONFIG["trading_site"]["sites"]
    user_sites = ts.get("sites") or {}
    # keep only user overrides that still exist as built-ins (legacy
    # diablo2io posting_url survives); everything else follows defaults
    sites = dict(defaults)
    for k, v in user_sites.items():
        if k in defaults and isinstance(v, str) and v.strip():
            sites[k] = v
    ts["sites"] = sites
    valid = set(defaults)
    customs = ts.get("custom_sites") or []
    custom_keys = {"custom:" + (c.get("name") or "").strip().lower()
                   for c in customs if isinstance(c, dict) and c.get("name")}
    legacy_url = (ts.get("custom_url") or "").strip()
    if legacy_url:
        ts.pop("custom_url", None)
        if legacy_url not in sites.values():  # else it's now a built-in
            customs = list(customs) + [{"name": "My custom site", "url": legacy_url}]
            ts["custom_sites"] = customs
            custom_keys.add("custom:my custom site")
    # prune custom entries whose URL is now a built-in marketplace
    customs = [c for c in ts.get("custom_sites") or []
               if (c.get("url") or "").strip() not in sites.values()]
    ts["custom_sites"] = customs
    key = (ts.get("site") or "diablo2io").lower()
    if key not in valid and key not in custom_keys:
        ts["site"] = "diablo2io"  # old lootlens/lootgoblin/runewizard/custom
    merged["trading_site"] = ts
    return merged


def get_trading_url(cfg=None):
    """Resolve the posting URL for the user's selected trading site.

    Built-in keys resolve from trading_site.sites; 'custom:<name>' keys
    resolve from trading_site.custom_sites (user-named entries). Anything
    unknown falls back to Diablo2.io. Returns (site_key, url)."""
    cfg = cfg or load_config()
    ts = cfg.get("trading_site") or {}
    sites = ts.get("sites") or DEFAULT_CONFIG["trading_site"]["sites"]
    customs = {c.get("name", "").strip().lower(): (c.get("url") or "").strip()
               for c in (ts.get("custom_sites") or []) if c.get("name")}
    key = (ts.get("site") or "diablo2io").lower()
    if key.startswith("custom:"):
        url = customs.get(key[len("custom:"):], "")
        if url and not url.lower().startswith(("http://", "https://")):
            url = "https://" + url
        return key, url or sites["diablo2io"]
    return key, sites.get(key, sites["diablo2io"])


def trading_site_options(cfg=None):
    """Dropdown options as (key, label) pairs: built-ins first, then the
    user's saved custom sites, then a 'New custom site...' entry."""
    cfg = cfg or load_config()
    ts = cfg.get("trading_site") or {}
    opts = list(TRADING_SITE_LABELS)
    for c in ts.get("custom_sites") or []:
        name = (c.get("name") or "").strip()
        if name:
            opts.append(("custom:" + name.lower(), name))
    opts.append(("custom:new", "New custom site..."))
    return opts


def add_custom_site(cfg, name, url):
    """Save (or update) a named custom site. Returns its key."""
    name = (name or "").strip()
    url = (url or "").strip()
    if not name:
        raise ValueError("Give the custom site a name.")
    if not url:
        raise ValueError("Enter a URL for the custom site.")
    if not url.lower().startswith(("http://", "https://")):
        url = "https://" + url
    ts = cfg.setdefault("trading_site", {})
    customs = ts.setdefault("custom_sites", [])
    for c in customs:
        if (c.get("name") or "").strip().lower() == name.lower():
            c["name"], c["url"] = name, url
            break
    else:
        customs.append({"name": name, "url": url})
    return "custom:" + name.lower()


TRADING_SITE_LABELS = [
    ("diablo2io", "Diablo2.io"),
    ("diablotrade", "Diablo.Trade"),
]


def save_config(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
