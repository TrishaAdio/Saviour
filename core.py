"""Channel resolving, invite-link handling and redirect-post rendering."""

import html
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from telegram import Bot, Message
from telegram.constants import ChatMemberStatus, ParseMode
from telegram.error import BadRequest, Forbidden, NetworkError, RetryAfter, TimedOut

import config
from storage import store

log = logging.getLogger(__name__)

TRANSIENT = (TimedOut, NetworkError, RetryAfter)

# @name | t.me/name | https://t.me/name | -1001234567890 | t.me/+hash | t.me/joinchat/hash
_USERNAME_RE = re.compile(r"(?:https?://)?(?:t(?:elegram)?\.me|telegram\.dog)/([A-Za-z]\w{3,31})/?")
_PRIVATE_RE = re.compile(r"(?:https?://)?(?:t(?:elegram)?\.me|telegram\.dog)/(?:\+|joinchat/)([\w-]+)/?")
_AT_RE = re.compile(r"(?<![\w/])@([A-Za-z]\w{3,31})")
_ID_RE = re.compile(r"(?<![\w.])(-100\d{5,})(?![\w.])")


# --------------------------------------------------------------- parsing
def parse_targets(text: str) -> Tuple[List[str], List[str]]:
    """Split a blob of text into resolvable targets and unresolvable invite links.

    The Bot API cannot turn a private ``t.me/+hash`` invite into a chat id, so
    those are reported back to the owner with instructions instead.
    """
    text = text or ""
    targets: List[str] = []
    unresolvable: List[str] = []

    for match in _PRIVATE_RE.finditer(text):
        unresolvable.append(match.group(0))
    text_wo_private = _PRIVATE_RE.sub(" ", text)

    for match in _ID_RE.finditer(text_wo_private):
        targets.append(match.group(1))
    for match in _USERNAME_RE.finditer(text_wo_private):
        targets.append("@" + match.group(1))
    for match in _AT_RE.finditer(text_wo_private):
        targets.append("@" + match.group(1))

    seen, deduped = set(), []
    for item in targets:
        key = item.lower()
        if key not in seen:
            seen.add(key)
            deduped.append(item)
    return deduped, unresolvable


def forwarded_channel_id(message: Message) -> Optional[int]:
    """Channel id when the owner forwards a post from the channel instead."""
    origin = getattr(message, "forward_origin", None)
    chat = getattr(origin, "chat", None)
    if chat is not None and getattr(chat, "type", "") == "channel":
        return chat.id
    return None


# ------------------------------------------------------- channel checks
async def bot_is_admin(bot: Bot, chat_id: Any) -> bool:
    member = await bot.get_chat_member(chat_id, bot.id)
    return member.status == ChatMemberStatus.ADMINISTRATOR


async def fetch_invite_link(bot: Bot, chat_id: int, title: str) -> Optional[str]:
    """Prefer a public @username link, otherwise mint a fresh invite link."""
    try:
        chat = await bot.get_chat(chat_id)
        if chat.username:
            return f"https://t.me/{chat.username}"
        link = await bot.create_chat_invite_link(chat_id, name=title[:32] or "redirect")
        return link.invite_link
    except (BadRequest, Forbidden) as exc:
        log.warning("invite link failed for %s: %s", chat_id, exc)
        return None


async def verify_target(bot: Bot, target: Any) -> Dict[str, Any]:
    """Resolve a target and confirm the bot is an admin there."""
    result: Dict[str, Any] = {"target": str(target), "ok": False, "reason": "", "chat": None}
    try:
        chat_id: Any = int(target) if str(target).lstrip("-").isdigit() else target
        chat = await bot.get_chat(chat_id)
    except (BadRequest, Forbidden) as exc:
        result["reason"] = f"not reachable ({exc.message if hasattr(exc, 'message') else exc})"
        return result

    if chat.type != "channel":
        result["reason"] = f"this is a {chat.type}, not a channel"
        return result

    try:
        if not await bot_is_admin(bot, chat.id):
            result["reason"] = "bot is not an admin there"
            return result
    except (BadRequest, Forbidden) as exc:
        result["reason"] = f"admin check failed ({exc})"
        return result

    link = await fetch_invite_link(bot, chat.id, chat.title or "")
    if not link:
        result["reason"] = "cannot create invite link (need 'Invite Users via Link' right)"
        return result

    result.update(ok=True, chat=chat, link=link)
    return result


async def is_channel_alive(bot: Bot, channel: Dict[str, Any]) -> bool:
    """False only on a definite failure - transient network errors keep it alive."""
    try:
        await bot.get_chat(channel["id"])
        return await bot_is_admin(bot, channel["id"])
    except (BadRequest, Forbidden) as exc:
        log.info("channel %s looks dead: %s", channel["id"], exc)
        return False
    except TRANSIENT as exc:
        log.warning("transient error checking %s: %s - keeping it", channel["id"], exc)
        return True


# ------------------------------------------------------- post rendering
def assign_slots(slots: int) -> List[str]:
    """Fill every slot from the pool so N lines always render.

    ``single`` mode repeats the active channel's link on every line (the rest
    of the pool is held in reserve); ``spread`` mode rotates through the pool.
    """
    links = [c["link"] for c in store.channels if c.get("link")]
    if not links:
        return []
    slots = max(slots, 1)
    if config.LINK_MODE == "spread":
        return [links[i % len(links)] for i in range(slots)]
    return [links[0]] * slots


def render_post(links: List[str]) -> str:
    """Exact formatting of the post - reused for edits so nothing visually shifts."""
    body = "\n".join(html.escape(link) for link in links)
    tag = "blockquote expandable" if config.EXPANDABLE_QUOTE else "blockquote"
    return (
        f"<b>{html.escape(config.POST_TITLE)}</b>\n"
        f"<{tag}>{body}</blockquote>"
    )


async def publish_post(bot: Bot, chat_id: int, slots: int) -> Optional[Message]:
    links = assign_slots(slots)
    if not links:
        return None
    message = await bot.send_message(
        chat_id,
        render_post(links),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )
    store.set_post(chat_id, message.message_id, slots)
    await store.save()
    return message


async def refresh_post(bot: Bot) -> str:
    """Re-render the existing post in place. Returns a short status string."""
    post = store.post
    if not post:
        return "no-post"

    links = assign_slots(post.get("slots", config.POST_SLOTS))
    if not links:
        return "no-links"

    try:
        await bot.edit_message_text(
            chat_id=post["chat_id"],
            message_id=post["message_id"],
            text=render_post(links),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
        return "edited"
    except BadRequest as exc:
        msg = (exc.message or "").lower()
        if "not modified" in msg:
            return "unchanged"
        if "message to edit not found" in msg or "message can't be edited" in msg:
            log.warning("redirect post gone (%s) - reposting", exc)
            store.clear_post()
            await store.save()
            new = await publish_post(bot, post["chat_id"], post.get("slots", config.POST_SLOTS))
            return "reposted" if new else "no-links"
        log.error("edit failed: %s", exc)
        return f"error: {exc.message}"
    except Forbidden as exc:
        log.error("lost access to the .set channel: %s", exc)
        return f"error: {exc}"
