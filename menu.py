"""Inline control panel.

``/start`` sends one panel message. Every button edits that same message in
place; nothing new is posted per click. Each sub-screen has a Back button.

Callback data (all under 64 bytes, Telegram's limit):

    m:home                 main panel
    m:status               overview
    m:chs:<page>           channel list
    m:ch:<id>              one channel
    m:act:<id>             make channel active
    m:rm:<id>              ask to remove a channel
    m:rmok:<id>            remove it
    m:post                 redirect post
    m:refresh              re-apply links to the post
    m:lines:<n>            set link lines (default-format posts)
    m:unset                ask to stop monitoring
    m:unsetok              stop monitoring
    m:add                  add channels (instructions)
    m:help                 how it works
    m:close                remove the panel
"""

from typing import List, Optional, Tuple

from telegram import InlineKeyboardButton as Btn
from telegram import InlineKeyboardMarkup

import config
import ui
from storage import store

PAGE_SIZE = 8
Screen = Tuple[str, InlineKeyboardMarkup]


def _kb(rows: List[List[Btn]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(rows)


def _back(to: str = "m:home", label: str = "‹ Back") -> List[Btn]:
    return [Btn(label, callback_data=to)]


def _short(text: str, limit: int = 28) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _spread() -> bool:
    return config.LINK_MODE == "spread"


# ---------------------------------------------------------------- home
def home() -> Screen:
    pool = len(store.channels)
    post = store.post
    if pool == 0:
        dot, health = ui.CRIT, "No live links"
    elif not post:
        dot, health = ui.WARN, "No post linked"
    elif pool == 1:
        dot, health = ui.WARN, "Running without a backup"
    else:
        dot, health = ui.OK, "Protected"

    import userbot
    if not userbot.logged_in() and dot == ui.OK:
        dot, health = ui.WARN, "Link checks unverified"
    text = ui.card(dot, "Saviour control panel", [
        ("Health", health),
        ("Channels", ui.pool_value(pool)),
        ("Post", "Monitored" if post else "Not linked"),
        ("Verify", ui.verify_method()),
        ("Last check", ui.ago(store.data.get("last_check"))),
    ], "Choose an option below." if userbot.logged_in() else
       "Send <code>/login</code> to connect a user account so I can verify links "
       "with Telegram directly.")
    return text, _kb([
        [Btn("📊 Status", callback_data="m:status"),
         Btn(f"📡 Channels · {pool}", callback_data="m:chs:0")],
        [Btn("📝 Redirect post", callback_data="m:post"),
         Btn("➕ Add channels", callback_data="m:add")],
        [Btn("❔ How it works", callback_data="m:help"),
         Btn("✕ Close", callback_data="m:close")],
    ])


# -------------------------------------------------------------- status
def status() -> Screen:
    text = ui.status(store.channels, store.post, _spread(), store.data.get("last_check"))
    return text, _kb([
        [Btn("↻ Update", callback_data="m:status")],
        _back(),
    ])


# ------------------------------------------------------------ channels
def channels(page: int = 0) -> Screen:
    chans = store.channels
    if not chans:
        text = ui.card(ui.WARN, "Channels", [("Pool", "<b>empty</b>")],
                       "Make me admin in a channel and it connects automatically, "
                       "or add one manually.")
        return text, _kb([[Btn("➕ Add channels", callback_data="m:add")], _back()])

    pages = max((len(chans) - 1) // PAGE_SIZE + 1, 1)
    page = min(max(page, 0), pages - 1)
    start = page * PAGE_SIZE
    chunk = chans[start:start + PAGE_SIZE]

    note = "Tap a channel to manage it."
    if not _spread():
        note = "⭐ marks the channel shown in the post. " + note
    rows = [("Pool", ui.pool_value(len(chans)))]
    if pages > 1:
        rows.append(("Page", f"{page + 1} of {pages}"))
    text = ui.card(ui.INFO, "Channels", rows, note)

    buttons = []
    for i, ch in enumerate(chunk, start=start):
        star = "⭐ " if i == 0 and not _spread() else ""
        buttons.append([Btn(f"{star}{_short(ch['title'])}", callback_data=f"m:ch:{ch['id']}")])
    nav = []
    if page > 0:
        nav.append(Btn("‹ Prev", callback_data=f"m:chs:{page - 1}"))
    if page < pages - 1:
        nav.append(Btn("Next ›", callback_data=f"m:chs:{page + 1}"))
    if nav:
        buttons.append(nav)
    buttons.append(_back())
    return text, _kb(buttons)


def channel(chat_id: int) -> Optional[Screen]:
    ch = store.get_channel(chat_id)
    if not ch:
        return None
    index = store.channels.index(ch)
    active = index == 0 and not _spread()
    role = "Shown in the post" if active else ("In rotation" if _spread() else "Backup")
    text = ui.card(ui.INFO, ch["title"], [
        ("ID", ui.code(ch["id"])),
        ("Invite", ui.link(ch["link"])),
        ("Role", role),
        ("Position", f"{index + 1} of {len(store.channels)}"),
    ])
    rows = []
    if not active and not _spread():
        rows.append([Btn("⭐ Make active", callback_data=f"m:act:{chat_id}")])
    rows.append([Btn("🗑 Remove", callback_data=f"m:rm:{chat_id}")])
    rows.append(_back(f"m:chs:{index // PAGE_SIZE}", "‹ Channels"))
    return text, _kb(rows)


def confirm_remove(chat_id: int) -> Optional[Screen]:
    ch = store.get_channel(chat_id)
    if not ch:
        return None
    left = len(store.channels) - 1
    warn = ("This is your last channel; the post will have no live link."
            if left == 0 else f"{ui.plural(left, 'channel')} will remain.")
    text = ui.card(ui.WARN, "Remove channel?", [
        ("Channel", f"<b>{ui.esc(ch['title'])}</b>"),
        ("ID", ui.code(ch["id"])),
    ], warn)
    return text, _kb([
        [Btn("Remove", callback_data=f"m:rmok:{chat_id}"),
         Btn("Cancel", callback_data=f"m:ch:{chat_id}")],
    ])


# ---------------------------------------------------------------- post
def post() -> Screen:
    p = store.post
    if not p:
        text = ui.card(ui.WARN, "Redirect post", [("Status", "Not linked")],
                       "In your public channel, reply <code>.set</code> to your "
                       "redirect post. Or send <code>.set</code> to create one.")
        return text, _kb([_back()])

    cloned = bool(p.get("template"))
    text = ui.card(ui.INFO, "Redirect post", [
        ("Channel", ui.code(p["chat_id"])),
        ("Message", ui.code(p["message_id"])),
        ("Links", str(p["slots"])),
        ("Format", "Original kept" if cloned else "Default"),
        ("Showing", ui.link(store.channels[0]["link"]) if store.channels else "<i>no live link</i>"),
    ], None if not cloned else
        "The link count follows your post. Edit the post and reply "
        "<code>.set</code> again to change it.")

    rows = [[Btn("↻ Refresh links", callback_data="m:refresh")]]
    if not cloned:
        n = p["slots"]
        rows.append([
            Btn("−", callback_data=f"m:lines:{max(n - 1, 1)}"),
            Btn(f"{n} lines", callback_data="m:post"),
            Btn("+", callback_data=f"m:lines:{min(n + 1, 20)}"),
        ])
    rows.append([Btn("⏹ Stop monitoring", callback_data="m:unset")])
    rows.append(_back())
    return text, _kb(rows)


def confirm_unset() -> Screen:
    text = ui.card(ui.WARN, "Stop monitoring?", (),
                   "The post stays in your channel, but its links will no longer "
                   "be updated when a channel goes down.")
    return text, _kb([
        [Btn("Stop monitoring", callback_data="m:unsetok"),
         Btn("Cancel", callback_data="m:post")],
    ])


# --------------------------------------------------------- add / help
def add() -> Screen:
    text = (
        f"{ui.INFO} <b>Add channels</b>\n"
        "<b>Automatic</b>\nMake me admin in a channel with <i>Invite Users via "
        "Link</i>. It connects on its own.\n\n"
        "<b>Manual</b>\nSend the channels in this chat now, one per line:\n"
        "<blockquote>• <code>@username</code> or <code>t.me/username</code>\n"
        "• Numeric id: <code>-1001234567890</code>\n"
        "• A forwarded post from a private channel</blockquote>"
    )
    return text, _kb([_back()])


def help_screen() -> Screen:
    minutes = max(config.CHECK_INTERVAL // 60, 1)
    text = (
        f"{ui.INFO} <b>How it works</b>\n"
        "<blockquote><b>1.</b> Make me admin in your backup channels. They connect "
        "automatically.\n"
        "<b>2.</b> In your public channel, reply <code>.set</code> to your redirect "
        "post.\n"
        f"<b>3.</b> Every {ui.plural(minutes, 'minute')} I check each channel. If one "
        "goes down, I swap the link in the post and notify you.</blockquote>\n"
        "<b>Commands</b>\n"
        "<code>/start</code>  Open this panel\n"
        "<code>/login</code>  Connect a user account (reliable link checks)\n"
        "<code>/logout</code>  Disconnect it\n"
        "<code>/load</code>  Add channels by link or id\n"
        "<code>/preview</code>  Sample notifications\n"
        "<code>/cancel</code>  Stop what's in progress"
    )
    return text, _kb([_back()])


def closed() -> str:
    return f"{ui.INFO} <b>Panel closed</b>\nSend /start to open it again."
