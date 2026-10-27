"""Take over an existing post and keep its exact look.

Reply ".set" to a post and its text and formatting (quote / expandable quote,
bold, colours, custom emoji, hidden hyperlinks...) are saved as a template.
Every later edit rebuilds that same text with the same formatting and swaps
only the t.me links, so the post keeps looking exactly the way you made it.

Telegram measures entity offsets in UTF-16 code units, not Python characters,
so all the position maths below happens in UTF-16.
"""

import re

from telethon import types

# A t.me link written in the post text.
_LINK = re.compile(r"(?:https?://)?(?:t|telegram)\.(?:me|dog)/[^\s]+", re.IGNORECASE)
_TRAILING = ".,;:!?)]}»\"'”’"


def _u16(text):
    return len(text.encode("utf-16-le")) // 2


def _is_tme(url):
    return bool(url) and bool(_LINK.fullmatch(url.strip().rstrip(_TRAILING)))


def _visible(text):
    """(start, end) UTF-16 ranges of every t.me link written in the text."""
    spans = []
    for match in _LINK.finditer(text):
        clean = match.group(0).rstrip(_TRAILING)
        start = _u16(text[:match.start()])
        spans.append((start, start + _u16(clean)))
    return spans


def _overlaps(ent, spans):
    start, end = ent["offset"], ent["offset"] + ent["length"]
    return any(s < end and start < e for s, e in spans)


def _hidden(entities, visible):
    """Hyperlinks (text with a t.me link behind it) that aren't visible links."""
    return [e for e in entities
            if e.get("_") == "MessageEntityTextUrl" and _is_tme(e.get("url"))
            and not _overlaps(e, visible)]


# ------------------------------------------------------------- snapshot
def snapshot(message):
    """A message's text + formatting as plain JSON-able data, or None."""
    text = getattr(message, "message", None)
    if not text:
        return None
    return {
        "text": text,
        "entities": [e.to_dict() for e in (message.entities or ())],
        "media": message.media is not None
                 and not isinstance(message.media, types.MessageMediaWebPage),
        "preview": isinstance(message.media, types.MessageMediaWebPage),
    }


def slot_count(tpl):
    """How many links the post holds - visible ones plus hidden hyperlinks."""
    visible = _visible(tpl["text"])
    return len(visible) + len(_hidden(tpl["entities"], visible))


def _build(ent):
    ent = dict(ent)
    cls = getattr(types, ent.pop("_"), None)
    return cls(**ent) if cls else None


# --------------------------------------------------------------- render
def render(tpl, link):
    """The post with every link position pointing at ``link``.

    Returns (text, [MessageEntity...]).
    """
    text = tpl["text"]
    entities = [dict(e) for e in tpl["entities"]]
    visible = _visible(text)

    for ent in entities:
        if ent.get("_") == "MessageEntityTextUrl" and _is_tme(ent.get("url")):
            ent["url"] = link            # hidden or on a visible link: both follow

    units = text.encode("utf-16-le")
    new = link.encode("utf-16-le")
    out, shifts, cursor = bytearray(), [], 0
    for start, end in visible:
        out += units[cursor * 2:start * 2] + new
        shifts.append((start, end, len(new) // 2))
        cursor = end
    out += units[cursor * 2:]

    def move(pos):
        delta = 0
        for start, end, new_len in shifts:
            if end <= pos:
                delta += new_len - (end - start)
            elif start < pos < end:      # a boundary inside a link: clamp into it
                return start + delta + min(pos - start, new_len)
        return pos + delta

    rebuilt = []
    for ent in entities:
        start = move(ent["offset"])
        end = move(ent["offset"] + ent["length"])
        if end <= start:
            continue
        ent["offset"], ent["length"] = start, end - start
        built = _build(ent)
        if built is not None:
            rebuilt.append(built)
    return out.decode("utf-16-le"), rebuilt
