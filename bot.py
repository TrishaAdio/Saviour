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

import html as _html
import logging
import re
from typing import Optional

from telegram import Update
from telegram.constants import ChatMemberStatus, ParseMode
from telegram.error import BadRequest, Forbidden
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
import menu
import ui
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
            ("Pool", ui.pool_value(len(store.channels))),
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


async def on_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Every panel button lands here and edits the same message in place."""
    query = update.callback_query
    if query is None or not query.data:
        return
    if not is_owner(update):
        await query.answer("This panel is only for the bot owner.", show_alert=True)
        return

    _, action, *rest = query.data.split(":") + [""]
    arg = rest[0] if rest else ""
    toast = None
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
            toast, screen = "That channel is no longer connected.", menu.channels(0)
    elif action == "act":
        cid = channel_id()
        ch = store.get_channel(cid) if cid is not None else None
        if ch:
            store.channels.remove(ch)
            store.channels.insert(0, ch)
            await store.save()
            result = await core.refresh_post(context.bot)
            toast = f"Now showing {ch['title']}. Post: {_plain(ui.post_status(result))}"
            screen = menu.channel(cid)
        else:
            toast, screen = "That channel is no longer connected.", menu.channels(0)
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
    elif action == "help":
        screen = menu.help_screen()
    elif action == "close":
        await query.answer()
        try:
            await query.edit_message_text(menu.closed(), parse_mode=ParseMode.HTML)
        except BadRequest:
            pass
        return
    else:
        screen = menu.home()

    try:
        await query.answer(toast)
    except (BadRequest, core.TRANSIENT) as exc:  # answering late is harmless
        log.info("callback answer skipped: %s", exc)

    text, keyboard = screen
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
        f"{ui.INFO} <b>Verifying {ui.plural(len(targets), 'channel')}…</b>",
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
            len(store.channels), post_result, rescued=was_out_of_links,
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
    if len(store.channels) >= max(config.MIN_CHANNELS, 1):
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

    dead = []
    for channel in list(store.channels):
        if not await core.is_channel_alive(context.bot, channel):
            dead.append(channel)

    if dead:
        for channel in dead:
            store.remove_channel(channel["id"])
        await store.save()

        result = await core.refresh_post(context.bot)
        await notify_owner(context, ui.channels_offline(dead, len(store.channels), result))
        log.info(
            "dead: %s | post -> %s | pool left %s",
            ", ".join(str(c["id"]) for c in dead),
            result,
            len(store.channels),
        )
    elif store.post:
        # Nothing died, but confirm the post still exists. Re-rendering an
        # unchanged post is a no-op ("not modified"), so this stays cheap.
        if await core.refresh_post(context.bot) == "post-gone":
            await notify_owner(context, ui.post_deleted())

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


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log anything unhandled and tell the owner instead of dying silently."""
    err = context.error
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


def main() -> None:
    if not config.BOT_TOKEN:
        raise SystemExit("BOT_TOKEN missing — run: python3 setup.py")
    if not config.OWNER_ID:
        raise SystemExit("OWNER_ID missing — run: python3 setup.py")

    # PTB's default HTTP timeouts are 5s, which a busy VPS link regularly
    # exceeds (that was the TimedOut in the health check). Give Telegram room.
    timeout = config.HTTP_TIMEOUT
    app = (
        Application.builder()
        .token(config.BOT_TOKEN)
        .connect_timeout(timeout)
        .read_timeout(timeout)
        .write_timeout(timeout)
        .pool_timeout(timeout)
        .get_updates_connect_timeout(timeout)
        .get_updates_read_timeout(timeout)
        .get_updates_write_timeout(timeout)
        .get_updates_pool_timeout(timeout)
        .post_init(on_startup)
        .build()
    )

    # UpdateType.MESSAGE keeps *edited* messages out: an edit leaves
    # update.message as None, which used to crash the handlers.
    private = filters.ChatType.PRIVATE & filters.UpdateType.MESSAGE

    app.add_handler(CommandHandler("start", cmd_start, private))
    app.add_handler(CommandHandler("help", cmd_start, private))
    app.add_handler(CommandHandler("load", cmd_load, private))
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

    log.info("polling…")
    app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
