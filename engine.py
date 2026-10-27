"""Everything that talks to Telegram.

The one rule this file obeys: **only Telegram, answering clearly, may call a
channel dead.** Every unclear answer - a timeout, a server error, a
flood-wait, a reply we can't read - comes back as UNSURE and changes nothing.
A false "dead" verdict is the only thing that can cost you a channel, so the
code is biased against ever producing one.
"""

import asyncio
import html
import logging
import re

from telethon import functions, types
from telethon.errors import (
    ChannelInvalidError,
    ChannelPrivateError,
    ChatAdminRequiredError,
    FloodWaitError,
    InviteHashExpiredError,
    InviteHashInvalidError,
    MessageIdInvalidError,
    MessageNotModifiedError,
    RPCError,
    ServerError,
    SlowModeWaitError,
    TimedOutError,
    UserNotParticipantError,
)

import config
import template
from state import state

log = logging.getLogger(__name__)

# Telegram hiccupping. Never evidence that a channel is gone.
HICCUP = (ServerError, TimedOutError, asyncio.TimeoutError, ConnectionError, OSError)
# Telegram being definite: the channel is gone, or we are banned from it.
GONE = (ChannelPrivateError, ChannelInvalidError)

# Verdicts from check().
OK = "ok"              # verified working
RELINKED = "relinked"  # was broken, fixed itself with a fresh link
DEAD = "dead"          # (kept for old data; check() no longer returns it)
UNSURE = "unsure"      # no idea - change nothing
GONE_FOR_SURE = "gone-for-sure"  # the "." test was refused: out of the post now

tg = None        # the TelegramClient, set by connect_client()
ME = 0           # our own user id


def bind(client, me_id):
    global tg, ME
    tg, ME = client, me_id


_SHORT = {
    "ChannelPrivateError": "the channel is private or deleted, or I was banned",
    "ChannelInvalidError": "the channel doesn't exist any more",
    "ChatWriteForbiddenError": "I'm not allowed to post there",
    "ChatAdminRequiredError": "I'm missing an admin right",
    "UserBannedInChannelError": "I'm banned from posting there",
}


def brief(exc):
    """An error as one short line, fit for a message to the owner."""
    if type(exc).__name__ in _SHORT:
        return _SHORT[type(exc).__name__]
    text = (str(exc) or type(exc).__name__).split("\n")[0]
    return re.sub(r"\s*\(caused by \w+\)$", "", text)[:160]


def peer(entity):
    """The -100… id Telegram shows for a channel."""
    raw = int(getattr(entity, "id", 0))
    return raw if raw < 0 else int(f"-100{raw}")


async def call(make, what, retries=1):
    """Run one request. ``make`` builds it fresh each attempt.

    It must be a callable, not an awaitable: a coroutine can only be awaited
    once, so a retry needs a brand new one.
    """
    tries = 0
    while True:
        try:
            return await make()
        except (FloodWaitError, SlowModeWaitError) as exc:
            wait = int(getattr(exc, "seconds", 0) or 0)
            if wait > config.MAX_WAIT:
                log.error("flood-wait of %ss on %s is too long to sit out", wait, what)
                raise
            log.warning("flood-wait %ss on %s - waiting", wait, what)
            await asyncio.sleep(wait + 1)
        except HICCUP as exc:
            if tries >= retries:
                raise
            log.warning("%s failed (%s) - one more try", what, brief(exc))
            await asyncio.sleep(3)
        tries += 1
        if tries > retries + 1:
            raise RuntimeError(f"{what}: gave up")


async def entity_of(ref):
    return await call(lambda: tg.get_entity(ref), f"open {ref}")


async def rights_in(entity):
    """Our admin rights, or None if Telegram says clearly we have none.

    Unclear answers are raised so the caller can report UNSURE rather than
    punish the channel.
    """
    try:
        return await call(lambda: tg.get_permissions(entity, "me"), "read rights")
    except (UserNotParticipantError, ChatAdminRequiredError):
        return None


def cached_rights(entity):
    """Rights Telegram already sent with the dialog list - free to read."""
    if getattr(entity, "creator", False):
        return types.ChatAdminRights(
            change_info=True, post_messages=True, edit_messages=True,
            delete_messages=True, ban_users=True, invite_users=True,
            pin_messages=True, add_admins=True)
    return getattr(entity, "admin_rights", None)


# --------------------------------------------------------------- the link
def invite_code(link):
    for marker in ("t.me/+", "t.me/joinchat/"):
        if marker in (link or ""):
            return link.split(marker, 1)[1].strip("/").split("?")[0]
    return None


async def link_works(link):
    """True = Telegram says it works, False = clearly dead, None = unclear."""
    if not link:
        return False
    code = invite_code(link)
    if code is None:                      # a public t.me/name link
        name = link.rstrip("/").split("/")[-1].lstrip("@")
        if not name:
            return None
        try:
            found = await entity_of(name)
            return isinstance(found, types.Channel)
        except GONE:
            return False
        except (ValueError, RPCError, *HICCUP) as exc:
            log.info("could not resolve %s: %s", link, brief(exc))
            return None
    try:
        answer = await call(
            lambda: tg(functions.messages.CheckChatInviteRequest(code)),
            f"check invite {code}")
        return isinstance(answer, (types.ChatInviteAlready, types.ChatInvite,
                                   types.ChatInvitePeek))
    except (InviteHashExpiredError, InviteHashInvalidError):
        return False
    except (RPCError, *HICCUP) as exc:
        log.warning("invite check of %s unclear: %s", link, brief(exc))
        return None


async def make_link(entity, replace=False):
    """Create - or with replace=True, rotate - the channel's invite link."""
    try:
        made = await call(
            lambda: tg(functions.messages.ExportChatInviteRequest(
                entity, legacy_revoke_permanent=replace)),
            f"new invite for {peer(entity)}")
    except ChatAdminRequiredError:
        return None, 'missing the "Invite Users via Link" right'
    except (RPCError, *HICCUP) as exc:
        return None, brief(exc)
    link = getattr(made, "link", None) or getattr(getattr(made, "invite", None), "link", None)
    return (link, "") if link else (None, "Telegram returned no link")


async def rotate_link(channel, entity):
    """Swap in a fresh invite link and only keep it once it checks out."""
    link, why = await make_link(entity, replace=True)
    if not link:
        return False, why
    await asyncio.sleep(2)            # give Telegram a moment to register it
    channel["link"] = link            # keep it either way: it's all we have
    await state.save()
    if await link_works(link) is True:
        return True, ""
    return False, "the fresh link has not confirmed yet"


# ------------------------------------------------------------ the dot test
ALIVE, GONE_NOW, NO_ANSWER = "alive", "gone", "no-answer"


async def dot_test(target):
    """Post a "." in the channel and delete it again at once.

    Posted -> the channel is alive. Refused, for any reason -> treated as
    deleted. Only a failure to reach Telegram at all (our own connection)
    proves nothing and comes back as NO_ANSWER.

    Returns (result, what_happened).
    """
    if target is None:
        return GONE_NOW, "I can't reach it any more"
    try:
        sent = await call(lambda: tg.send_message(target, ".", silent=True), "dot test")
    except HICCUP as exc:
        return NO_ANSWER, f"Telegram didn't answer the test post ({brief(exc)})"
    except (RPCError, ValueError) as exc:
        return GONE_NOW, f'a test "." post was refused ({brief(exc)})'
    try:
        await call(lambda: tg.delete_messages(target, [sent.id]), "dot test undo")
    except (RPCError, *HICCUP) as exc:
        log.error("left a test dot in %s: %s", target, brief(exc))
    return ALIVE, ""


async def _target(cid):
    """Something we can post to: the peer saved in the session, else a lookup."""
    try:
        return await tg.get_input_entity(cid)
    except ValueError:
        pass
    try:
        return await entity_of(cid)
    except (*GONE, ValueError):
        return None


# --------------------------------------------------------------- one check
async def check(channel):
    """One channel, one test: post a "." and delete it. Returns (verdict, note).

    Nothing else is ever changed in the channel - no bio, no name.
    """
    try:
        target = await _target(channel["id"])
    except (RPCError, *HICCUP) as exc:
        return UNSURE, f"Telegram did not answer ({brief(exc)})"

    result, how = await dot_test(target)
    if result == GONE_NOW:
        return GONE_FOR_SURE, how
    if result == NO_ANSWER:
        return UNSURE, how

    # Alive. Make sure the invite link people see still works (read-only);
    # a revoked one is replaced with a fresh, verified link.
    works = await link_works(channel.get("link") or "")
    if works is not False:
        return OK, ""
    fixed, detail = await rotate_link(channel, target)
    if fixed:
        return RELINKED, "its old invite link had stopped working"
    return UNSURE, f"its invite link died; {detail}"


# ------------------------------------------------------------ joining the pool
async def adopt(entity):
    """Check an entity over and put it in the pool. (added, why_not)."""
    if not isinstance(entity, types.Channel) or not getattr(entity, "broadcast", False):
        return False, "that is not a broadcast channel"
    cid = peer(entity)
    if state.is_post_channel(cid):
        return False, "that channel holds the redirect post"
    if state.get(cid):
        state.revive(cid)
        await state.save()
        return True, ""
    try:
        rights = await rights_in(entity)
    except (RPCError, *HICCUP) as exc:
        return False, f"could not read our rights ({brief(exc)})"
    if rights is None or not getattr(rights, "is_admin", False):
        return False, "we are not admin there"
    if not getattr(rights, "invite_users", False):
        return False, 'missing the "Invite Users via Link" right'
    link, why = await make_link(entity)
    if not link:
        return False, f"could not make an invite link ({why})"
    state.add(cid, entity.title or "channel", getattr(entity, "username", None), link)
    await state.save()
    log.info("pooled %s (%s)", entity.title, cid)
    return True, ""


async def discover():
    """Find channels we admin that aren't pooled yet.

    A userbot can simply list its own chats, and the dialog list already
    carries our admin rights - so this costs one request per 100 chats
    instead of one per channel.
    """
    out = []
    try:
        chats = [d async for d in tg.iter_dialogs()]
    except (RPCError, *HICCUP) as exc:
        log.warning("could not list chats: %s", brief(exc))
        return out
    for chat in chats:
        entity = chat.entity
        if not isinstance(entity, types.Channel) or not getattr(entity, "broadcast", False):
            continue
        if getattr(entity, "left", False):
            continue
        cid = peer(entity)
        if state.is_post_channel(cid) or state.get(cid):
            continue
        rights = cached_rights(entity)
        if rights is None:
            continue                                  # we're only a member
        if not getattr(rights, "invite_users", False):
            out.append(("needs-invite-right", entity))
            continue
        link, why = await make_link(entity)
        if not link:
            log.info("skipping %s: %s", cid, why)
            continue
        state.add(cid, entity.title or "channel",
                  getattr(entity, "username", None), link)
        out.append(("pooled", entity))
        await asyncio.sleep(config.GAP)
    await state.save()
    return out


# -------------------------------------------------------------- the post
def post_text():
    """The post body as HTML.

    HTML and not markdown, because invite codes legitimately contain "_",
    which markdown would read as italics and break every link.
    """
    lines = []
    if config.TITLE.lower() not in ("", "none"):
        lines.append(f"<b>{html.escape(config.TITLE)}</b>")
    chosen = state.active_channel()
    if chosen:
        lines += [html.escape(chosen["link"])] * state.slots()
    else:
        lines.append("⏳ Redirecting…")
    return "\n".join(lines)


def post_body(tpl=None):
    """What to send: (text, kwargs) for send_message / edit_message.

    With a template (a post taken over with a reply ".set") its own text and
    formatting are rebuilt with today's link. Without one, the default post.
    """
    chosen = state.active_channel()
    if tpl and chosen:
        text, entities = template.render(tpl, chosen["link"])
        return text, {"formatting_entities": entities,
                      "link_preview": bool(tpl.get("preview"))}
    if tpl:                    # nothing live: leave the old links standing
        text, entities = tpl["text"], [template._build(e) for e in tpl["entities"]]
        return text, {"formatting_entities": [e for e in entities if e],
                      "link_preview": bool(tpl.get("preview"))}
    return post_text(), {"parse_mode": "html"}


async def _can_post(chat_id, need_edit=False):
    """(entity, why_not)."""
    try:
        entity = await entity_of(chat_id)
    except (ValueError, RPCError, *HICCUP) as exc:
        return None, f"cannot open the channel ({brief(exc)})"
    try:
        rights = await rights_in(entity)
    except (RPCError, *HICCUP) as exc:
        return None, f"could not read our rights ({brief(exc)})"
    if rights is None or not getattr(rights, "is_admin", False):
        return None, "not an admin there yet"
    if not getattr(rights, "post_messages", False):
        return None, 'admin, but without the "Post Messages" right'
    if need_edit and not getattr(rights, "edit_messages", False):
        return None, 'admin, but without the "Edit Messages of Others" right'
    return entity, ""


async def put_post(chat_id, slots, tpl=None):
    """Publish the redirect post. (done, why_not).

    A template made from a media post can't be re-sent (we don't have the
    media), so a deleted one comes back as text with the same formatting.
    """
    entity, why = await _can_post(chat_id)
    if entity is None:
        return False, why
    text, extra = post_body(tpl)
    try:
        sent = await call(lambda: tg.send_message(entity, text, **extra),
                          "publish post")
    except (RPCError, *HICCUP) as exc:
        return False, brief(exc)
    state.set_post(chat_id, sent.id, slots, tpl)
    await state.save()
    log.info("redirect post is up in %s (message %s)", chat_id, sent.id)
    return True, ""


async def adopt_post(chat_id, msg_id):
    """Take over an existing post: keep its look, swap only its links.

    (done, why_not, slots).
    """
    entity, why = await _can_post(chat_id, need_edit=True)
    if entity is None:
        return False, why, 0
    try:
        message = await call(lambda: tg.get_messages(entity, ids=msg_id), "read post")
    except (RPCError, *HICCUP) as exc:
        return False, f"could not read that post ({brief(exc)})", 0
    if message is None:
        return False, "that post is gone", 0
    tpl = template.snapshot(message)
    if tpl is None:
        return False, "that post has no text", 0
    slots = template.slot_count(tpl)
    if not slots:
        return False, "that post has no t.me links to keep updated", 0
    state.set_post(chat_id, msg_id, slots, tpl)
    await state.save()
    log.info("took over post %s in %s (%s link slot(s))", msg_id, chat_id, slots)
    return True, "", slots


async def delete_message(chat_id, msg_id):
    try:
        await call(lambda: tg.delete_messages(chat_id, [msg_id]), "delete old post")
        return True
    except (ValueError, RPCError, *HICCUP) as exc:
        log.info("could not delete %s in %s: %s", msg_id, chat_id, brief(exc))
        return False


async def redraw_post():
    """Bring the post in line with the pool. Returns a one-word result."""
    post = state.post
    if not post:
        return "none"
    text, extra = post_body(post.get("template"))
    try:
        await call(lambda: tg.edit_message(post["chat"], post["msg"], text, **extra),
                   "edit post")
        return "updated"
    except MessageNotModifiedError:
        return "already-right"
    except GONE as exc:
        log.error("lost access to the post channel %s: %s", post["chat"], brief(exc))
        return f"error ({brief(exc)})"
    except MessageIdInvalidError:
        if not config.REPOST:
            state.forget_post()
            await state.save()
            return "post-deleted"
        done, why = await put_post(post["chat"], post["slots"], post.get("template"))
        return "reposted" if done else f"error ({why})"
    except (RPCError, *HICCUP) as exc:
        return f"error ({brief(exc)})"
