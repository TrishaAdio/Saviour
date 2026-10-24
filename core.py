"""Channel resolving, invite-link handling and redirect-post rendering."""

import asyncio
import html
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from telegram import Bot, Message
from telegram.constants import ChatMemberStatus, ParseMode
from telegram.error import BadRequest, Forbidden, NetworkError, RetryAfter, TimedOut

import config
import template
from storage import store

log = logging.getLogger(__name__)

TRANSIENT = (TimedOut, NetworkError, RetryAfter)

RETRY_ATTEMPTS = 3
RETRY_BACKOFF = (2, 5)  # seconds to wait after attempt 1 and attempt 2


def is_transient(exc: BaseException) -> bool:
    """Network hiccup or flood limit - worth retrying. Real rejections are not.

    BadRequest subclasses NetworkError in python-telegram-bot, so it has to be
    ruled out explicitly or every rejection would look like a network error.
    """
    if isinstance(exc, (BadRequest, Forbidden)):
        return False
    return isinstance(exc, TRANSIENT)


async def with_retry(call, *args, what: str = "telegram call", **kwargs):
    """Run ``await call(*args, **kwargs)``, retrying network hiccups.

    Timeouts and connection drops are retried with a short backoff; flood
    limits wait the time Telegram asks for. Anything else (BadRequest,
    Forbidden...) is raised immediately. After the last attempt the transient
    error is raised so the caller can decide what to do.
    """
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            return await call(*args, **kwargs)
        except Exception as exc:
            if not is_transient(exc) or attempt == RETRY_ATTEMPTS:
                raise
            if isinstance(exc, RetryAfter):
                wait = float(getattr(exc, "retry_after", 5) or 5) + 1
            else:
                wait = RETRY_BACKOFF[min(attempt - 1, len(RETRY_BACKOFF) - 1)]
            log.warning("%s: %s (attempt %s/%s) - retrying in %ss",
                        what, type(exc).__name__, attempt, RETRY_ATTEMPTS, wait)
            await asyncio.sleep(wait)

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
        chat = await with_retry(bot.get_chat, chat_id, what=f"get_chat {chat_id}")
        if chat.username:
            return f"https://t.me/{chat.username}"
        link = await with_retry(bot.create_chat_invite_link, chat_id,
                                name=title[:32] or "redirect", what="create invite link")
        return link.invite_link
    except (BadRequest, Forbidden) as exc:  # must come first: BadRequest is a NetworkError
        log.warning("invite link failed for %s: %s", chat_id, exc)
        return None
    except TRANSIENT as exc:
        log.warning("invite link for %s still timing out after retries: %s", chat_id, exc)
        return None


# Right the bot absolutely needs to serve links from a channel.
LINK_RIGHT = ("can_invite_users", "Invite Users via Link")

# Extra rights needed before a channel can host the ".set" redirect post.
POST_RIGHTS = (
    ("can_post_messages", "Post Messages"),
    ("can_edit_messages", "Edit Messages"),
    ("can_delete_messages", "Delete Messages"),
)


def rights_report(member: Any) -> Dict[str, Any]:
    """What can this admin membership actually do?"""
    attr, label = LINK_RIGHT
    can_link = bool(getattr(member, attr, False))
    missing_post = [name for key, name in POST_RIGHTS if not getattr(member, key, False)]
    return {
        "can_link": can_link,
        "missing_link": [] if can_link else [label],
        "missing_post": missing_post,
        "can_host_post": not missing_post,
        "full": can_link and not missing_post,
    }


async def register_channel(bot: Bot, chat: Any, title: str) -> Dict[str, Any]:
    """Mint a link and drop the channel into the pool. Returns a status dict."""
    link = await fetch_invite_link(bot, chat.id, title)
    if not link:
        return {"ok": False, "reason": "could not create an invite link"}
    is_new = store.add_channel(chat.id, title or str(chat.id), chat.username, link)
    store.verified = True
    if store.channels:
        store.awaiting_links = False
    await store.save()
    return {"ok": True, "new": is_new, "link": link}


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
        await with_retry(bot.get_chat, channel["id"], what=f"get_chat {channel['id']}")
        return await with_retry(bot_is_admin, bot, channel["id"],
                                what=f"admin check {channel['id']}")
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
    """Default look, used only when there is no cloned post to copy."""
    body = "\n".join(html.escape(link) for link in links)
    tag = "blockquote expandable" if config.EXPANDABLE_QUOTE else "blockquote"
    title = (config.POST_TITLE or "").strip()
    head = f"<b>{html.escape(title)}</b>\n" if title and title.lower() != "none" else ""
    return f"{head}<{tag}>{body}</blockquote>"


async def edit_post(bot: Bot, chat_id: int, message_id: int,
                    links: List[str], tpl: Optional[Dict[str, Any]] = None) -> None:
    """Write ``links`` into the post, keeping the cloned formatting if any."""
    if tpl:
        text, entities = template.render(tpl, links)
        if tpl["kind"] == "caption":
            await bot.edit_message_caption(
                chat_id=chat_id, message_id=message_id,
                caption=text, caption_entities=entities,
            )
        else:
            await bot.edit_message_text(
                chat_id=chat_id, message_id=message_id,
                text=text, entities=entities, disable_web_page_preview=True,
            )
        return
    await bot.edit_message_text(
        chat_id=chat_id, message_id=message_id, text=render_post(links),
        parse_mode=ParseMode.HTML, disable_web_page_preview=True,
    )


async def adopt_existing(bot: Bot, target: Message) -> Dict[str, Any]:
    """Take over a post you replied ``.set`` to, keeping its exact formatting.

    Its line count is whatever the post already has (5 links -> 5 slots).
    """
    tpl = template.snapshot(target)
    if tpl is None:
        return {"ok": False, "reason": "that message has no text or caption to edit"}
    slots = template.slot_count(tpl)
    if slots == 0:
        return {"ok": False, "reason": "that post has no t.me links for me to swap"}
    links = assign_slots(slots)
    if not links:
        return {"ok": False, "reason": "my link pool is empty"}
    try:
        await with_retry(edit_post, bot, target.chat_id, target.message_id, links, tpl,
                         what="adopt post")
    except BadRequest as exc:
        if "not modified" not in (exc.message or "").lower():
            raise
    store.set_post(target.chat_id, target.message_id, slots, tpl)
    await store.save()
    log.info("adopted existing post %s in %s (%s link slots, formatting cloned)",
             target.message_id, target.chat_id, slots)
    return {"ok": True, "slots": slots}


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


async def adopt_post(bot: Bot, chat_id: int, message_id: int, slots: int) -> bool:
    """Turn an existing message (your ``.set``) into the redirect post.

    Nothing new is sent - the message you typed is edited in place and becomes
    the post the bot keeps alive from then on.
    """
    links = assign_slots(slots)
    if not links:
        return False
    await with_retry(edit_post, bot, chat_id, message_id, links, what="adopt .set")
    store.set_post(chat_id, message_id, slots)
    await store.save()
    log.info("adopted message %s in %s as the redirect post (%s lines)",
             message_id, chat_id, len(links))
    return True


# Telegram's many ways of saying "that message doesn't exist any more".
POST_GONE = (
    "message to edit not found",
    "message to be edited not found",
    "message can't be edited",
    "message_id_invalid",
    "message id invalid",
    "message identifier is not specified",
)


async def refresh_post(bot: Bot) -> str:
    """Re-render the existing post in place. Returns a short status string."""
    post = store.post
    if not post:
        return "no-post"

    links = assign_slots(post.get("slots", config.POST_SLOTS))
    if not links:
        return "no-links"

    try:
        await with_retry(edit_post, bot, post["chat_id"], post["message_id"], links,
                         post.get("template"), what="post edit")
        return "edited"
    except BadRequest as exc:
        msg = (exc.message or "").lower()
        if "not modified" in msg:
            return "unchanged"
        if any(marker in msg for marker in POST_GONE):
            log.warning("redirect post %s in %s is gone (%s)",
                        post["message_id"], post["chat_id"], exc)
            chat_id, slots = post["chat_id"], post.get("slots", config.POST_SLOTS)
            store.clear_post()
            await store.save()
            if config.REPOST_IF_DELETED:
                new = await publish_post(bot, chat_id, slots)
                return "reposted" if new else "no-links"
            return "post-gone"
        log.error("edit failed: %s", exc)
        return f"error: {exc.message}"
    except Forbidden as exc:
        log.error("lost access to the .set channel: %s", exc)
        return f"error: {exc}"
    except TRANSIENT as exc:
        # Telegram didn't answer in time even after retries. The post is fine
        # as far as we know - keep it and try again on the next check.
        log.warning("post edit still failing after %s attempts (%s) - will retry "
                    "on the next check", RETRY_ATTEMPTS, type(exc).__name__)
        return "network-error"
