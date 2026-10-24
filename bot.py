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
from telegram.constants import ChatMemberStatus, ParseMode
from telegram.error import BadRequest, Forbidden
from telegram.ext import (
    Application,
    ChatMemberHandler,
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
        "Just <b>make me admin</b> in a channel and I'll detect it, create an "
        "invite link and add it to the pool by myself.\n\n"
        "<code>/load</code> - add channels manually (for ones I'm already in)\n"
        "<code>/status</code> - pool &amp; post status\n"
        "<code>/refresh</code> - re-render the post now\n"
        "<code>/remove</code> <code>&lt;id&gt;</code> - drop a channel\n"
        "<code>/active</code> <code>&lt;id&gt;</code> - choose the shown channel\n"
        "<code>/slots</code> <code>&lt;n&gt;</code> - how many link lines to show\n"
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


async def cmd_slots(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Change how many link lines the post shows, live."""
    if not is_owner(update):
        return
    current = store.post["slots"] if store.post else config.POST_SLOTS
    if not context.args:
        await update.message.reply_text(
            f"Post currently shows <b>{current}</b> link line(s).\n"
            f"Default from <code>.env</code>: <b>{config.POST_SLOTS}</b>\n\n"
            "Change it with <code>/slots 5</code>.",
            parse_mode=ParseMode.HTML,
        )
        return
    try:
        count = max(1, min(20, int(context.args[0])))
    except ValueError:
        await update.message.reply_text("Give me a number, e.g. <code>/slots 5</code>", parse_mode=ParseMode.HTML)
        return

    if store.post and store.post.get("template"):
        await update.message.reply_text(
            "This post was taken over with a reply <code>.set</code>, so its line "
            "count is whatever the original post has. Edit the post's links count "
            "and reply <code>.set</code> to it again to change it.",
            parse_mode=ParseMode.HTML,
        )
        return

    if not store.post:
        await update.message.reply_text(
            f"No post is set yet. Post <code>.set {count}</code> in your channel, "
            f"or set <code>POST_SLOTS={count}</code> in <code>.env</code>.",
            parse_mode=ParseMode.HTML,
        )
        return

    store.post["slots"] = count
    await store.save()
    result = await core.refresh_post(context.bot)
    await update.message.reply_text(
        f"Post now shows <b>{count}</b> link line(s). Result: <code>{result}</code>",
        parse_mode=ParseMode.HTML,
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

    # This channel displays the post, so it must not be one of its own targets
    # (auto-detect will have grabbed it when you promoted the bot here).
    if store.get_channel(chat_id):
        store.remove_channel(chat_id)
        await store.save()
        log.info("%s hosts the post - removed it from the link pool", chat_id)

    if not store.channels:
        await notify_owner(context, sass.no_pool_on_set(post.chat.title or "?", chat_id))
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
        await notify_owner(context, sass.cant_edit_set(chat_id, exc))
        return

    if adopted:
        await notify_owner(
            context,
            sass.post_adopted(post.chat.title or str(chat_id), chat_id, slots),
        )


async def adopt_replied_post(context: ContextTypes.DEFAULT_TYPE, cmd, target) -> None:
    """``.set`` replied to an existing post: edit that post, delete ``.set``."""
    chat_id = cmd.chat_id
    title = cmd.chat.title or str(chat_id)
    try:
        result = await core.adopt_existing(context.bot, target)
    except (BadRequest, Forbidden) as exc:
        await notify_owner(context, sass.cant_edit_set(chat_id, exc))
        return
    if not result["ok"]:
        await notify_owner(
            context,
            f"⚠️ Can't take over that post in <b>{sass.esc(title)}</b>: "
            f"{sass.esc(result['reason'])}.",
        )
        return

    removed = True
    try:
        await context.bot.delete_message(chat_id, cmd.message_id)
    except (BadRequest, Forbidden) as exc:
        removed = False
        log.info("could not delete the .set reply in %s: %s", chat_id, exc)

    await notify_owner(
        context, sass.post_taken_over(title, chat_id, result["slots"], removed)
    )


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
            await notify_owner(
                context,
                f"ℹ️ I was made admin in the group <b>{sass.esc(chat.title)}</b>, "
                "but I only work with <b>channels</b>. Ignoring it.",
            )
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
            context, sass.access_lost(channel, reason, len(store.channels), result)
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
        await notify_owner(context, sass.missing_rights(title, chat.id, report))
        return

    result = await core.register_channel(context.bot, chat, title)
    if not result["ok"]:
        await notify_owner(
            context,
            f"⚠️ I'm admin in <b>{sass.esc(title)}</b> (<code>{chat.id}</code>) but "
            f"{result['reason']}. Check my permissions there.",
        )
        return

    post_result = await core.refresh_post(context.bot) if store.post else "no-post"
    log.info("auto-added %s (%s), post -> %s", chat.id, title, post_result)
    await notify_owner(
        context,
        sass.auto_added(
            title, chat.id, result["link"], result["new"], report,
            len(store.channels), post_result,
        ),
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
    elif store.post:
        # Nothing died, but confirm the post still exists. Re-rendering an
        # unchanged post is a no-op ("not modified"), so this stays cheap.
        if await core.refresh_post(context.bot) == "post-gone":
            await notify_owner(context, sass.post_vanished())

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


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log anything unhandled and tell the owner instead of dying silently."""
    log.error("unhandled exception", exc_info=context.error)
    if not config.OWNER_ID:
        return
    err = context.error
    where = ""
    if isinstance(update, Update) and update.effective_chat:
        where = f"\nChat: <code>{update.effective_chat.id}</code>"
    try:
        await context.bot.send_message(
            config.OWNER_ID,
            "💥 <b>Something broke.</b>\n"
            f"<code>{sass.esc(type(err).__name__)}: {sass.esc(err)[:300]}</code>"
            f"{where}\n\nI'm still running. Check <code>bot.log</code> for the traceback.",
            parse_mode=ParseMode.HTML,
        )
    except Exception:  # never let the error handler raise
        log.exception("could not report the error to the owner")


def main() -> None:
    if not config.BOT_TOKEN:
        raise SystemExit("BOT_TOKEN missing — run: python3 setup.py")
    if not config.OWNER_ID:
        raise SystemExit("OWNER_ID missing — run: python3 setup.py")

    app = Application.builder().token(config.BOT_TOKEN).post_init(on_startup).build()

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
    app.add_handler(CommandHandler("sass", cmd_sass, private))
    app.add_handler(CommandHandler("unset", cmd_unset, private))

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
