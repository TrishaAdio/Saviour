"""Saviour - a redirect keeper that is one Telegram user account, nothing else.

How it works, start to finish:

  1. You add this account to a backup channel and promote it to admin.
     It notices on the spot and DMs you "channel synced".
  2. You add it to your public redirect channel and post ".set" there.
     If it isn't admin yet it quietly remembers and waits; the moment you
     promote it, the post goes up and it tells you.
  3. Every 15 minutes (or on ".check") it posts a "." in each channel and
     deletes it at once. Nothing else is ever changed - no bio, no name.
  4. Posted -> alive. Refused -> the channel is gone: it leaves the post
     right away and the post shows the next working channel. Parked
     channels keep being tested and come back by themselves.

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
import ui
from engine import GONE_FOR_SURE, OK, RELINKED, UNSURE
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
# Every message is HTML built by ui.py, which escapes all outside values -
# channel titles and invite codes contain "_", "*" and "<". A list of strings
# is sent as several messages (long lists are split into pages).
def _plain(text):
    return ui.HTML.parse(text)[0].replace("\n", " | ")


async def tell(text):
    """Send the owner a message (or several, given a list)."""
    for part in (text if isinstance(text, list) else [text]):
        try:
            await tg.send_message(config.OWNER_ID, part, parse_mode=ui.HTML,
                                  link_preview=False)
        except (ValueError, RPCError, *engine.HICCUP) as exc:
            log.warning("could not DM the owner (%s): %s", engine.brief(exc),
                        _plain(part))


async def answer(event, text):
    sent = None
    for part in (text if isinstance(text, list) else [text]):
        try:
            sent = await event.reply(part, parse_mode=ui.HTML, link_preview=False)
        except (RPCError, *engine.HICCUP) as exc:
            log.warning("could not reply: %s", engine.brief(exc))
    return sent


async def rewrite(message, text):
    if message is None:
        return
    try:
        await message.edit(text, parse_mode=ui.HTML, link_preview=False)
    except (RPCError, *engine.HICCUP):
        pass


def synced_card(title, chat_id, channel, rights):
    can_post = ("yes" if rights is None or getattr(rights, "post_messages", False)
                else "no " + ui.i('(needed for the "." test - it would count as deleted)'))
    return ui.card("🟢", "Channel synced", [
        ui.kv("Name", ui.esc(title)),
        ui.kv("ID", ui.code(chat_id)),
        ui.kv("Invite", ui.esc(channel["link"]) if channel else "?"),
        ui.kv("Post right", can_post),
        ui.kv("Pool", f"{len(state.live())} live"),
    ])


def channel_list(title, channels, foot=None):
    """A channel list as one message per page."""
    active = (state.active_channel() or {}).get("id")
    chunks = ui.pages(channels)
    return [ui.card("📡", ui.page_title(title, n, len(chunks)),
                    text=ui.quote([ui.channel_line(c, active) for c in chunk]),
                    foot=foot if n == len(chunks) else None)
            for n, chunk in enumerate(chunks, 1)]


async def ask_for_links_if_empty():
    """Nag the owner exactly once when the pool has nothing live left."""
    if state.channels and not state.live():
        if not state.d["begging"]:
            state.d["begging"] = True
            await state.save()
            await tell(ui.card(
                "🆘", "No working links left",
                text="The redirect post has nothing to point at.\n\n"
                     "Send me channel links here, one per line:",
                extra=[ui.quote([ui.code("@name"), ui.code("t.me/name"),
                                 ui.code("t.me/+invite"), ui.code("-100…")])],
                foot="I'll check them and load them right away."))
    elif state.live() and state.d["begging"]:
        state.d["begging"] = False
        await state.save()


# -------------------------------------------------------------- ".set" post
# Three ways to use it, all posted in the redirect channel:
#   .set              as a reply to a post -> take that post over, keep its look
#   .set / .set 3     with a post already there -> keep editing that one
#   .set / .set 3     no post yet -> publish a new default post
SET = re.compile(r"^\s*\.set(?:\s+(\d+))?\s*$")


async def try_post(chat_id, slots, reply=None):
    """Put the redirect post up (or take one over) if we're allowed to.

    Tells the owner when it worked. False = not allowed yet, keep waiting.
    """
    old = state.post
    if reply:
        done, why, slots = await engine.adopt_post(chat_id, reply)
    else:
        done, why = await engine.put_post(chat_id, slots)
    if not done:
        log.info("can't post in %s yet: %s", chat_id, why)
        if reply and why in ("that post is gone", "that post has no text",
                             "that post has no t.me links to keep updated"):
            # Not a rights problem: waiting won't fix it, so say so and stop.
            state.waiting_set = None
            await state.save()
            await tell(ui.card("❌", "Can't take over that post", [
                ui.kv("Channel", ui.esc(await name_of(chat_id))),
                ui.kv("Why", ui.i(why)),
            ], foot="Reply .set to a post that has your t.me links in it."))
            return True
        return False
    # A channel can't redirect to itself.
    if state.get(chat_id):
        state.drop(chat_id)
        await state.save()
        log.info("took %s out of the pool: it holds the post now", chat_id)

    # Taking over a post replaces the one we made ourselves in the same
    # channel - leaving it would show two redirect posts.
    removed = False
    if reply and old and old["chat"] == chat_id and old["msg"] != reply \
            and not old.get("template"):
        removed = await engine.delete_message(chat_id, old["msg"])

    result = await engine.redraw_post()
    rows = [ui.kv("Channel", ui.esc(await name_of(chat_id))),
            ui.kv("ID", ui.code(chat_id))]
    if reply:
        rows += [ui.kv("Look", "your own post, kept exactly as it is"),
                 ui.kv("Links", f"{slots} position(s), swapped automatically")]
        if removed:
            rows.append(ui.kv("Old post", "🗑 deleted (it was a duplicate)"))
    else:
        rows.append(ui.kv("Lines", str(slots)))
    if result.startswith("error"):
        rows.append(ui.kv("Post", ui.post_result(result)))
    await tell(ui.card("✅", "Took over your post" if reply else "Redirect post is live",
                       rows, foot="It updates itself whenever the links change."))
    return True


async def keep_existing(chat_id, slots, asked_slots):
    """A plain .set where our post already is: edit that one, don't add another."""
    post = state.post
    if asked_slots and not post.get("template"):
        post["slots"] = slots
        await state.save()
    result = await engine.redraw_post()
    if result.startswith("error") or result in ("post-deleted", "none"):
        return False                     # it's really gone: make a new one
    await tell(ui.card("✏️", "Kept your existing post", [
        ui.kv("Channel", ui.esc(await name_of(chat_id))),
        ui.kv("Post", ui.post_result(result)),
        ui.kv("Lines", str(state.slots())),
    ], text="A redirect post is already up here, so I updated it instead of "
            "posting a second one.",
        foot="To take over a different post, reply .set to it."))
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
    asked = event.pattern_match.group(1)
    slots = int(asked or config.SLOTS)
    slots = slots if 1 <= slots <= 20 else config.SLOTS
    reply = event.message.reply_to_msg_id

    try:
        await event.delete()
    except (RPCError, *engine.HICCUP):
        pass

    if not reply and state.post and state.post["chat"] == chat_id:
        if await keep_existing(chat_id, slots, bool(asked)):
            return

    # Remember it before going on, so a restart can't lose the request.
    state.waiting_set = {"chat": chat_id, "slots": slots, "reply": reply}
    await state.save()
    if await try_post(chat_id, slots, reply):
        return
    await tell(ui.card("⏳", "Waiting for admin rights", [
        ui.kv("Channel", ui.esc(await name_of(chat_id))),
        ui.kv("ID", ui.code(chat_id)),
    ], text=f"I saw your {ui.code('.set')} but I can't "
            f"{'edit posts' if reply else 'post'} there yet.",
        foot=("Promote me with Post + Edit Messages rights and I'll take the post "
              "over by itself.") if reply else
             "Promote me and the post goes up by itself."))


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
        await tell(ui.card("👀", "Joined a channel", [
            ui.kv("Name", ui.esc(title)),
            ui.kv("ID", ui.code(chat_id)),
            ui.kv("Role", "member, not admin"),
        ], foot="Promote me and I'll take it from there."))
        return

    if promoted:
        # The redirect channel: finish the ".set" that was waiting.
        if state.is_post_channel(chat_id):
            waiting = state.waiting_set
            if waiting and (not state.post or waiting.get("reply")):
                await try_post(chat_id, waiting["slots"], waiting.get("reply"))
            return
        if entity is None:
            return
        try:
            rights = await engine.rights_in(entity)
        except (RPCError, *engine.HICCUP) as exc:
            log.info("can't read rights in %s: %s", chat_id, engine.brief(exc))
            return
        if rights is not None and not getattr(rights, "invite_users", False):
            await tell(ui.card("🟠", "One admin right missing", [
                ui.kv("Name", ui.esc(title)),
                ui.kv("ID", ui.code(chat_id)),
                ui.kv("Needs", ui.b("Invite Users via Link")),
            ], foot="Tick it and I'll add the channel on my own."))
            return
        added, why = await engine.adopt(entity)
        if not added:
            log.info("promotion in %s didn't pool it: %s", title, why)
            return
        channel = state.get(chat_id)
        await tell(synced_card(title, chat_id, channel, rights))
        await ask_for_links_if_empty()
        await engine.redraw_post()
        return

    if demoted:
        if state.post and state.post["chat"] == chat_id:
            state.forget_post()
            await state.save()
            await tell(ui.card("🔴", "Redirect post lost", [
                ui.kv("Channel", ui.esc(title)),
                ui.kv("ID", ui.code(chat_id)),
            ], text="I lost my admin rights there, so I can't keep the post.",
                foot="Fix my rights and post .set there again."))
            return
        if state.get(chat_id):
            was_shown = (state.active_channel() or {}).get("id") == chat_id
            state.park(chat_id, "we were demoted")
            await state.save()
            result = await engine.redraw_post()
            if was_shown:
                await tell_switch(state.get(chat_id), result)
            await tell(ui.card("🔴", "Demoted in a channel", [
                ui.kv("Name", ui.esc(title)),
                ui.kv("ID", ui.code(chat_id)),
                ui.kv("Now", "parked, out of the post"),
                ui.kv("Post", ui.post_result(result)),
            ], foot="Re-promote me and it comes back by itself."))
            await ask_for_links_if_empty()


# ------------------------------------------------------------- health rounds
async def tell_switch(old, result):
    """The channel in the post just left it: say which one shows now."""
    new = state.active_channel()
    if new is None:
        return                       # ask_for_links_if_empty() takes it from here
    await tell(ui.card("🔁", "Post switched to a working channel", [
        ui.kv("From", ui.esc(old["title"]) if old else "?"),
        ui.kv("To", ui.esc(new["title"])),
        ui.kv("Invite", ui.esc(new["link"])),
        ui.kv("Post", ui.post_result(result)),
    ]))


async def round_now():
    async with _round:
        await one_round()


async def one_round():
    # A ".set" whose promotion we never heard about is retried for ever.
    waiting = state.waiting_set
    if waiting and (not state.post or waiting.get("reply")):
        await try_post(waiting["chat"], waiting["slots"], waiting.get("reply"))

    if not state.channels:
        await ask_for_links_if_empty()
        return

    before = (state.active_channel() or {}).get("id")
    parked, gone, relinked, back, warned, unclear = [], [], [], [], [], 0
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
        elif verdict == GONE_FOR_SURE:
            # The dot test settled it: no strikes, no waiting.
            channel["strikes"] = config.STRIKES
            channel["reason"] = note
            log.warning("%s is gone: %s", channel["id"], note)
            if not was_off:
                state.park(channel["id"], note)
                gone.append(channel)
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
    old = state.get(before) if before else None
    switched = bool(old and old.get("offline"))
    result = await engine.redraw_post()
    log.info("round done: %s live, %s parked | newly parked %s, relinked %s, "
             "recovered %s, unverified %s | post %s",
             len(state.live()), len(state.dead()), len(parked) + len(gone), len(relinked),
             len(back), unclear, result)

    for channel in gone:
        await tell(ui.card("🔴", "Channel deleted — removed from the post", [
            ui.kv("Name", ui.esc(channel["title"])),
            ui.kv("ID", ui.code(channel["id"])),
            ui.kv("Why", ui.i(channel.get("reason") or "unknown")),
            ui.kv("Post", ui.post_result(result)),
            ui.kv("Pool", f"{len(state.live())} live"),
        ], foot="Kept in my list and re-tested every round — if it ever works "
                "again it comes back by itself."))
    if switched:
        await tell_switch(old, result)
    for channel in warned:
        await tell(ui.card("🟠", "A channel failed a check", [
            ui.kv("Name", ui.esc(channel["title"])),
            ui.kv("ID", ui.code(channel["id"])),
            ui.kv("Strike", f"1 / {config.STRIKES}"),
            ui.kv("Why", ui.i(channel.get("reason") or "unknown")),
        ], foot="Nothing has changed yet — I'm watching it."))
    for chunk in ui.pages(parked):
        if not chunk:
            break
        await tell(ui.card(
            "🔴", "Taken out of the post",
            text=f"These failed {config.STRIKES} checks in a row:",
            extra=[ui.quote([ui.channel_line(c) for c in chunk]),
                   ui.kv("Pool", f"{len(state.live())} live")],
            foot="They stay in my list and come back on their own if they recover."))
    for channel in back:
        await tell(ui.card("🟢", "Channel is back", [
            ui.kv("Name", ui.esc(channel["title"])),
            ui.kv("ID", ui.code(channel["id"])),
        ], foot="It's working again and back in the pool."))
    for channel, note in relinked:
        await tell(ui.card("🔁", "New invite link", [
            ui.kv("Name", ui.esc(channel["title"])),
            ui.kv("Why", ui.i(note)),
            ui.kv("Invite", ui.esc(channel["link"])),
        ]))
    if result == "reposted":
        await tell(ui.card("♻️", "Redirect post replaced",
                           text="It had been deleted, so I put a new one up."))
    elif result == "post-deleted":
        await tell(ui.card("🔴", "Redirect post deleted",
                           foot="Post .set in the channel again when you're ready."))

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
            await tell(synced_card(entity.title, cid, channel, rights))
        for entity in nores:
            await tell(ui.card("🟠", "One admin right missing", [
                ui.kv("Name", ui.esc(entity.title)),
                ui.kv("ID", ui.code(engine.peer(entity))),
                ui.kv("Needs", ui.b("Invite Users via Link")),
            ], foot="Tick it and I'll add the channel on my own."))
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
            await answer(event, ui.card("⚠️", "That didn't work", [
                ui.kv("Error", ui.i(engine.brief(exc)))],
                foot="I'm still running."))
    return guard


@owner
async def cmd_help(event):
    def row(cmd, what):
        return f"{ui.code(cmd)} — {ui.esc(what)}"

    await answer(event, ui.card(
        "🛟", "Saviour · commands",
        extra=[
            ui.b("Overview"),
            ui.quote([row("/status", "pool, post and last check"),
                      row("/channels", "every channel I know"),
                      row("/links", "every invite link")]),
            ui.b("Checks"),
            ui.quote([row(".check", "test every channel with a \".\" now (or /scan)"),
                      row("/find", "look for channels I'm admin in")]),
            ui.b("The post"),
            ui.quote([row("/use <id>", "show this channel in the post"),
                      row("/slots <n>", "how many link lines it has"),
                      row("/redraw", "rewrite the post now"),
                      row("/unset", "forget the redirect post")]),
            ui.b("Cleanup"),
            ui.quote([row("/forget <id>", "drop a channel from my list"),
                      row(".clearall", "wipe the account (asks first)")]),
            ui.b("In your channel"),
            ui.quote([f"{ui.code('.set')} as a {ui.b('reply')} to a post — take it "
                      "over, keep its look, swap only the links",
                      f"{ui.code('.set')} / {ui.code('.set 3')} — new post (or update "
                      "the one that's already there)"]),
        ],
        foot="Paste channel links here any time to add them."))


@owner
async def cmd_status(event):
    post = state.post
    if post:
        post_row = f"live in {ui.code(post['chat'])}" + (
            " · your own design" if post.get("template") else "")
    elif state.waiting_set:
        post_row = f"⏳ waiting for admin in {ui.code(state.waiting_set['chat'])}"
    else:
        post_row = "not set " + ui.i("(post .set in a channel)")
    chosen = state.active_channel()
    rows = [
        ui.kv("Pool", f"🟢 {len(state.live())} live · 🔴 {len(state.dead())} parked"),
        ui.kv("Post", post_row),
        ui.kv("Lines", str(state.slots())),
        ui.kv("Showing", ui.esc(chosen["title"]) if chosen else "nothing"),
        ui.kv("Last check", ui.when(state.d.get("checked"))),
    ]
    head = ui.card("📊", "Status", rows)
    if not state.channels:
        await answer(event, head + "\n" + ui.i("No channels yet — promote me in one."))
        return
    if len(state.channels) <= ui.PER_PAGE:
        active = chosen["id"] if chosen else None
        await answer(event, head + "\n" + ui.b("Channels") + "\n" + ui.quote(
            [ui.channel_line(c, active) for c in state.channels], expandable=True))
        return
    await answer(event, [head] + channel_list("Channels", state.channels))


@owner
async def cmd_channels(event):
    if not state.channels:
        await answer(event, ui.card("📡", "Channels",
                                    foot="Nothing yet. Promote me in a channel "
                                         "and it shows up here."))
        return
    await answer(event, channel_list(
        f"Channels · {len(state.live())} live, {len(state.dead())} parked",
        state.channels, foot="⭐ = shown in the post right now"))


@owner
async def cmd_links(event):
    if not state.channels:
        await answer(event, ui.card("⛓", "Invite links", foot="No channels yet."))
        return
    chunks = ui.pages(state.channels, per=20)
    await answer(event, [
        ui.card("⛓", ui.page_title("Invite links", n, len(chunks)),
                text="\n\n".join(ui.link_line(c) for c in chunk))
        for n, chunk in enumerate(chunks, 1)])


@owner
async def cmd_scan(event):
    if _round.locked():
        await answer(event, ui.card("⏳", "A check is already running",
                                    foot="Hold on, results come in a moment."))
        return
    note = await answer(event, ui.card(
        "🔍", "Checking every channel…",
        foot=f"{len(state.channels)} channel(s), about "
             f"{len(state.channels) * config.GAP}s or more. Each gets a \".\" that "
             "is deleted at once."))
    await round_now()
    await rewrite(note, ui.card("✅", "Check finished"))
    await cmd_status(event)


@owner
async def cmd_find(event):
    note = await answer(event, ui.card("🔎", "Going through my chats…"))
    fresh, nores = await look_for_channels(quiet=True)
    if not fresh and not nores:
        await rewrite(note, ui.card("🔎", "Search finished",
                                    foot="Nothing new found."))
        return
    blocks = []
    if fresh:
        blocks += [ui.b(f"🟢 Synced ({len(fresh)})"),
                   ui.quote([f"{ui.esc(e.title)}  {ui.code(engine.peer(e))}"
                             for e in fresh[:ui.PER_PAGE]])]
    if nores:
        blocks += [ui.b(f"🟠 Needs “Invite Users via Link” ({len(nores)})"),
                   ui.quote([f"{ui.esc(e.title)}  {ui.code(engine.peer(e))}"
                             for e in nores[:ui.PER_PAGE]])]
    await rewrite(note, ui.card("🔎", "Search finished", extra=blocks,
                                foot=f"Pool: {len(state.live())} live"))


@owner
async def cmd_redraw(event):
    await answer(event, ui.card("📝", "Redirect post", [
        ui.kv("Result", ui.post_result(await engine.redraw_post()))]))


@owner
async def cmd_unset(event):
    state.forget_post()
    state.waiting_set = None
    await state.save()
    await answer(event, ui.card("🗑", "Redirect post forgotten",
                                foot="Post .set in a channel to claim a new one."))


def usage(cmd, now=None):
    rows = [ui.kv("Usage", ui.code(cmd))]
    if now is not None:
        rows.append(ui.kv("Now", now))
    return ui.card("ℹ️", "How to use it", rows)


def not_listed(raw):
    return ui.card("❌", "Not in my list", [ui.kv("ID", ui.code(raw))],
                   foot="See /channels for the IDs I know.")


@owner
async def cmd_slots(event):
    raw = (event.pattern_match.group(1) or "").strip()
    if not raw:
        await answer(event, usage("/slots <1-20>", str(state.slots())))
        return
    want = int(raw)
    if not 1 <= want <= 20:
        await answer(event, ui.card("❌", "Out of range",
                                    foot="Pick a number between 1 and 20."))
        return
    if state.post and state.post.get("template"):
        await answer(event, ui.card("ℹ️", "Your post decides this", [
            ui.kv("Links", f"{state.slots()} position(s) in your post")],
            foot="Edit the post to add or remove links, then reply .set to it "
                 "again."))
        return
    if state.post:
        state.post["slots"] = want
        await state.save()
        await answer(event, ui.card("✅", "Lines changed", [
            ui.kv("Lines", str(want)),
            ui.kv("Post", ui.post_result(await engine.redraw_post()))]))
        return
    config.SLOTS = want
    await answer(event, ui.card("✅", "Lines changed", [ui.kv("Lines", str(want))],
                                foot="The next .set will use this."))


@owner
async def cmd_forget(event):
    raw = (event.pattern_match.group(1) or "").strip()
    if not raw:
        await answer(event, usage("/forget <-100 channel id>"))
        return
    channel = state.get(int(raw))
    if not channel:
        await answer(event, not_listed(raw))
        return
    state.drop(int(raw))
    await state.save()
    await answer(event, ui.card("🗑", "Channel dropped", [
        ui.kv("Name", ui.esc(channel["title"])),
        ui.kv("ID", ui.code(channel["id"])),
        ui.kv("Post", ui.post_result(await engine.redraw_post()))]))
    await ask_for_links_if_empty()


@owner
async def cmd_use(event):
    raw = (event.pattern_match.group(1) or "").strip()
    if not raw:
        chosen = state.active_channel()
        await answer(event, usage("/use <-100 channel id>",
                                  ui.esc(chosen["title"]) if chosen else "nothing"))
        return
    channel = state.get(int(raw))
    if not channel:
        await answer(event, not_listed(raw))
        return
    if channel.get("offline"):
        await answer(event, ui.card("🔴", "That channel is parked", [
            ui.kv("Name", ui.esc(channel["title"])),
            ui.kv("Why", ui.i(channel.get("reason") or "unknown"))],
            foot="Pick a live one, or /scan to re-check it."))
        return
    state.d["active"] = int(raw)
    await state.save()
    await answer(event, ui.card("⭐", "Now showing", [
        ui.kv("Name", ui.esc(channel["title"])),
        ui.kv("ID", ui.code(channel["id"])),
        ui.kv("Post", ui.post_result(await engine.redraw_post()))]))


# ---------------------------------------------------------------- .clearall
@owner
async def cmd_clearall(event):
    if not event.pattern_match.group(1):
        await answer(event, ui.card(
            "🧨", "Wipe this account?",
            text="This empties the account completely:",
            extra=[ui.quote([
                "🗑 deletes every group and channel it created",
                "🚪 leaves every other group and channel — including the whole "
                "link pool and the redirect channel",
                "🚫 blocks every private chat and wipes its history"]),
                "Only this chat and Saved Messages survive. "
                "The link pool and the post are reset.",
                f"⚠️ {ui.b('There is no undo.')} Send {ui.code('.clearall confirm')} "
                "to go ahead."]))
        return
    note = await answer(event, ui.card("🧨", "Clearing the account…",
                                       foot="This can take a while."))
    tally = {"gone": 0, "left": 0, "blocked": 0, "failed": 0}
    try:
        chats = [d async for d in tg.iter_dialogs()]
    except (RPCError, *engine.HICCUP) as exc:
        await rewrite(note, ui.card("⚠️", "Couldn't list the chats", [
            ui.kv("Error", ui.i(engine.brief(exc)))]))
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
            await rewrite(note, ui.card("🧨", "Clearing the account…", [
                ui.kv("Progress", f"{i} / {len(chats)} chats · "
                                  f"{ui.progress(i, len(chats))}")]))
        await asyncio.sleep(config.GAP)

    state.d["channels"] = []
    state.forget_post()
    state.waiting_set = None
    state.d["active"] = None
    state.d["begging"] = False
    await state.save()
    await rewrite(note, ui.card("✅", "Account cleared", [
        ui.kv("Deleted (mine)", str(tally["gone"])),
        ui.kv("Left", str(tally["left"])),
        ui.kv("Blocked and wiped", str(tally["blocked"])),
        ui.kv("Failed", str(tally["failed"])),
    ], text="My pool and post are reset. This chat and Saved Messages were kept "
            "on purpose.",
        foot="I'm still running — add me to channels whenever you want to "
             "start again."))


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

    lines = lines[:ui.PER_PAGE]               # one report message is enough
    note = await answer(event, ui.card("🔎", f"Checking {len(lines)} link(s)…"))
    good, bad = [], []
    for line in lines:
        entity, why = await find_channel(line)
        if entity is None:
            bad.append(f"❌ {ui.code(line)}\n      ↳ {ui.i(why)}")
        else:
            added, why = await engine.adopt(entity)
            if added:
                good.append(entity)
            else:
                bad.append(f"❌ {ui.b(getattr(entity, 'title', None) or line)}\n"
                           f"      ↳ {ui.i(why)}")
        await asyncio.sleep(config.GAP)

    blocks = []
    if good:
        blocks += [ui.b(f"🟢 Loaded ({len(good)})"),
                   ui.quote([f"{ui.esc(e.title)}  {ui.code(engine.peer(e))}"
                             for e in good])]
    if bad:
        blocks += [ui.b(f"❌ Not loaded ({len(bad)})"), ui.quote(bad)]
    rows = []
    foot = None
    if good:
        rows = [ui.kv("Pool", f"{len(state.live())} live"),
                ui.kv("Post", ui.post_result(await engine.redraw_post()))]
        if not state.post and not state.waiting_set:
            foot = "No redirect post yet — post .set in your public channel."
    icon = "✅" if good and not bad else "⚠️" if good else "❌"
    await rewrite(note, ui.card(icon, "Links checked", rows, extra=blocks, foot=foot))
    if good:
        await ask_for_links_if_empty()


# ------------------------------------------------------------------- start
def hook_up():
    tg.add_event_handler(on_set, events.NewMessage(pattern=SET))
    tg.add_event_handler(on_membership, events.Raw(types.UpdateChannelParticipant))
    for handler, pattern in (
        (cmd_help, r"(?i)^/(?:start|help)\s*$"),
        (cmd_status, r"(?i)^/status\s*$"),
        (cmd_channels, r"(?i)^/channels\s*$"),
        (cmd_links, r"(?i)^/links\s*$"),
        (cmd_scan, r"(?i)^[./](?:check|scan)\s*$"),
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
    account = f"@{me.username}" if me.username else str(me.id)
    await tell(ui.card("✅", "Saviour is online", [
        ui.kv("Account", ui.esc(account)),
        ui.kv("Pool", f"🟢 {len(state.live())} live · 🔴 {len(state.dead())} parked"),
        ui.kv("Post", f"live in {ui.code(state.post['chat'])}" if state.post
              else "not set"),
        ui.kv("Checks", f"every {config.CHECK_EVERY // 60 or 1} min"),
    ], foot="Send /help for the command list."))
    log.info("watching…")
    await tg.run_until_disconnected()


if __name__ == "__main__":
    try:
        asyncio.run(start())
    except KeyboardInterrupt:
        sys.exit(0)
