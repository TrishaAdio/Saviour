"""Builds icon_data.py from the tgmacicons pack JSON.

Usage:  python3 tools/gen_icon_data.py pack.json icon_data.py

To add an icon: find its number on the pack preview sheets, add a line to WANT
below, run this again. IDs are read from the JSON, never typed by hand.

name -> (pack number, plain character shown when custom icons are off)
Pack numbers refer to the preview sheets of https://t.me/addemoji/tgmacicons.
"""
import json
import sys

WANT = {
    # --- severity (card titles). Plain mode keeps the old coloured dots.
    "ok": (78, "🟢"),          # check circle
    "info": (86, "🔵"),        # info circle
    "warn": (69, "🟠"),        # exclamation circle
    "crit": (28, "🔴"),        # x circle
    # --- list markers
    "good": (27, "✓"),         # check
    "retry": (64, "↻"),        # refresh arrow
    "bad": (28, "✕"),          # x circle
    "unknown": (109, "?"),     # three dots in circle
    "offline": (128, "⚠"),     # minus circle
    "active": (108, "⭐"),     # filled star
    "star": (5, "⭐"),         # outline star
    # --- buttons
    "stats": (91, "📊"),       # chart
    "channel": (26, "📡"),     # megaphone
    "post": (38, "📝"),        # document
    "add": (60, "➕"),         # plus circle
    "book": (18, "❔"),        # open book
    "close": (28, "✕"),        # x circle
    "refresh": (64, "↻"),      # refresh arrow
    "back": (68, "‹"),         # back arrow
    "next": (40, "›"),         # forward arrow
    "trash": (34, "🗑"),       # bin
    "stop": (128, "⏹"),       # minus circle
    "use": (78, "✅"),         # check circle
    "cancel": (28, "✕"),       # x circle
    "scan": (126, "🔎"),       # magnifier
    # --- card rows
    "link": (29, ""),          # chain
    "pool": (33, ""),          # people
    "lock": (48, ""),          # padlock
    "verify": (131, ""),       # verified badge
    "clock": (75, ""),         # clock with arrow
    "health": (43, ""),        # heart
    "user": (54, ""),          # person
    "bot": (19, ""),           # robot
    "text": (137, ""),         # text format
    "gear": (107, ""),         # settings
    "pin": (58, ""),           # pushpin
    "bulb": (46, ""),          # light bulb
    "chat": (50, ""),          # chat bubbles
    "page": (18, ""),          # book
    "plane": (1, ""),          # telegram plane
    "eye": (127, ""),          # eye
}

pack = json.load(open(sys.argv[1], encoding="utf-8"))
by_no = {e["index"]: e for e in pack["emoji"]}

lines = [
    '"""Custom emoji from the tgmacicons pack (https://t.me/addemoji/tgmacicons).',
    "",
    "GENERATED from the pack JSON - do not edit by hand. Each entry is",
    "    name: (custom_emoji_id, fallback, plain, pack number)",
    "where ``fallback`` is the pack's own emoji (shown by clients that can't render",
    "custom emoji) and ``plain`` is what the bot shows when custom icons are off.",
    '"""',
    "",
    "ICONS = {",
]
for name, (no, plain) in WANT.items():
    e = by_no[no]
    assert e["needs_repainting"], f"#{no} is not repaintable"
    lines.append(f'    {name!r}: ({e["custom_emoji_id"]!r}, {e["fallback"]!r}, {plain!r}, {no}),')
lines.append("}")
open(sys.argv[2], "w", encoding="utf-8").write("\n".join(lines) + "\n")
print(f"wrote {len(WANT)} icons to {sys.argv[2]}")
