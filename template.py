"""Clone an existing post's exact formatting and swap only its links.

When you reply ``.set`` to a post, the bot snapshots that post's text and its
Telegram entities (blockquote / expandable blockquote, bold, custom emoji,
hyperlinks...). Every later edit rebuilds the very same text with the same
entities - only the t.me links change. Nothing is re-rendered from HTML, so
the post looks exactly like the one you made.

Telegram entity offsets are measured in UTF-16 code units, not Python
characters, so all position maths below happens in UTF-16.
"""

import re
from typing import Any, Dict, List, Optional, Tuple

from telegram import MessageEntity

# A visible Telegram link inside the post text.
_LINK_RE = re.compile(r"(?:https?://)?(?:t|telegram)\.(?:me|dog)/[^\s]+", re.IGNORECASE)
_TRAILING = ".,;:!?)]}»\"'"


def _u16(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def _is_tme(url: Optional[str]) -> bool:
    return bool(url) and bool(_LINK_RE.fullmatch(url.strip().rstrip(_TRAILING)))


def _visible_links(text: str) -> List[Tuple[int, int]]:
    """(start, end) UTF-16 ranges of every t.me link written in the text."""
    spans = []
    for match in _LINK_RE.finditer(text):
        raw = match.group(0)
        clean = raw.rstrip(_TRAILING)
        start = _u16(text[: match.start()])
        spans.append((start, start + _u16(clean)))
    return spans


# ----------------------------------------------------------- snapshot
def snapshot(message: Any) -> Optional[Dict[str, Any]]:
    """Capture a message's text + entities. None if it has no text at all."""
    if message.text is not None:
        kind, text, entities = "text", message.text, message.entities or ()
    elif message.caption is not None:
        kind, text, entities = "caption", message.caption, message.caption_entities or ()
    else:
        return None
    return {
        "kind": kind,
        "text": text,
        "entities": [e.to_dict() for e in entities],
    }


def slot_count(tpl: Dict[str, Any]) -> int:
    """How many links the post holds - visible ones plus hidden hyperlinks."""
    visible = _visible_links(tpl["text"])
    hidden = [
        e for e in tpl["entities"]
        if e.get("type") == "text_link" and _is_tme(e.get("url"))
        and not _overlaps(e, visible)
    ]
    return len(visible) + len(hidden)


def _overlaps(entity: Dict[str, Any], spans: List[Tuple[int, int]]) -> bool:
    start = entity["offset"]
    end = start + entity["length"]
    return any(s < end and start < e for s, e in spans)


# ------------------------------------------------------------- render
def render(tpl: Dict[str, Any], links: List[str]) -> Tuple[str, List[MessageEntity]]:
    """Rebuild the post with ``links`` dropped into its link positions."""
    text: str = tpl["text"]
    entities = [dict(e) for e in tpl["entities"]]
    visible = _visible_links(text)

    # Link positions in reading order: visible text links + hidden hyperlinks.
    slots: List[Tuple[int, str, Any]] = [(s, "text", (s, e)) for s, e in visible]
    for ent in entities:
        if ent.get("type") == "text_link" and _is_tme(ent.get("url")) \
                and not _overlaps(ent, visible):
            slots.append((ent["offset"], "hidden", ent))
    slots.sort(key=lambda item: item[0])

    if not links:
        return text, [MessageEntity.de_json(e, None) for e in tpl["entities"]]
    chosen = [links[i % len(links)] for i in range(len(slots))]

    # Which new link replaces which visible span / hidden hyperlink.
    replacement: Dict[Tuple[int, int], str] = {}
    for (_, kind, ref), link in zip(slots, chosen):
        if kind == "text":
            replacement[ref] = link
        else:
            ref["url"] = link

    # Hyperlinks sitting exactly on a visible link follow that link too.
    for ent in entities:
        if ent.get("type") == "text_link":
            key = (ent["offset"], ent["offset"] + ent["length"])
            if key in replacement:
                ent["url"] = replacement[key]

    # Rebuild the text in UTF-16 space and record how lengths shifted.
    units = text.encode("utf-16-le")
    out = bytearray()
    shifts: List[Tuple[int, int, int]] = []  # (old_start, old_end, new_len)
    cursor = 0
    for start, end in visible:
        new = replacement[(start, end)].encode("utf-16-le")
        out += units[cursor * 2: start * 2]
        out += new
        shifts.append((start, end, len(new) // 2))
        cursor = end
    out += units[cursor * 2:]
    new_text = out.decode("utf-16-le")

    def move(pos: int) -> int:
        delta = 0
        for start, end, new_len in shifts:
            if end <= pos:
                delta += new_len - (end - start)
            elif start < pos < end:  # boundary inside a link: clamp into it
                return start + delta + min(pos - start, new_len)
        return pos + delta

    rebuilt: List[MessageEntity] = []
    for ent in entities:
        start = move(ent["offset"])
        end = move(ent["offset"] + ent["length"])
        if end <= start:
            continue
        ent["offset"], ent["length"] = start, end - start
        rebuilt.append(MessageEntity.de_json(ent, None))
    return new_text, rebuilt
