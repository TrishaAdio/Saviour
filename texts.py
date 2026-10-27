"""Random channel bios (the liveness probe) and owner message templates."""

import time
from typing import Optional

# One of these is written into every pool channel's bio each check round.
# It proves the account can still write there and keeps the channels looking
# alive. Keep each line under 250 characters (Telegram's about limit is 255).
BIOS = [
    "🔥 Fresh content every day. Join before the link changes.",
    "⚡ The main channel moves often — stay close.",
    "🎬 New uploads daily. Don't lose the link.",
    "🚀 Back soon with more. Invite your friends.",
    "💎 Quality over quantity. Always.",
    "📌 Save this channel, links rotate regularly.",
    "🎁 Daily drops. Stay tuned.",
    "🔔 Turn notifications on, posts move fast.",
    "🌟 The good stuff is always here first.",
    "🎯 One channel, everything you need.",
    "🆕 Updated today. Check the pinned post.",
    "📢 Official backup. Main channel rotates.",
    "🔒 Private drops for members only.",
    "🎥 Watch it here before it's gone.",
    "💬 Requests open. Ask in the comments.",
    "🧲 If the link stops working, find us again — we never disappear.",
    "🏆 The most reliable source, day after day.",
    "🎉 Thanks for being here. More coming tonight.",
    "🌍 Members from everywhere. All welcome.",
    "🛡 This channel is protected and always restored.",
    "📅 Daily schedule: new posts every evening.",
    "🎧 Relax and scroll. We do the finding for you.",
    "🍿 Grab a seat, the next drop is close.",
    "✨ Small channel, big content.",
    "🔁 Links refresh automatically — never panic.",
    "🎊 Milestone after milestone, thanks to you.",
    "🥇 Ranked #1 by our members.",
    "🧭 Lost? This channel always points you home.",
    "🌙 Late-night drops are the best drops.",
    "☀️ Good morning. New content is already up.",
]


# ------------------------------------------------------------ small helpers
def _fmt_id(chat_id: int) -> str:
    return str(chat_id)


def channel_line(channel: dict) -> str:
    status = "🔴 offline" if channel.get("offline") else "🟢 online"
    return f"{channel['title']} ({_fmt_id(channel['id'])}) — {status}"


# ------------------------------------------------------------- notifications
def startup(username: str, channels: int, post_set: bool) -> str:
    return (
        f"✅ Saviour is running as @{username}\n"
        f"Pool: {channels} channel(s) · Redirect post: {'set' if post_set else 'not set'}\n"
        "Send /help here for commands."
    )


def joined_waiting(title: str, chat_id: int) -> str:
    return (
        f"👀 Joined {title} ({_fmt_id(chat_id)})\n"
        "Waiting for admin rights — promote the account and I'll take it from there."
    )


def channel_synced(title: str, chat_id: int, link: str, can_bio: bool, pool: int) -> str:
    return (
        f"🟢 Channel synced: {title}\n"
        f"ID: {_fmt_id(chat_id)}\n"
        f"Invite: {link}\n"
        f"Bio updates: {'on' if can_bio else 'off (no Change Info right)'}\n"
        f"Pool: {pool} active"
    )


def sync_needs_invite_right(title: str, chat_id: int) -> str:
    return (
        f"🟠 {title} ({_fmt_id(chat_id)}) is missing the \"Invite Users via Link\" "
        "admin right.\nTick it and I'll add the channel automatically."
    )


def set_waiting(title: str, chat_id: int) -> str:
    return (
        f"⏳ Got your .set in {title} ({_fmt_id(chat_id)}) but I'm not admin there yet.\n"
        "Promote the account and the redirect post goes up by itself."
    )


def post_ready(title: str, chat_id: int, slots: int) -> str:
    return (
        f"✅ Redirect post is live in {title} ({_fmt_id(chat_id)})\n"
        f"Slots: {slots} link line(s). It now edits itself whenever links change."
    )


def demoted(title: str, chat_id: int) -> str:
    return (
        f"🔴 Lost admin rights in {title} ({_fmt_id(chat_id)})\n"
        "The channel was parked offline and the post was updated. "
        "Re-promote the account and it comes back by itself."
    )


def post_channel_lost(title: str, chat_id: int) -> str:
    return (
        f"🔴 I can no longer write in {title} ({_fmt_id(chat_id)}) — the redirect post is gone.\n"
        "Fix my rights and post .set there again."
    )


def channel_offline(channels: list, online: int) -> str:
    names = "\n".join(f"• {c['title']} — {c.get('reason', 'not responding')}" for c in channels)
    return (
        f"🔴 Channel(s) confirmed offline after repeated checks:\n{names}\n"
        f"Pool: {online} active. Post updated. They keep being re-checked and "
        "return by themselves if they recover."
    )


def channel_recovered(channel: dict) -> str:
    return f"🟢 {channel['title']} is back online. Post updated."


def channel_relinked(channel: dict) -> str:
    return (
        f"🔁 Invite link for {channel['title']} stopped working — "
        f"a fresh one was created and verified:\n{channel['link']}"
    )


def channel_suspect(channel: dict, strikes: int, needed: int) -> str:
    return (
        f"🟠 {channel['title']} failed a check ({strikes}/{needed}): "
        f"{channel.get('reason', 'unknown')}\nNothing changes yet — watching it."
    )


def no_links_left() -> str:
    return (
        "🆘 No live links left — the redirect post has nothing to show.\n"
        "Paste channel links here (one per line: @name, t.me/name, +invite or -100 id) "
        "and I'll verify and load them immediately."
    )


def pool_restored() -> str:
    return "✅ Links loaded — the redirect post is updated. Thanks!"


def post_deleted_reposted() -> str:
    return "♻️ The redirect post was deleted, so I posted a fresh one."


def post_deleted_manual() -> str:
    return (
        "🔴 The redirect post was deleted and REPOST_IF_DELETED is off.\n"
        "Post .set in the channel again when you're ready."
    )


def probe_failed(channel: dict, detail: str) -> str:
    return (
        f"🟠 Could not update the bio of {channel['title']}: {detail}\n"
        "Verifying the channel directly…"
    )


def help_text() -> str:
    return (
        "Saviour userbot — commands (this chat only):\n\n"
        "/status — pool, post, last check\n"
        "/channels — list every channel\n"
        "/scan — check all channels right now\n"
        "/sync — find channels I'm admin in and add them\n"
        "/load — paste links after this to add channels\n"
        "/active <id> — channel shown in the post\n"
        "/slots <n> — link lines in the post\n"
        "/refresh — re-render the post now\n"
        "/remove <id> — drop a channel from the pool\n"
        "/unset — forget the redirect post\n"
        "/cancel — cancel link input\n"
        ".clearall — wipe the account (asks for confirmation)\n\n"
        "In a channel: post .set (or .set 3) to claim the redirect post."
    )


def status_text(store, last_check_ago: Optional[str]) -> str:
    post = store.post
    lines = [
        "📊 Status",
        f"Pool: {len(store.online())} online · {len(store.offline())} offline",
        f"Redirect post: {'chat ' + str(post['chat_id']) if post else 'not set'}",
        f"Slots: {store.post_slots()}",
        f"Last check: {last_check_ago or 'never'}",
    ]
    if store.channels:
        lines.append("")
        lines.extend(channel_line(c) for c in store.channels)
    return "\n".join(lines)


def ago(ts: int) -> Optional[str]:
    if not ts:
        return None
    delta = max(int(time.time()) - ts, 0)
    if delta < 60:
        return f"{delta}s ago"
    if delta < 3600:
        return f"{delta // 60}m ago"
    return f"{delta // 3600}h ago"
