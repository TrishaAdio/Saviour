"""Tiny JSON-backed state store (no DB needed)."""

import asyncio
import json
import logging
from typing import Any, Dict, List, Optional

import config

log = logging.getLogger(__name__)

_DEFAULT: Dict[str, Any] = {
    # verified channels the bot is admin in -> link pool
    # {"id": -100.., "title": str, "username": str|None, "link": str}
    "channels": [],
    # the ".set" channel where the redirect post lives
    # {"chat_id": int, "message_id": int, "slots": int}
    "post": None,
    "verified": False,
    # True while the bot waits for the owner to hand over fresh links
    "awaiting_links": False,
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
            return False
        self.channels.append(
            {"id": chat_id, "title": title, "username": username, "link": link}
        )
        return True

    def remove_channel(self, chat_id: int) -> None:
        self.data["channels"] = [c for c in self.channels if c["id"] != chat_id]

    # ------------------------------------------------------------- post
    @property
    def post(self) -> Optional[Dict[str, Any]]:
        return self.data["post"]

    def set_post(self, chat_id: int, message_id: int, slots: int,
                 template: Optional[Dict[str, Any]] = None) -> None:
        self.data["post"] = {"chat_id": chat_id, "message_id": message_id, "slots": slots}
        if template:
            # snapshot of the original post's text + entities (see template.py)
            self.data["post"]["template"] = template

    def clear_post(self) -> None:
        self.data["post"] = None

    # ------------------------------------------------------------ flags
    @property
    def verified(self) -> bool:
        return bool(self.data["verified"])

    @verified.setter
    def verified(self, value: bool) -> None:
        self.data["verified"] = bool(value)

    @property
    def awaiting_links(self) -> bool:
        return bool(self.data["awaiting_links"])

    @awaiting_links.setter
    def awaiting_links(self, value: bool) -> None:
        self.data["awaiting_links"] = bool(value)


store = Storage()
