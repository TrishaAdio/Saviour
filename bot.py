#!/usr/bin/env python3
"""Redirect-link bot.

Flow
----
1. Add the bot as admin to your backup channels.
2. ``/load`` in DM, then paste 3-4 channel links  ->  "Access Verified".
3. Add the bot as admin to the public channel and post ``.set`` there.
   The bot deletes ``.set`` and publishes the "Redirecting..." post.
4. Every CHECK_INTERVAL seconds it re-checks each loaded channel. A deleted
   or revoked channel is dropped and its line in the post is replaced with a
   live link - same post, same formatting, just edited.
5. Out of links -> it DMs the owner asking for new ones.
"""

import fcntl
import html as _html
import logging
import os
import re
import time
from typing import Optional

from telegram import Update
from telegram.constants import ChatMemberStatus, ParseMode
from telegram.error import BadRequest, Conflict, Forbidden
from telegram.request import HTTPXRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ChatMemberHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import config
import core
import icons
import menu
import ui
import userbot
from safebot import SafeBot
from storage import store

logging.basicConfig(
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    level=logging.INFO,
    handlers=[logging.FileHandler(config.LOG_FILE, encoding="utf-8"), logging.StreamHandler()],
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("redirect-bot")

LOAD_HINT = ui.load_prompt()


def is_owner(update: Update) -> bool:
    user = update.effective_user
    return bool(user and config.OWNER_ID and user.id == config.OWNER_ID)


# ------------------------------------------------------------- commands
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(update):
        return
    await send_screen(update, menu.home())


async def cmd_login(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Start the user-account login used for reliable link checks."""
    if not is_owner(update):
        return
    if not userbot.AVAILABLE:
        await update.message.reply_text(
            ui.problem("Telethon not installed",
                       "Run <code>pip install -r requirements.txt</code> on the "
                       "server and restart me."),
            parse_mode=ParseMode.HTML)
        return
    if userbot.logged_in():
        await update.message.reply_text(
            ui.card(ui.OK, "Already signed in", [("Account", ui.esc(userbot.account()))],
                    "Use <code>/logout</code> first if you want a different account."),
            parse_mode=ParseMode.HTML)
        return

    session = userbot.Login()
    context.user_data["login"] = session
    context.user_data["awaiting_load"] = False
    await update.message.reply_text(ui.login_intro(session.step), parse_mode=ParseMode.HTML)


async def cmd_logout(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(update):
        return
    pending = context.user_data.pop("login", None)
    if pending is not None:
        await pending.close()
    if not userbot.logged_in():
        await update.message.reply_text(
            ui.problem("Not signed in", "No user account is connected."),
            parse_mode=ParseMode.HTML)
        return
    name = userbot.account()
    clean = await userbot.logout()
    await update.message.reply_text(
        ui.card(ui.INFO, "Signed out", [("Account", ui.esc(name))],
                ("Link checks fall back to reading t.me, which is less reliable."
                 if clean else
                 "The stored session was deleted, but Telegram didn't confirm the "
                 "sign-out. Remove the session in Telegram → Devices to be sure.")),
        parse_mode=ParseMode.HTML)


async def handle_login_step(update: Update, context: ContextTypes.DEFAULT_TYPE,
                            message, session) -> None:
    """Feed one message into the login flow, deleting secrets as they arrive."""
    secret = session.step in ("api_hash", "code", "password")
    text = message.text or ""
    if secret:
        try:
            await context.bot.delete_message(message.chat_id, message.message_id)
        except (BadRequest, Forbidden) as exc:
            log.info("could not delete a login secret: %s", exc)

    step, info = await session.feed(text)

    if step == "done":
        context.user_data.pop("login", None)
        await message.reply_text(ui.login_done(info), parse_mode=ParseMode.HTML)
        log.info("userbot signed in as %s", info)
        # Now that checks are trustworthy: find the channels the bot is admin
        # in, then take a baseline of every channel. In the background, so the
        # bot keeps answering while it works.
        context.application.create_task(after_login(context, message))
        return
    if step == "failed":
        context.user_data.pop("login", None)
        await message.reply_text(ui.login_failed(info), parse_mode=ParseMode.HTML)
        return
    await message.reply_text(ui.login_step(step, info), parse_mode=ParseMode.HTML)


async def cmd_load(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(update):
        return
    context.user_data["awaiting_load"] = True
    await update.message.reply_text(LOAD_HINT, parse_mode=ParseMode.HTML)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(update):
        return
    await send_screen(update, menu.status())


async def cmd_refresh(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(update):
        return
    result = await core.refresh_post(context.bot)
    ok = result in ("edited", "unchanged")
    reply = (ui.done("Post refreshed", [("Post", ui.post_status(result))]) if ok
             else ui.problem("Refresh incomplete", ui.post_status(result)))
    await update.message.reply_text(reply, parse_mode=ParseMode.HTML)


async def cmd_remove(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(update):
        return
    chat_id = await _channel_arg(update, context, "/remove")
    if chat_id is None:
        return
    channel = store.get_channel(chat_id)
    store.remove_channel(chat_id)
    await store.save()
    result = await core.refresh_post(context.bot)
    await update.message.reply_text(
        ui.done("Channel removed", [
            ("Channel", f"<b>{ui.esc(channel['title'])}</b>"),
            ("Pool", ui.pool_value(len(store.online()))),
            ("Post", ui.post_status(result)),
        ]),
        parse_mode=ParseMode.HTML,
    )
    await beg_if_empty(context)


async def _channel_arg(update: Update, context: ContextTypes.DEFAULT_TYPE, cmd: str):
    """Parse and validate the channel id argument; reply and return None if bad."""
    if not context.args:
        await update.message.reply_text(
            ui.problem("Channel id required", f"Usage: <code>{cmd} -1001234567890</code>. "
                       "See /status for ids."),
            parse_mode=ParseMode.HTML)
        return None
    try:
        chat_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text(
            ui.problem("Invalid channel id", "Use the numeric id shown in /status, "
                       "e.g. <code>-1001234567890</code>."),
            parse_mode=ParseMode.HTML)
        return None
    if not store.get_channel(chat_id):
        await update.message.reply_text(
            ui.problem("Channel not found", f"{ui.code(chat_id)} isn't connected. "
                       "See /status."),
            parse_mode=ParseMode.HTML)
        return None
    return chat_id


async def cmd_active(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Move a channel to the front of the pool (it becomes the shown link)."""
    if not is_owner(update):
        return
    chat_id = await _channel_arg(update, context, "/active")
    if chat_id is None:
        return
    channel = store.get_channel(chat_id)
    store.channels.remove(channel)
    store.channels.insert(0, channel)
    await store.save()
    result = await core.refresh_post(context.bot)
    await update.message.reply_text(
        ui.done("Active channel changed", [
            ("Channel", f"<b>{ui.esc(channel['title'])}</b>"),
            ("Invite", ui.link(channel["link"])),
            ("Post", ui.post_status(result)),
        ]),
        parse_mode=ParseMode.HTML, disable_web_page_preview=True,
    )


async def run_scan(context: ContextTypes.DEFAULT_TYPE, message) -> None:
    """Check every channel once and report. Nothing is parked from one scan."""
    channels = list(store.channels)
    note = await message.reply_text(
        f"{ui.dot(ui.INFO)} <b>Scanning {ui.plural(len(channels), 'channel')}…</b>",
        parse_mode=ParseMode.HTML)

    results = []
    for channel in channels:
        state, reason = await core.check_channel(context.bot, channel)
        results.append((channel, state, reason))
        if state in ("ok", "relinked"):
            channel["strikes"] = 0
            if channel.get("offline"):
                store.bring_online(channel["id"])
        elif state == "suspect":
            # A scan only records the first strike; parking still needs
            # DEATH_STRIKES failures across separate rounds.
            channel["strikes"] = max(int(channel.get("strikes", 0)), 1)
            channel["reason"] = reason

    store.data["last_check"] = int(time.time())
    await store.save()
    result = await core.refresh_post(context.bot) if store.post else "no-post"
    log.info("scan: %s", ", ".join(f"{c['title']}={s}" for c, s, _ in results))

    try:
        await note.edit_text(ui.scan_report(results, result), parse_mode=ParseMode.HTML,
                             disable_web_page_preview=True)
    except BadRequest:
        await message.reply_text(ui.scan_report(results, result),
                                 parse_mode=ParseMode.HTML,
                                 disable_web_page_preview=True)
    await beg_if_empty(context)


async def cmd_scan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Check every channel right now."""
    if not is_owner(update):
        return
    if not store.channels:
        await update.message.reply_text(
            ui.problem("Nothing to scan", "No channels are connected yet."),
            parse_mode=ParseMode.HTML)
        return
    if config.REQUIRE_USERBOT and not userbot.logged_in():
        await update.message.reply_text(ui.monitoring_paused(), parse_mode=ParseMode.HTML)
        return
    await run_scan(context, update.message)


async def cmd_icons(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show, or switch, the Premium icons (they need the owner to have Premium)."""
    if not is_owner(update):
        return
    arg = (context.args[0].lower() if context.args else "")
    if arg in ("on", "off"):
        icons.set_override(arg == "on")
        await store.save()
    elif arg:
        await update.message.reply_text(
            ui.problem("Unknown option", "Use <code>/icons on</code>, "
                       "<code>/icons off</code> or just <code>/icons</code>."),
            parse_mode=ParseMode.HTML)
        return

    sent = await update.message.reply_text(ui.icons_status(), parse_mode=ParseMode.HTML)
    # Turning icons on is a test: if Telegram strips them from this very
    # message, the owner has no Premium - say so now instead of silently.
    if arg == "on" and icons.enabled() and not icons.delivered(sent):
        icons._switch_off("Telegram is not showing custom icons "
                          "(the bot owner needs Telegram Premium)")
        await store.save()
        await update.message.reply_text(ui.icons_off(), parse_mode=ParseMode.HTML)


_sync_running = False


async def do_sync(context: ContextTypes.DEFAULT_TYPE) -> dict:
    """One search + connect run. Only one at a time."""
    global _sync_running
    if _sync_running or userbot.finding():
        return {"busy": True}
    _sync_running = True
    try:
        report = await core.sync_channels(context.bot)
        if report["added"] and store.post:
            report["post"] = await core.refresh_post(context.bot)
        log.info("sync via %s: scanned %s, found %s, added %s, existing %s, "
                 "need permission %s, failed %s", report["via"], report["scanned"],
                 report["found"], len(report["added"]), len(report["existing"]),
                 len(report["need_permission"]), len(report["failed"]))
        return report
    finally:
        _sync_running = False


async def after_login(context: ContextTypes.DEFAULT_TYPE, message) -> None:
    note = await message.reply_text(ui.sync_running(True), parse_mode=ParseMode.HTML)
    await _finish_sync(context, note)
    if store.channels:
        await run_scan(context, message)


async def cmd_sync(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Find every channel the bot is admin in and connect the unknown ones."""
    if not is_owner(update):
        return
    note = await update.message.reply_text(ui.sync_running(userbot.logged_in()),
                                           parse_mode=ParseMode.HTML)
    # Long searches must not block the bot, so run in the background and edit
    # this one message with the result.
    context.application.create_task(_finish_sync(context, note))


async def _finish_sync(context: ContextTypes.DEFAULT_TYPE, note) -> None:
    try:
        report = await do_sync(context)
        text = ui.sync_report(report)
    except Exception as exc:  # noqa: BLE001 - tell the owner instead of going silent
        log.exception("sync failed")
        text = ui.problem("Sync failed", f"<code>{ui.esc(type(exc).__name__)}</code>: "
                          f"{ui.esc(str(exc)[:200])}")
    try:
        await note.edit_text(text, parse_mode=ParseMode.HTML,
                             disable_web_page_preview=True)
    except BadRequest as exc:
        if "not modified" not in (exc.message or "").lower():
            await note.reply_text(text, parse_mode=ParseMode.HTML,
                                  disable_web_page_preview=True)


async def auto_sync(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Quietly look for unknown channels now and then; speak only if one is new."""
    if not userbot.logged_in() or userbot.cooling_down():
        return
    try:
        report = await do_sync(context)
    except Exception:  # noqa: BLE001 - a background job must never raise
        log.exception("automatic sync failed")
        return
    if report.get("added"):
        await notify_owner(context, ui.sync_report(report))


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(update):
        return
    pending = context.user_data.pop("login", None)
    if pending is not None:
        await pending.close()
    context.user_data["awaiting_load"] = False
    await update.message.reply_text(
        ui.done("Cancelled", (), "Nothing in progress. Send /start for the panel."),
        parse_mode=ParseMode.HTML)


async def cmd_preview(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show sample notifications without having to break a real channel."""
    if not is_owner(update):
        return
    one = [{"title": "Movies Backup", "id": -1001234567890}]
    full = {"full": True, "missing_post": [], "missing_link": []}
    samples = [
        ui.channel_connected("Movies Backup", -1001234567890,
                             "https://t.me/+AbCdEf123456", True, full, 3, "edited", False),
        ui.channels_offline(one, 2, "edited"),
        ui.no_live_links(),
    ]
    for text in samples:
        await update.message.reply_text(
            text, parse_mode=ParseMode.HTML, disable_web_page_preview=True
        )


async def cmd_slots(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Change how many link lines the post shows, live."""
    if not is_owner(update):
        return
    current = store.post["slots"] if store.post else config.POST_SLOTS

    def say(text):
        return update.message.reply_text(text, parse_mode=ParseMode.HTML)

    if not context.args:
        await say(ui.card(ui.INFO, "Link lines", [
            ("Current", str(current)),
            ("Default", f"{config.POST_SLOTS} <i>(POST_SLOTS in .env)</i>"),
        ], "Change it with <code>/slots 5</code>."))
        return
    try:
        count = max(1, min(20, int(context.args[0])))
    except ValueError:
        await say(ui.problem("Invalid number", "Example: <code>/slots 5</code>"))
        return

    if store.post and store.post.get("template"):
        await say(ui.problem(
            "Line count follows your post",
            "This post was linked by replying <code>.set</code>, so it keeps the "
            "number of links it already has. Edit the post, then reply "
            "<code>.set</code> to it again.",
        ))
        return

    if not store.post:
        await say(ui.problem(
            "No post linked",
            f"Send <code>.set {count}</code> in your channel, or set "
            f"<code>POST_SLOTS={count}</code> in <code>.env</code>.",
        ))
        return

    store.post["slots"] = count
    await store.save()
    result = await core.refresh_post(context.bot)
    await say(ui.done("Link lines updated", [
        ("Lines", str(count)), ("Post", ui.post_status(result)),
    ]))


async def cmd_unset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(update):
        return
    store.clear_post()
    await store.save()
    await update.message.reply_text(
        ui.done("Monitoring stopped", (),
                "The post is no longer tracked. Reply <code>.set</code> to a post "
                "to start again."),
        parse_mode=ParseMode.HTML,
    )


# -------------------------------------------------------- inline panel
def _plain(html_text: str) -> str:
    """Pop-up toasts can't show formatting; strip tags and unescape."""
    return _html.unescape(re.sub(r"<[^>]+>", "", html_text))[:200]


async def send_screen(update: Update, screen) -> None:
    text, keyboard = screen
    await update.message.reply_text(
        text, parse_mode=ParseMode.HTML, reply_markup=keyboard,
        disable_web_page_preview=True,
    )


async def _sync_into_panel(context: ContextTypes.DEFAULT_TYPE, query) -> None:
    try:
        report = await do_sync(context)
        text, keyboard = menu.sync_done(report)
    except Exception as exc:  # noqa: BLE001 - show it instead of leaving "searching"
        log.exception("sync from the panel failed")
        text = ui.problem("Sync failed", f"<code>{ui.esc(type(exc).__name__)}</code>")
        keyboard = menu._kb([menu._back()])
    try:
        await query.edit_message_text(text, parse_mode=ParseMode.HTML,
                                      reply_markup=keyboard,
                                      disable_web_page_preview=True)
    except BadRequest as exc:
        if "not modified" not in (exc.message or "").lower():
            log.warning("could not show the sync result: %s", exc)


_STALE = ("query is too old", "query id is invalid", "response timeout expired")


async def answer_press(query) -> bool:
    """Acknowledge a button press. False means it's stale and must be ignored."""
    try:
        await query.answer()
        return True
    except BadRequest as exc:
        if any(m in (exc.message or "").lower() for m in _STALE):
            log.info("ignoring a stale button press (%s): %s", query.data, exc.message)
            return False
        log.warning("could not acknowledge button %s: %s", query.data, exc)
        return True
    except core.TRANSIENT as exc:
        # The network hiccuped; the press itself is current, so carry on.
        log.warning("acknowledging button %s timed out: %s", query.data, exc)
        return True


async def on_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Every panel button lands here and edits the same message in place."""
    query = update.callback_query
    if query is None or not query.data:
        return
    if not is_owner(update):
        try:
            await query.answer("This panel is only for the bot owner.", show_alert=True)
        except (BadRequest, *core.TRANSIENT):
            pass
        return

    # Answer FIRST, before doing anything. Telegram rejects an answer to a press
    # that is too old - which happens when presses queued while the bot was down
    # are replayed on start. Such a press must not run: it could be a stale
    # "Remove" or "Stop monitoring". So a rejected answer means: do nothing.
    if not await answer_press(query):
        return

    _, action, *rest = query.data.split(":") + [""]
    arg = rest[0] if rest else ""
    toast = None          # result line shown at the top of the redrawn panel
    toast_bad = False
    screen = None

    def channel_id() -> Optional[int]:
        try:
            return int(arg)
        except ValueError:
            return None

    if action == "home":
        screen = menu.home()
    elif action == "status":
        screen = menu.status()
    elif action == "chs":
        screen = menu.channels(int(arg) if arg.lstrip("-").isdigit() else 0)
    elif action in ("ch", "rm"):
        cid = channel_id()
        screen = (menu.channel(cid) if action == "ch" else menu.confirm_remove(cid)) \
            if cid is not None else None
        if screen is None:
            toast, toast_bad = "That channel is no longer connected.", True
            screen = menu.channels(0)
    elif action == "act":
        cid = channel_id()
        ch = store.get_channel(cid) if cid is not None else None
        if ch:
            revived = bool(ch.get("offline"))
            if revived:
                store.bring_online(cid)        # owner overrules the check
            store.channels.remove(ch)
            store.channels.insert(0, ch)
            await store.save()
            result = await core.refresh_post(context.bot)
            lead = "Back in use" if revived else "Now showing"
            toast = f"{lead}: {ch['title']}. Post: {_plain(ui.post_status(result))}"
            screen = menu.channel(cid)
        else:
            toast, toast_bad = "That channel is no longer connected.", True
            screen = menu.channels(0)
    elif action == "rmok":
        cid = channel_id()
        ch = store.get_channel(cid) if cid is not None else None
        if ch:
            store.remove_channel(cid)
            await store.save()
            result = await core.refresh_post(context.bot)
            toast = f"Removed {ch['title']}. Post: {_plain(ui.post_status(result))}"
        screen = menu.channels(0)
    elif action == "post":
        screen = menu.post()
    elif action == "refresh":
        result = await core.refresh_post(context.bot)
        toast = _plain(ui.post_status(result))
        screen = menu.post()
    elif action == "lines":
        post = store.post
        if post and not post.get("template") and arg.isdigit():
            post["slots"] = max(1, min(20, int(arg)))
            await store.save()
            result = await core.refresh_post(context.bot)
            toast = f"{post['slots']} lines. Post: {_plain(ui.post_status(result))}"
        screen = menu.post()
    elif action == "unset":
        screen = menu.confirm_unset() if store.post else menu.post()
    elif action == "unsetok":
        store.clear_post()
        await store.save()
        toast, screen = "Monitoring stopped.", menu.post()
    elif action == "add":
        context.user_data["awaiting_load"] = True
        screen = menu.add()
    elif action == "sync":
        # Long-running: show "searching" now, and let a background task edit
        # this same message when it has the result.
        text, keyboard = menu.sync_running()
        try:
            await query.edit_message_text(text, parse_mode=ParseMode.HTML,
                                          reply_markup=keyboard,
                                          disable_web_page_preview=True)
        except BadRequest:
            pass
        context.application.create_task(_sync_into_panel(context, query))
        return
    elif action == "help":
        screen = menu.help_screen()
    elif action == "close":
        try:
            await query.edit_message_text(menu.closed(), parse_mode=ParseMode.HTML)
        except BadRequest:
            pass
        return
    else:
        screen = menu.home()

    text, keyboard = screen
    if toast:
        text = ui.banner(toast, bad=toast_bad) + text
    try:
        await core.with_retry(
            query.edit_message_text, text, parse_mode=ParseMode.HTML,
            reply_markup=keyboard, disable_web_page_preview=True, what="panel edit",
        )
    except BadRequest as exc:
        if "not modified" not in (exc.message or "").lower():
            log.warning("panel edit failed: %s", exc)
    except core.TRANSIENT as exc:
        log.warning("panel edit timed out: %s", exc)

    if action == "rmok":
        await beg_if_empty(context)


# ------------------------------------------------------ link ingestion
async def on_private_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles both the /load reply and the 'I ran out of links' reply."""
    if not is_owner(update):
        return
    message = update.effective_message
    if message is None:  # edited/odd update slipped through
        return

    # A /login flow in progress takes every message until it finishes.
    session = context.user_data.get("login")
    if session is not None:
        await handle_login_step(update, context, message, session)
        return

    expecting = context.user_data.get("awaiting_load") or store.awaiting_links
    text = message.text or message.caption or ""

    targets, unresolvable = core.parse_targets(text)
    forwarded = core.forwarded_channel_id(message)
    if forwarded is not None:
        targets.append(str(forwarded))

    if not targets:
        if unresolvable:
            await message.reply_text(ui.private_invites_unsupported(), parse_mode=ParseMode.HTML)
        elif expecting:
            await message.reply_text(LOAD_HINT, parse_mode=ParseMode.HTML)
        return

    status = await message.reply_text(
        f"{ui.dot(ui.INFO)} <b>Verifying {ui.plural(len(targets), 'channel')}…</b>",
        parse_mode=ParseMode.HTML,
    )
    added, refreshed, failed = [], [], []

    for target in targets:
        try:
            result = await core.verify_target(context.bot, target)
        except core.TRANSIENT as exc:
            failed.append((str(target), "Telegram did not respond, try again"))
            continue
        if not result["ok"]:
            failed.append((str(target), result["reason"]))
            continue
        chat = result["chat"]
        is_new = store.add_channel(chat.id, chat.title or str(chat.id), chat.username, result["link"])
        (added if is_new else refreshed).append(chat.title or str(chat.id))

    if added or refreshed:
        store.verified = True
        store.awaiting_links = False
    await store.save()
    context.user_data["awaiting_load"] = False

    post_line = None
    if store.post and (added or refreshed):
        post_line = ui.post_status(await core.refresh_post(context.bot))

    await status.edit_text(
        ui.load_result(added, refreshed, failed, unresolvable, len(store.channels), post_line),
        parse_mode=ParseMode.HTML, disable_web_page_preview=True,
    )


# -------------------------------------------------------------- ".set"
async def on_set_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    post = update.channel_post
    if not post:
        return
    chat_id = post.chat_id

    # ".set" or ".set 7"
    parts = (post.text or "").split()
    slots = config.POST_SLOTS
    if len(parts) > 1:
        try:
            slots = max(1, min(20, int(parts[1])))
        except ValueError:
            pass

    # This channel displays the post, so it must not be one of its own targets
    # (auto-detect will have grabbed it when you promoted the bot here).
    if store.get_channel(chat_id):
        store.remove_channel(chat_id)
        await store.save()
        log.info("%s hosts the post - removed it from the link pool", chat_id)

    if not store.channels:
        await notify_owner(context, ui.nothing_to_link(post.chat.title or "?", chat_id))
        return

    # ".set" sent as a REPLY -> take over the post you replied to, keep its
    # exact formatting, swap only its links, and remove the ".set" message.
    target = post.reply_to_message
    if target is not None:
        await adopt_replied_post(context, post, target)
        return

    # Plain ".set" -> that message *becomes* the post, edited in place.
    try:
        adopted = await core.adopt_post(context.bot, chat_id, post.message_id, slots)
    except (BadRequest, Forbidden) as exc:
        await notify_owner(context, ui.post_failed(chat_id, getattr(exc, "message", exc)))
        return

    if adopted:
        await notify_owner(
            context, ui.post_created(post.chat.title or str(chat_id), chat_id, slots)
        )


async def adopt_replied_post(context: ContextTypes.DEFAULT_TYPE, cmd, target) -> None:
    """``.set`` replied to an existing post: edit that post, delete ``.set``."""
    chat_id = cmd.chat_id
    title = cmd.chat.title or str(chat_id)
    try:
        result = await core.adopt_existing(context.bot, target)
    except (BadRequest, Forbidden) as exc:
        await notify_owner(context, ui.post_failed(chat_id, getattr(exc, "message", exc)))
        return
    if not result["ok"]:
        reason = result["reason"]
        await notify_owner(context, ui.post_rejected(title, reason[:1].upper() + reason[1:]))
        return

    removed = True
    try:
        await context.bot.delete_message(chat_id, cmd.message_id)
    except (BadRequest, Forbidden) as exc:
        removed = False
        log.info("could not delete the .set reply in %s: %s", chat_id, exc)

    await notify_owner(context, ui.post_linked(title, chat_id, result["slots"], removed))


# ------------------------------------------- auto-detect channel joins
async def on_my_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Fires the moment the bot is added, promoted, demoted or removed.

    Promoted to admin with 'Invite Users via Link'  ->  the channel is added to
    the pool automatically, with a freshly minted invite link. No /load needed.
    Demoted or removed  ->  it leaves the pool immediately and the post is fixed
    without waiting for the next health check.
    """
    event = update.my_chat_member
    if not event:
        return
    chat = event.chat
    status = event.new_chat_member.status
    was = event.old_chat_member.status if event.old_chat_member else None

    if chat.type != "channel":
        if status == ChatMemberStatus.ADMINISTRATOR and chat.type in ("group", "supergroup"):
            await notify_owner(context, ui.group_ignored(chat.title or chat.id))
        return

    # Remember it, so /sync can try again even if adding it fails right now.
    known = store.data.setdefault("known_chats", {})
    if status == ChatMemberStatus.ADMINISTRATOR:
        known[str(chat.id)] = chat.title or str(chat.id)
    else:
        known.pop(str(chat.id), None)
    await store.save()

    # ---------------------------------------------------- lost access
    if status != ChatMemberStatus.ADMINISTRATOR:
        channel = store.get_channel(chat.id)
        if not channel:
            return
        store.remove_channel(chat.id)
        await store.save()
        result = await core.refresh_post(context.bot)
        reason = "demoted" if status == ChatMemberStatus.MEMBER else str(status)
        log.info("auto-removed %s (%s), post -> %s", chat.id, reason, result)
        await notify_owner(
            context, ui.channel_disconnected(channel, reason, len(store.channels), result)
        )
        await beg_if_empty(context)
        return

    # ------------------------------------------------ promoted to admin
    if not config.AUTO_ADD:
        return
    if store.post and chat.id == store.post["chat_id"]:
        return  # this is the channel hosting the post - never link it to itself
    if was == ChatMemberStatus.ADMINISTRATOR and store.get_channel(chat.id):
        return  # rights were merely edited and we already have it

    report = core.rights_report(event.new_chat_member)
    title = chat.title or str(chat.id)

    if not report["can_link"]:
        await notify_owner(context, ui.permission_required(title, chat.id, report))
        return

    was_out_of_links = store.awaiting_links
    result = await core.register_channel(context.bot, chat, title)
    if not result["ok"]:
        await notify_owner(context, ui.invite_failed(title, chat.id, result["reason"]))
        return

    post_result = await core.refresh_post(context.bot) if store.post else "no-post"
    log.info("auto-added %s (%s), post -> %s", chat.id, title, post_result)
    await notify_owner(
        context,
        ui.channel_connected(
            title, chat.id, result["link"], result["new"], report,
            len(store.online()), post_result, rescued=was_out_of_links,
        ),
    )


# --------------------------------------------------------- owner pings
async def notify_owner(context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    if not config.OWNER_ID:
        return
    try:
        await core.with_retry(
            context.bot.send_message, config.OWNER_ID, text,
            parse_mode=ParseMode.HTML, disable_web_page_preview=True, what="owner DM",
        )
    except (BadRequest, Forbidden) as exc:  # first: BadRequest is a NetworkError
        log.error("cannot DM owner: %s", exc)
    except core.TRANSIENT as exc:
        log.error("owner DM lost after retries (%s): %s", type(exc).__name__,
                  text.splitlines()[0][:120] if text else "")


async def beg_if_empty(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Ask the owner for links once the pool runs dry (only once per drought)."""
    if len(store.online()) >= max(config.MIN_CHANNELS, 1):
        return
    if store.awaiting_links:
        return
    store.awaiting_links = True
    await store.save()
    await notify_owner(context, ui.no_live_links())


# ----------------------------------------------------------- watchdog
async def health_check(context: ContextTypes.DEFAULT_TYPE) -> None:
    if not store.channels:
        await beg_if_empty(context)
        return

    # Without a user account the only available check is reading t.me, which
    # reports live channels as missing. Judging channels on that is what
    # emptied the pool, so monitoring stays paused until /login.
    if config.REQUIRE_USERBOT and not userbot.logged_in():
        if store.post:
            await core.refresh_post(context.bot)
        if not store.data.get("paused_notified"):
            store.data["paused_notified"] = True
            await store.save()
            await notify_owner(context, ui.monitoring_paused())
        log.warning("monitoring paused: no user account connected (/login)")
        return
    store.data["paused_notified"] = False

    parked, relinked, recovered, suspects, unknown = [], [], [], [], 0

    for channel in list(store.channels):
        state, reason = await core.check_channel(context.bot, channel)
        was_offline = bool(channel.get("offline"))

        if state in ("ok", "relinked"):
            channel["strikes"] = 0
            if was_offline:
                store.bring_online(channel["id"])
                recovered.append(channel)
            if state == "relinked":
                relinked.append(channel)
            continue

        if state == "unknown":
            unknown += 1
            log.info("channel %s unverified this round: %s", channel["id"], reason)
            continue

        # suspect: count a strike. Only after DEATH_STRIKES rounds in a row,
        # each CHECK_INTERVAL apart, is it parked offline.
        channel["strikes"] = int(channel.get("strikes", 0)) + 1
        channel["reason"] = reason
        log.warning("channel %s suspect (%s/%s): %s", channel["id"],
                    channel["strikes"], config.DEATH_STRIKES, reason)
        if was_offline:
            continue
        if channel["strikes"] >= config.DEATH_STRIKES:
            store.park_offline(channel["id"], reason)
            parked.append(channel)
        else:
            suspects.append(channel)

    store.data["last_check"] = int(time.time())
    await store.save()

    # Re-render every round: fixes the post whenever its links don't match the
    # working channels, and confirms the post still exists.
    result = await core.refresh_post(context.bot) if store.post else "no-post"

    log.info("health check: %s working, %s offline | parked %s, relinked %s, "
             "recovered %s, unverified %s | post -> %s",
             len(store.online()), len(store.offline()), len(parked), len(relinked),
             len(recovered), unknown, result)

    if parked:
        await notify_owner(context, ui.channels_offline(parked, len(store.online()), result))
    for channel in recovered:
        await notify_owner(context, ui.channel_recovered(channel, result))
    for channel in relinked:
        await notify_owner(context, ui.channel_relinked(channel, result))
    for channel in suspects:
        if channel["strikes"] == 1:  # tell the owner early, but only once
            await notify_owner(context, ui.channel_suspect(channel))
    if result == "post-gone":
        await notify_owner(context, ui.post_deleted())
    elif result.startswith("error") and (parked or relinked or recovered):
        await notify_owner(context, ui.post_update_failed(result))

    await beg_if_empty(context)


async def on_startup(app: Application) -> None:
    me = await app.bot.get_me()
    log.info("started as @%s (%s)", me.username, me.id)
    if not config.OWNER_ID:
        return
    try:
        await app.bot.send_message(
            config.OWNER_ID,
            ui.startup(me.username, len(store.channels), bool(store.post)),
            parse_mode=ParseMode.HTML,
        )
    except (BadRequest, Forbidden) as exc:  # first: BadRequest is a NetworkError
        log.warning("startup DM failed (send /start to the bot once): %s", exc)
    except core.TRANSIENT as exc:
        log.warning("startup DM timed out, carrying on: %s", exc)


async def on_shutdown(app: Application) -> None:
    await userbot.disconnect()


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log anything unhandled and tell the owner instead of dying silently."""
    err = context.error

    # Two copies of the bot are polling Telegram. It repeats for every poll,
    # so report it once with instructions instead of flooding the chat.
    if isinstance(err, Conflict):
        log.error("another bot instance is polling: %s", err)
        if not context.application.bot_data.get("conflict_reported"):
            context.application.bot_data["conflict_reported"] = True
            await notify_owner(context, ui.duplicate_instance())
        return

    if core.is_transient(err):
        # A slow or dropped connection to Telegram. Not a bug and nothing the
        # owner can act on - one log line, no traceback, no DM.
        log.warning("telegram network hiccup: %s: %s", type(err).__name__, err)
        return
    log.error("unhandled exception", exc_info=err)
    if not config.OWNER_ID:
        return
    chat_id = None
    if isinstance(update, Update) and update.effective_chat:
        chat_id = update.effective_chat.id
    try:
        await context.bot.send_message(
            config.OWNER_ID, ui.unexpected_error(err, chat_id), parse_mode=ParseMode.HTML,
        )
    except Exception:  # never let the error handler raise
        log.exception("could not report the error to the owner")


_instance_lock = None


def claim_single_instance() -> None:
    """Refuse to start if another copy of the bot is already running.

    Two running copies fight over getUpdates and Telegram answers both with
    "Conflict: terminated by other getUpdates request", so neither works
    properly. A lock file held for the life of the process makes that
    impossible: the second copy exits immediately with a clear message.
    """
    global _instance_lock
    path = config.BASE_DIR / "bot.lock"
    _instance_lock = open(path, "a+")
    try:
        fcntl.flock(_instance_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        _instance_lock.seek(0)
        other = (_instance_lock.read() or "").strip() or "unknown"
        raise SystemExit(
            f"Another copy of the bot is already running (PID {other}).\n"
            "Running two copies breaks both, so this one is stopping.\n\n"
            "To see what is running:   pgrep -af 'bot.py'\n"
            "Stop everything:          pkill -f 'bot.py'; systemctl stop saviour "
            "2>/dev/null; screen -S saviour -X quit 2>/dev/null\n"
            "Then start exactly one."
        )
    _instance_lock.seek(0)
    _instance_lock.truncate()
    _instance_lock.write(str(os.getpid()))
    _instance_lock.flush()
    log.info("instance lock held (PID %s)", os.getpid())


def main() -> None:
    if not config.BOT_TOKEN:
        raise SystemExit("BOT_TOKEN missing — run: python3 setup.py")
    if not config.OWNER_ID:
        raise SystemExit("OWNER_ID missing — run: python3 setup.py")
    claim_single_instance()

    # PTB's default HTTP timeouts are 5s, which a busy VPS link regularly
    # exceeds (that was the TimedOut in the health check). Give Telegram room.
    timeout = config.HTTP_TIMEOUT
    limits = dict(connect_timeout=timeout, read_timeout=timeout,
                  write_timeout=timeout, pool_timeout=timeout)
    bot = SafeBot(
        config.BOT_TOKEN,
        request=HTTPXRequest(connection_pool_size=16, **limits),
        get_updates_request=HTTPXRequest(connection_pool_size=1, **limits),
    )
    app = (
        Application.builder()
        .bot(bot)
        .post_init(on_startup)
        .post_shutdown(on_shutdown)
        .build()
    )

    # UpdateType.MESSAGE keeps *edited* messages out: an edit leaves
    # update.message as None, which used to crash the handlers.
    private = filters.ChatType.PRIVATE & filters.UpdateType.MESSAGE

    app.add_handler(CommandHandler("start", cmd_start, private))
    app.add_handler(CommandHandler("help", cmd_start, private))
    app.add_handler(CommandHandler("load", cmd_load, private))
    app.add_handler(CommandHandler("login", cmd_login, private))
    app.add_handler(CommandHandler("logout", cmd_logout, private))
    app.add_handler(CommandHandler("cancel", cmd_cancel, private))
    app.add_handler(CommandHandler("scan", cmd_scan, private))
    app.add_handler(CommandHandler("icons", cmd_icons, private))
    app.add_handler(CommandHandler("sync", cmd_sync, private))
    app.add_handler(CommandHandler("status", cmd_status, private))
    app.add_handler(CommandHandler("refresh", cmd_refresh, private))
    app.add_handler(CommandHandler("remove", cmd_remove, private))
    app.add_handler(CommandHandler("active", cmd_active, private))
    app.add_handler(CommandHandler("slots", cmd_slots, private))
    app.add_handler(CommandHandler("preview", cmd_preview, private))
    app.add_handler(CommandHandler("unset", cmd_unset, private))

    app.add_handler(CallbackQueryHandler(on_menu, pattern=r"^m:"))
    app.add_handler(
        ChatMemberHandler(on_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER)
    )
    app.add_handler(
        MessageHandler(
            filters.UpdateType.CHANNEL_POST & filters.Regex(r"^\s*\.set(\s|$)"),
            on_set_command,
        )
    )
    app.add_handler(
        MessageHandler(
            private & ~filters.COMMAND & (filters.TEXT | filters.FORWARDED),
            on_private_message,
        )
    )
    app.add_error_handler(on_error)

    app.job_queue.run_repeating(
        health_check, interval=config.CHECK_INTERVAL, first=30, name="health_check"
    )

    if config.SYNC_INTERVAL > 0:
        app.job_queue.run_repeating(
            auto_sync, interval=config.SYNC_INTERVAL, first=45, name="auto_sync"
        )

    log.info("polling…")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
