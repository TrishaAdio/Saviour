"""Saviour - a redirect keeper that is one Telegram user account, nothing else.

How it works, start to finish:

  1. You add this account to a backup channel and promote it to admin.
     It notices on the spot and DMs you "channel synced".
  2. You add it to your public redirect channel and post ".set" there.
     If it isn't admin yet it quietly remembers and waits; the moment you
     promote it, the post goes up and it tells you.
  3. From then on, every few minutes it writes a random bio into each pooled
     channel. That keeps the channels looking alive and proves the account
     can still act in them. A channel whose bio can't be written gets a
     brand new invite link.
  4. If anything claims a channel is deleted, that is not believed until the
     account has also failed to read its info, failed to rename it, and
     failed to post-and-delete in it. Only then does a strike count, and
     only after STRIKES rounds in a row does the channel leave the post -
     parked, never deleted, and re-checked for ever.

Start it: python3 saviour.py   (setup.py does the login for you first)
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
import engine
from engine import DEAD, OK, RELINKED, UNSURE
from state import state

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[logging.FileHandler(config.LOG, encoding="utf-8"),
              logging.StreamHandler()],
)
log = logging.getLogger("saviour")

tg = None
_lockfile = None
# One round at a time: /scan firing while the timer runs would have two
# rounds fighting over the same strike counters. Created in start(), inside
# the running loop (on Python 3.8/3.9 an import-time Lock binds the wrong one).
_round = None


def only_one_copy():
    """Two copies sharing one session corrupt it. Refuse to be the second."""
    global _lockfile
    _lockfile = open(config.HERE / "saviour.lock", "a+")
    try:
        fcntl.flock(_lockfile, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        _lockfile.seek(0)
        raise SystemExit(
            f"Already running (PID {(_lockfile.read() or '?').strip()}).\n"
            "Stop it first:  pkill -f saviour.py ; systemctl stop saviour")
    _lockfile.seek(0)
    _lockfile.truncate()
    _lockfile.write(str(os.getpid()))
    _lockfile.flush()


# ----------------------------------------------------------------- talking
# Always plain text: channel titles and invite codes contain "_" and "*",
# which Telegram's markdown would mangle or reject outright.
async def tell(text):
    """Send the owner a message."""
    try:
        await tg.send_message(config.OWNER_ID, text, parse_mode=None,
                              link_preview=False)
    except (ValueError, RPCError, *engine.HICCUP) as exc:
        log.warning("could not DM the owner (%s): %s", engine.brief(exc),
                    text.replace("\n", " | "))


async def answer(event, text):
    try:
        return await event.reply(text, parse_mode=None, link_preview=False)
    except (RPCError, *engine.HICCUP) as exc:
        log.warning("could not reply: %s", engine.brief(exc))


async def rewrite(message, text):
    if message is None:
        return
    try:
        await message.edit(text, parse_mode=None, link_preview=False)
    except (RPCError, *engine.HICCUP):
        pass


def describe(channel):
    mark = "🔴 offline" if channel.get("offline") else "🟢 online"
    line = f"{mark}  {channel['title']}  ({channel['id']})"
    if channel.get("offline") and channel.get("reason"):
        line += f"\n        {channel['reason']}"
    return line


async def ask_for_links_if_empty():
    """Nag the owner exactly once when the pool has nothing live left."""
    if state.channels and not state.live():
        if not state.d["begging"]:
            state.d["begging"] = True
            await state.save()
            await tell("🆘 No working links left — the redirect post has nothing "
                       "to point at.\n\nSend me channel links here (one per line: "
                       "@name, t.me/name, a +invite or a -100 id) and I'll check "
                       "and load them right away.")
    elif state.live() and state.d["begging"]:
        state.d["begging"] = False
        await state.save()


# -------------------------------------------------------------- ".set" post
SET = re.compile(r"^\s*\.set(?:\s+(\d+))?\s*$")


async def try_post(chat_id, slots):
    """Put the redirect post up if we're allowed to. Tells the owner if so."""
    done, why = await engine.put_post(chat_id, slots)
    if not done:
        log.info("can't post in %s yet: %s", chat_id, why)
        return False
    # A channel can't redirect to itself.
    if state.get(chat_id):
        state.drop(chat_id)
        await state.save()
        log.info("took %s out of the pool: it holds the post now", chat_id)
    await tell(f"✅ The redirect post is up in {await name_of(chat_id)} ({chat_id})\n"
               f"It shows {slots} link line(s) and will keep itself updated.")
    await engine.redraw_post()
    return True


async def name_of(chat_id):
    try:
        return (await engine.entity_of(chat_id)).title or str(chat_id)
    except (ValueError, RPCError, *engine.HICCUP):
        return str(chat_id)


async def on_set(event):
    # Only ".set" inside a broadcast channel counts. Channel posts are
    # normally anonymous - the author shows up as the channel itself - so the
    # sender can't be checked. That's fine: only admins can post at all.
    if not event.is_channel or event.is_group:
        return
    if not getattr(event.message, "post", False) \
            and event.sender_id not in (config.OWNER_ID, engine.ME):
        return
    chat_id = event.chat_id
    slots = int(event.pattern_match.group(1) or config.SLOTS)
    slots = slots if 1 <= slots <= 20 else config.SLOTS

    # Remember it before anything else, so a restart can't lose the request.
    state.waiting_set = {"chat": chat_id, "slots": slots}
    await state.save()
    try:
        await event.delete()
    except (RPCError, *engine.HICCUP):
        pass

    if await try_post(chat_id, slots):
        return
    await tell(f"⏳ I saw your .set in {await name_of(chat_id)} ({chat_id}) but I'm "
               "not admin there yet.\nPromote me and the post goes up by itself — "
               "I'll keep trying in the background.")


# --------------------------------------------------- promoted / demoted / joined
def is_admin_role(role):
    return isinstance(role, (types.ChannelParticipantAdmin,
                             types.ChannelParticipantCreator))


async def on_membership(update):
    """Telegram telling us our own role in a channel just changed."""
    if getattr(update, "user_id", None) != engine.ME:
        return
    chat_id = int(f"-100{update.channel_id}")
    now, before = update.new_participant, update.prev_participant
    promoted = is_admin_role(now) and not is_admin_role(before)
    demoted = is_admin_role(before) and not is_admin_role(now)
    joined = before is None and isinstance(
        now, (types.ChannelParticipantSelf, types.ChannelParticipant))

    entity, title = None, str(chat_id)
    try:
        entity = await engine.entity_of(chat_id)
        title = getattr(entity, "title", title) or title
    except (ValueError, RPCError, *engine.HICCUP) as exc:
        log.info("role change in %s but it won't open: %s", chat_id, engine.brief(exc))

    # Joined, no rights yet: say so and wait. Nothing else happens.
    if joined and not promoted:
        await tell(f"👀 I'm in {title} ({chat_id}) but not an admin.\n"
                   "Promote me and I'll take it from there.")
        return

    if promoted:
        # The redirect channel: finish the ".set" that was waiting.
        if state.is_post_channel(chat_id):
            waiting = state.waiting_set
            if waiting and not state.post:
                await try_post(chat_id, waiting["slots"])
            return
        if entity is None:
            return
        try:
            rights = await engine.rights_in(entity)
        except (RPCError, *engine.HICCUP) as exc:
            log.info("can't read rights in %s: %s", chat_id, engine.brief(exc))
            return
        if rights is not None and not getattr(rights, "invite_users", False):
            await tell(f'🟠 {title} ({chat_id}) still needs the "Invite Users via '
                       'Link" right.\nTick it and I\'ll add the channel on my own.')
            return
        added, why = await engine.adopt(entity)
        if not added:
            log.info("promotion in %s didn't pool it: %s", title, why)
            return
        channel = state.get(chat_id)
        await tell(f"🟢 Channel synced: {title}\n"
                   f"ID: {chat_id}\n"
                   f"Invite: {channel['link'] if channel else '?'}\n"
                   f"Bio updates: {'on' if getattr(rights, 'change_info', False) else 'off (no Change Info right)'}\n"
                   f"Pool: {len(state.live())} live")
        await ask_for_links_if_empty()
        await engine.redraw_post()
        return

    if demoted:
        if state.post and state.post["chat"] == chat_id:
            state.forget_post()
            await state.save()
            await tell(f"🔴 I lost my rights in {title} ({chat_id}), so the redirect "
                       "post is gone.\nFix my rights and post .set there again.")
            return
        if state.get(chat_id):
            state.park(chat_id, "we were demoted")
            await state.save()
            result = await engine.redraw_post()
            await tell(f"🔴 Demoted in {title} ({chat_id}) — parked it and updated "
                       f"the post ({result}).\nRe-promote me and it comes back by itself.")
            await ask_for_links_if_empty()


# ------------------------------------------------------------- health rounds
async def round_now():
    async with _round:
        await one_round()


async def one_round():
    # A ".set" whose promotion we never heard about is retried for ever.
    waiting = state.waiting_set
    if waiting and not state.post:
        await try_post(waiting["chat"], waiting["slots"])

    if not state.channels:
        await ask_for_links_if_empty()
        return

    parked, relinked, back, warned, unclear = [], [], [], [], 0
    for channel in list(state.channels):
        try:
            verdict, note = await engine.check(channel)
        except Exception as exc:          # a broken channel can't end a round
            log.exception("checking %s blew up", channel["id"])
            verdict, note = UNSURE, f"the check crashed ({engine.brief(exc)})"
        was_off = bool(channel.get("offline"))

        if verdict in (OK, RELINKED):
            channel["strikes"] = 0
            channel.pop("reason", None)
            if was_off:
                state.revive(channel["id"])
                back.append(channel)
            if verdict == RELINKED:
                relinked.append((channel, note))
        elif verdict == UNSURE:
            unclear += 1
            log.info("%s unverified this round: %s", channel["id"], note)
        else:                              # DEAD
            channel["strikes"] = int(channel.get("strikes", 0)) + 1
            channel["reason"] = note
            log.warning("%s looks dead (%s/%s): %s", channel["id"],
                        channel["strikes"], config.STRIKES, note)
            if not was_off:
                if channel["strikes"] >= config.STRIKES:
                    state.park(channel["id"], note)
                    parked.append(channel)
                elif channel["strikes"] == 1:
                    warned.append(channel)
        await asyncio.sleep(config.GAP)

    state.d["checked"] = int(time.time())
    await state.save()
    result = await engine.redraw_post()
    log.info("round done: %s live, %s parked | newly parked %s, relinked %s, "
             "recovered %s, unverified %s | post %s",
             len(state.live()), len(state.dead()), len(parked), len(relinked),
             len(back), unclear, result)

    for channel in warned:
        await tell(f"🟠 {channel['title']} failed a check "
                   f"(1/{config.STRIKES}): {channel.get('reason')}\n"
                   "Nothing has changed yet — I'm watching it.")
    if parked:
        await tell("🔴 Taken out of the post after "
                   f"{config.STRIKES} failed checks in a row:\n"
                   + "\n".join(f"• {c['title']} — {c.get('reason')}" for c in parked)
                   + f"\n\nPool: {len(state.live())} live. They stay in my list and "
                     "come back on their own if they recover.")
    for channel in back:
        await tell(f"🟢 {channel['title']} is working again — back in the post.")
    for channel, note in relinked:
        await tell(f"🔁 {channel['title']} got a new invite link ({note}):\n"
                   f"{channel['link']}")
    if result == "reposted":
        await tell("♻️ The redirect post had been deleted, so I put a new one up.")
    elif result == "post-deleted":
        await tell("🔴 The redirect post was deleted. Post .set again when ready.")

    await ask_for_links_if_empty()


async def health_loop():
    await asyncio.sleep(20)
    while True:
        try:
            await round_now()
        except Exception:
            log.exception("health round crashed")
        await asyncio.sleep(config.CHECK_EVERY)


async def look_for_channels(quiet=False):
    """Catch promotions whose live update we missed (restart, dropped update)."""
    found = await engine.discover()
    fresh = [e for kind, e in found if kind == "pooled"]
    nores = [e for kind, e in found if kind == "needs-invite-right"]
    if not quiet:
        for entity in fresh:
            cid = engine.peer(entity)
            channel = state.get(cid)
            rights = engine.cached_rights(entity)
            await tell(f"🟢 Channel synced: {entity.title}\n"
                       f"ID: {cid}\n"
                       f"Invite: {channel['link'] if channel else '?'}\n"
                       f"Bio updates: {'on' if getattr(rights, 'change_info', False) else 'off (no Change Info right)'}\n"
                       f"Pool: {len(state.live())} live")
        for entity in nores:
            await tell(f'🟠 {entity.title} ({engine.peer(entity)}) needs the '
                       '"Invite Users via Link" right before I can use it.')
    if fresh:
        await ask_for_links_if_empty()
        await engine.redraw_post()
    return fresh, nores


async def sync_loop():
    if config.SYNC_EVERY <= 0:
        return
    await asyncio.sleep(40)
    while True:
        try:
            await look_for_channels()
        except Exception:
            log.exception("channel search crashed")
        await asyncio.sleep(config.SYNC_EVERY)


# ---------------------------------------------------------------- commands
def may_command(event):
    """Who is allowed to give commands, and where.

    - OWNER_ID, in its private chat with this account (the normal way), or
    - this account itself, typing in any private chat or Saved Messages
      (the usual userbot habit - the message is outgoing, so sender == us).
    """
    if not event.is_private:
        return False
    return event.out or event.sender_id in (config.OWNER_ID, engine.ME)


def owner(func):
    @functools.wraps(func)
    async def guard(event):
        if not may_command(event):
            # Used to be silent, which looked exactly like "commands are
            # broken". Say in the log who tried, so a wrong OWNER_ID shows.
            if event.is_private:
                log.warning("ignored %r from user %s: only OWNER_ID=%s (or this "
                            "account itself) may give commands. Fix OWNER_ID in "
                            ".env if that was you.", (event.raw_text or "")[:40],
                            event.sender_id, config.OWNER_ID)
            return
        try:
            await func(event)
        except Exception as exc:
            log.exception("/%s failed", func.__name__)
            await answer(event, f"⚠️ That didn't work: {engine.brief(exc)}\n"
                                "I'm still running.")
    return guard


@owner
async def cmd_help(event):
    await answer(event,
                 "Saviour — your commands (this chat only):\n\n"
                 "/status      pool, post, last check\n"
                 "/channels    list every channel I know\n"
                 "/scan        check everything right now\n"
                 "/find        look for channels I'm admin in\n"
                 "/links       show every invite link\n"
                 "/use <id>    show this channel in the post\n"
                 "/slots <n>   how many link lines the post has\n"
                 "/redraw      rewrite the post now\n"
                 "/forget <id> drop a channel from my list\n"
                 "/unset       forget the redirect post\n"
                 ".clearall    wipe the account (asks first)\n\n"
                 "In a channel: post .set (or .set 3) to claim the redirect post.\n"
                 "Paste channel links here any time to add them.")


def when(ts):
    if not ts:
        return "never"
    gap = max(int(time.time()) - ts, 0)
    if gap < 60:
        return f"{gap}s ago"
    if gap < 3600:
        return f"{gap // 60}m ago"
    return f"{gap // 3600}h ago"


@owner
async def cmd_status(event):
    post = state.post
    lines = ["📊 Status",
             f"Pool: {len(state.live())} live · {len(state.dead())} parked",
             f"Post: {'channel ' + str(post['chat']) if post else 'not set'}"]
    if not post and state.waiting_set:
        lines.append(f"      waiting for admin rights in {state.waiting_set['chat']}")
    lines += [f"Lines: {state.slots()}", f"Last check: {when(state.d.get('checked'))}"]
    chosen = state.active_channel()
    if chosen:
        lines.append(f"Showing: {chosen['title']}")
    if state.channels:
        lines += [""] + [describe(c) for c in state.channels]
    await answer(event, "\n".join(lines))


@owner
async def cmd_channels(event):
    if not state.channels:
        await answer(event, "Nothing yet. Promote me in a channel and it shows up here.")
        return
    await answer(event, "\n".join(describe(c) for c in state.channels))


@owner
async def cmd_links(event):
    if not state.channels:
        await answer(event, "No channels yet.")
        return
    await answer(event, "\n\n".join(
        f"{c['title']} ({c['id']})\n{c['link']}" for c in state.channels))


@owner
async def cmd_scan(event):
    if _round.locked():
        await answer(event, "A check is already running — hold on.")
        return
    await answer(event, "🔍 Checking every channel now…")
    await round_now()
    await cmd_status(event)


@owner
async def cmd_find(event):
    await answer(event, "🔎 Going through my chats…")
    fresh, nores = await look_for_channels(quiet=True)
    lines = [f"🟢 Synced: {e.title}" for e in fresh]
    lines += [f'🟠 Needs "Invite Users via Link": {e.title}' for e in nores]
    await answer(event, "\n".join(lines) if lines else "Nothing new found.")


@owner
async def cmd_redraw(event):
    await answer(event, f"Post: {await engine.redraw_post()}")


@owner
async def cmd_unset(event):
    state.forget_post()
    state.waiting_set = None
    await state.save()
    await answer(event, "Forgotten. Post .set in a channel to claim a new post.")


@owner
async def cmd_slots(event):
    raw = (event.pattern_match.group(1) or "").strip()
    if not raw:
        await answer(event, f"Usage: /slots <1-20>   (now {state.slots()})")
        return
    want = int(raw)
    if not 1 <= want <= 20:
        await answer(event, "Pick a number between 1 and 20.")
        return
    if state.post:
        state.post["slots"] = want
        await state.save()
        await answer(event, f"{want} line(s). Post: {await engine.redraw_post()}")
        return
    config.SLOTS = want
    await answer(event, f"The next .set will use {want} line(s).")


@owner
async def cmd_forget(event):
    raw = (event.pattern_match.group(1) or "").strip()
    if not raw:
        await answer(event, "Usage: /forget <-100 channel id>   (see /channels)")
        return
    channel = state.get(int(raw))
    if not channel:
        await answer(event, f"{raw} isn't in my list.")
        return
    state.drop(int(raw))
    await state.save()
    await answer(event, f"🗑 Dropped {channel['title']}. "
                        f"Post: {await engine.redraw_post()}")
    await ask_for_links_if_empty()


@owner
async def cmd_use(event):
    raw = (event.pattern_match.group(1) or "").strip()
    if not raw:
        chosen = state.active_channel()
        await answer(event, "Usage: /use <-100 channel id>   (now "
                            f"{chosen['title'] if chosen else 'nothing'})")
        return
    channel = state.get(int(raw))
    if not channel:
        await answer(event, f"{raw} isn't in my list.")
        return
    if channel.get("offline"):
        await answer(event, f"{channel['title']} is parked — pick a live one, or "
                            "/scan to re-check it.")
        return
    state.d["active"] = int(raw)
    await state.save()
    await answer(event, f"⭐ Showing {channel['title']}. "
                        f"Post: {await engine.redraw_post()}")


# ---------------------------------------------------------------- .clearall
@owner
async def cmd_clearall(event):
    if not event.pattern_match.group(1):
        await answer(event,
                     "🧨 .clearall empties this account completely:\n"
                     "• deletes every group and channel it created\n"
                     "• leaves every other group and channel, including the whole "
                     "link pool and the redirect channel\n"
                     "• blocks every private chat and wipes its history\n\n"
                     "Only this chat and Saved Messages survive, and my link pool "
                     "and post are reset.\n\nThere is no undo. "
                     "Send  .clearall confirm  to go ahead.")
        return
    note = await answer(event, "🧨 Clearing the account… this can take a while.")
    tally = {"gone": 0, "left": 0, "blocked": 0, "failed": 0}
    try:
        chats = [d async for d in tg.iter_dialogs()]
    except (RPCError, *engine.HICCUP) as exc:
        await rewrite(note, f"🧨 Couldn't list the chats: {engine.brief(exc)}")
        return

    for i, chat in enumerate(chats, 1):
        entity = chat.entity
        try:
            if isinstance(entity, types.User):
                if entity.id in (engine.ME, config.OWNER_ID):
                    continue           # Saved Messages, and this very chat
                await engine.call(lambda e=entity: tg(
                    functions.messages.DeleteHistoryRequest(
                        e, max_id=0, just_clear=False, revoke=True)), "wipe chat")
                await engine.call(lambda e=entity: tg(
                    functions.contacts.BlockRequest(e)), "block")
                tally["blocked"] += 1
            elif isinstance(entity, types.Chat):
                if getattr(entity, "creator", False):
                    await engine.call(lambda e=entity: tg(
                        functions.messages.DeleteChatRequest(e.id)), "delete group")
                    tally["gone"] += 1
                else:
                    await engine.call(lambda e=entity: tg(
                        functions.messages.DeleteChatUserRequest(
                            e.id, types.InputUserSelf())), "leave group")
                    tally["left"] += 1
            elif isinstance(entity, types.Channel):
                if getattr(entity, "creator", False):
                    await engine.call(lambda e=entity: tg(
                        functions.channels.DeleteChannelRequest(e)), "delete channel")
                    tally["gone"] += 1
                else:
                    await engine.call(lambda e=entity: tg(
                        functions.channels.LeaveChannelRequest(e)), "leave channel")
                    tally["left"] += 1
            else:
                continue
        except Exception as exc:
            tally["failed"] += 1
            log.warning("clearall failed on %s: %s", getattr(entity, "id", "?"),
                        engine.brief(exc))
        if i % 5 == 0:
            await rewrite(note, f"🧨 Clearing… {i}/{len(chats)} chats done.")
        await asyncio.sleep(config.GAP)

    state.d["channels"] = []
    state.forget_post()
    state.waiting_set = None
    state.d["active"] = None
    state.d["begging"] = False
    await state.save()
    await rewrite(note,
                  "🧨 Done.\n"
                  f"• Deleted (mine): {tally['gone']}\n"
                  f"• Left: {tally['left']}\n"
                  f"• Chats blocked and wiped: {tally['blocked']}\n"
                  f"• Failed: {tally['failed']}\n\n"
                  "My pool and post are reset. This chat and Saved Messages were "
                  "kept on purpose — I'm still running, so add me to channels "
                  "whenever you want to start again.")


# -------------------------------------------------------- links pasted in DM
# A line is only treated as a channel if it looks like one; otherwise normal
# chatter in this DM would get answered with "that's not a channel".
LOOKS_LIKE = re.compile(r"^(?:@[A-Za-z][A-Za-z0-9_]{3,}"
                        r"|(?:https?://)?t\.me/\S+"
                        r"|-100\d{5,})$")


async def find_channel(line):
    """Turn one pasted line into a channel entity, or (None, why not)."""
    try:
        if re.fullmatch(r"-100\d{5,}", line):
            return await engine.entity_of(int(line)), ""
        code = engine.invite_code(line)
        if code:
            answer_ = await engine.call(
                lambda: tg(functions.messages.CheckChatInviteRequest(code)),
                "check invite")
            if isinstance(answer_, types.ChatInviteAlready):
                return answer_.chat, ""
            return None, "I haven't joined that one — add me to it first"
        name = re.sub(r"^(?:https?://)?t\.me/", "", line).strip("/").lstrip("@")
        return (await engine.entity_of(name), "") if name else (None, "not a channel")
    except (ValueError, RPCError, *engine.HICCUP) as exc:
        return None, engine.brief(exc)


async def on_dm(event):
    if not event.is_private or event.sender_id != config.OWNER_ID or event.out:
        return
    text = event.raw_text or ""
    if text.startswith(("/", ".")):
        return                              # commands have their own handlers
    lines = [l.strip() for l in text.splitlines() if LOOKS_LIKE.match(l.strip())]
    if not lines:
        return                              # ordinary chat: stay quiet

    good, bad = [], []
    for line in lines:
        entity, why = await find_channel(line)
        if entity is None:
            bad.append(f"❌ {line}: {why}")
        else:
            added, why = await engine.adopt(entity)
            if added:
                good.append(entity)
            else:
                bad.append(f"❌ {getattr(entity, 'title', line)}: {why}")
        await asyncio.sleep(config.GAP)

    report = [f"🟢 {e.title} — loaded" for e in good] + bad
    if good:
        result = await engine.redraw_post()
        report.append(f"Pool: {len(state.live())} live · Post: {result}")
        if not state.post and not state.waiting_set:
            report.append("No redirect post yet — post .set in your public channel.")
        await ask_for_links_if_empty()
    await answer(event, "\n".join(report))


# ------------------------------------------------------------------- start
def hook_up():
    tg.add_event_handler(on_set, events.NewMessage(pattern=SET))
    tg.add_event_handler(on_membership, events.Raw(types.UpdateChannelParticipant))
    for handler, pattern in (
        (cmd_help, r"(?i)^/(?:start|help)\s*$"),
        (cmd_status, r"(?i)^/status\s*$"),
        (cmd_channels, r"(?i)^/channels\s*$"),
        (cmd_links, r"(?i)^/links\s*$"),
        (cmd_scan, r"(?i)^/scan\s*$"),
        (cmd_find, r"(?i)^/find\s*$"),
        (cmd_redraw, r"(?i)^/redraw\s*$"),
        (cmd_unset, r"(?i)^/unset\s*$"),
        (cmd_slots, r"(?i)^/slots(?:\s+(\d+))?\s*$"),
        (cmd_forget, r"(?i)^/forget(?:\s+(-?\d+))?\s*$"),
        (cmd_use, r"(?i)^/use(?:\s+(-?\d+))?\s*$"),
        (cmd_clearall, r"(?i)^\.clearall(\s+confirm)?\s*$"),
    ):
        tg.add_event_handler(handler, events.NewMessage(pattern=pattern))
    tg.add_event_handler(on_dm, events.NewMessage())


async def start():
    global tg, _round
    _round = asyncio.Lock()
    if not config.API_ID or not config.API_HASH:
        raise SystemExit("API_ID / API_HASH missing — run: python3 setup.py")
    if not config.OWNER_ID:
        raise SystemExit("OWNER_ID missing — run: python3 setup.py")
    only_one_copy()

    tg = TelegramClient(str(config.HERE / config.SESSION), config.API_ID, config.API_HASH)
    hook_up()
    await tg.start()
    me = await tg.get_me()
    engine.bind(tg, me.id)
    log.info("signed in as @%s (%s)", me.username, me.id)
    if me.id == config.OWNER_ID:
        log.warning("OWNER_ID is this same account: notifications land in Saved "
                    "Messages and pasted links are ignored. Use your other account.")
    else:
        # A bare numeric id can only be messaged once Telethon has seen that
        # user. A fresh session (or a moved one) hasn't, so every DM to the
        # owner used to fail with "Could not find the input entity". Loading
        # the chat list once fills the cache.
        try:
            await tg.get_input_entity(config.OWNER_ID)
        except ValueError:
            try:
                await tg.get_dialogs(limit=None)
                await tg.get_input_entity(config.OWNER_ID)
            except (ValueError, RPCError, *engine.HICCUP):
                log.warning("can't reach OWNER_ID=%s yet - send this account any "
                            "message from that account once.", config.OWNER_ID)
    log.info("commands accepted from OWNER_ID=%s and from this account itself",
             config.OWNER_ID)

    asyncio.create_task(health_loop())
    asyncio.create_task(sync_loop())
    await tell(f"✅ Saviour is up as @{me.username or me.id}\n"
               f"Pool: {len(state.channels)} channel(s) · "
               f"Post: {'set' if state.post else 'not set'}\n"
               "Send /help for the command list.")
    log.info("watching…")
    await tg.run_until_disconnected()


if __name__ == "__main__":
    try:
        asyncio.run(start())
    except KeyboardInterrupt:
        sys.exit(0)
