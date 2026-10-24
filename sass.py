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


# ------------------------------------------------- auto-detected joins
AUTO_ADDED: List[str] = [
    "👀 Noticed you made me admin in <b>{title}</b>. I took the liberty of generating a link. You're welcome.",
    "👀 New channel detected: <b>{title}</b>. Link minted, pool updated, no instructions required.",
    "👀 <b>{title}</b> just appeared in my life. I've already made myself useful.",
    "👀 Spotted <b>{title}</b>. Admin rights look fine, so I've grabbed a link. Efficiency, unlike some.",
    "👀 Added to <b>{title}</b>. I've handled the paperwork myself — no <code>/load</code> needed.",
]

AUTO_RESCUE: List[str] = [
    "🎉 And just in time — I was out of links and genuinely considering a career change.",
    "🎉 The drought is over. The post points at something real again. Try to keep it that way.",
    "🎉 Crisis resolved. The post is no longer a monument to nothing.",
]

ACCESS_LOST: List[str] = [
    "🚪 I've been {reason} in <b>{title}</b>. Rude, but noted.",
    "🚪 Someone {reason} me in <b>{title}</b>. I'll assume that was intentional.",
    "🚪 No longer admin in <b>{title}</b> ({reason}). Dropped it from the pool before you could ask.",
    "🚪 <b>{title}</b> showed me the door ({reason}). I've already moved on.",
]

MISSING_RIGHTS: List[str] = [
    "🔒 You made me admin in <b>{title}</b> but forgot the one permission that matters.",
    "🔒 Admin in <b>{title}</b>, yet somehow unable to do the single thing I exist for.",
    "🔒 Promoted in <b>{title}</b> with impressive precision — every right except the useful one.",
]


def auto_added(title, chat_id, link, is_new, report, pool, post_result) -> str:
    """Bot was auto-registered after being promoted in a channel."""
    was_begging = pool == 1
    if not config.SASS:
        head = (
            f"✅ Auto-added <b>{esc(title)}</b> (<code>{chat_id}</code>) — "
            f"{'new' if is_new else 'link refreshed'}."
        )
    else:
        head = pick(AUTO_ADDED, title=esc(title))
        if not is_new:
            head += "\n<i>(already knew this one — link refreshed)</i>"

    lines = [head, "", f"🔗 <a href=\"{esc(link)}\">{esc(link)}</a>", f"Pool: <b>{pool}</b>"]

    if report["missing_post"]:
        lines.append(
            "⚠️ Missing for hosting the post: <b>"
            + ", ".join(report["missing_post"])
            + "</b> — fine as a link source, can't host <code>.set</code>."
        )
    elif report["full"]:
        lines.append("💪 Full rights — can host the <code>.set</code> post too.")

    if post_result != "no-post":
        lines.append(post_line(post_result))
    if config.SASS and was_begging:
        lines.append(pick(AUTO_RESCUE))
    return "\n".join(lines)


def access_lost(channel: dict, reason: str, left: int, post_result: str) -> str:
    """Bot was demoted or removed from a pooled channel."""
    if not config.SASS:
        return (
            f"⚠️ Lost admin in <b>{esc(channel['title'])}</b> "
            f"(<code>{channel['id']}</code>) — {esc(reason)}.\n"
            f"{post_line(post_result)}\nPool left: <b>{left}</b>"
        )
    lines = [
        pick(ACCESS_LOST, title=esc(channel["title"]), reason=esc(reason)),
        f"<code>{channel['id']}</code>",
        "",
        post_line(post_result),
    ]
    if left:
        lines.append(pick(POOL_HEALTHY if left > 1 else POOL_THIN, left=left))
    return "\n".join(lines)


def missing_rights(title, chat_id, report) -> str:
    """Promoted, but without the permission needed to create invite links."""
    missing = ", ".join(report["missing_link"] + report["missing_post"])
    if not config.SASS:
        return (
            f"⚠️ I'm admin in <b>{esc(title)}</b> (<code>{chat_id}</code>) but "
            f"missing: <b>{esc(missing)}</b>.\nEnable it and I'll add the channel "
            "automatically."
        )
    return (
        f"{pick(MISSING_RIGHTS, title=esc(title))}\n"
        f"<code>{chat_id}</code>\n\n"
        f"Missing: <b>{esc(missing)}</b>\n"
        "Tick <i>Invite Users via Link</i> and I'll add it on my own — "
        "no further involvement needed from you."
    )


# ------------------------------------------------ ".set" adopted a post
POST_ADOPTED: List[str] = [
    "📌 Your <code>.set</code> message is now the redirect post in <b>{title}</b>. Edited in place, nothing new sent.",
    "📌 Took your <code>.set</code> in <b>{title}</b> and turned it into the post. Recycling, essentially.",
    "📌 <code>.set</code> consumed. <b>{title}</b> now has its redirect post, in the very same message.",
]

CANT_EDIT: List[str] = [
    "I can't edit my own message there. Give me <i>Edit Messages</i> and try again.",
    "Editing failed — I need the <i>Edit Messages</i> right in that channel.",
    "That channel won't let me edit. Check my admin rights.",
]


def post_adopted(title, chat_id, slots) -> str:
    if not config.SASS:
        return (
            f"📌 Redirect post set in <b>{esc(title)}</b> (<code>{chat_id}</code>) "
            f"with {slots} link line(s) — your <code>.set</code> message was edited "
            "in place.\nI'll keep it alive automatically."
        )
    return (
        f"{pick(POST_ADOPTED, title=esc(title))}\n"
        f"<code>{chat_id}</code> · <b>{slots}</b> link line(s)\n"
        "I'll keep it alive from here. Change the count with <code>/slots N</code>."
    )


def cant_edit_set(chat_id, error) -> str:
    if not config.SASS:
        return (
            f"⚠️ Couldn't edit the <code>.set</code> message in <code>{chat_id}</code>: "
            f"{esc(error)}\nI need the <i>Edit Messages</i> admin right."
        )
    return (
        f"⚠️ {pick(CANT_EDIT)}\n\n"
        f"Channel: <code>{chat_id}</code>\n"
        f"Telegram said: <code>{esc(error)}</code>\n"
        "Your <code>.set</code> message is still sitting there, by the way."
    )
