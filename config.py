"""Settings, read once from .env (written by setup.py)."""

import os
from pathlib import Path

from dotenv import load_dotenv

HERE = Path(__file__).resolve().parent
load_dotenv(HERE / ".env")


def _int(key, default):
    try:
        return int((os.getenv(key) or "").strip())
    except ValueError:
        return default


def _bool(key, default):
    raw = (os.getenv(key) or "").strip().lower()
    if raw in ("1", "true", "yes", "y", "on"):
        return True
    if raw in ("0", "false", "no", "n", "off"):
        return False
    return default


# --- the account (https://my.telegram.org/apps) ----------------------------
API_ID = _int("API_ID", 0)
API_HASH = (os.getenv("API_HASH") or "").strip()
SESSION = (os.getenv("SESSION_NAME") or "saviour").strip()

# Your main account: every notification goes here and only it may command.
OWNER_ID = _int("OWNER_ID", 0)

# --- the redirect post -----------------------------------------------------
SLOTS = _int("POST_SLOTS", 5)                 # link lines in the post
TITLE = (os.getenv("POST_TITLE") or "none").strip()   # "none" = no title line

# --- timing ----------------------------------------------------------------
CHECK_EVERY = _int("CHECK_EVERY", 900)        # seconds between "." tests (15 min)
SYNC_EVERY = _int("SYNC_EVERY", 900)          # seconds between dialog scans
GAP = max(_int("GAP", 3), 1)                  # seconds between two channels
MAX_WAIT = max(_int("MAX_WAIT", 300), 10)     # longest flood-wait to sit out

# --- safety ----------------------------------------------------------------
# Failed rounds in a row before a channel leaves the post. One bad answer is
# never enough; anything unclear doesn't even count as a failure.
STRIKES = max(_int("STRIKES", 3), 1)

# If the redirect post gets deleted, put a fresh one up automatically.
REPOST = _bool("REPOST_IF_DELETED", True)

DATA = HERE / "data.json"
LOG = HERE / "saviour.log"
