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

## Usage

1. **Add the bot as admin** to each backup channel — it needs *Invite Users via Link*.
2. DM the bot `/load`, then paste the channel links (one per line):
   ```
   @backup_one
   https://t.me/backup_two
   -1001234567890
   ```
   It verifies admin rights in each and replies **✅ Okay — access verified.**
3. **Add the bot as admin** to your public channel (needs *Post* + *Edit* +
   *Delete Messages*) and post:
   ```
   .set
   ```
   The bot deletes your `.set` message and publishes the redirect post:

   > **Redirecting...**
   > ```
   > https://t.me/+pbib86sOwyU1NjRh
   > https://t.me/+pbib86sOwyU1NjRh
   > https://t.me/+pbib86sOwyU1NjRh
   > https://t.me/+pbib86sOwyU1NjRh
   > https://t.me/+pbib86sOwyU1NjRh
   > ```

   `.set 3` sets a different number of lines for that post.
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
| `POST_TITLE` | `Redirecting...` | bold first line |
| `CHECK_INTERVAL` | `300` | seconds between health checks |
| `LINK_MODE` | `single` | `single` = every line shows the active channel, rest held in reserve · `spread` = rotate lines across the whole pool |
| `SASS` | `true` | sarcastic death notifications |
| `EXPANDABLE_QUOTE` | `true` | collapsible grey quote block |
| `MIN_CHANNELS` | `1` | beg the owner below this many live channels |

State lives in `data.json`, logs in `bot.log`.

## Files

```
setup.py      colorama installer / screen launcher
bot.py        handlers, watchdog job, owner pings
core.py       link parsing, admin checks, post rendering + editing
sass.py       sarcastic notification lines
storage.py    JSON state
config.py     .env loader
```
