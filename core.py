"""The working heart: channel pool, active liveness probes, invite links,
and the redirect post.

Everything runs through one user account, so a check is never a guess from a
web page - the account simply tries to *do* something in the channel and
watches whether Telegram lets it.
"""

import asyncio
import logging
import random
import re
from typing import Optional, Tuple

from telethon import TelegramClient, functions, types
from telethon.errors import (
    ChatAdminRequiredError,
    FloodWaitError,
    InviteHashExpiredError,
    InviteHashInvalidError,
    MessageIdInvalidError,
    RPCError,
    UserNotParticipantError,
)

import config
import texts
from storage import store

log = logging.getLogger(__name__)

INVITE_RE = re.compile(r"(?:t\.me/\+|t\.me/joinchat/)([\w-]+)")
USERNAME_RE = re.compile(r"(?:^|t\.me/)(@?[A-Za-z][A-Za-z0-9_]{3,})\s*$")

# Set by main once the account is known.
client: TelegramClient = None  # type: ignore[assignment]
ME_ID: int = 0


def bind(tg_client: TelegramClient, me_id: int) -> None:
    global client, ME_ID
    client, ME_ID = tg_client, me_id


# ------------------------------------------------------------------ helpers
async def rpc(coro, what: str):
    """Run one Telegram request, respecting flood-waits. Re-raises the rest."""
    try:
        return await coro
    except FloodWaitError as exc:
        log.warning("flood-wait %ss on %s - sleeping it off", exc.seconds, what)
        await asyncio.sleep(exc.seconds + 1)
        return await coro  # one retry; if it still fails the caller decides
    except SlowModeWaitError as exc:
        log.warning("slow-mode %ss on %s", exc.seconds, what)
        await asyncio.sleep(exc.seconds + 1)
        return await coro


def invite_hash(link: str) -> Optional[str]:
    match = INVITE_RE.search(link or "")
    return match.group(1) if match else None


async def get_entity(chat_id: int):
    return await rpc(client.get_entity(chat_id), f"get_entity {chat_id}")


async def my_rights(entity) -> Optional[object]:
    """My admin rights in a channel, or None if they couldn't be read."""
    try:
        return await rpc(client.get_permissions(entity, "me"), "permissions")
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
            entity = await rpc(client.get_entity(match.group(1).lstrip("@")),
                                   f"resolve {link}")
            return isinstance(entity, types.Channel)
        except (ValueError, RPCError):
            return None
    try:
        result = await rpc(
            client(functions.messages.CheckChatInviteRequest(h)), f"check invite {h}")
        # ChatInviteAlready / ChatInvite / ChatInvitePeek all mean: link valid.
        return isinstance(
            result, (types.ChatInviteAlready, types.ChatInvite, types.ChatInvitePeek))
    except (InviteHashExpiredError, InviteHashInvalidError):
        return False
    except RPCError as exc:
        log.warning("invite check of %s inconclusive: %s", link, exc)
        return None


async def mint_invite(entity, revoke_old: bool = False) -> Tuple[Optional[str], str]:
    """Create (or, with revoke_old, replace) the channel's invite link."""
    if not hasattr(entity, "broadcast"):
        return None, "not a channel"
    try:
        result = await rpc(
            client(functions.messages.ExportChatInviteRequest(
                entity, legacy_revoke_permanent=revoke_old)),
            f"export invite {getattr(entity, 'id', '?')}")
    except ChatAdminRequiredError:
        return None, 'missing the "Invite Users via Link" admin right'
    except RPCError as exc:
        return None, str(exc)
    link = getattr(result, "link", None) or getattr(getattr(result, "invite", None), "link", None)
    if not link:
        return None, "Telegram returned no link"
    return link, ""


# ------------------------------------------------------------- bio updates
async def update_bio(entity) -> Tuple[bool, str]:
    """Write a random line into the channel bio. This is both a feature (the
    channel always looks fresh) and the main liveness probe."""
    about = random.choice(texts.BIOS)[:255]
    try:
        # messages.editChatAbout takes an InputPeer and works for channels.
        await rpc(client(functions.messages.EditChatAboutRequest(entity, about)),
                  "edit bio")
        return True, ""
    except ChatAdminRequiredError:
        return False, 'missing the "Change Info" admin right'
    except RPCError as exc:
        return False, str(exc)


# ------------------------------------------------------------- deep verify
# Normal checks sometimes scream "deleted" when the channel is fine. Before
# believing them, prove it with actions only a live channel allows.
async def deep_verify(entity) -> Tuple[bool, str]:
    if entity is None:
        return False, "channel unreachable"

    # Probe 1: add a dot to the name and put it back.
    try:
        title = (getattr(entity, "title", "") or "")[:250]
        if title:
            await rpc(client(functions.channels.EditTitleRequest(entity, title + ".")),
                      "rename probe (add)")
            await asyncio.sleep(1)
            await rpc(client(functions.channels.EditTitleRequest(entity, title)),
                      "rename probe (revert)")
            return True, "rename probe passed"
    except RPCError as exc:
        log.info("rename probe failed: %s", exc)

    # Probe 2: post a silent message and delete it within a second.
    try:
        msg = await rpc(client.send_message(entity, ".", silent=True), "post probe")
        await asyncio.sleep(1)
        await rpc(client.delete_messages(entity, [msg.id]), "delete probe")
        return True, "post/delete probe passed"
    except RPCError as exc:
        log.info("post/delete probe failed: %s", exc)

    return False, "rename and post/delete probes both failed"


# --------------------------------------------------------------- one round
async def probe(channel: dict) -> Tuple[str, str]:
    """Health check of one pool channel.

    Returns (state, reason):
      ok        verified working (bio rewritten if we have the right)
      relinked  invite was dead; a fresh, verified link was saved
      suspect   looks dead *this round* - the caller counts strikes
      unknown   could not verify; change nothing
    """
    cid = channel["id"]

    # 1. Can the account still open the channel at all? If not, there is no
    #    entity to probe with - this already smells like deleted or kicked.
    try:
        entity = await get_entity(cid)
    except (ValueError, RPCError) as exc:
        return "suspect", f"cannot open the channel ({exc})"

    # 2. Still admin?
    rights = await my_rights(entity)
    if rights is None or not getattr(rights, "is_admin", False):
        alive, detail = await deep_verify(entity)
        if alive:
            return "suspect", "no longer admin there"
        return "suspect", f"no longer admin; {detail}"

    # 3. Bio probe: rewrite the bio with a random line.
    if getattr(rights, "change_info", False):
        ok, why = await update_bio(entity)
        if not ok:
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
    new_link, why = await mint_invite(entity, revoke_old=True)
    if new_link:
        await asyncio.sleep(2)  # let Telegram register the new invite
        if await invite_valid(new_link) is True:
            channel["link"] = new_link
            await store.save()
            return "relinked", "old invite stopped working"
        return "unknown", "fresh invite could not be verified yet"

    alive, detail = await deep_verify(entity)
    if alive:
        return "suspect", f"invite dead and no new one ({why})"
    return "suspect", f"invite dead ({why}); {detail}"


# ------------------------------------------------------------ channel pool
async def connect_channel(entity) -> Tuple[bool, str]:
    """Verify rights, mint an invite and add the channel to the pool.

    Returns (added_or_refreshed, human_reason_on_failure).
    """
    if not isinstance(entity, types.Channel) or not getattr(entity, "broadcast", False):
        return False, "not a broadcast channel"
    if store.post and entity.id == store.post["chat_id"]:
        return False, "that channel hosts the redirect post"
    chat_id = int(f"-100{entity.id}") if entity.id > 0 else entity.id
    if store.get_channel(chat_id):
        store.bring_online(chat_id)
        await store.save()
        return True, ""

    rights = await my_rights(entity)
    if rights is None or not getattr(rights, "is_admin", False):
        return False, "the account is not admin there"
    if not getattr(rights, "invite_users", False):
        return False, 'missing the "Invite Users via Link" admin right'

    link, why = await mint_invite(entity)
    if not link:
        return False, f"could not create an invite link ({why})"

    username = getattr(entity, "username", None)
    is_new = store.add_channel(chat_id, entity.title or "channel", username, link)
    await store.save()
    log.info("channel %s (%s) %s", entity.title, chat_id,
             "added" if is_new else "refreshed")
    return True, ""


async def sync_discovered() -> list:
    """Walk the account's own dialogs - a userbot CAN list its channels, no
    bot API needed - and connect every broadcast channel it admins."""
    added, post_chat = [], store.post["chat_id"] if store.post else None
    async for dialog in client.iter_dialogs():
        entity = dialog.entity
        if not isinstance(entity, types.Channel) or not getattr(entity, "broadcast", False):
            continue
        chat_id = int(f"-100{entity.id}")
        if chat_id == post_chat or store.get_channel(chat_id):
            continue
        rights = await my_rights(entity)
        await asyncio.sleep(config.REQUEST_GAP)
        if rights is None or not getattr(rights, "is_admin", False):
            continue
        if not getattr(rights, "invite_users", False):
            added.append(("needs-invite-right", entity))
            continue
        link, _ = await mint_invite(entity)
        if not link:
            continue
        store.add_channel(chat_id, entity.title or "channel",
                          getattr(entity, "username", None), link)
        added.append(("added", entity))
        await asyncio.sleep(config.REQUEST_GAP)
    await store.save()
    return added


# ------------------------------------------------------------ redirect post
def render_post_text() -> str:
    active = store.active_channel()
    lines = []
    if config.POST_TITLE.strip().lower() not in ("", "none"):
        lines.append(f"**{config.POST_TITLE.strip()}**")
    if active:
        lines.extend([active["link"]] * store.post_slots())
    else:
        lines.append("⏳ Redirecting…")
    return "\n".join(lines)


async def create_post(chat_id: int, slots: int) -> Tuple[bool, str]:
    """Post the redirect message in a channel and remember it."""
    try:
        entity = await get_entity(chat_id)
    except (ValueError, RPCError) as exc:
        return False, f"cannot open the channel: {exc}"
    rights = await my_rights(entity)
    if rights is None or not getattr(rights, "is_admin", False) \
            or not getattr(rights, "post_messages", False):
        return False, "not admin with Post Messages right yet"
    try:
        msg = await rpc(client.send_message(entity, render_post_text()), "create post")
    except RPCError as exc:
        return False, str(exc)
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
        await rpc(client.edit_message(post["chat_id"], post["message_id"], text),
                  "edit post")
        return "updated"
    except MessageIdInvalidError:
        if not config.REPOST_IF_DELETED:
            store.clear_post()
            await store.save()
            return "post-gone"
        ok, why = await create_post(post["chat_id"], post["slots"])
        return "reposted" if ok else f"error:{why}"
    except RPCError as exc:
        return f"error:{exc}"
