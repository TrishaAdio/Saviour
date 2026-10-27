"""Everything the bot remembers, in one JSON file.

The golden rule lives here: a channel is **never deleted** because a check
failed. It is parked "offline", kept in the file and re-checked every round,
so a wrong verdict repairs itself instead of costing you a channel.
"""

import asyncio
import json
import logging
import time

import config

log = logging.getLogger(__name__)

EMPTY = {
    "channels": [],      # [{id,title,username,link,offline,strikes,reason,last_bio}]
    "post": None,        # {"chat": -100.., "msg": 123, "slots": 5}
    "waiting_set": None,  # {"chat": -100.., "slots": 5}  -> ".set" seen, no rights yet
    "active": None,      # id of the channel shown in the post
    "begging": False,    # already asked the owner for new links
    "checked": 0,        # unix time of the last finished round
}


class State:
    def __init__(self, path=config.DATA):
        self.path = path
        self._lock = None    # made on first use, inside the running loop
        self.d = json.loads(json.dumps(EMPTY))
        if path.exists():
            try:
                saved = json.loads(path.read_text("utf-8"))
                if isinstance(saved, dict):
                    self.d.update(saved)
            except (json.JSONDecodeError, OSError) as exc:
                log.error("data.json unreadable (%s) - starting fresh", exc)

    async def save(self):
        # Created here, not in __init__: on Python 3.8/3.9 a Lock made at
        # import time is tied to a different loop than asyncio.run's.
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            # run_in_executor rather than asyncio.to_thread: to_thread only
            # exists from Python 3.9, and setup.py accepts 3.8.
            await asyncio.get_running_loop().run_in_executor(None, self._write)

    def _write(self):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.d, indent=2, ensure_ascii=False), "utf-8")
        tmp.replace(self.path)   # atomic: a crash can't leave half a file

    # ----------------------------------------------------------- channels
    @property
    def channels(self):
        return self.d["channels"]

    def get(self, chat_id):
        return next((c for c in self.channels if c["id"] == chat_id), None)

    def add(self, chat_id, title, username, link):
        """True if this is a new channel, False if it was already known."""
        known = self.get(chat_id)
        if known:
            known.update(title=title, username=username, link=link)
            self.revive(chat_id)
            return False
        self.channels.append({"id": chat_id, "title": title, "username": username,
                              "link": link, "offline": False, "strikes": 0})
        return True

    def drop(self, chat_id):
        self.d["channels"] = [c for c in self.channels if c["id"] != chat_id]
        if self.d["active"] == chat_id:
            self.d["active"] = None

    def live(self):
        return [c for c in self.channels if not c.get("offline")]

    def dead(self):
        return [c for c in self.channels if c.get("offline")]

    def park(self, chat_id, reason):
        """Out of the post, but still in the file and still re-checked."""
        c = self.get(chat_id)
        if not c:
            return
        c["offline"] = True
        c["reason"] = reason
        c.setdefault("offline_since", int(time.time()))
        if self.d["active"] == chat_id:
            self.d["active"] = None

    def revive(self, chat_id):
        c = self.get(chat_id)
        if not c:
            return
        c["offline"] = False
        c["strikes"] = 0
        c.pop("reason", None)
        c.pop("offline_since", None)

    def active_channel(self):
        """The channel whose link fills the post right now."""
        chosen = self.get(self.d.get("active") or 0)
        if chosen and not chosen.get("offline"):
            return chosen
        live = self.live()
        if not live:
            return None
        self.d["active"] = live[0]["id"]
        return live[0]

    # --------------------------------------------------------------- post
    @property
    def post(self):
        return self.d["post"]

    def set_post(self, chat_id, msg_id, slots):
        self.d["post"] = {"chat": chat_id, "msg": msg_id, "slots": slots}
        self.d["waiting_set"] = None

    def forget_post(self):
        self.d["post"] = None

    def slots(self):
        return int((self.d.get("post") or {}).get("slots") or config.SLOTS)

    @property
    def waiting_set(self):
        return self.d["waiting_set"]

    @waiting_set.setter
    def waiting_set(self, value):
        self.d["waiting_set"] = value

    def is_post_channel(self, chat_id):
        """The redirect channel must never be one of its own targets."""
        for key in ("post", "waiting_set"):
            entry = self.d.get(key)
            if entry and entry["chat"] == chat_id:
                return True
        return False


state = State()
