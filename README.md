# Redirect Bot

Keeps a "Redirecting..." post in your public channel always pointing at a **live**
channel. When a linked channel gets deleted or the bot loses admin there, the bot
swaps in a backup link by **editing the same post** — identical formatting, no repost.
When it runs out of backups it DMs you for new links.

## Install

```bash
python3 setup.py
```

It checks Python, installs `screen` and the pip packages, asks for your token /
owner id / post settings, writes `.env`, and starts the bot in a `screen` session.

| command | what it does |
|---|---|
| `python3 setup.py` | full install + configure + start |
| `python3 setup.py --start` | start in screen |
| `python3 setup.py --stop` | kill the session |
| `python3 setup.py --restart` | restart |
| `python3 setup.py --status` | running? |
| `python3 setup.py --logs` | tail `bot.log` |
| `screen -r redirectbot` | attach to the live console (`Ctrl+A` then `D` to detach) |

## Auto-detect

You don't have to tell the bot anything. **Make it admin in a channel** and it
notices immediately, checks its own rights, mints an invite link and adds the
channel to the pool:

> 👀 Noticed you made me admin in **Backup One**. I took the liberty of generating a link. You're welcome.
>
> 🔗 https://t.me/+auto1001
> Pool: **1**
> 💪 Full rights — can host the `.set` post too.

- Missing *Invite Users via Link* → it tells you exactly which permission to tick,
  and adds the channel by itself once you do.
- Admin but no post/edit/delete rights → still used as a **link source**, just
  flagged as unable to host the `.set` post.
- **Demoted or removed** → dropped from the pool instantly and the post is fixed
  on the spot, without waiting for the next health check.
- Promoted again while the pool is empty → the post is repaired automatically and
  the begging stops.
- Added to a **group** instead of a channel → ignored, with a note.
- The channel hosting the `.set` post is **never** added as one of its own targets.

Set `AUTO_ADD=false` in `.env` to require `/load` instead.

> Auto-detect fires on *future* promotions. For channels the bot is **already**
> admin in, use `/load` once — Telegram sends no event for memberships that
> predate the bot starting up, and the Bot API has no way to list them.

## Usage

1. **Add the bot as admin** to each backup channel — it needs *Invite Users via Link*.
   Auto-detect adds it to the pool right there; steps 2 is only for channels it
   was already in before.
2. DM the bot `/load`, then paste the channel links (one per line):
   ```
   @backup_one
   https://t.me/backup_two
   -1001234567890
   ```
   It verifies admin rights in each and replies **✅ Okay — access verified.**
3. **Add the bot as admin** to your public channel (needs *Edit Messages*, plus
   *Delete Messages* for the reply method).

   **Already have a post? Reply `.set` to it.** The bot takes over *that* post,
   copies its exact formatting (quote box, bold, emoji, hidden links, photo
   caption, all of it) and swaps only the t.me links. Your `.set` reply is
   deleted. The number of links stays whatever the post already had.

   **No post yet? Just send `.set`.** That message is edited in place and
   becomes the redirect post:

   > **Redirecting...**
   > ```
   > https://t.me/+pbib86sOwyU1NjRh
   > https://t.me/+pbib86sOwyU1NjRh
   > https://t.me/+pbib86sOwyU1NjRh
   > https://t.me/+pbib86sOwyU1NjRh
   > https://t.me/+pbib86sOwyU1NjRh
   > ```

   The line count comes from `POST_SLOTS` in `.env` (default **5**). Override it
   per post with `.set 3`, or change it live with `/slots 5`.
4. From then on it checks every linked channel on a timer. A dead one is dropped
   and the post is **edited** to show the next backup's link.
5. Pool empty → you get a DM: *"Hey bro, give me a link so I can update it."*
   Reply with new links in the same chat; it verifies them and updates the post
   immediately — no `/load` needed.

### Private invite links
`t.me/+hash` / `t.me/joinchat/...` links **cannot** be resolved by any Telegram bot.
For a private channel, forward any post from it to the bot, or send its numeric
`-100…` id. The bot then generates its own invite link for that channel.

## Commands (owner DM only)

| command | |
|---|---|
| `/load` | verify and load backup channels |
| `/status` | pool, active channel, post location |
| `/refresh` | re-render the post right now |
| `/active <id>` | make that channel the one shown |
| `/slots <n>` | change how many link lines the post shows |
| `/remove <id>` | drop a channel from the pool |
| `/sass` | preview the death notifications |
| `/unset` | forget the redirect post |

## Death notifications

Every time a linked channel dies you get a DM — sarcastic by default, with a
random line each time (never the same one twice in a row):

> 💀 Shocking absolutely nobody, **Movies Backup 3** is gone.
> `-1001234567890`
>
> 🔁 Post **edited** — link swapped, formatting untouched.
> Backups left: **2**. We'll live.

Thinning pool gets its own nagging, and running dry always keeps the line you
asked for:

> 🆘 Congratulations, every single channel is dead. Impressive consistency.
>
> **Hey bro, give me a link so I can update it.**
> Send the link(s) in this chat. No `/load` needed — I'm not completely useless.

Run `/sass` to preview all of them. Set `SASS=false` in `.env` for plain,
humourless reports. The status line is always honest — it says *"not updated —
nothing live left to point at"* rather than claiming a swap that never happened.

## `.env`

| key | default | |
|---|---|---|
| `BOT_TOKEN` | — | from @BotFather |
| `OWNER_ID` | — | your numeric id (@userinfobot) |
| `POST_SLOTS` | `5` | link lines in the post |
| `POST_TITLE` | `none` | bold line above the links (plain `.set` only); `none` = no title |
| `CHECK_INTERVAL` | `300` | seconds between health checks |
| `HTTP_TIMEOUT` | `20` | seconds to wait for Telegram before a request counts as timed out |
| `LINK_MODE` | `single` | `single` = every line shows the active channel, rest held in reserve · `spread` = rotate lines across the whole pool |
| `AUTO_ADD` | `true` | auto-pool any channel the bot is promoted in |
| `REPOST_IF_DELETED` | `false` | if the post is deleted: send a new one (`true`) or ask you to `.set` again (`false`) |
| `SASS` | `true` | sarcastic death notifications |
| `EXPANDABLE_QUOTE` | `false` | collapsible quote box (plain `.set` only) |
| `MIN_CHANNELS` | `1` | beg the owner below this many live channels |

State lives in `data.json`, logs in `bot.log`.

## Files

```
setup.py      colorama installer / screen launcher
bot.py        handlers, watchdog job, owner pings
core.py       link parsing, admin checks, post rendering + editing
template.py   clones a post's formatting and swaps only its links
sass.py       sarcastic notification lines
storage.py    JSON state
config.py     .env loader
```
