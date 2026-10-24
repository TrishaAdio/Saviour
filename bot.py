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

import logging
from typing import List

from telegram import Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import config
import core
import sass
from storage import store

logging.basicConfig(
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    level=logging.INFO,
    handlers=[logging.FileHandler(config.LOG_FILE, encoding="utf-8"), logging.StreamHandler()],
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("redirect-bot")

LOAD_HINT = (
    "Send me the channels now - one per line. I accept:\n"
    "• <code>@channelusername</code>\n"
    "• <code>https://t.me/channelusername</code>\n"
    "• <code>-1001234567890</code> (numeric id)\n"
    "• or just <b>forward any post</b> from a private channel\n\n"
    "Make sure I'm already an <b>admin</b> in each one with "
    "<i>Invite Users via Link</i> permission."
)


def is_owner(update: Update) -> bool:
    user = update.effective_user
    return bool(user and config.OWNER_ID and user.id == config.OWNER_ID)


# ------------------------------------------------------------- commands
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(update):
        return
    await update.message.reply_text(
        "<b>Redirect Bot online.</b>\n\n"
        "<code>/load</code> - verify + load backup channels\n"
        "<code>/status</code> - pool &amp; post status\n"
        "<code>/refresh</code> - re-render the post now\n"
        "<code>/remove</code> <code>&lt;id&gt;</code> - drop a channel\n"
        "<code>/active</code> <code>&lt;id&gt;</code> - choose the shown channel\n"
        "<code>/sass</code> - preview the death notifications\n"
        "<code>/unset</code> - forget the redirect post\n\n"
        "In your public channel just post <code>.set</code>.",
        parse_mode=ParseMode.HTML,
    )


async def cmd_load(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(update):
        return
    context.user_data["awaiting_load"] = True
    await update.message.reply_text(LOAD_HINT, parse_mode=ParseMode.HTML)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(update):
        return
    lines: List[str] = [f"<b>Pool:</b> {len(store.channels)} channel(s)"]
    for index, channel in enumerate(store.channels):
        tag = " ⭐ <i>active</i>" if index == 0 and config.LINK_MODE != "spread" else ""
        lines.append(
            f"• {channel['title']} — <code>{channel['id']}</code>{tag}\n"
            f"  <a href=\"{channel['link']}\">link</a>"
        )
    post = store.post
    lines.append("")
    if post:
        lines.append(
            f"<b>Post:</b> chat <code>{post['chat_id']}</code> · "
            f"msg <code>{post['message_id']}</code> · {post['slots']} slots"
        )
    else:
        lines.append("<b>Post:</b> not set — post <code>.set</code> in your channel")
    lines.append(f"<b>Verified:</b> {'yes' if store.verified else 'no'}")
    lines.append(f"<b>Check every:</b> {config.CHECK_INTERVAL}s")
    await update.message.reply_text(
        "\n".join(lines), parse_mode=ParseMode.HTML, disable_web_page_preview=True
    )


async def cmd_refresh(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(update):
        return
    result = await core.refresh_post(context.bot)
    await update.message.reply_text(f"Refresh: <code>{result}</code>", parse_mode=ParseMode.HTML)


async def cmd_remove(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(update):
        return
    if not context.args:
        await update.message.reply_text("Usage: <code>/remove -1001234567890</code>", parse_mode=ParseMode.HTML)
        return
    try:
        chat_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("That's not a numeric channel id.")
        return
    if not store.get_channel(chat_id):
        await update.message.reply_text("Not in the pool.")
        return
    store.remove_channel(chat_id)
    await store.save()
    result = await core.refresh_post(context.bot)
    await update.message.reply_text(
        f"Removed. Post: <code>{result}</code>", parse_mode=ParseMode.HTML
    )
    await beg_if_empty(context)


async def cmd_active(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Move a channel to the front of the pool (it becomes the shown link)."""
    if not is_owner(update):
        return
    if not context.args:
        await update.message.reply_text(
            "Usage: <code>/active -1001234567890</code>\n"
            "Makes that channel the one shown in the post.",
            parse_mode=ParseMode.HTML,
        )
        return
    try:
        chat_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("That's not a numeric channel id.")
        return
    channel = store.get_channel(chat_id)
    if not channel:
        await update.message.reply_text("Not in the pool.")
        return
    store.channels.remove(channel)
    store.channels.insert(0, channel)
    await store.save()
    result = await core.refresh_post(context.bot)
    await update.message.reply_text(
        f"⭐ <b>{channel['title']}</b> is now active. Post: <code>{result}</code>",
        parse_mode=ParseMode.HTML,
    )


async def cmd_sass(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Preview the notifications without having to kill a real channel."""
    if not is_owner(update):
        return
    fake_one = [{"title": "Movies Backup 3", "id": -1001234567890}]
    fake_many = [
        {"title": "Movies Backup 3", "id": -1001234567890},
        {"title": "Series Hub", "id": -1009876543210},
    ]
    preview = (
        f"<b>1 channel dies (2 backups left)</b>\n"
        f"{sass.death_notice(fake_one, 2, 'edited')}\n\n"
        f"<b>1 channel dies (1 backup left)</b>\n"
        f"{sass.death_notice(fake_one, 1, 'edited')}\n\n"
        f"<b>2 channels die</b>\n"
        f"{sass.death_notice(fake_many, 3, 'edited')}\n\n"
        f"<b>Out of links</b>\n{sass.beg_notice()}\n\n"
        f"<i>SASS is {'on' if config.SASS else 'off'} — toggle with SASS=false in .env</i>"
    )
    await update.message.reply_text(
        preview, parse_mode=ParseMode.HTML, disable_web_page_preview=True
    )


async def cmd_unset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(update):
        return
    store.clear_post()
    await store.save()
    await update.message.reply_text("Redirect post forgotten. Post <code>.set</code> again when ready.", parse_mode=ParseMode.HTML)


# ------------------------------------------------------ link ingestion
async def on_private_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles both the /load reply and the 'I ran out of links' reply."""
    if not is_owner(update):
        return
    message = update.message
    expecting = context.user_data.get("awaiting_load") or store.awaiting_links
    text = message.text or message.caption or ""

    targets, unresolvable = core.parse_targets(text)
    forwarded = core.forwarded_channel_id(message)
    if forwarded is not None:
        targets.append(str(forwarded))

    if not targets:
        if unresolvable:
            await message.reply_text(
                "Those are private <code>t.me/+hash</code> invites — I can't resolve "
                "those through the Bot API.\n\nForward me a post from that channel "
                "instead, or send its numeric <code>-100…</code> id.",
                parse_mode=ParseMode.HTML,
            )
        elif expecting:
            await message.reply_text(LOAD_HINT, parse_mode=ParseMode.HTML)
        return

    status = await message.reply_text(f"Checking {len(targets)} channel(s)…")
    added, refreshed, failed = [], [], []

    for target in targets:
        try:
            result = await core.verify_target(context.bot, target)
        except core.TRANSIENT as exc:
            failed.append((str(target), f"network error ({exc})"))
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

    lines: List[str] = []
    if added or refreshed:
        lines.append("✅ <b>Okay — access verified.</b>")
    for name in added:
        lines.append(f"  + {name}")
    for name in refreshed:
        lines.append(f"  ↻ {name} (link refreshed)")
    for target, reason in failed:
        lines.append(f"  ✖ <code>{target}</code> — {reason}")
    for link in unresolvable:
        lines.append(f"  ⚠️ <code>{link}</code> — forward a post from it instead")
    lines.append(f"\n<b>Pool:</b> {len(store.channels)} channel(s)")

    if store.post and (added or refreshed):
        result = await core.refresh_post(context.bot)
        lines.append(f"<b>Post:</b> <code>{result}</code>")
    elif not store.post:
        lines.append("Now post <code>.set</code> in your public channel.")

    await status.edit_text(
        "\n".join(lines), parse_mode=ParseMode.HTML, disable_web_page_preview=True
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

    try:
        await context.bot.delete_message(chat_id, post.message_id)
    except (BadRequest, Forbidden) as exc:
        log.info("could not delete .set in %s: %s", chat_id, exc)

    if not store.channels:
        await notify_owner(context, sass.no_pool_on_set(post.chat.title or "?", chat_id))
        return

    try:
        message = await core.publish_post(context.bot, chat_id, slots)
    except (BadRequest, Forbidden) as exc:
        await notify_owner(context, sass.cant_post(chat_id, exc))
        return

    if message:
        log.info("redirect post published in %s (msg %s)", chat_id, message.message_id)
        await notify_owner(
            context,
            f"📌 Redirect post set in <b>{post.chat.title}</b> with {slots} link line(s).\n"
            "I'll keep it alive automatically.",
        )


# --------------------------------------------------------- owner pings
async def notify_owner(context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    if not config.OWNER_ID:
        return
    try:
        await context.bot.send_message(
            config.OWNER_ID, text, parse_mode=ParseMode.HTML, disable_web_page_preview=True
        )
    except (BadRequest, Forbidden) as exc:
        log.error("cannot DM owner: %s", exc)


async def beg_if_empty(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Ask the owner for links once the pool runs dry (only once per drought)."""
    if len(store.channels) >= max(config.MIN_CHANNELS, 1):
        return
    if store.awaiting_links:
        return
    store.awaiting_links = True
    await store.save()
    await notify_owner(context, sass.beg_notice())


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
        await notify_owner(context, sass.death_notice(dead, len(store.channels), result))
        log.info(
            "dead: %s | post -> %s | pool left %s",
            ", ".join(str(c["id"]) for c in dead),
            result,
            len(store.channels),
        )

    await beg_if_empty(context)


async def on_startup(app: Application) -> None:
    me = await app.bot.get_me()
    log.info("started as @%s (%s)", me.username, me.id)
    if not config.OWNER_ID:
        return
    try:
        await app.bot.send_message(
            config.OWNER_ID,
            f"🤖 <b>@{me.username} is up.</b>\n"
            f"Pool: {len(store.channels)} · Post: {'set' if store.post else 'not set'}",
            parse_mode=ParseMode.HTML,
        )
    except (BadRequest, Forbidden) as exc:
        log.warning("startup DM failed (send /start to the bot once): %s", exc)


def main() -> None:
    if not config.BOT_TOKEN:
        raise SystemExit("BOT_TOKEN missing — run: python3 setup.py")
    if not config.OWNER_ID:
        raise SystemExit("OWNER_ID missing — run: python3 setup.py")

    app = Application.builder().token(config.BOT_TOKEN).post_init(on_startup).build()

    app.add_handler(CommandHandler("start", cmd_start, filters.ChatType.PRIVATE))
    app.add_handler(CommandHandler("help", cmd_start, filters.ChatType.PRIVATE))
    app.add_handler(CommandHandler("load", cmd_load, filters.ChatType.PRIVATE))
    app.add_handler(CommandHandler("status", cmd_status, filters.ChatType.PRIVATE))
    app.add_handler(CommandHandler("refresh", cmd_refresh, filters.ChatType.PRIVATE))
    app.add_handler(CommandHandler("remove", cmd_remove, filters.ChatType.PRIVATE))
    app.add_handler(CommandHandler("active", cmd_active, filters.ChatType.PRIVATE))
    app.add_handler(CommandHandler("sass", cmd_sass, filters.ChatType.PRIVATE))
    app.add_handler(CommandHandler("unset", cmd_unset, filters.ChatType.PRIVATE))

    app.add_handler(
        MessageHandler(
            filters.UpdateType.CHANNEL_POST & filters.Regex(r"^\s*\.set(\s|$)"),
            on_set_command,
        )
    )
    app.add_handler(
        MessageHandler(
            filters.ChatType.PRIVATE & ~filters.COMMAND & (filters.TEXT | filters.FORWARDED),
            on_private_message,
        )
    )

    app.job_queue.run_repeating(
        health_check, interval=config.CHECK_INTERVAL, first=30, name="health_check"
    )

    log.info("polling…")
    app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
