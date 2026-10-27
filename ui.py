"""How every message to the owner looks.

All messages are Telegram HTML. Every value that comes from outside - channel
titles, invite links, error text - goes through ``esc()`` first, so a title
full of "_", "*" or "<" can never break a message (that is why the old plain
text existed at all).

Building blocks:

    card(icon, title, rows, ...)   a titled card with a quoted body
    kv(label, value)               one "Label: value" row
    channel_line(channel)          one channel in a list

Telegram allows at most 100 formatting entities per message, so lists are
cut into pages with ``pages()``. A page stays well under that limit.
"""

import html as _html
import time

from telethon.extensions import html as _tl_html
from telethon.helpers import add_surrogate, del_surrogate, strip_text

PER_PAGE = 25           # channels per list message (keeps entities < 100)


# ------------------------------------------------------------------ markup
def esc(value):
    return _html.escape(str(value), quote=False)


def b(value):
    return f"<b>{esc(value)}</b>"


def i(value):
    return f"<i>{esc(value)}</i>"


def code(value):
    return f"<code>{esc(value)}</code>"


def kv(label, value):
    """A row. ``value`` is already HTML (use esc/code/b on raw values)."""
    return f"<b>{esc(label)}:</b> {value}"


def quote(lines, expandable=False):
    lines = [l for l in lines if l is not None]
    if not lines:
        return ""
    tag = "<blockquote expandable>" if expandable else "<blockquote>"
    return tag + "\n".join(lines) + "</blockquote>"


def card(icon, title, rows=(), text=None, foot=None, extra=None):
    """
    ✅ Title
    ┃ row
    ┃ row
    free text
    extra (more blocks, already HTML)
    foot, in italics
    """
    parts = [f"{icon} <b>{esc(title)}</b>"]
    if rows:
        parts.append(quote(list(rows)))
    if text:
        parts.append(text)
    if extra:
        parts.extend(p for p in extra if p)
    if foot:
        parts.append(f"<i>{esc(foot)}</i>")
    return "\n".join(parts)


# ---------------------------------------------------------------- pieces
def when(ts):
    if not ts:
        return "never"
    gap = max(int(time.time()) - ts, 0)
    if gap < 60:
        return f"{gap}s ago"
    if gap < 3600:
        return f"{gap // 60}m ago"
    if gap < 86400:
        return f"{gap // 3600}h {gap % 3600 // 60}m ago"
    return f"{gap // 86400}d ago"


def dot(channel):
    return "🔴" if channel.get("offline") else "🟢"


def channel_line(channel, active_id=None):
    star = " ⭐" if active_id is not None and channel["id"] == active_id else ""
    line = f"{dot(channel)} {b(channel['title'])}{star}\n      {code(channel['id'])}"
    if channel.get("offline") and channel.get("reason"):
        line += f"\n      ↳ {i(channel['reason'])}"
    return line


def link_line(channel):
    return (f"{dot(channel)} {b(channel['title'])}  {code(channel['id'])}\n"
            f"      {esc(channel['link'])}")


def post_result(result):
    """engine.redraw_post()'s one-word result, said nicely."""
    words = {
        "updated": "✏️ updated",
        "already-right": "✔️ already up to date",
        "none": "— no post yet",
        "reposted": "♻️ posted again",
        "post-deleted": "🗑 it was deleted",
    }
    if result in words:
        return words[result]
    return "⚠️ " + esc(result)


def progress(done, total, width=10):
    """▰▰▰▱▱▱▱▱▱▱"""
    filled = round(width * done / total) if total else width
    return "▰" * filled + "▱" * (width - filled)


def pages(items, per=PER_PAGE):
    return [items[n:n + per] for n in range(0, len(items), per)] or [[]]


def page_title(title, page, total):
    return title if total == 1 else f"{title} ({page}/{total})"


# ---------------------------------------------------- Telethon parse mode
class _Parser(_tl_html.HTMLToTelegramParser):
    """Telethon's HTML parser, plus <blockquote expandable>."""

    def handle_starttag(self, tag, attrs):
        super().handle_starttag(tag, attrs)
        if tag == "blockquote" and "expandable" in dict(attrs):
            entity = self._building_entities.get("blockquote")
            if entity is not None:
                entity.collapsed = True


class HTML:
    """Pass as ``parse_mode=ui.HTML`` - same as "html", with expandable quotes."""

    @staticmethod
    def parse(text):
        if not text:
            return text, []
        parser = _Parser()
        parser.feed(add_surrogate(text))
        out = strip_text(parser.text, parser.entities)
        parser.entities.reverse()
        parser.entities.sort(key=lambda e: e.offset)
        return del_surrogate(out), parser.entities

    @staticmethod
    def unparse(text, entities):
        return _tl_html.unparse(text, entities)
