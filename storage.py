"""Tiny JSON-backed state store (no DB needed)."""

import asyncio
import json
import logging
import time
from typing import Any, Dict, List, Optional

import config

log = logging.getLogger(__name__)

_DEFAULT: Dict[str, Any] = {
    # channels the account is admin in -> link pool
    # {"id": -100.., "title": str, "username": str|None, "link": str,
    #  "offline": bool, "strikes": int, "reason": str?}
    "channels": [],
    # the channel where the redirect post lives
    # {"chat_id": int, "message_id": int, "slots": int}
    "post": None,
    # a ".set" seen while the account was not admin yet
    # {"chat_id": int, "slots": int}
    "pending_set": None,
    # channel shown on every line of the post (None = first online)
    "active_id": None,
    # True while the bot waits for the owner to hand over fresh links
    "awaiting_links": False,
    "last_check": 0,
}


class Storage:
    def __init__(self, path=config.DATA_FILE):
        self.path = path
        self._lock = asyncio.Lock()
        self.data: Dict[str, Any] = json.loads(json.dumps(_DEFAULT))
        self._read()

    # ------------------------------------------------------------- disk
    def _read(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text("utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            log.error("data.json unreadable (%s) - starting fresh", exc)
            return
        if isinstance(raw, dict):
            self.data.update(raw)

    def _write(self) -> None:
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.data, indent=2, ensure_ascii=False), "utf-8")
        tmp.replace(self.path)

    async def save(self) -> None:
        async with self._lock:
            await asyncio.to_thread(self._write)

    # --------------------------------------------------------- channels
    @property
    def channels(self) -> List[Dict[str, Any]]:
        return self.data["channels"]

    def get_channel(self, chat_id: int) -> Optional[Dict[str, Any]]:
        return next((c for c in self.channels if c["id"] == chat_id), None)

    def add_channel(self, chat_id: int, title: str, username: Optional[str], link: str) -> bool:
        """Returns True when newly added, False when it was only refreshed."""
        existing = self.get_channel(chat_id)
        if existing:
            existing.update(title=title, username=username, link=link)
            self.bring_online(chat_id)
            return False
        self.channels.append(
            {"id": chat_id, "title": title, "username": username, "link": link,
             "offline": False, "strikes": 0}
        )
        return True

    def remove_channel(self, chat_id: int) -> None:
        self.data["channels"] = [c for c in self.channels if c["id"] != chat_id]
        if self.data.get("active_id") == chat_id:
            self.data["active_id"] = None

    # --------------------------------------------- online / offline
    # A channel is never deleted just because a check failed: it is parked as
    # "offline", kept in the file and re-checked every round, so a wrong
    # verdict repairs itself and the pool can't be burned through.
    def online(self) -> List[Dict[str, Any]]:
        return [c for c in self.channels if not c.get("offline")]

    def offline(self) -> List[Dict[str, Any]]:
        return [c for c in self.channels if c.get("offline")]

    def park_offline(self, chat_id: int, reason: str) -> None:
        channel = self.get_channel(chat_id)
        if channel is None:
            return
        channel["offline"] = True
        channel["reason"] = reason
        channel.setdefault("offline_since", int(time.time()))
        if self.data.get("active_id") == chat_id:
            self.data["active_id"] = None

    def bring_online(self, chat_id: int) -> None:
        channel = self.get_channel(chat_id)
        if channel is None:
            return
        channel["offline"] = False
        channel["strikes"] = 0
        channel.pop("offline_since", None)
        channel.pop("reason", None)

    def active_channel(self) -> Optional[Dict[str, Any]]:
        """The channel whose link fills the post."""
        active = self.get_channel(self.data.get("active_id") or 0)
        if active and not active.get("offline"):
            return active
        online = self.online()
        if online:
            self.data["active_id"] = online[0]["id"]
            return online[0]
        return None

    # ------------------------------------------------------------- post
    @property
    def post(self) -> Optional[Dict[str, Any]]:
        return self.data["post"]

    def set_post(self, chat_id: int, message_id: int, slots: int) -> None:
        self.data["post"] = {"chat_id": chat_id, "message_id": message_id, "slots": slots}
        self.data["pending_set"] = None

    def clear_post(self) -> None:
        self.data["post"] = None

    def post_slots(self) -> int:
        post = self.data.get("post") or {}
        return int(post.get("slots") or config.POST_SLOTS)

    # ------------------------------------------------------ pending .set
    @property
    def pending_set(self) -> Optional[Dict[str, Any]]:
        return self.data.get("pending_set")

    @pending_set.setter
    def pending_set(self, value: Optional[Dict[str, Any]]) -> None:
        self.data["pending_set"] = value

    # ------------------------------------------------------------ flags
    @property
    def awaiting_links(self) -> bool:
        return bool(self.data.get("awaiting_links"))

    @awaiting_links.setter
    def awaiting_links(self, value: bool) -> None:
        self.data["awaiting_links"] = bool(value)


store = Storage()
