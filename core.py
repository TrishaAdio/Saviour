"""The working heart: channel pool, active liveness probes, invite links,
and the redirect post.

Everything runs through one user account, so a check is never a guess from a
web page - the account simply tries to *do* something in the channel and
watches whether Telegram lets it.

Rule that governs this whole file: **only a definite answer from Telegram may
mark a channel dead.** Anything unclear (network trouble, server error,
flood-wait, an unreadable answer) is reported as ``unknown`` and changes
nothing, because a false "dead" verdict is what burns through a link pool.
"""

import asyncio
import html
import logging
import random
import re
from typing import Any, Callable, Dict, Optional, Tuple

from telethon import TelegramClient, functions, types
from telethon.errors import (
    ChannelInvalidError,
    ChannelPrivateError,
    ChatAboutNotModifiedError,
    ChatAdminRequiredError,
    ChatNotModifiedError,
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
import texts
from storage import store

log = logging.getLogger(__name__)

INVITE_RE = re.compile(r"(?:t\.me/\+|t\.me/joinchat/)([\w-]+)")
USERNAME_RE = re.compile(r"(?:^|t\.me/)(@?[A-Za-z][A-Za-z0-9_]{3,})\s*$")

# Telegram hiccups: never evidence that a channel is gone.
TRANSIENT = (ServerError, TimedOutError, asyncio.TimeoutError, ConnectionError, OSError)
# Telegram being definite: the channel is gone, or we are banned from it.
GONE = (ChannelPrivateError, ChannelInvalidError)

# Set by main once the account is known.
client: TelegramClient = None  # type: ignore[assignment]
ME_ID: int = 0


def bind(tg_client: TelegramClient, me_id: int) -> None:
    global client, ME_ID
    client, ME_ID = tg_client, me_id


def short(exc: BaseException) -> str:
    """One-line description of an error, for owner messages."""
    text = str(exc) or type(exc).__name__
    return text.split("\n")[0][:160]


def peer_id(entity) -> int:
    """The -100… id Telegram shows for a channel entity."""
    raw = int(getattr(entity, "id", 0))
    return raw if raw < 0 else int(f"-100{raw}")


# ------------------------------------------------------------------ helpers
async def rpc(factory: Callable[[], Any], what: str, retries: int = 1):
    """Run one Telegram request, surviving flood-waits and server hiccups.

    ``factory`` must be a zero-argument callable that builds a *fresh*
    awaitable each time (``lambda: client.get_entity(x)``): a coroutine can
    only be awaited once, so a retry needs a new one.

    A flood-wait longer than ``MAX_FLOOD_WAIT`` is raised instead of slept
    off - blocking the whole bot for an hour is worse than reporting the
    round as unverified.
    """
    attempt = 0
    while True:
        try:
            return await factory()
        except (FloodWaitError, SlowModeWaitError) as exc:
            wait = int(getattr(exc, "seconds", 0) or 0)
            if wait > config.MAX_FLOOD_WAIT:
                log.error("flood-wait of %ss on %s is too long to sit out", wait, what)
                raise
            log.warning("flood-wait %ss on %s - sleeping it off", wait, what)
            await asyncio.sleep(wait + 1)
        except TRANSIENT as exc:
            if attempt >= retries:
                raise
            log.warning("%s failed (%s) - retrying once", what, short(exc))
            await asyncio.sleep(3)
        attempt += 1
        if attempt > retries + 1:  # belt and braces, never loop forever
            raise RuntimeError(f"{what}: retries exhausted")


async def get_entity(ref):
    return await rpc(lambda: client.get_entity(ref), f"get_entity {ref}")


async def my_rights(entity) -> Optional[object]:
    """My admin rights in a channel.

    ``None`` means Telegram answered clearly that I am not an admin (or not
    even a member). Unclear answers are raised, so the caller can report the
    round as unverified instead of punishing the channel.
    """
    try:
        return await rpc(lambda: client.get_permissions(entity, "me"), "permissions")
    except (UserNotParticipantError, ChatAdminRequiredError):
        return None


# ------------------------------------------------------------ invite links
async def invite_valid(link: str) -> Optional[bool]:
    """True = works, False = Telegram says dead, None = couldn't tell."""
    if not link:
        return False
    h = invite_hash(link)
    if h is None:
        # A public t.me/name link: resolve the username instead.
        match = USERNAME_RE.search(link)
        if not match:
            return None
        try:
            entity = await get_entity(match.group(1).lstrip("@"))
            return isinstance(entity, types.Channel)
        except GONE:
            return False
        except (ValueError, RPCError, *TRANSIENT) as exc:
            log.info("could not resolve %s: %s", link, short(exc))
            return None
    try:
        result = await rpc(
            lambda: client(functions.messages.CheckChatInviteRequest(h)),
            f"check invite {h}")
        # ChatInviteAlready / ChatInvite / ChatInvitePeek all mean: link valid.
        return isinstance(
            result, (types.ChatInviteAlready, types.ChatInvite, types.ChatInvitePeek))
    except (InviteHashExpiredError, InviteHashInvalidError):
        return False
    except (RPCError, *TRANSIENT) as exc:
        log.warning("invite check of %s inconclusive: %s", link, short(exc))
        return None


def invite_hash(link: str) -> Optional[str]:
    match = INVITE_RE.search(link or "")
    return match.group(1) if match else None


async def mint_invite(entity, revoke_old: bool = False) -> Tuple[Optional[str], str]:
    """Create (or, with revoke_old, replace) the channel's invite link."""
    if not hasattr(entity, "broadcast"):
        return None, "not a channel"
    try:
        result = await rpc(
            lambda: client(functions.messages.ExportChatInviteRequest(
                entity, legacy_revoke_permanent=revoke_old)),
            f"export invite {peer_id(entity)}")
    except ChatAdminRequiredError:
        return None, 'missing the "Invite Users via Link" admin right'
    except (RPCError, *TRANSIENT) as exc:
        return None, short(exc)
    link = getattr(result, "link", None) or getattr(getattr(result, "invite", None), "link", None)
    if not link:
        return None, "Telegram returned no link"
    return link, ""


# ------------------------------------------------------------- bio updates
async def update_bio(channel: Dict[str, Any], entity) -> Tuple[bool, str]:
    """Write a random line into the channel bio. This is both a feature (the
    channel always looks fresh) and the main liveness probe.

    The line is always different from the one written last round, so Telegram
    can't answer "not modified" - which used to look like a failure.
    """
    choices = [b for b in texts.BIOS if b != channel.get("last_bio")] or list(texts.BIOS)
    about = random.choice(choices)[:255]
    try:
        await rpc(lambda: client(functions.messages.EditChatAboutRequest(entity, about)),
                  "edit bio")
    except ChatAboutNotModifiedError:
        return True, ""  # the text was already there: the channel is writable
    except ChatAdminRequiredError:
        return False, 'missing the "Change Info" admin right'
    except (RPCError, *TRANSIENT) as exc:
        return False, short(exc)
    channel["last_bio"] = about
    return True, ""


# ------------------------------------------------------------- deep verify
async def deep_verify(entity) -> Tuple[bool, str]:
    """Prove a channel is alive with actions, not guesses.

    Normal checks sometimes scream "deleted" when the channel is fine, so
    before any verdict is believed the account tries, in order:

      1. read the channel's full info - needs no rights and has no side
         effects; a deleted or banned channel refuses this outright
      2. rename the channel (add a ``.``, put it back a second later)
      3. post a silent message and delete it within a second

    Any one succeeding means the channel is alive.
    """
    if entity is None:
        return False, "channel unreachable"

    # Probe 1: full info. The strongest signal, and free of side effects.
    try:
        full = await rpc(lambda: client(functions.channels.GetFullChannelRequest(entity)),
                         "full-info probe")
        if full is not None:
            return True, "full-info probe passed"
    except GONE as exc:
        log.info("full-info probe says the channel is gone: %s", short(exc))
    except (RPCError, *TRANSIENT) as exc:
        log.info("full-info probe inconclusive: %s", short(exc))

    # Probe 2: add a dot to the name and put it back.
    title = (getattr(entity, "title", "") or "")[:250]
    if title:
        try:
            await rpc(lambda: client(functions.channels.EditTitleRequest(entity, title + ".")),
                      "rename probe (add)")
            await asyncio.sleep(1)
            try:
                await rpc(lambda: client(functions.channels.EditTitleRequest(entity, title)),
                          "rename probe (revert)")
            except (RPCError, *TRANSIENT) as exc:
                # The probe already proved the channel is alive; a failed
                # revert must never hide that, but it must be shouted about.
                log.error("could not restore the title of %s - it still ends with "
                          "a dot: %s", peer_id(entity), short(exc))
            return True, "rename probe passed"
        except ChatNotModifiedError:
            return True, "rename probe passed (title already set)"
        except (RPCError, *TRANSIENT) as exc:
            log.info("rename probe failed: %s", short(exc))

    # Probe 3: post a silent message and delete it within a second.
    try:
        msg = await rpc(lambda: client.send_message(entity, ".", silent=True),
                        "post probe")
        await asyncio.sleep(1)
        try:
            await rpc(lambda: client.delete_messages(entity, [msg.id]), "delete probe")
        except (RPCError, *TRANSIENT) as exc:
            log.error("posted the probe message in %s but could not delete it: %s",
                      peer_id(entity), short(exc))
        return True, "post/delete probe passed"
    except (RPCError, *TRANSIENT) as exc:
        log.info("post/delete probe failed: %s", short(exc))

    return False, "full-info, rename and post/delete probes all failed"


# --------------------------------------------------------------- one round
async def probe(channel: Dict[str, Any]) -> Tuple[str, str]:
    """Health check of one pool channel.

    Returns (state, reason):
      ok        verified working (bio rewritten if we have the right)
      relinked  invite was dead; a fresh, verified link was saved
      suspect   looks dead *this round* - the caller counts strikes
      unknown   could not verify; change nothing
    """
    cid = channel["id"]

    # 1. Can the account still open the channel at all?
    try:
        entity = await get_entity(cid)
    except GONE as exc:
        return "suspect", f"Telegram refuses the channel ({short(exc)})"
    except ValueError as exc:
        return "suspect", f"channel not found ({short(exc)})"
    except (RPCError, *TRANSIENT) as exc:
        return "unknown", f"could not reach Telegram ({short(exc)})"

    # 2. Still admin? An unclear answer must not count against the channel.
    try:
        rights = await my_rights(entity)
    except GONE:
        rights = None
    except (RPCError, *TRANSIENT) as exc:
        return "unknown", f"could not read my rights ({short(exc)})"

    if rights is None or not getattr(rights, "is_admin", False):
        alive, detail = await deep_verify(entity)
        if alive:
            return "suspect", "admin rights lost"
        return "suspect", f"not admin and {detail}"

    # 3. Bio probe: rewrite the bio with a random line.
    if getattr(rights, "change_info", False):
        wrote, why = await update_bio(channel, entity)
        if not wrote:
            alive, detail = await deep_verify(entity)
            if not alive:
                return "suspect", f"bio update failed ({why}); {detail}"
            log.warning("bio update of %s failed but the channel is alive: %s", cid, why)

    # 4. The invite link itself.
    valid = await invite_valid(channel.get("link") or "")
    if valid is True:
        return "ok", ""
    if valid is None:
        return "unknown", "invite check inconclusive"

    # Telegram says the invite is dead but the channel and my rights are fine
    # -> the invite was revoked. Mint a fresh one and verify it before saving.
    if not getattr(rights, "invite_users", False):
        return "suspect", ('invite link dead and I lack the "Invite Users via '
                           'Link" right to make a new one')
    new_link, why = await mint_invite(entity, revoke_old=True)
    if new_link:
        await asyncio.sleep(2)  # let Telegram register the new invite
        fresh = await invite_valid(new_link)
        if fresh is True:
            channel["link"] = new_link
            await store.save()
            return "relinked", "old invite stopped working"
        # Keep the channel: we have a link, it just isn't confirmed yet.
        channel["link"] = new_link
        await store.save()
        return "unknown", "fresh invite created but not verified yet"

    alive, detail = await deep_verify(entity)
    if alive:
        return "suspect", f"invite dead and no new one could be made ({why})"
    return "suspect", f"invite dead ({why}); {detail}"


# ------------------------------------------------------------ channel pool
def hosts_post(chat_id: int) -> bool:
    """Is this the channel the redirect post lives in?"""
    post = store.post
    if post and post["chat_id"] == chat_id:
        return True
    pending = store.pending_set
    return bool(pending and pending["chat_id"] == chat_id)


async def connect_channel(entity) -> Tuple[bool, str]:
    """Verify rights, mint an invite and add the channel to the pool.

    Returns (added_or_refreshed, human_reason_on_failure).
    """
    if not isinstance(entity, types.Channel) or not getattr(entity, "broadcast", False):
        return False, "not a broadcast channel"
    chat_id = peer_id(entity)
    if hosts_post(chat_id):
        return False, "that channel hosts the redirect post"
    if store.get_channel(chat_id):
        store.bring_online(chat_id)
        await store.save()
        return True, ""

    try:
        rights = await my_rights(entity)
    except (RPCError, *TRANSIENT) as exc:
        return False, f"could not read my rights ({short(exc)})"
    if rights is None or not getattr(rights, "is_admin", False):
        return False, "the account is not admin there"
    if not getattr(rights, "invite_users", False):
        return False, 'missing the "Invite Users via Link" admin right'

    link, why = await mint_invite(entity)
    if not link:
        return False, f"could not create an invite link ({why})"

    is_new = store.add_channel(chat_id, entity.title or "channel",
                               getattr(entity, "username", None), link)
    await store.save()
    log.info("channel %s (%s) %s", entity.title, chat_id,
             "added" if is_new else "refreshed")
    return True, ""


def _cached_rights(entity) -> Optional[types.ChatAdminRights]:
    """Admin rights Telegram already sent with the dialog list - free to read."""
    if getattr(entity, "creator", False):
        return types.ChatAdminRights(
            change_info=True, post_messages=True, edit_messages=True,
            delete_messages=True, ban_users=True, invite_users=True,
            pin_messages=True, add_admins=True)
    return getattr(entity, "admin_rights", None)


async def sync_discovered() -> list:
    """Walk the account's own dialogs - a userbot CAN list its channels, no
    bot API needed - and connect every broadcast channel it admins.

    Rights come from the dialog list itself, so this costs one request per
    100 dialogs instead of one per channel.
    """
    found = []
    try:
        dialogs = [d async for d in client.iter_dialogs()]
    except (RPCError, *TRANSIENT) as exc:
        log.warning("could not list dialogs: %s", short(exc))
        return found

    for dialog in dialogs:
        entity = dialog.entity
        if not isinstance(entity, types.Channel) or not getattr(entity, "broadcast", False):
            continue
        if getattr(entity, "left", False):
            continue
        chat_id = peer_id(entity)
        if hosts_post(chat_id) or store.get_channel(chat_id):
            continue
        rights = _cached_rights(entity)
        if rights is None:
            continue  # not an admin there
        if not getattr(rights, "invite_users", False):
            found.append(("needs-invite-right", entity))
            continue
        link, why = await mint_invite(entity)
        if not link:
            log.info("skipping %s: %s", chat_id, why)
            continue
        store.add_channel(chat_id, entity.title or "channel",
                          getattr(entity, "username", None), link)
        found.append(("added", entity))
        await asyncio.sleep(config.REQUEST_GAP)
    await store.save()
    return found


# ------------------------------------------------------------ redirect post
def render_post_text() -> str:
    """The post body, as HTML.

    HTML, not markdown: invite hashes legitimately contain ``_``, which
    markdown would eat as italics and break every link.
    """
    active = store.active_channel()
    lines = []
    title = config.POST_TITLE.strip()
    if title.lower() not in ("", "none"):
        lines.append(f"<b>{html.escape(title)}</b>")
    if active:
        lines.extend([html.escape(active["link"])] * store.post_slots())
    else:
        lines.append("⏳ Redirecting…")
    return "\n".join(lines)


async def create_post(chat_id: int, slots: int) -> Tuple[bool, str]:
    """Post the redirect message in a channel and remember it."""
    try:
        entity = await get_entity(chat_id)
    except (ValueError, RPCError, *TRANSIENT) as exc:
        return False, f"cannot open the channel: {short(exc)}"
    try:
        rights = await my_rights(entity)
    except (RPCError, *TRANSIENT) as exc:
        return False, f"could not read my rights ({short(exc)})"
    if rights is None or not getattr(rights, "is_admin", False):
        return False, "not an admin there yet"
    if not getattr(rights, "post_messages", False):
        return False, 'admin, but without the "Post Messages" right'
    try:
        msg = await rpc(lambda: client.send_message(entity, render_post_text(),
                                                    parse_mode="html"),
                        "create post")
    except (RPCError, *TRANSIENT) as exc:
        return False, short(exc)
    store.set_post(chat_id, msg.id, slots)
    await store.save()
    log.info("redirect post created in %s (message %s)", chat_id, msg.id)
    return True, ""


async def refresh_post() -> str:
    """Re-render the redirect post. Returns a short result for logs/alerts."""
    post = store.post
    if not post:
        return "no-post"
    text = render_post_text()
    try:
        await rpc(lambda: client.edit_message(post["chat_id"], post["message_id"],
                                              text, parse_mode="html"),
                  "edit post")
        return "updated"
    except MessageNotModifiedError:
        return "unchanged"  # the post already shows the right links
    except (MessageIdInvalidError, *GONE) as exc:
        if isinstance(exc, GONE):
            log.error("lost access to the post channel %s: %s",
                      post["chat_id"], short(exc))
            return f"error:{short(exc)}"
        if not config.REPOST_IF_DELETED:
            store.clear_post()
            await store.save()
            return "post-gone"
        ok, why = await create_post(post["chat_id"], post["slots"])
        return "reposted" if ok else f"error:{why}"
    except (RPCError, *TRANSIENT) as exc:
        return f"error:{short(exc)}"
