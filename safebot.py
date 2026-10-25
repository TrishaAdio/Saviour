"""A Bot that can never lose a message because of an icon.

Every message the bot sends or edits passes through ``send_message`` /
``edit_message_text``. If it carries custom icons and Telegram refuses it, it is
sent again without them. If Telegram accepts it but silently drops the icons
(bot owner without Premium), icons are switched off and the owner is told once.
"""

import logging
from typing import Any, Dict, Optional, Tuple

from telegram import Message
from telegram.error import BadRequest, TelegramError
from telegram.ext import ExtBot

import config
import icons
from storage import store

log = logging.getLogger(__name__)


def _find_text(name: str, args: tuple, kwargs: Dict[str, Any]) -> Tuple[Optional[str], Optional[int]]:
    """The message text and, when positional, its index in ``args``."""
    if "text" in kwargs:
        return kwargs["text"], None
    index = 1 if name == "send_message" else 0
    if len(args) > index:
        return args[index], index
    return None, None


def _without_icons(args: tuple, kwargs: Dict[str, Any], text, index):
    kwargs = dict(kwargs)
    args = list(args)
    plain = icons.strip_text(text)
    if index is None:
        if "text" in kwargs:
            kwargs["text"] = plain
    else:
        args[index] = plain
    if kwargs.get("reply_markup") is not None:
        kwargs["reply_markup"] = icons.strip_markup(kwargs["reply_markup"])
    return tuple(args), kwargs


class SafeBot(ExtBot):
    async def send_message(self, *args, **kwargs):
        return await self._guard("send_message", args, kwargs)

    async def edit_message_text(self, *args, **kwargs):
        return await self._guard("edit_message_text", args, kwargs)

    async def _guard(self, name: str, args: tuple, kwargs: Dict[str, Any]):
        call = getattr(ExtBot, name)
        text, index = _find_text(name, args, kwargs)
        if not icons.uses_icons(text, kwargs.get("reply_markup")):
            return await call(self, *args, **kwargs)

        try:
            result = await call(self, *args, **kwargs)
        except BadRequest as exc:
            if not icons.looks_icon_related(exc):
                raise
            log.warning("Telegram refused a message with icons (%s) - sending it "
                        "again without them", exc)
            args2, kwargs2 = _without_icons(args, kwargs, text, index)
            result = await call(self, *args2, **kwargs2)  # raises if still bad
            if icons.note_rejection():
                await self._announce_off()
            return result

        icons.note_ok()
        if name == "send_message" and text and "<tg-emoji" in text \
                and isinstance(result, Message):
            if icons.observe_delivery(result):
                await self._announce_off()
        return result

    async def _announce_off(self) -> None:
        """Icons were just switched off: save that and tell the owner once."""
        await store.save()
        if not config.OWNER_ID:
            return
        import ui  # late: ui imports icons
        try:
            await self.send_message(config.OWNER_ID, ui.icons_off(),
                                    parse_mode="HTML")
        except TelegramError as exc:
            log.warning("could not tell the owner icons are off: %s", exc)
