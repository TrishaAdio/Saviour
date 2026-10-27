"""Configuration loader. Values come from .env (create it from .env.example)."""

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


# --- the userbot account (from https://my.telegram.org/apps) ----------------
API_ID = _int("API_ID", 0)
API_HASH = (os.getenv("API_HASH") or "").strip()

# Your main account. All notifications and the whole control chat live in the
# DM between the userbot and this user. Only this user may give commands.
OWNER_ID = _int("OWNER_ID", 0)

# Telethon session file name (saviour.session next to this file).
SESSION_NAME = (os.getenv("SESSION_NAME") or "saviour").strip()

# How many link lines the redirect post contains.
POST_SLOTS = _int("POST_SLOTS", 5)

# Optional bold line above the links. "none" or empty = no title.
POST_TITLE = os.getenv("POST_TITLE", "none")

# Seconds between health-check rounds (each round also rewrites every bio).
CHECK_INTERVAL = _int("CHECK_INTERVAL", 300)

# Minimum seconds between two channels inside one round. Keeps the account
# well inside Telegram's limits.
REQUEST_GAP = max(_int("REQUEST_GAP", 3), 1)

# How many failed rounds in a row before a channel leaves the post. One bad
# answer is never enough.
DEATH_STRIKES = max(_int("DEATH_STRIKES", 3), 1)

# Seconds between automatic scans of the account's dialogs for channels where
# it is admin but that are not in the pool yet. 0 turns it off.
SYNC_INTERVAL = _int("SYNC_INTERVAL", 21600)

# If the redirect post itself gets deleted: post a fresh one automatically.
REPOST_IF_DELETED = _bool("REPOST_IF_DELETED", True)

DATA_FILE = BASE_DIR / "data.json"
LOG_FILE = BASE_DIR / "bot.log"
