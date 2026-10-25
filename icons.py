"""Premium icons from the tgmacicons pack, with safe fallbacks.

Two places use them:

* message text      ->  ``<tg-emoji emoji-id="ID">fallback</tg-emoji>``
* inline buttons    ->  ``icon_custom_emoji_id`` (+ optional ``style`` colour)

Both only work when the bot's owner has Telegram Premium. Everything here is
built so that a missing Premium subscription costs nothing but the icons:

* ``enabled()`` is the single switch. Off => plain emoji / text, same layout.
* ``SafeBot`` retries without icons if Telegram rejects a message, and turns
  icons off when Telegram silently drops them (see ``observe_delivery``).
* ``/icons on|off`` flips the switch at runtime; ``CUSTOM_EMOJI`` in .env sets
  the default.
"""

import logging
import re
from typing import Any, Optional, Tuple

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, MessageEntity

import config
from icon_data import ICONS
from storage import store

log = logging.getLogger(__name__)

STYLES = ("primary", "success", "danger")
BUTTON_KEYS = ("icon_custom_emoji_id", "style")

# Which icon sits in front of each label in a card row. Unlisted labels get
# DEFAULT_ROW so every row lines up the same way.
DEFAULT_ROW = "text"
ROW_ICONS = {
    "Channel": "channel", "Group": "channel", "Channels": "pool", "Pool": "pool",
    "Working": "pool", "Offline": "offline", "Not reachable": "bad",
    "ID": "pin", "Position": "pin", "Page": "page",
    "Invite": "link", "New link": "link", "Link": "link", "Links": "link",
    "Lines": "link", "Get keys": "link",
    "Post": "post", "Message": "post", "Format": "text",
    "Verify": "verify", "Link checks": "verify", "Confirmed": "good",
    "Last check": "clock", "Since": "clock", "Checks": "clock",
    "Health": "health", "Status": "info",
    "Account": "user", "Bot": "bot", "Access": "lock", "Role": "star",
    "Reason": "bulb", "Why": "bulb", "Telegram": "plane", "Showing": "eye",
    "Mode": "gear", "Current": "gear", "Default": "gear",
    "Strike": "warn", "Warning": "warn", "Risk": "warn", "Effect": "warn",
    "Missing": "warn", "Error": "crit",
}

_session_drops = 0       # consecutive messages Telegram stripped icons from
_session_rejections = 0  # consecutive messages Telegram refused over icons


# ----------------------------------------------------------- the switch
def enabled() -> bool:
    override = store.data.get("icons")
    if override is not None:
        return bool(override)
    return config.CUSTOM_EMOJI


def state_text() -> str:
    """Human description of the current state, for /icons."""
    override = store.data.get("icons")
    if enabled():
        return "On"
    if override is False and store.data.get("icons_reason"):
        return f"Off · {store.data['icons_reason']}"
    if override is False:
        return "Off · turned off with /icons off"
    return "Off · CUSTOM_EMOJI=false"


def set_override(value: Optional[bool]) -> None:
    """Persisting is the caller's job (``await store.save()``)."""
    global _session_drops, _session_rejections
    store.data["icons"] = value
    store.data.pop("icons_reason", None)
    _session_drops = _session_rejections = 0


def _switch_off(reason: str) -> bool:
    store.data["icons"] = False
    store.data["icons_reason"] = reason
    log.warning("custom icons switched off: %s", reason)
    return True


# ------------------------------------------------------------ rendering
def tg(name: str) -> str:
    """An icon for message text. Falls back to the plain character."""
    icon_id, fallback, plain, _ = ICONS[name]
    if enabled():
        return f'<tg-emoji emoji-id="{icon_id}">{fallback}</tg-emoji>'
    return plain


def row(label: str) -> str:
    """Icon + space for the start of a card row; empty when icons are off."""
    if not enabled():
        return ""
    return tg(ROW_ICONS.get(label, DEFAULT_ROW)) + " "


def button(text: str, name: Optional[str], callback_data: str,
           style: Optional[str] = None) -> InlineKeyboardButton:
    """An inline button with an icon (and optional colour) when available.

    With icons off the plain character is put in front of the label instead,
    so the button reads the same as before.
    """
    extra = {}
    label = text
    if name:
        icon_id, _, plain, _ = ICONS[name]
        if enabled():
            extra["icon_custom_emoji_id"] = icon_id
        elif plain:
            label = f"{plain} {text}"
    if style in STYLES and enabled():
        extra["style"] = style
    return InlineKeyboardButton(label, callback_data=callback_data,
                                api_kwargs=extra or None)


# ------------------------------------------------- removing the icons again
_TAG = re.compile(r'<tg-emoji emoji-id="\d+">(.*?)</tg-emoji>', re.S)


def strip_text(text: Optional[str]) -> Optional[str]:
    return _TAG.sub(r"\1", text) if text else text


def markup_has_icons(markup: Any) -> bool:
    for row_ in getattr(markup, "inline_keyboard", None) or ():
        for btn in row_:
            if any(btn.api_kwargs.get(k) for k in BUTTON_KEYS):
                return True
    return False


def uses_icons(text: Optional[str], markup: Any) -> bool:
    return bool(text and "<tg-emoji" in text) or markup_has_icons(markup)


def strip_markup(markup: Any) -> Any:
    if not markup_has_icons(markup):
        return markup
    data = markup.to_dict()
    for row_ in data["inline_keyboard"]:
        for btn in row_:
            for key in BUTTON_KEYS:
                btn.pop(key, None)
    return InlineKeyboardMarkup.de_json(data, None)


def looks_icon_related(exc: Exception) -> bool:
    msg = (getattr(exc, "message", None) or str(exc)).lower()
    return bool(re.search(r"emoji|document_invalid|entit|button|icon|style|parse", msg))


# --------------------------------------------- what Telegram actually did
def delivered(message: Any) -> bool:
    """True if Telegram kept at least one custom emoji in the sent message."""
    return any(e.type == MessageEntity.CUSTOM_EMOJI
               for e in (getattr(message, "entities", None) or ()))


def observe_delivery(message: Any) -> bool:
    """Call after sending a message that contained icons.

    Telegram does not refuse custom emoji from a bot whose owner lacks
    Premium - it just drops them and shows the fallback emoji. The reply tells
    us which happened. Two drops in a row switch icons off. Returns True when
    this call switched them off.
    """
    global _session_drops
    if delivered(message):
        _session_drops = 0
        return False
    _session_drops += 1
    log.warning("Telegram dropped the custom icons from a message (%s in a row)",
                _session_drops)
    if _session_drops >= 2:
        return _switch_off("Telegram is not showing custom icons "
                           "(the bot owner needs Telegram Premium)")
    return False


def note_rejection() -> bool:
    """A message had to be re-sent without icons. Two in a row => off."""
    global _session_rejections
    _session_rejections += 1
    if _session_rejections >= 2:
        return _switch_off("Telegram rejected messages that used icons")
    return False


def note_ok() -> None:
    global _session_rejections
    _session_rejections = 0
