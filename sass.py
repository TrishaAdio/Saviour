"""Sarcastic one-liners for owner notifications.

Every message still carries the real information (which channel, its id, how
many backups are left, what happened to the post) - the sass is just the
wrapper. Set ``SASS=false`` in .env for the boring version.
"""

import html
import random
from typing import Dict, List

import config

# ------------------------------------------------------------ one death
DEATH_ONE: List[str] = [
    "💀 Shocking absolutely nobody, <b>{title}</b> is gone.",
    "💀 <b>{title}</b> has died. I'm sure it had a very good reason.",
    "💀 Breaking news: <b>{title}</b> no longer exists. Who could have seen it coming. Anyone. Everyone.",
    "💀 <b>{title}</b> just evaporated. Another fine addition to the graveyard.",
    "💀 RIP <b>{title}</b>. It had a good run of, let me check… not very long at all.",
    "💀 <b>{title}</b> is dead and I've already cleaned up the mess. Don't mention it. Seriously, don't.",
    "💀 Well. <b>{title}</b> is no more. Shall I act surprised, or do you want to skip to the part where I fix it?",
    "💀 <b>{title}</b> vanished into thin air. Telegram's finest work yet.",
    "💀 Lost <b>{title}</b>. I'd say this is unexpected, but we both know I'd be lying.",
    "💀 <b>{title}</b> has been deleted. I feel nothing. I was built for this.",
]

# ---------------------------------------------------------- mass death
DEATH_MANY: List[str] = [
    "☠️ <b>{count} channels</b> died at the same time. A beautifully coordinated effort.",
    "☠️ <b>{count} channels</b> gone in one sweep. Was there a group discount?",
    "☠️ <b>{count} channels</b> decided to quit together. Solidarity. Touching, really.",
    "☠️ Mass extinction event: <b>{count} channels</b> down. I'll just handle it, as usual.",
    "☠️ <b>{count} channels</b> dead. At this rate I should start charging for funeral services.",
]

# ----------------------------------------------- comment on pool depth
POOL_HEALTHY: List[str] = [
    "Backups left: <b>{left}</b>. We'll live.",
    "Still <b>{left}</b> in reserve. Crisis averted, hero.",
    "<b>{left}</b> backups remaining. I've seen worse. Mostly from you.",
    "Pool: <b>{left}</b>. Comfortable. Enjoy it while it lasts.",
]

POOL_THIN: List[str] = [
    "Backups left: <b>{left}</b>. Living dangerously, I see.",
    "Down to <b>{left}</b>. Maybe load a few more before the inevitable?",
    "Only <b>{left}</b> left. No pressure. Well, some pressure.",
    "<b>{left}</b> backup remaining. One more death and I start begging. You've been warned.",
]

# ------------------------------------------------------- out of links
OUT_OF_LINKS: List[str] = [
    "🆘 Fantastic news: we are <b>completely out of channels</b>. The post now proudly points at nothing.",
    "🆘 Pool: <b>empty</b>. Post: pointing into the void. Me: waiting. You: presumably asleep.",
    "🆘 That was the last one. The redirect post is now a monument to absolutely nothing.",
    "🆘 Congratulations, every single channel is dead. Impressive consistency.",
    "🆘 I have run out of links. All of them. Zero. This is the part where you do something.",
]

# The line the owner actually asked for - always included, never randomised.
BEG_LINE = "<b>Hey bro, give me a link so I can update it.</b>"

BEG_TAIL: List[str] = [
    "Drop the new channel link(s) right here and I'll verify them myself. I'd make my own channel, but I lack hands.",
    "Send the link(s) in this chat. No <code>/load</code> needed — I'm not completely useless.",
    "Paste the new link(s) here. I'll do the verifying, the posting and the worrying. You just copy-paste.",
    "New link(s) in this chat, please. I'll handle the rest, like always.",
]

# --------------------------------------------------- misc sassy events
NO_POOL_ON_SET: List[str] = [
    "You used <code>.set</code> with an <b>empty pool</b>. Bold strategy.",
    "<code>.set</code> received. Links available: <b>zero</b>. See the problem?",
    "Ah yes, <code>.set</code> with nothing to link to. Visionary.",
]

CANT_POST: List[str] = [
    "I can't post there. Permissions, probably. Shocking.",
    "That channel won't let me post. Maybe check what rights you gave me?",
    "Posting failed. I'd blame myself, but this one's on the admin settings.",
]

_last: Dict[int, str] = {}


def pick(pool: List[str], **fmt) -> str:
    """Random line, never the same one twice in a row for a given pool."""
    key = id(pool)
    choices = [line for line in pool if line != _last.get(key)] or pool
    line = random.choice(choices)
    _last[key] = line
    return line.format(**fmt)


def esc(value) -> str:
    return html.escape(str(value))


# ------------------------------------------------------------- builders
def death_notice(dead: List[dict], left: int, post_result: str) -> str:
    """The 'a link died' DM."""
    if not config.SASS:
        names = "\n".join(f"  • {esc(c['title'])} (<code>{c['id']}</code>)" for c in dead)
        return (
            f"♻️ <b>{len(dead)} channel(s) went down:</b>\n{names}\n\n"
            f"Post updated: <code>{esc(post_result)}</code>\n"
            f"Pool left: <b>{left}</b>"
        )

    if len(dead) == 1:
        head = pick(DEATH_ONE, title=esc(dead[0]["title"]))
        body = f"<code>{dead[0]['id']}</code>"
    else:
        head = pick(DEATH_MANY, count=len(dead))
        body = "\n".join(f"  • {esc(c['title'])} — <code>{c['id']}</code>" for c in dead)

    lines = [head, body, "", post_line(post_result)]
    if left:
        lines.append(pick(POOL_HEALTHY if left > 1 else POOL_THIN, left=left))
    return "\n".join(lines)


def post_line(result: str) -> str:
    """Honest one-liner about what actually happened to the redirect post."""
    if result in ("edited", "unchanged"):
        return "🔁 Post <b>edited</b> — link swapped, formatting untouched."
    if result == "reposted":
        return "🔁 The post itself was deleted too, so I published a fresh one."
    if result == "no-links":
        return "🔁 Post <b>not updated</b> — there's nothing live left to point at."
    if result == "no-post":
        return "🔁 No redirect post is set, so there was nothing to fix."
    return f"🔁 Post update failed: <code>{esc(result)}</code>"


def beg_notice() -> str:
    """The 'I'm out of links' DM."""
    if not config.SASS:
        return (
            "🆘 <b>Hey bro, give me a link so I can update it.</b>\n\n"
            "I'm out of working channels — the redirect post has nothing live to "
            "point at.\nSend the new channel link(s) here and I'll verify and "
            "update the post myself."
        )
    return f"{pick(OUT_OF_LINKS)}\n\n{BEG_LINE}\n{pick(BEG_TAIL)}"


def no_pool_on_set(title: str, chat_id: int) -> str:
    if not config.SASS:
        return (
            f"⚠️ <code>.set</code> was used in <b>{esc(title)}</b> "
            f"(<code>{chat_id}</code>) but my link pool is empty.\n\n"
            "Run <code>/load</code> and give me channels first."
        )
    return (
        f"⚠️ {pick(NO_POOL_ON_SET)}\n\n"
        f"Channel: <b>{esc(title)}</b> (<code>{chat_id}</code>)\n"
        "Run <code>/load</code>, hand me some channels, then try again."
    )


def cant_post(chat_id: int, error) -> str:
    if not config.SASS:
        return f"⚠️ Couldn't post in <code>{chat_id}</code>: {esc(error)}"
    return (
        f"⚠️ {pick(CANT_POST)}\n\n"
        f"Channel: <code>{chat_id}</code>\n"
        f"Telegram said: <code>{esc(error)}</code>"
    )
