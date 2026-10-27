"""Saviour - a pure userbot redirect keeper.

One user account does everything:
  * it notices when you promote it in a channel and adds the channel to the
    link pool (notifying you in DM),
  * it watches for ".set" in the redirect channel, waits for admin rights if
    it doesn't have them yet, and puts the multi-link redirect post up,
  * every CHECK_INTERVAL it rewrites every pool channel's bio with a random
    line (proof of life + fresh look), verifies invite links, mints new ones
    when needed, and only believes a channel is dead after direct action
    probes (rename dot-revert, post-and-delete) also fail.

Run it once by hand to log in (phone + code), then keep it alive with systemd.
"""

import asyncio
import fcntl
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
async def notify(text: str) -> None:
    """DM the owner. If that's impossible, at least keep it in the log."""
    try:
        await tg.send_message(config.OWNER_ID, text)
    except (ValueError, RPCError) as exc:
        log.warning("could not DM the owner (%s) - message was: %s", exc,
                    text.replace("\n", " | "))


async def beg_if_empty() -> None:
    if store.channels and not store.online():
        if not store.awaiting_links:
            store.awaiting_links = True
            await store.save()
            await notify(texts.no_links_left())
    elif store.online():
        store.awaiting_links = False


# --------------------------------------------------------------- .set posts
SET_RE = re.compile(r"^\s*\.set(?:\s+(\d+))?\s*$")


async def try_make_post(chat_id: int, slots: int) -> bool:
    ok, why = await core.create_post(chat_id, slots)
    if ok:
        entity_title = str(chat_id)
        try:
            entity_title = (await core.get_entity(chat_id)).title or entity_title
        except (ValueError, RPCError):
            pass
        await notify(texts.post_ready(entity_title, chat_id, slots))
        return True
    log.info("cannot create the post in %s yet: %s", chat_id, why)
    return False


async def on_set(event: events.NewMessage.Event) -> None:
    # Only care about ".set" posted in a broadcast channel by the owner
    # (or by this account itself).
    if not event.is_channel or event.is_group:
        return
    if event.sender_id not in (config.OWNER_ID, core.ME_ID):
        return
    chat_id = event.chat_id
    slots = int(event.pattern_match.group(1) or config.POST_SLOTS)

    # Try to remove the ".set" command message; ignore failures.
    try:
        await event.delete()
    except RPCError:
        pass

    if await try_make_post(chat_id, slots):
        return
    # Not admin yet: remember the wish and wait for the promotion event.
    store.pending_set = {"chat_id": chat_id, "slots": slots}
    await store.save()
    title = str(chat_id)
    try:
        title = (await core.get_entity(chat_id)).title or title
    except (ValueError, RPCError):
        pass
    await notify(texts.set_waiting(title, chat_id))


# ------------------------------------------- promotions / demotions / joins
def _is_admin(participant) -> bool:
    return isinstance(participant, (types.ChannelParticipantAdmin,
                                    types.ChannelParticipantCreator))


async def on_participant(update: types.UpdateChannelParticipant) -> None:
    if update.user_id != core.ME_ID:
        return
    chat_id = int(f"-100{update.channel_id}")
    new, prev = update.new_participant, update.prev_participant
    now_admin, was_admin = _is_admin(new), _is_admin(prev)
    now_member = isinstance(new, types.ChannelParticipantSelf)

    title = str(chat_id)
    entity = None
    try:
        entity = await core.get_entity(chat_id)
        title = getattr(entity, "title", title) or title
    except (ValueError, RPCError):
        pass

    # 1. Freshly joined (not admin yet).
    if prev is None and now_member and not now_admin:
        await notify(texts.joined_waiting(title, chat_id))
        return

    # 2. Promoted to admin.
    if now_admin and not was_admin:
        pending = store.pending_set
        if pending and pending["chat_id"] == chat_id:
            # The redirect channel: try the post; if it still can't be made,
            # keep waiting - and never pool the channel that hosts the post.
            await try_make_post(chat_id, pending["slots"])
            return
        if store.post and store.post["chat_id"] == chat_id:
            return
        if entity is not None:
            rights = await core.my_rights(entity)
            if rights is not None and not getattr(rights, "invite_users", False):
                await notify(texts.sync_needs_invite_right(title, chat_id))
                return
            added, why = await core.connect_channel(entity)
            if added:
                can_bio = bool(getattr(rights, "change_info", False))
                channel = store.get_channel(chat_id)
                await notify(texts.channel_synced(
                    title, chat_id, channel["link"], can_bio, len(store.online())))
                store.awaiting_links = False
                await core.refresh_post()
            else:
                log.info("promotion in %s did not connect the channel: %s", title, why)
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
    if not store.channels:
        await beg_if_empty()
        return

    parked, relinked, recovered, suspects = [], [], [], []
    for channel in list(store.channels):
        try:
            state, reason = await core.probe(channel)
        except Exception as exc:  # one broken channel must not kill the round
            log.exception("probe of %s crashed: %s", channel["id"], exc)
            continue
        was_offline = bool(channel.get("offline"))

        if state in ("ok", "relinked"):
            channel["strikes"] = 0
            if was_offline:
                store.bring_online(channel["id"])
                recovered.append(channel)
            if state == "relinked":
                relinked.append(channel)
        elif state == "unknown":
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
                else:
                    suspects.append(channel)
        await asyncio.sleep(config.REQUEST_GAP)

    store.data["last_check"] = int(time.time())
    await store.save()

    result = await core.refresh_post()
    log.info("health check: %s online, %s offline | parked %s, relinked %s, "
             "recovered %s | post -> %s",
             len(store.online()), len(store.offline()),
             len(parked), len(relinked), len(recovered), result)

    if parked:
        await notify(texts.channel_offline(parked, len(store.online())))
    for channel in recovered:
        await notify(texts.channel_recovered(channel))
    for channel in relinked:
        await notify(texts.channel_relinked(channel))
    for channel in suspects:
        if channel["strikes"] == 1:  # warn early, but only once
            await notify(texts.channel_suspect(
                channel, channel["strikes"], config.DEATH_STRIKES))
    if result == "reposted":
        await notify(texts.post_deleted_reposted())
    elif result == "post-gone":
        await notify(texts.post_deleted_manual())

    await beg_if_empty()


async def health_loop() -> None:
    await asyncio.sleep(30)  # let the client settle after connecting
    while True:
        try:
            await health_check()
        except Exception:
            log.exception("health check round crashed")
        await asyncio.sleep(config.CHECK_INTERVAL)


async def sync_loop() -> None:
    if config.SYNC_INTERVAL <= 0:
        return
    await asyncio.sleep(45)
    while True:
        try:
            added = await core.sync_discovered()
            new = [e for kind, e in added if kind == "added"]
            needs = [e for kind, e in added if kind == "needs-invite-right"]
            for entity in new:
                chat_id = int(f"-100{entity.id}")
                channel = store.get_channel(chat_id)
                rights = await core.my_rights(entity)
                await notify(texts.channel_synced(
                    entity.title or "channel", chat_id,
                    channel["link"] if channel else "?",
                    bool(getattr(rights, "change_info", False)),
                    len(store.online())))
            for entity in needs:
                await notify(texts.sync_needs_invite_right(
                    entity.title or "channel", int(f"-100{entity.id}")))
            if new:
                await core.refresh_post()
        except Exception:
            log.exception("auto-sync crashed")
        await asyncio.sleep(config.SYNC_INTERVAL)


# ------------------------------------------------------------- owner commands
def owner_only(func):
    async def wrapper(event: events.NewMessage.Event) -> None:
        if not event.is_private or event.sender_id != config.OWNER_ID:
            return
        await func(event)
    return wrapper


@owner_only
async def cmd_help(event) -> None:
    await event.reply(texts.help_text())


@owner_only
async def cmd_status(event) -> None:
    await event.reply(texts.status_text(store, texts.ago(store.data.get("last_check", 0))))


@owner_only
async def cmd_channels(event) -> None:
    if not store.channels:
        await event.reply("No channels yet. Promote the account in a channel and it appears here.")
        return
    await event.reply("\n".join(texts.channel_line(c) for c in store.channels))


@owner_only
async def cmd_scan(event) -> None:
    await event.reply("🔍 Checking every channel now…")
    await health_check()
    await event.reply(texts.status_text(store, texts.ago(store.data.get("last_check", 0))))


@owner_only
async def cmd_sync(event) -> None:
    await event.reply("🔎 Looking through my channels…")
    added = await core.sync_discovered()
    new = [e for kind, e in added if kind == "added"]
    needs = [e for kind, e in added if kind == "needs-invite-right"]
    lines = [f"✅ Synced: {e.title}" for e in new]
    lines += [f"🟠 Needs Invite right: {e.title}" for e in needs]
    await event.reply("\n".join(lines) if lines else "Nothing new found.")
    if new:
        await core.refresh_post()


@owner_only
async def cmd_load(event) -> None:
    store.awaiting_links = True
    await store.save()
    await event.reply("Paste the channel links (one per line: @name, t.me/name, "
                      "+invite or -100 id). /cancel to stop.")


@owner_only
async def cmd_cancel(event) -> None:
    store.awaiting_links = False
    await store.save()
    await event.reply("Cancelled.")


@owner_only
async def cmd_refresh(event) -> None:
    await event.reply(f"Post: {await core.refresh_post()}")


@owner_only
async def cmd_unset(event) -> None:
    store.clear_post()
    store.pending_set = None
    await store.save()
    await event.reply("Redirect post forgotten. Post .set in a channel to claim a new one.")


@owner_only
async def cmd_slots(event) -> None:
    n = int(event.pattern_match.group(1))
    if not 1 <= n <= 20:
        await event.reply("Slots must be between 1 and 20.")
        return
    post = store.post
    if post:
        post["slots"] = n
        await store.save()
        await event.reply(f"Slots: {n}. Post: {await core.refresh_post()}")
    else:
        config.POST_SLOTS = n  # applies to the next .set
        await event.reply(f"Slots for the next .set: {n} (no post claimed yet).")


@owner_only
async def cmd_remove(event) -> None:
    chat_id = int(event.pattern_match.group(1))
    channel = store.get_channel(chat_id)
    if not channel:
        await event.reply(f"Channel {chat_id} is not in the pool.")
        return
    store.remove_channel(chat_id)
    await store.save()
    await event.reply(f"🗑 Removed {channel['title']}. Post: {await core.refresh_post()}")
    await beg_if_empty()


@owner_only
async def cmd_active(event) -> None:
    chat_id = int(event.pattern_match.group(1))
    channel = store.get_channel(chat_id)
    if not channel:
        await event.reply(f"Channel {chat_id} is not in the pool.")
        return
    if channel.get("offline"):
        await event.reply(f"{channel['title']} is offline - pick an online one.")
        return
    store.data["active_id"] = chat_id
    await store.save()
    await event.reply(f"⭐ Active: {channel['title']}. Post: {await core.refresh_post()}")


# ------------------------------------------------------------- .clearall
@owner_only
async def cmd_clearall(event) -> None:
    if not event.pattern_match.group(1):
        await event.reply(
            "🧨 .clearall wipes the whole account:\n"
            "• deletes every group/channel it created\n"
            "• leaves every other group and channel (including the whole link pool "
            "and the redirect channel)\n"
            "• blocks every DM and deletes its history\n\n"
            "Only this chat and Saved Messages survive. The redirect state "
            "(pool, post) is wiped too.\n\n"
            "This cannot be undone. Send  .clearall confirm  to do it.")
        return
    await run_clearall(event)


async def run_clearall(event) -> None:
    status = await event.reply("🧨 Clearing the account… this can take a while.")
    counts = {"deleted_channels": 0, "deleted_groups": 0, "left_channels": 0,
              "left_groups": 0, "blocked": 0, "failed": 0}
    dialogs = [d async for d in tg.iter_dialogs()]

    for i, dialog in enumerate(dialogs, 1):
        entity = dialog.entity
        try:
            if isinstance(entity, types.User):
                if entity.id in (core.ME_ID, config.OWNER_ID):
                    continue  # Saved Messages and this command chat
                await core.rpc(
                    tg(functions.messages.DeleteHistoryRequest(
                        entity, max_id=0, just_clear=False, revoke=True)),
                    "delete history")
                await core.rpc(tg(functions.contacts.BlockRequest(entity)), "block")
                counts["blocked"] += 1
            elif isinstance(entity, types.Chat):  # basic group
                if getattr(entity, "creator", False):
                    await core.rpc(tg(functions.messages.DeleteChatRequest(entity.id)),
                                   "delete group")
                    counts["deleted_groups"] += 1
                else:
                    await core.rpc(
                        tg(functions.messages.DeleteChatUserRequest(
                            entity.id, types.InputUserSelf())),
                        "leave group")
                    counts["left_groups"] += 1
            elif isinstance(entity, types.Channel):
                if getattr(entity, "creator", False):
                    await core.rpc(tg(functions.channels.DeleteChannelRequest(entity)),
                                   "delete channel")
                    if getattr(entity, "megagroup", False):
                        counts["deleted_groups"] += 1
                    else:
                        counts["deleted_channels"] += 1
                else:
                    await core.rpc(tg(functions.channels.LeaveChannelRequest(entity)),
                                   "leave channel")
                    if getattr(entity, "megagroup", False):
                        counts["left_groups"] += 1
                    else:
                        counts["left_channels"] += 1
        except Exception as exc:
            counts["failed"] += 1
            log.warning("clearall: failed on %s: %s", getattr(entity, "id", "?"), exc)
        if i % 5 == 0:
            try:
                await status.edit(f"🧨 Clearing… {i}/{len(dialogs)} dialogs done.")
            except RPCError:
                pass
        await asyncio.sleep(config.REQUEST_GAP)

    # Everything is left/deleted - the redirect state is meaningless now.
    store.data["channels"] = []
    store.clear_post()
    store.pending_set = None
    store.data["active_id"] = None
    store.awaiting_links = False
    await store.save()

    await status.edit(
        "🧨 Account cleared.\n"
        f"• Channels deleted (created by me): {counts['deleted_channels']}\n"
        f"• Groups deleted (created by me): {counts['deleted_groups']}\n"
        f"• Channels left: {counts['left_channels']}\n"
        f"• Groups left: {counts['left_groups']}\n"
        f"• DMs blocked + wiped: {counts['blocked']}\n"
        f"• Failed: {counts['failed']}\n\n"
        "Link pool and redirect post state wiped. This chat and Saved Messages "
        "were kept on purpose. I'm still running - add me to channels to start over.")


# ------------------------------------------------- pasted links in the DM
ID_RE = re.compile(r"^(-100\d{5,})\s*$")


async def resolve_target(line: str):
    """Turn one pasted line into a channel entity, or return (None, reason)."""
    line = line.strip()
    if not line:
        return None, ""
    match = ID_RE.match(line)
    try:
        if match:
            return await core.get_entity(int(match.group(1))), ""
        h = core.invite_hash(line)
        if h:
            result = await core.rpc(
                tg(functions.messages.CheckChatInviteRequest(h)), "resolve invite")
            if isinstance(result, types.ChatInviteAlready):
                return result.chat, ""
            return None, "that invite is for a channel I'm not in - add the account first"
        name = line.lstrip("@")
        if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{3,}", name) or "t.me/" in line:
            name = re.sub(r".*t\.me/", "", line).strip("/") if "t.me/" in line else name
            return await core.get_entity(name), ""
    except (ValueError, RPCError) as exc:
        return None, str(exc)
    return None, "not a channel link or id"


async def on_owner_text(event: events.NewMessage.Event) -> None:
    if not event.is_private or event.sender_id != config.OWNER_ID:
        return
    text = event.raw_text or ""
    if text.startswith(("/", ".")):
        return  # commands handled by the specific handlers
    added, failed = [], []
    for line in text.splitlines():
        if not line.strip():
            continue
        entity, why = await resolve_target(line)
        if entity is None:
            failed.append(f"❌ {line.strip()}: {why or 'could not resolve'}")
            continue
        if not isinstance(entity, types.Channel) or not getattr(entity, "broadcast", False):
            failed.append(f"❌ {line.strip()}: not a broadcast channel")
            continue
        ok, reason = await core.connect_channel(entity)
        if ok:
            added.append(entity)
        else:
            failed.append(f"❌ {entity.title}: {reason}")
        await asyncio.sleep(config.REQUEST_GAP)

    if added:
        store.awaiting_links = False
        await store.save()
        result = await core.refresh_post()
        lines = [f"✅ {e.title} — loaded" for e in added] + failed
        lines.append(f"Pool: {len(store.online())} active · Post: {result}")
        if store.post is None:
            lines.append("No redirect post yet - post .set in the target channel.")
        await event.reply("\n".join(lines))
        if store.online():
            await notify(texts.pool_restored())
    elif failed and (store.awaiting_links or len(failed) == 1):
        await event.reply("\n".join(failed))


# -------------------------------------------------------------------- startup
def register_handlers() -> None:
    tg.add_event_handler(on_set, events.NewMessage(pattern=SET_RE))
    tg.add_event_handler(on_participant, events.Raw(types.UpdateChannelParticipant))

    tg.add_event_handler(cmd_help, events.NewMessage(pattern=r"(?i)^/(start|help)\s*$"))
    tg.add_event_handler(cmd_status, events.NewMessage(pattern=r"(?i)^/status\s*$"))
    tg.add_event_handler(cmd_channels, events.NewMessage(pattern=r"(?i)^/channels\s*$"))
    tg.add_event_handler(cmd_scan, events.NewMessage(pattern=r"(?i)^/scan\s*$"))
    tg.add_event_handler(cmd_sync, events.NewMessage(pattern=r"(?i)^/sync\s*$"))
    tg.add_event_handler(cmd_load, events.NewMessage(pattern=r"(?i)^/load\s*$"))
    tg.add_event_handler(cmd_cancel, events.NewMessage(pattern=r"(?i)^/cancel\s*$"))
    tg.add_event_handler(cmd_refresh, events.NewMessage(pattern=r"(?i)^/refresh\s*$"))
    tg.add_event_handler(cmd_unset, events.NewMessage(pattern=r"(?i)^/unset\s*$"))
    tg.add_event_handler(cmd_slots, events.NewMessage(pattern=r"(?i)^/slots\s+(\d+)\s*$"))
    tg.add_event_handler(cmd_remove, events.NewMessage(pattern=r"(?i)^/remove\s+(-?\d+)\s*$"))
    tg.add_event_handler(cmd_active, events.NewMessage(pattern=r"(?i)^/active\s+(-?\d+)\s*$"))
    tg.add_event_handler(cmd_clearall, events.NewMessage(pattern=r"(?i)^\.clearall(\s+confirm)?\s*$"))
    tg.add_event_handler(on_owner_text, events.NewMessage())  # owner DM checked inside


async def main() -> None:
    global tg
    if not config.API_ID or not config.API_HASH:
        raise SystemExit("API_ID / API_HASH missing - copy .env.example to .env and fill it in.")
    if not config.OWNER_ID:
        raise SystemExit("OWNER_ID missing - copy .env.example to .env and fill it in.")
    claim_single_instance()

    tg = TelegramClient(str(config.BASE_DIR / config.SESSION_NAME),
                        config.API_ID, config.API_HASH)
    register_handlers()

    await tg.start()  # first run asks for phone + login code interactively
    me = await tg.get_me()
    core.bind(tg, me.id)
    log.info("logged in as @%s (%s)", me.username, me.id)

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
