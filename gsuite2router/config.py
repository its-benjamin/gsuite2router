"""Configuration constants, timing profiles, browser detection, and config file management."""

import os
import sys
import json

# ============================================================
# DEFAULTS
# ============================================================
DEFAULT_ROUTER_URL = "http://localhost:20128"
DEFAULT_ROUTER_PASSWORD = "123456"
DEFAULT_REDIRECT_URI = "http://localhost:20128/callback"
DEFAULT_AKUN_FILE = "akun.txt"
DEFAULT_DELAY = 3
DEFAULT_PROVIDERS = ["antigravity"]
DEFAULT_PROVIDER = "antigravity"
# ============================================================
# CONFIG FILE
# ============================================================
CONFIG_FILE = ".gs2router.json"


def get_config_path():
    """Get config file path in CWD."""
    return os.path.join(os.getcwd(), CONFIG_FILE)


def load_config():
    """Load config from file. Returns dict or empty dict if not found."""
    path = get_config_path()
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return {}


def save_config(config):
    """Save config to file."""
    path = get_config_path()
    with open(path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)
    print(f"Config saved to {path}")


def get_config_value(key, cli_value, default):
    """Get value: CLI arg > config file > default."""
    if cli_value is not None:
        return cli_value
    config = load_config()
    if key in config:
        return config[key]
    return default


# ============================================================
# TIMING PROFILES
# ============================================================
# Event-driven waits (URL-change / element-appear polls) do the real
# synchronization; these are just small human-like settles after actions.
TIMING = {
    "fast": {
        "google_initial": 0.4,
        "after_email_input": 0.2,
        "after_email_next": 0.4,
        "password_timeout": 10,
        "after_pw_input": 0.2,
        "after_pw_next": 0.4,
        "step_loop_wait": 0.4,
        "tos_button_timeout": 3,
        "after_tos_click": 1.5,
        "btn_find_timeout": 1,
        "after_consent_btn": 0.5,
        "after_allow": 0.6,
        "no_btn_wait": 0.4,
        "redirect_wait": 0.8,
        "after_success": 0.2,
    },
    "normal": {
        "google_initial": 0.8,
        "after_email_input": 0.4,
        "after_email_next": 0.8,
        "password_timeout": 12,
        "after_pw_input": 0.4,
        "after_pw_next": 0.8,
        "step_loop_wait": 0.5,
        "tos_button_timeout": 3,
        "after_tos_click": 2.0,
        "btn_find_timeout": 1.5,
        "after_consent_btn": 0.8,
        "after_allow": 1.0,
        "no_btn_wait": 0.8,
        "redirect_wait": 1.2,
        "after_success": 0.5,
    },
}

# ============================================================
# BROWSER DETECTION PATHS
# ============================================================
BROWSER_CANDIDATES = []

if sys.platform == "win32":
    local_app_data = os.environ.get("LOCALAPPDATA", "")
    BROWSER_CANDIDATES = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.join(local_app_data, r"Google\Chrome\Application\chrome.exe"),
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        os.path.join(local_app_data, r"BraveSoftware\Brave-Browser\Application\brave.exe"),
        r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe",
    ]
elif sys.platform == "darwin":
    BROWSER_CANDIDATES = [
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
    ]
else:
    BROWSER_CANDIDATES = [
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
        "/usr/bin/chromium-browser",
        "/usr/bin/chromium",
    ]

# ============================================================
# CHROME LAUNCH ARGS
# ============================================================
# NOTE: "--disable-blink-features=AutomationControlled" is intentionally NOT
# here — Chrome shows an "unsupported command-line flag" warning for it.
# Anti-detection is injected via CDP in google_auth.py instead.
# The flags below cut background work (faster OAuth) without changing the
# automation fingerprint.
CHROME_ARGS = [
    "--start-maximized",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-sync",
    "--disable-translate",
    "--disable-infobars",
    "--disable-features=SignInProfileCreation,SyncPromoAfterSignin,SigninPromo,ChromeWhatsNewUI",
    "--signin-process",
    "--no-service-autorun",
    "--disable-dev-shm-usage",
    "--disable-hang-monitor",
    "--disable-client-side-phishing-detection",
    "--disable-domain-reliability",
    "--disable-background-timer-throttling",
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
    "--lang=en-US",
]

CHROME_IGNORE_DEFAULT_ARGS = ["--enable-automation"]
