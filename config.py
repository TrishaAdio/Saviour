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

# Seconds to wait for Telegram before a request counts as timed out.
HTTP_TIMEOUT = _int("HTTP_TIMEOUT", 20)

# Optional user account used to verify links (see /login). If these are set
# here, /login only asks for the phone number and code.
API_ID = _int("API_ID", 0)
API_HASH = (os.getenv("API_HASH") or "").strip()

# Only monitor once a user account is connected. The web check alone produces
# false alarms, which burn through the link pool, so it never declares a
# channel dead - it can only confirm one is alive.
REQUIRE_USERBOT = _bool("REQUIRE_USERBOT", True)

# How many checks in a row, each CHECK_INTERVAL apart, must agree a channel is
# dead before it is parked offline. One bad answer is never enough.
DEATH_STRIKES = max(_int("DEATH_STRIKES", 3), 1)

# Minimum seconds between two requests made by the user account. Keeps the
# account well inside Telegram's limits.
MIN_REQUEST_GAP = max(_int("MIN_REQUEST_GAP", 3), 1)

# Premium icons (tgmacicons pack) in messages and on buttons. They need the bot
# owner to have Telegram Premium; if Telegram doesn't show them the bot turns
# them off by itself. /icons on|off changes this at runtime.
CUSTOM_EMOJI = _bool("CUSTOM_EMOJI", True)

# Optional bold line above the links, used only for a plain ".set" (a reply
# ".set" copies the original post exactly). "none" or empty = no title line,
# which matches a post that is just the quote box of links.
POST_TITLE = os.getenv("POST_TITLE", "none")

# Collapsible quote box instead of a plain one (plain .set only).
EXPANDABLE_QUOTE = _bool("EXPANDABLE_QUOTE", False)

# "single" -> every line shows the active channel's link (like your screenshot),
#             the rest of the pool sits in reserve for when it dies.
# "spread"  -> lines are filled round-robin across all loaded channels.
LINK_MODE = (os.getenv("LINK_MODE") or "single").strip().lower()

# Minimum healthy channels before the bot starts begging the owner for links.
MIN_CHANNELS = _int("MIN_CHANNELS", 1)

# Auto-add any channel the bot is promoted to admin in (no /load needed).
AUTO_ADD = _bool("AUTO_ADD", True)

# If the redirect post gets deleted: send a brand new one (true), or just
# forget it and ask the owner to post ".set" again (false).
REPOST_IF_DELETED = _bool("REPOST_IF_DELETED", False)

DATA_FILE = BASE_DIR / "data.json"
LOG_FILE = BASE_DIR / "bot.log"
