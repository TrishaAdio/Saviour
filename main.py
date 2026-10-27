"""Saviour - a pure userbot redirect keeper.

One user account does everything:
  * it notices when you promote it in a channel and adds the channel to the
    link pool (notifying you in DM),
  * it watches for ".set" in the redirect channel, waits for admin rights if
    it doesn't have them yet, and puts the multi-link redirect post up,
  * every CHECK_INTERVAL it rewrites every pool channel's bio with a random
    line (proof of life + fresh look), verifies invite links, mints new ones
    when needed, and only believes a channel is dead after direct action
    probes (full info, rename dot-revert, post-and-delete) also fail.

Run it once by hand to log in (phone + code), then keep it alive with systemd.
"""

import asyncio
import fcntl
import functools
import logging
import os
import re
import sys
import time

from telethon import TelegramClient, events, types
from telethon.errors import RPCError
from telethon.tl import functions

import config
import core
import texts
from storage import store

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(config.LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("saviour")

tg: TelegramClient = None  # type: ignore[assignment]  # created in main()
_instance_lock = None
# Only one health round at a time: /scan while the timer fires would have two
# rounds racing over the same strike counters.
_round_lock = asyncio.Lock()


def claim_single_instance() -> None:
    """Two copies sharing one session corrupt it - refuse to be the second."""
    global _instance_lock
    path = config.BASE_DIR / "bot.lock"
    _instance_lock = open(path, "a+")
    try:
        fcntl.flock(_instance_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        _instance_lock.seek(0)
        other = (_instance_lock.read() or "").strip() or "unknown"
        raise SystemExit(
            f"Another copy is already running (PID {other}). Stop it first:\n"
            "  pgrep -af main.py && pkill -f main.py; systemctl stop saviour"
        )
    _instance_lock.seek(0)
    _instance_lock.truncate()
    _instance_lock.write(str(os.getpid()))
    _instance_lock.flush()


# ------------------------------------------------------------------- notify
# Everything is sent as plain text on purpose: channel titles and invite
# hashes legitimately contain "_" and "*", which markdown would mangle or
# reject outright.
async def notify(text: str) -> None:
    """DM the owner. If that's impossible, at least keep it in the log."""
    try:
        await tg.send_message(config.OWNER_ID, text, parse_mode=None,
                              link_preview=False)
    except (ValueError, RPCError, *core.TRANSIENT) as exc:
        log.warning("could not DM the owner (%s) - message was: %s",
                    core.short(exc), text.replace("\n", " | "))


async def reply(event, text: str):
    """Answer a command in the owner chat, as plain text."""
    try:
        return await event.reply(text, parse_mode=None, link_preview=False)
    except (RPCError, *core.TRANSIENT) as exc:
        log.warning("could not reply: %s", core.short(exc))
        return None


async def edit(message, text: str) -> None:
    if message is None:
        return
    try:
        await message.edit(text, parse_mode=None, link_preview=False)
    except (RPCError, *core.TRANSIENT):
        pass


async def beg_if_empty() -> None:
    if store.channels and not store.online():
        if not store.awaiting_links:
            store.awaiting_links = True
            await store.save()
            await notify(texts.no_links_left())
    elif store.online() and store.awaiting_links:
        store.awaiting_links = False
        await store.save()


# --------------------------------------------------------------- .set posts
SET_RE = re.compile(r"^\s*\.set(?:\s+(\d+))?\s*$")


async def try_make_post(chat_id: int, slots: int) -> bool:
    """Create the redirect post if the account may post there."""
    ok, why = await core.create_post(chat_id, slots)
    if not ok:
        log.info("cannot create the post in %s yet: %s", chat_id, why)
        return False
    # The host channel must never be one of its own redirect targets.
    if store.get_channel(chat_id):
        store.remove_channel(chat_id)
        log.info("removed %s from the pool: it now hosts the post", chat_id)
        await store.save()
    title = str(chat_id)
    try:
        title = (await core.get_entity(chat_id)).title or title
    except (ValueError, RPCError, *core.TRANSIENT):
        pass
    await notify(texts.post_ready(title, chat_id, slots))
    await core.refresh_post()
    return True


async def on_set(event) -> None:
    # Only ".set" posted in a broadcast channel counts. Channel posts are
    # usually anonymous (the author is the channel itself), so the sender id
    # cannot be checked - but only admins can post in a channel at all.
    if not event.is_channel or event.is_group:
        return
    if not getattr(event.message, "post", False) \
            and event.sender_id not in (config.OWNER_ID, core.ME_ID):
        return
    chat_id = event.chat_id
    slots = int(event.pattern_match.group(1) or config.POST_SLOTS)
    if not 1 <= slots <= 20:
        slots = config.POST_SLOTS

    # Remember the wish first, so a crash or restart can't lose it.
    store.pending_set = {"chat_id": chat_id, "slots": slots}
    await store.save()

    # Try to remove the ".set" command message; ignore failures.
    try:
        await event.delete()
    except (RPCError, *core.TRANSIENT):
        pass

    if await try_make_post(chat_id, slots):
        return
    title = str(chat_id)
    try:
        title = (await core.get_entity(chat_id)).title or title
    except (ValueError, RPCError, *core.TRANSIENT):
        pass
    await notify(texts.set_waiting(title, chat_id))


# ------------------------------------------- promotions / demotions / joins
def _is_admin(participant) -> bool:
    return isinstance(participant, (types.ChannelParticipantAdmin,
                                    types.ChannelParticipantCreator))


async def on_participant(update) -> None:
    if getattr(update, "user_id", None) != core.ME_ID:
        return
    chat_id = int(f"-100{update.channel_id}")
    new, prev = update.new_participant, update.prev_participant
    now_admin, was_admin = _is_admin(new), _is_admin(prev)
    now_member = isinstance(new, (types.ChannelParticipantSelf,
                                  types.ChannelParticipant))

    title, entity = str(chat_id), None
    try:
        entity = await core.get_entity(chat_id)
        title = getattr(entity, "title", title) or title
    except (ValueError, RPCError, *core.TRANSIENT) as exc:
        log.info("promotion update for %s but the channel is unreadable: %s",
                 chat_id, core.short(exc))

    # 1. Freshly joined, not admin yet.
    if prev is None and now_member and not now_admin:
        await notify(texts.joined_waiting(title, chat_id))
        return

    # 2. Promoted to admin.
    if now_admin and not was_admin:
        if core.hosts_post(chat_id):
            pending = store.pending_set
            if pending and pending["chat_id"] == chat_id and not store.post:
                await try_make_post(chat_id, pending["slots"])
            return
        if entity is None:
            return
        try:
            rights = await core.my_rights(entity)
        except (RPCError, *core.TRANSIENT) as exc:
            log.info("could not read rights in %s: %s", chat_id, core.short(exc))
            return
        if rights is not None and not getattr(rights, "invite_users", False):
            await notify(texts.sync_needs_invite_right(title, chat_id))
            return
        added, why = await core.connect_channel(entity)
        if not added:
            log.info("promotion in %s did not connect the channel: %s", title, why)
            return
        channel = store.get_channel(chat_id)
        await notify(texts.channel_synced(
            title, chat_id, channel["link"] if channel else "?",
            bool(getattr(rights, "change_info", False)), len(store.online())))
        await beg_if_empty()
        await core.refresh_post()
        return

    # 3. Demoted or kicked.
    if was_admin and not now_admin:
        if store.post and store.post["chat_id"] == chat_id:
            store.clear_post()
            await store.save()
            await notify(texts.post_channel_lost(title, chat_id))
            return
        if store.get_channel(chat_id):
            store.park_offline(chat_id, "admin rights lost")
            await store.save()
            result = await core.refresh_post()
            log.info("demoted in %s -> parked offline, post -> %s", title, result)
            await notify(texts.demoted(title, chat_id))
            await beg_if_empty()


# ------------------------------------------------------------- health check
async def health_check() -> None:
    """One full round. Serialised, so /scan and the timer never overlap."""
    async with _round_lock:
        await _health_round()


async def _health_round() -> None:
    # A ".set" whose promotion update was missed (bot restarting, update
    # dropped) is retried every round until the post exists.
    pending = store.pending_set
    if pending and not store.post:
        await try_make_post(pending["chat_id"], pending["slots"])

    if not store.channels:
        await beg_if_empty()
        return

    parked, relinked, recovered, suspects, unknown = [], [], [], [], 0
    for channel in list(store.channels):
        try:
            state, reason = await core.probe(channel)
        except Exception as exc:  # one broken channel must not kill the round
            log.exception("probe of %s crashed: %s", channel["id"], exc)
            state, reason = "unknown", f"probe crashed ({core.short(exc)})"
        was_offline = bool(channel.get("offline"))

        if state in ("ok", "relinked"):
            channel["strikes"] = 0
            channel.pop("reason", None)
            if was_offline:
                store.bring_online(channel["id"])
                recovered.append(channel)
            if state == "relinked":
                relinked.append(channel)
        elif state == "unknown":
            unknown += 1
            log.info("channel %s unverified this round: %s", channel["id"], reason)
        else:  # suspect
            channel["strikes"] = int(channel.get("strikes", 0)) + 1
            channel["reason"] = reason
            log.warning("channel %s suspect (%s/%s): %s", channel["id"],
                        channel["strikes"], config.DEATH_STRIKES, reason)
            if not was_offline:
                if channel["strikes"] >= config.DEATH_STRIKES:
                    store.park_offline(channel["id"], reason)
                    parked.append(channel)
                elif channel["strikes"] == 1:  # warn early, but only once
                    suspects.append(channel)
        await asyncio.sleep(config.REQUEST_GAP)

    store.data["last_check"] = int(time.time())
    await store.save()

    result = await core.refresh_post()
    log.info("health check: %s online, %s offline | parked %s, relinked %s, "
             "recovered %s, unverified %s | post -> %s",
             len(store.online()), len(store.offline()), len(parked),
             len(relinked), len(recovered), unknown, result)

    if parked:
        await notify(texts.channel_offline(parked, len(store.online())))
    for channel in recovered:
        await notify(texts.channel_recovered(channel))
    for channel in relinked:
        await notify(texts.channel_relinked(channel))
    for channel in suspects:
        await notify(texts.channel_suspect(
            channel, channel["strikes"], config.DEATH_STRIKES))
    if result == "reposted":
        await notify(texts.post_deleted_reposted())
    elif result == "post-gone":
        await notify(texts.post_deleted_manual())

    await beg_if_empty()


async def health_loop() -> None:
    await asyncio.sleep(20)  # let the client settle after connecting
    while True:
        try:
            await health_check()
        except Exception:
            log.exception("health check round crashed")
        await asyncio.sleep(config.CHECK_INTERVAL)


async def run_discovery(announce: bool = True) -> list:
    """Find channels the account admins that aren't in the pool yet."""
    found = await core.sync_discovered()
    new = [e for kind, e in found if kind == "added"]
    needs = [e for kind, e in found if kind == "needs-invite-right"]
    if announce:
        for entity in new:
            chat_id = core.peer_id(entity)
            channel = store.get_channel(chat_id)
            rights = core._cached_rights(entity)
            await notify(texts.channel_synced(
                entity.title or "channel", chat_id,
                channel["link"] if channel else "?",
                bool(getattr(rights, "change_info", False)),
                len(store.online())))
        for entity in needs:
            await notify(texts.sync_needs_invite_right(
                entity.title or "channel", core.peer_id(entity)))
    if new:
        await beg_if_empty()
        await core.refresh_post()
    return found


async def sync_loop() -> None:
    if config.SYNC_INTERVAL <= 0:
        return
    await asyncio.sleep(40)
    while True:
        try:
            await run_discovery()
        except Exception:
            log.exception("auto-sync crashed")
        await asyncio.sleep(config.SYNC_INTERVAL)


# ------------------------------------------------------------- owner commands
def owner_only(func):
    @functools.wraps(func)
    async def wrapper(event) -> None:
        if not event.is_private or event.sender_id != config.OWNER_ID:
            return
        try:
            await func(event)
        except Exception as exc:
            log.exception("command failed: %s", exc)
            await reply(event, f"⚠️ That failed: {core.short(exc)}\nThe bot is still running.")
    return wrapper


@owner_only
async def cmd_help(event) -> None:
    await reply(event, texts.help_text())


@owner_only
async def cmd_status(event) -> None:
    await reply(event, texts.status_text(store, texts.ago(store.data.get("last_check", 0))))


@owner_only
async def cmd_channels(event) -> None:
    if not store.channels:
        await reply(event, "No channels yet. Promote the account in a channel "
                           "and it appears here.")
        return
    await reply(event, "\n".join(texts.channel_line(c) for c in store.channels))


@owner_only
async def cmd_scan(event) -> None:
    if _round_lock.locked():
        await reply(event, "A check is already running - hold on.")
        return
    await reply(event, "🔍 Checking every channel now…")
    await health_check()
    await reply(event, texts.status_text(store, texts.ago(store.data.get("last_check", 0))))


@owner_only
async def cmd_sync(event) -> None:
    await reply(event, "🔎 Looking through my channels…")
    found = await run_discovery(announce=False)
    new = [e for kind, e in found if kind == "added"]
    needs = [e for kind, e in found if kind == "needs-invite-right"]
    lines = [f"✅ Synced: {e.title}" for e in new]
    lines += [f"🟠 Needs the Invite Users via Link right: {e.title}" for e in needs]
    await reply(event, "\n".join(lines) if lines else "Nothing new found.")


@owner_only
async def cmd_load(event) -> None:
    store.awaiting_links = True
    await store.save()
    await reply(event, "Paste the channel links (one per line: @name, t.me/name, "
                       "+invite or -100 id). /cancel to stop.")


@owner_only
async def cmd_cancel(event) -> None:
    store.awaiting_links = False
    await store.save()
    await reply(event, "Cancelled.")


@owner_only
async def cmd_refresh(event) -> None:
    await reply(event, f"Post: {await core.refresh_post()}")


@owner_only
async def cmd_unset(event) -> None:
    store.clear_post()
    store.pending_set = None
    await store.save()
    await reply(event, "Redirect post forgotten. Post .set in a channel to claim a new one.")


@owner_only
async def cmd_slots(event) -> None:
    raw = (event.pattern_match.group(1) or "").strip()
    if not raw:
        await reply(event, f"Usage: /slots <1-20>   (currently {store.post_slots()})")
        return
    n = int(raw)
    if not 1 <= n <= 20:
        await reply(event, "Slots must be between 1 and 20.")
        return
    post = store.post
    if post:
        post["slots"] = n
        await store.save()
        await reply(event, f"Slots: {n}. Post: {await core.refresh_post()}")
        return
    config.POST_SLOTS = n  # applies to the next .set
    await reply(event, f"Slots for the next .set: {n} (no post claimed yet).")


@owner_only
async def cmd_remove(event) -> None:
    raw = (event.pattern_match.group(1) or "").strip()
    if not raw:
        await reply(event, "Usage: /remove <-100 channel id>   (see /channels)")
        return
    chat_id = int(raw)
    channel = store.get_channel(chat_id)
    if not channel:
        await reply(event, f"Channel {chat_id} is not in the pool.")
        return
    store.remove_channel(chat_id)
    await store.save()
    await reply(event, f"🗑 Removed {channel['title']}. Post: {await core.refresh_post()}")
    await beg_if_empty()


@owner_only
async def cmd_active(event) -> None:
    raw = (event.pattern_match.group(1) or "").strip()
    if not raw:
        active = store.active_channel()
        await reply(event, "Usage: /active <-100 channel id>   (currently "
                           f"{active['title'] if active else 'none'})")
        return
    chat_id = int(raw)
    channel = store.get_channel(chat_id)
    if not channel:
        await reply(event, f"Channel {chat_id} is not in the pool.")
        return
    if channel.get("offline"):
        await reply(event, f"{channel['title']} is offline - pick an online one.")
        return
    store.data["active_id"] = chat_id
    await store.save()
    await reply(event, f"⭐ Active: {channel['title']}. Post: {await core.refresh_post()}")


# ------------------------------------------------------------- .clearall
@owner_only
async def cmd_clearall(event) -> None:
    if not event.pattern_match.group(1):
        await reply(event,
                    "🧨 .clearall wipes the whole account:\n"
                    "• deletes every group/channel it created\n"
                    "• leaves every other group and channel (including the whole "
                    "link pool and the redirect channel)\n"
                    "• blocks every DM and deletes its history\n\n"
                    "Only this chat and Saved Messages survive. The redirect state "
                    "(pool, post) is wiped too.\n\n"
                    "This cannot be undone. Send  .clearall confirm  to do it.")
        return
    await run_clearall(event)


async def run_clearall(event) -> None:
    status = await reply(event, "🧨 Clearing the account… this can take a while.")
    counts = {"deleted_channels": 0, "deleted_groups": 0, "left_channels": 0,
              "left_groups": 0, "blocked": 0, "failed": 0}
    try:
        dialogs = [d async for d in tg.iter_dialogs()]
    except (RPCError, *core.TRANSIENT) as exc:
        await edit(status, f"🧨 Could not list the chats: {core.short(exc)}")
        return

    for i, dialog in enumerate(dialogs, 1):
        entity = dialog.entity
        try:
            if isinstance(entity, types.User):
                if entity.id in (core.ME_ID, config.OWNER_ID):
                    continue  # Saved Messages and this command chat
                await core.rpc(
                    lambda e=entity: tg(functions.messages.DeleteHistoryRequest(
                        e, max_id=0, just_clear=False, revoke=True)),
                    "delete history")
                await core.rpc(
                    lambda e=entity: tg(functions.contacts.BlockRequest(e)), "block")
                counts["blocked"] += 1
            elif isinstance(entity, types.Chat):  # basic group
                if getattr(entity, "creator", False):
                    await core.rpc(
                        lambda e=entity: tg(functions.messages.DeleteChatRequest(e.id)),
                        "delete group")
                    counts["deleted_groups"] += 1
                else:
                    await core.rpc(
                        lambda e=entity: tg(functions.messages.DeleteChatUserRequest(
                            e.id, types.InputUserSelf())),
                        "leave group")
                    counts["left_groups"] += 1
            elif isinstance(entity, types.Channel):
                group = bool(getattr(entity, "megagroup", False))
                if getattr(entity, "creator", False):
                    await core.rpc(
                        lambda e=entity: tg(functions.channels.DeleteChannelRequest(e)),
                        "delete channel")
                    counts["deleted_groups" if group else "deleted_channels"] += 1
                else:
                    await core.rpc(
                        lambda e=entity: tg(functions.channels.LeaveChannelRequest(e)),
                        "leave channel")
                    counts["left_groups" if group else "left_channels"] += 1
            else:
                continue
        except Exception as exc:
            counts["failed"] += 1
            log.warning("clearall: failed on %s: %s",
                        getattr(entity, "id", "?"), core.short(exc))
        if i % 5 == 0:
            await edit(status, f"🧨 Clearing… {i}/{len(dialogs)} chats done.")
        await asyncio.sleep(config.REQUEST_GAP)

    # Everything is left or deleted - the redirect state is meaningless now.
    store.data["channels"] = []
    store.clear_post()
    store.pending_set = None
    store.data["active_id"] = None
    store.awaiting_links = False
    await store.save()

    await edit(status,
               "🧨 Account cleared.\n"
               f"• Channels deleted (created by me): {counts['deleted_channels']}\n"
               f"• Groups deleted (created by me): {counts['deleted_groups']}\n"
               f"• Channels left: {counts['left_channels']}\n"
               f"• Groups left: {counts['left_groups']}\n"
               f"• DMs blocked + wiped: {counts['blocked']}\n"
               f"• Failed: {counts['failed']}\n\n"
               "Link pool and redirect post state wiped. This chat and Saved "
               "Messages were kept on purpose. I'm still running - add me to "
               "channels to start over.")


# ------------------------------------------------- pasted links in the DM
ID_RE = re.compile(r"^(-100\d{5,})$")
# A line only counts as a channel reference if it looks like one. Without
# this, ordinary chat in the DM gets answered with "not a channel link".
TARGET_RE = re.compile(r"^(?:@[A-Za-z][A-Za-z0-9_]{3,}"
                       r"|(?:https?://)?t\.me/\S+"
                       r"|-100\d{5,})$")


async def resolve_target(line: str):
    """Turn one pasted line into a channel entity, or return (None, reason)."""
    line = line.strip()
    if not line:
        return None, ""
    try:
        if ID_RE.match(line):
            return await core.get_entity(int(line)), ""
        h = core.invite_hash(line)
        if h:
            result = await core.rpc(
                lambda: tg(functions.messages.CheckChatInviteRequest(h)),
                "resolve invite")
            if isinstance(result, types.ChatInviteAlready):
                return result.chat, ""
            return None, ("that invite is for a channel I haven't joined - "
                          "add the account to it first")
        name = re.sub(r"^(?:https?://)?t\.me/", "", line).strip("/").lstrip("@")
        if not name:
            return None, "not a channel link or id"
        return await core.get_entity(name), ""
    except (ValueError, RPCError, *core.TRANSIENT) as exc:
        return None, core.short(exc)


async def on_owner_text(event) -> None:
    if not event.is_private or event.sender_id != config.OWNER_ID:
        return
    if event.out:
        return  # never react to our own messages (owner == the account itself)
    text = event.raw_text or ""
    if text.startswith(("/", ".")):
        return  # commands have their own handlers
    candidates = [l.strip() for l in text.splitlines() if TARGET_RE.match(l.strip())]
    if not candidates:
        return  # ordinary chat in the DM: say nothing

    added, failed = [], []
    for line in candidates:
        entity, why = await resolve_target(line)
        if entity is None:
            failed.append(f"❌ {line}: {why or 'could not resolve'}")
            continue
        if not isinstance(entity, types.Channel) or not getattr(entity, "broadcast", False):
            failed.append(f"❌ {line}: not a broadcast channel")
            continue
        ok, reason = await core.connect_channel(entity)
        if ok:
            added.append(entity)
        else:
            failed.append(f"❌ {entity.title}: {reason}")
        await asyncio.sleep(config.REQUEST_GAP)

    lines = [f"✅ {e.title} — loaded" for e in added] + failed
    if added:
        await store.save()
        result = await core.refresh_post()
        lines.append(f"Pool: {len(store.online())} active · Post: {result}")
        if store.post is None and store.pending_set is None:
            lines.append("No redirect post yet - post .set in the target channel.")
        await beg_if_empty()
    await reply(event, "\n".join(lines))


# -------------------------------------------------------------------- startup
def register_handlers() -> None:
    tg.add_event_handler(on_set, events.NewMessage(pattern=SET_RE))
    tg.add_event_handler(on_participant, events.Raw(types.UpdateChannelParticipant))

    for handler, pattern in (
        (cmd_help, r"(?i)^/(?:start|help)\s*$"),
        (cmd_status, r"(?i)^/status\s*$"),
        (cmd_channels, r"(?i)^/channels\s*$"),
        (cmd_scan, r"(?i)^/scan\s*$"),
        (cmd_sync, r"(?i)^/sync\s*$"),
        (cmd_load, r"(?i)^/load\s*$"),
        (cmd_cancel, r"(?i)^/cancel\s*$"),
        (cmd_refresh, r"(?i)^/refresh\s*$"),
        (cmd_unset, r"(?i)^/unset\s*$"),
        (cmd_slots, r"(?i)^/slots(?:\s+(\d+))?\s*$"),
        (cmd_remove, r"(?i)^/remove(?:\s+(-?\d+))?\s*$"),
        (cmd_active, r"(?i)^/active(?:\s+(-?\d+))?\s*$"),
        (cmd_clearall, r"(?i)^\.clearall(\s+confirm)?\s*$"),
    ):
        tg.add_event_handler(handler, events.NewMessage(pattern=pattern))

    tg.add_event_handler(on_owner_text, events.NewMessage())  # owner DM checked inside


async def main() -> None:
    global tg
    if not config.API_ID or not config.API_HASH:
        raise SystemExit("API_ID / API_HASH missing - run: python3 setup.py")
    if not config.OWNER_ID:
        raise SystemExit("OWNER_ID missing - run: python3 setup.py")
    claim_single_instance()

    tg = TelegramClient(str(config.BASE_DIR / config.SESSION_NAME),
                        config.API_ID, config.API_HASH)
    register_handlers()

    await tg.start()  # first run asks for phone + login code interactively
    me = await tg.get_me()
    core.bind(tg, me.id)
    log.info("logged in as @%s (%s)", me.username, me.id)
    if me.id == config.OWNER_ID:
        log.warning("OWNER_ID is the userbot's own account - notifications go to "
                    "Saved Messages and pasted links are ignored. Use a separate "
                    "account as the owner.")

    asyncio.create_task(health_loop())
    asyncio.create_task(sync_loop())
    await notify(texts.startup(me.username or str(me.id),
                               len(store.channels), bool(store.post)))
    log.info("running…")
    await tg.run_until_disconnected()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
