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
import random
import re

from telethon import functions, types
from telethon.errors import (
    ChannelInvalidError,
    ChannelPrivateError,
    ChatAboutNotModifiedError,
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
DEAD = "dead"          # Telegram is definite, and the probes agree
UNSURE = "unsure"      # no idea - change nothing
GONE_FOR_SURE = "gone-for-sure"  # the dot test failed: out of the post right now

# Random channel bios. One is written only when a channel is about to fill
# the post (test_takeover) - never in normal rounds.
BIOS = [
    "🔥 Fresh content every day. Join before the link changes.",
    "⚡ The main channel moves often — stay close.",
    "🎬 New uploads daily. Don't lose the link.",
    "🚀 Back soon with more. Invite your friends.",
    "💎 Quality over quantity. Always.",
    "📌 Save this channel, links rotate regularly.",
    "🎁 Daily drops. Stay tuned.",
    "🔔 Turn notifications on, posts move fast.",
    "🌟 The good stuff is always here first.",
    "🎯 One channel, everything you need.",
    "🆕 Updated today. Check the pinned post.",
    "📢 Official backup. Main channel rotates.",
    "🔒 Private drops for members only.",
    "🎥 Watch it here before it's gone.",
    "💬 Requests open. Ask in the comments.",
    "🧲 If a link stops working, we always come back.",
    "🏆 The most reliable source, day after day.",
    "🎉 Thanks for being here. More coming tonight.",
    "🌍 Members from everywhere. All welcome.",
    "🛡 This channel is protected and always restored.",
    "📅 New posts every evening.",
    "🎧 Relax and scroll. We do the finding for you.",
    "🍿 Grab a seat, the next drop is close.",
    "✨ Small channel, big content.",
    "🔁 Links refresh automatically — never panic.",
    "🥇 Ranked #1 by our members.",
    "🧭 Lost? This channel always points you home.",
    "🌙 Late-night drops are the best drops.",
    "☀️ Good morning. New content is already up.",
    "📀 The archive keeps growing.",
]

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


# ---------------------------------------------------------------- the bio
async def write_bio(channel, entity):
    """Put a random line in the channel bio (only from test_takeover)."""
    pool = [b for b in BIOS if b != channel.get("last_bio")] or BIOS
    text = random.choice(pool)[:255]
    try:
        await call(lambda: tg(functions.messages.EditChatAboutRequest(entity, text)),
                   "write bio")
    except ChatAboutNotModifiedError:
        return True, ""               # already there: we can still write
    except ChatAdminRequiredError:
        return False, 'missing the "Change Info" right'
    except (RPCError, *HICCUP) as exc:
        return False, brief(exc)
    channel["last_bio"] = text
    return True, ""


async def rename_test(entity, title):
    """Add a "." to the name and take it straight back off. (ok, why_not)."""
    name = (title or getattr(entity, "title", "") or "")[:250]
    if not name:
        return False, "no name to test with"
    try:
        await call(lambda: tg(functions.channels.EditTitleRequest(entity, name + ".")),
                   "rename test")
    except ChatAdminRequiredError:
        return False, 'missing the "Change Info" right'
    except (RPCError, *HICCUP) as exc:
        return False, brief(exc)
    await asyncio.sleep(1)
    try:
        await call(lambda: tg(functions.channels.EditTitleRequest(entity, name)),
                   "rename test undo")
    except (RPCError, *HICCUP) as exc:
        log.error("could not restore the name of %s - it may still end in a dot: %s",
                  peer(entity), brief(exc))
    return True, ""


async def test_takeover(channel):
    """Test a channel *before* it fills the post - only ever then.

    Runs when the channel in the post was deleted and this one is next in
    line: write a new bio and add/remove a "." in its name. Either working
    proves we can act in it. If both fail, the "." post test decides.

    Returns (usable, verdict, note). verdict is GONE_FOR_SURE when it is
    gone too, so the caller parks it and moves on to the next one.
    """
    try:
        entity = await entity_of(channel["id"])
    except (*GONE, ValueError):
        try:
            entity = await tg.get_input_entity(channel["id"])
        except ValueError:
            entity = None
    except (RPCError, *HICCUP) as exc:
        return True, UNSURE, f"couldn't test it ({brief(exc)}) - using it anyway"

    bio_ok, bio_why = (await write_bio(channel, entity)) if entity is not None \
        else (False, "can't reach it")
    name_ok, name_why = (await rename_test(entity, channel.get("title"))) \
        if entity is not None else (False, "can't reach it")
    if bio_ok or name_ok:
        done = " and ".join(w for w, ok in (("bio changed", bio_ok),
                                            ("name changed", name_ok)) if ok)
        return True, OK, done
    verdict, how = await _confirm(entity, f"bio ({bio_why}) and name ({name_why}) "
                                          "changes both failed")
    if verdict == GONE_FOR_SURE:
        return False, verdict, how
    if verdict == UNSURE:
        return True, UNSURE, how
    return True, OK, f"bio and name changes failed, but {how}"


# ------------------------------------------------------------ the dot test
ALIVE, GONE_NOW, NO_ANSWER = "alive", "gone", "no-answer"


async def dot_test(target):
    """The one test that decides whether a channel is gone.

    Post a "." in it. If that works, delete it again at once: the channel is
    alive. If Telegram refuses the post - for any reason - the channel is
    treated as deleted. Only a failure to reach Telegram at all (our own
    connection) proves nothing, and comes back as NO_ANSWER.

    Returns (result, what_happened).
    """
    if target is None:
        return GONE_NOW, "I can't reach it any more"
    try:
        sent = await call(lambda: tg.send_message(target, ".", silent=True), "dot test")
    except HICCUP as exc:
        return NO_ANSWER, f"Telegram didn't answer the test post ({brief(exc)})"
    except (RPCError, ValueError) as exc:
        return GONE_NOW, f"a test \".\" post was refused ({brief(exc)})"
    try:
        await call(lambda: tg.delete_messages(target, [sent.id]), "dot test undo")
    except (RPCError, *HICCUP) as exc:
        log.error("left a test dot in %s: %s", peer(target) if hasattr(target, "id")
                  else target, brief(exc))
    return ALIVE, "a test \".\" post went through (deleted again at once)"


async def _confirm(target, problem):
    """A check just failed: settle it on the spot with the dot test."""
    result, how = await dot_test(target)
    if result == GONE_NOW:
        return GONE_FOR_SURE, f"{problem} - and {how}"
    if result == NO_ANSWER:
        return UNSURE, f"{problem}, but {how}"
    return None, how                     # alive: the caller decides what's next


# --------------------------------------------------------------- one check
async def check(channel):
    """Check one channel. Returns (verdict, note)."""
    cid = channel["id"]

    # 1. Can we still open it?
    try:
        entity = await entity_of(cid)
    except (*GONE, ValueError):
        # Telegram says the same thing for "deleted", "you were banned" and,
        # now and then, wrongly for a healthy channel. The dot test settles it,
        # through the peer saved in our session.
        try:
            target = await tg.get_input_entity(cid)
        except ValueError:
            target = None
        verdict, how = await _confirm(target, "Telegram refused to open it")
        if verdict:
            return verdict, how
        return UNSURE, f"false alarm: Telegram refused it, but {how}"
    except (RPCError, *HICCUP) as exc:
        return UNSURE, f"Telegram did not answer ({brief(exc)})"

    # 2. Are we still admin? (Without that we can't keep its link alive.)
    try:
        rights = await rights_in(entity)
    except GONE:
        rights = None
    except (RPCError, *HICCUP) as exc:
        return UNSURE, f"could not read our rights ({brief(exc)})"
    if rights is None or not getattr(rights, "is_admin", False):
        verdict, how = await _confirm(entity, "I'm not admin there any more")
        if verdict:
            return verdict, how
        return UNSURE, f"Telegram says I'm not admin, but {how}"

    # Nothing is written to the channel in a normal round: constant bio and
    # name edits on healthy channels are what gets them flagged. Those are
    # saved for test_takeover(), when a channel is about to fill the post.

    # 3. Does the invite link still work?
    works = await link_works(channel.get("link") or "")
    if works is True:
        return OK, ""
    if works is None:
        return UNSURE, "the link check was inconclusive"

    # The link is dead but the channel and our rights are fine: the invite
    # was revoked. Make a new one rather than give up a working channel.
    if not getattr(rights, "invite_users", False):
        return DEAD, 'its link died and we lack the "Invite Users via Link" right'
    fixed, detail = await rotate_link(channel, entity)
    if fixed:
        return RELINKED, "its old link had stopped working"
    verdict, how = await _confirm(entity, f"its link died ({detail})")
    if verdict:
        return verdict, how
    return UNSURE, f"its link died; {detail}"


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
