"""Owner-facing messages.

Every notification is a *card* with the same anatomy, so the owner can scan
any message at a glance:

    🟢 Channel connected            <- severity dot + short title
    ┃ Channel   Demo Afx            <- details panel (blockquote)
    ┃ ID        -1004424595437
    ┃ Pool      3 active
    The redirect post now uses it.  <- one line: what happens / what to do

Severity: 🟢 done · 🔵 info · 🟠 needs attention · 🔴 critical.
"""

import html
import time
from typing import Iterable, List, Optional, Sequence, Tuple

import config

OK, INFO, WARN, CRIT = "🟢", "🔵", "🟠", "🔴"

Row = Tuple[str, str]


def esc(value) -> str:
    """Escape for Telegram HTML text. Quotes only matter inside attributes."""
    return html.escape(str(value), quote=False)


def code(value) -> str:
    return f"<code>{esc(value)}</code>"


def link(url: str) -> str:
    """Show a link without the https:// noise."""
    shown = url.split("://", 1)[-1]
    return f'<a href="{html.escape(url, quote=True)}">{esc(shown)}</a>'


def card(dot: str, title: str, rows: Sequence[Row] = (),
         note: Optional[str] = None, items: Iterable[str] = ()) -> str:
    """Render a notification card. ``rows`` values must already be HTML-safe."""
    parts: List[str] = [f"{dot} <b>{esc(title)}</b>"]
    body = list(items)  # bullet list first, then the label rows
    body += [f"<b>{esc(label)}</b>  {value}" for label, value in rows]
    if body:
        parts.append("<blockquote>" + "\n".join(body) + "</blockquote>")
    if note:
        parts.append(note)
    return "\n".join(parts)


def plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def pool_value(count: int) -> str:
    if count == 0:
        return "<b>empty</b>"
    if count == 1:
        return "1 active · <i>no backup</i>"
    return f"{count} active"


# --------------------------------------------------------- post status
_POST_STATUS = {
    "edited": "Updated",
    "unchanged": "Up to date",
    "reposted": "Re-published (original was deleted)",
    "no-links": "Not updated · no live links",
    "no-post": "Not set",
    "post-gone": "Deleted · send <code>.set</code> again",
    "network-error": "Delayed · Telegram timeout, retrying next check",
}


def post_status(result: str) -> str:
    if result in _POST_STATUS:
        return _POST_STATUS[result]
    return f"Failed · {esc(result.removeprefix('error: '))}"


# ---------------------------------------------------------- lifecycle
def startup(username: str, pool: int, post_set: bool) -> str:
    minutes = max(config.CHECK_INTERVAL // 60, 1)
    return card(INFO, "Saviour is online", [
        ("Bot", f"@{esc(username)}"),
        ("Pool", pool_value(pool)),
        ("Post", "Active" if post_set else "Not set"),
        ("Checks", f"every {plural(minutes, 'minute')}"),
    ])


def unexpected_error(err: BaseException, chat_id: Optional[int]) -> str:
    rows = [("Error", code(f"{type(err).__name__}: {str(err)[:300]}"))]
    if chat_id is not None:
        rows.append(("Chat", code(chat_id)))
    return card(CRIT, "Unexpected error", rows,
                "The bot is still running. Full details are in <code>bot.log</code>.")


# ----------------------------------------------------------- channels
def channel_connected(title, chat_id, invite, is_new, report, pool,
                      post_result, rescued: bool) -> str:
    if report["full"]:
        access = "Full admin"
    else:
        access = "Links only · can't host the post"
    rows = [
        ("Channel", f"<b>{esc(title)}</b>"),
        ("ID", code(chat_id)),
        ("Invite", link(invite)),
        ("Access", access),
        ("Pool", pool_value(pool)),
    ]
    if post_result != "no-post":
        rows.append(("Post", post_status(post_result)))

    if rescued:
        note = "Back online: the redirect post points to a live channel again."
    elif not is_new:
        note = "Already connected; its invite link was refreshed."
    elif report["missing_post"]:
        note = ("Missing " + ", ".join(report["missing_post"])
                + ". Fine as a backup, but it can't host <code>.set</code>.")
    else:
        note = None
    return card(OK, "Channel connected", rows, note)


def channel_disconnected(channel: dict, reason: str, pool: int, post_result: str) -> str:
    reasons = {
        "demoted": "Bot was demoted",
        "left": "Bot was removed",
        "kicked": "Bot was banned",
        "member": "Bot was demoted",
    }
    rows = [
        ("Channel", f"<b>{esc(channel['title'])}</b>"),
        ("ID", code(channel["id"])),
        ("Reason", reasons.get(reason, esc(reason))),
        ("Pool", pool_value(pool)),
        ("Post", post_status(post_result)),
    ]
    return card(WARN, "Channel disconnected", rows, _pool_advice(pool))


def channels_offline(dead: List[dict], pool: int, post_result: str) -> str:
    tail = [("Pool", pool_value(pool)), ("Post", post_status(post_result))]
    if len(dead) == 1:
        rows = [("Channel", f"<b>{esc(dead[0]['title'])}</b>"),
                ("ID", code(dead[0]["id"]))]
        if dead[0].get("reason"):
            rows.append(("Reason", esc(dead[0]["reason"])))
        return card(CRIT, "Channel offline", rows + tail, _pool_advice(pool))
    items = [f"• <b>{esc(c['title'])}</b>  {code(c['id'])}"
             + (f"\n   {esc(c['reason'])}" if c.get("reason") else "") for c in dead]
    return card(CRIT, f"{len(dead)} channels offline", tail, _pool_advice(pool), items)


def channel_relinked(channel: dict, post_result: str) -> str:
    return card(WARN, "Invite link replaced", [
        ("Channel", f"<b>{esc(channel['title'])}</b>"),
        ("ID", code(channel["id"])),
        ("New link", link(channel["link"])),
        ("Post", post_status(post_result)),
    ], "The old invite link stopped working, so I created a new one.")


def post_update_failed(result: str) -> str:
    return card(CRIT, "Post could not be updated", [("Error", post_status(result))],
                "The post may still show a dead link. Check my <i>Edit Messages</i> "
                "right in the channel, then press Refresh in /start.")


def _pool_advice(pool: int) -> Optional[str]:
    if pool == 0:
        return None  # the "no live links" card follows with instructions
    if pool == 1:
        return "Only one channel left. Add a backup to stay protected."
    return None


def no_live_links() -> str:
    return card(CRIT, "Action required · no live links", [
        ("Pool", "<b>empty</b>"),
        ("Post", "Pointing to offline channels"),
    ], "Send a new channel link in this chat and the post will be updated "
       "immediately. Or make me admin in a new channel and it connects "
       "automatically.")


def permission_required(title, chat_id, report) -> str:
    missing = ", ".join(report["missing_link"] + report["missing_post"])
    return card(WARN, "Permission required", [
        ("Channel", f"<b>{esc(title)}</b>"),
        ("ID", code(chat_id)),
        ("Missing", esc(missing)),
    ], "Enable <i>Invite Users via Link</i> and the channel connects automatically.")


def invite_failed(title, chat_id, reason: str) -> str:
    return card(WARN, "Couldn't create invite link", [
        ("Channel", f"<b>{esc(title)}</b>"),
        ("ID", code(chat_id)),
        ("Reason", esc(reason)),
    ], "The channel was not added. Fix the issue above, then demote and "
       "re-promote me (or use /load).")


def group_ignored(title) -> str:
    return card(INFO, "Group ignored", [("Group", f"<b>{esc(title)}</b>")],
                "Saviour only works with channels.")


# --------------------------------------------------------------- post
def post_linked(title, chat_id, slots, removed_set: bool) -> str:
    rows = [
        ("Channel", f"<b>{esc(title)}</b>"),
        ("ID", code(chat_id)),
        ("Links", str(slots)),
        ("Format", "Original kept"),
    ]
    note = ("Your <code>.set</code> message was removed. The post is now monitored."
            if removed_set else
            "The post is now monitored. I couldn't remove your <code>.set</code> "
            "message (needs <i>Delete Messages</i>); delete it manually.")
    return card(OK, "Redirect post linked", rows, note)


def post_created(title, chat_id, slots) -> str:
    return card(OK, "Redirect post created", [
        ("Channel", f"<b>{esc(title)}</b>"),
        ("ID", code(chat_id)),
        ("Links", str(slots)),
    ], "The post is now monitored. Change the line count with <code>/slots</code>.")


def post_failed(chat_id, error) -> str:
    return card(CRIT, "Couldn't edit the post", [
        ("Channel", code(chat_id)),
        ("Telegram", esc(error)),
    ], "Grant me <i>Edit Messages</i> in that channel, then send <code>.set</code> again.")


def post_rejected(title, reason: str) -> str:
    return card(WARN, "Post not linked", [
        ("Channel", f"<b>{esc(title)}</b>"),
        ("Reason", esc(reason)),
    ], "Reply <code>.set</code> to a post that contains t.me links.")


def post_deleted() -> str:
    return card(WARN, "Redirect post deleted", [("Post", "No longer exists")],
                "Reply <code>.set</code> to a post, or send <code>.set</code> to "
                "create a new one.")


def nothing_to_link(title, chat_id) -> str:
    return card(WARN, "No channels connected", [
        ("Channel", f"<b>{esc(title)}</b>"),
        ("ID", code(chat_id)),
    ], "Make me admin in a backup channel or use /load, then send "
       "<code>.set</code> again.")


# ----------------------------------------------------------- commands
def load_prompt() -> str:
    return card(INFO, "Add channels", (),
                "Send them one per line. I must already be admin in each, with "
                "<i>Invite Users via Link</i>.", [
                    "• <code>@username</code> or <code>t.me/username</code>",
                    "• Numeric id: <code>-1001234567890</code>",
                    "• A forwarded post from a private channel",
                ])


def private_invites_unsupported() -> str:
    return card(WARN, "Private invite links can't be resolved", (),
                "Telegram doesn't let bots look up <code>t.me/+…</code> links. "
                "Forward a post from that channel instead, or send its "
                "<code>-100…</code> id.")


def load_result(added, refreshed, failed, unresolvable, pool, post_line: Optional[str]) -> str:
    ok = bool(added or refreshed)
    items = [f"✓ <b>{esc(n)}</b>" for n in added]
    items += [f"↻ <b>{esc(n)}</b> · link refreshed" for n in refreshed]
    items += [f"✕ <code>{esc(t)}</code> · {esc(r)}" for t, r in failed]
    items += [f"✕ <code>{esc(l)}</code> · forward a post instead" for l in unresolvable]
    rows: List[Row] = [("Pool", pool_value(pool))]
    if post_line:
        rows.append(("Post", post_line))
    title = "Access verified" if ok else "No channels added"
    note = None
    if ok and not post_line:
        note = "Now reply <code>.set</code> to your post in the public channel."
    return card(OK if ok else WARN, title, rows, note, items)


def ago(ts: Optional[int]) -> str:
    if not ts:
        return "never"
    secs = max(int(time.time()) - int(ts), 0)
    if secs < 90:
        return "just now"
    if secs < 5400:
        return f"{secs // 60} min ago"
    return f"{secs // 3600} h {secs % 3600 // 60} min ago"


def status(channels: List[dict], post: Optional[dict], spread: bool,
           last_check: Optional[int] = None) -> str:
    items = []
    for i, ch in enumerate(channels):
        star = "  ⭐" if i == 0 and not spread else ""
        items.append(f"• <b>{esc(ch['title'])}</b>{star}\n   {code(ch['id'])} · {link(ch['link'])}")
    if not items:
        items.append("<i>No channels connected</i>")
    if post:
        fmt = "original kept" if post.get("template") else "default"
        post_txt = (f"{code(post['chat_id'])} · msg {code(post['message_id'])}\n"
                    f"{post['slots']} links · format {fmt}")
    else:
        post_txt = "<i>Not set</i>"
    minutes = max(config.CHECK_INTERVAL // 60, 1)
    return (
        f"{INFO} <b>Status</b>\n"
        f"<b>Channels</b> · {pool_value(len(channels))}\n"
        "<blockquote>" + "\n".join(items) + "</blockquote>\n"
        "<b>Redirect post</b>\n"
        f"<blockquote>{post_txt}</blockquote>\n"
        f"<b>Last check</b>  {ago(last_check)}\n"
        f"<i>Mode {'spread' if spread else 'single'} · checks every "
        f"{plural(minutes, 'minute')}</i>"
    )


def done(title: str, rows: Sequence[Row] = (), note: Optional[str] = None) -> str:
    return card(OK, title, rows, note)


def problem(title: str, note: str) -> str:
    return card(WARN, title, (), note)
