"""Configuration loader. Values come from .env (written by setup.py)."""

import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


def _int(key: str, default: int) -> int:
    raw = (os.getenv(key) or "").strip()
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _bool(key: str, default: bool) -> bool:
    raw = (os.getenv(key) or "").strip().lower()
    if raw in ("1", "true", "yes", "y", "on"):
        return True
    if raw in ("0", "false", "no", "n", "off"):
        return False
    return default


BOT_TOKEN = (os.getenv("BOT_TOKEN") or "").strip()
OWNER_ID = _int("OWNER_ID", 0)

# How many link lines the redirect post contains.
POST_SLOTS = _int("POST_SLOTS", 5)

# Seconds between health checks of every loaded channel.
CHECK_INTERVAL = _int("CHECK_INTERVAL", 300)

# Title line of the redirect post.
POST_TITLE = os.getenv("POST_TITLE", "Redirecting...")

# Use an expandable blockquote (the grey collapsible box in the screenshot).
EXPANDABLE_QUOTE = _bool("EXPANDABLE_QUOTE", True)

# "single" -> every line shows the active channel's link (like your screenshot),
#             the rest of the pool sits in reserve for when it dies.
# "spread"  -> lines are filled round-robin across all loaded channels.
LINK_MODE = (os.getenv("LINK_MODE") or "single").strip().lower()

# Minimum healthy channels before the bot starts begging the owner for links.
MIN_CHANNELS = _int("MIN_CHANNELS", 1)

# Sarcastic owner notifications. Set to false for dry, professional misery.
SASS = _bool("SASS", True)

# Auto-add any channel the bot is promoted to admin in (no /load needed).
AUTO_ADD = _bool("AUTO_ADD", True)

DATA_FILE = BASE_DIR / "data.json"
LOG_FILE = BASE_DIR / "bot.log"
