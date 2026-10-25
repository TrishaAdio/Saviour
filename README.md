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

```
🟢 Channel connected
┃ Channel  Backup One
┃ ID       -1001234567890
┃ Invite   t.me/+AbCdEf123456
┃ Access   Full admin
┃ Pool     3 active
```

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

## Reliable link checks: `/login`

A **bot account cannot ask Telegram whether an invite link still works** — that
is a hard limit of the Bot API. Without help, the bot can only open `t.me` and
guess from the page, which gets it wrong.

Connect a user account once and Telegram answers directly:

1. Get `api_id` and `api_hash` from [my.telegram.org](https://my.telegram.org/apps)
   → *API development tools*
2. DM the bot `/login` and follow the steps (api_id, api_hash, phone, the login
   code, and the two-step password if the account has one)

From then on every check calls `messages.checkChatInvite` /
`contacts.resolveUsername`, so a **revoked invite, a banned channel or a deleted
channel is detected for certain**. `/start` shows `Verify: User account @you`
when it's active. `/logout` disconnects it.

Right after login the bot **scans every channel once** and reports which links
work. `/scan` repeats that at any time.

**Until an account is connected, monitoring stays paused** (`REQUIRE_USERBOT`).
Guessing from the t.me page is what previously dropped working channels, so the
bot refuses to judge rather than risk it.

### Your account won't get banned
Only two read-only requests are ever made: *check invite* and *resolve
username*. The bot never joins, leaves, messages, searches or scrapes. On top of
that:

- at least `MIN_REQUEST_GAP` seconds (default 3) between any two requests
- requests are made one at a time, never in parallel
- a flood-wait from Telegram pauses **all** checks until it fully expires, and
  nothing is judged during the pause
- the session is reused, so the account doesn't repeatedly re-authenticate

## Why a working channel is never dropped

The old version sometimes decided a live channel was dead and burned through the
pool. Three rules now prevent that:

1. **Only Telegram may say "dead".** A negative result from reading t.me counts
   as *unknown*, never as dead.
2. **One bad answer is not enough.** A channel must fail `DEATH_STRIKES`
   checks in a row (default 3, one per `CHECK_INTERVAL`) before it leaves the
   post. Any single success resets the count. You get a 🟠 warning on the first
   failure, long before anything changes.
3. **Nothing is ever deleted.** A confirmed-dead channel is *parked offline*:
   kept in the file, kept out of the post, and re-checked every round. If it
   answers again it returns on its own, and you get a 🟢 *Channel is back* DM.

A revoked invite link is not treated as a dead channel either — the bot creates
a fresh link, verifies it, and keeps the channel.

You can always overrule a verdict: open the channel in `/start` → **Channels**
and press **✅ Use it anyway**.

> **Security.** The session grants full access to that account — use a spare one
> if you can. The bot deletes the api_hash, code and password messages as they
> arrive, and stores the session in `session.json` with owner-only permissions
> (`chmod 600`). It is in `.gitignore`; never commit or share it. `/logout`
> signs the account out and deletes the file.

Putting `API_ID` and `API_HASH` in `.env` (or answering them in `setup.py`) makes
`/login` ask only for the phone number and code.

## Keep it running (important)

If the bot isn't running, nothing gets swapped. Running `python3 bot.py` in an
SSH session stops the bot when the session closes. Use systemd instead; it
restarts the bot on crashes and after reboots:

```bash
cp saviour.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now saviour
systemctl status saviour        # should say "active (running)"
```

`/start` shows **Last check** — if that's more than a few minutes old, the bot
isn't running.

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
4. From then on it checks every linked channel on a timer, **asking Telegram
   whether each invite link still works** (see `/login` below). A channel that's
   deleted, banned by Telegram, or whose link stopped working is dropped (or
   gets a fresh link if the bot can still make one), and the post is **edited**
   to show a live link.
5. Pool empty → you get an *Action required · no live links* DM. Reply with new
   links in the same chat; it verifies them and updates the post immediately,
   no `/load` needed.

### Private invite links
`t.me/+hash` / `t.me/joinchat/...` links **cannot** be resolved by any Telegram bot.
For a private channel, forward any post from it to the bot, or send its numeric
`-100…` id. The bot then generates its own invite link for that channel.

## Control panel

Send `/start` to the bot. You get one panel message with buttons:

```
📊 Status        📡 Channels · 3
📝 Redirect post ➕ Add channels
❔ How it works  ✕ Close
```

Every button **edits that same message**; nothing new is sent per click. Every
screen has a **‹ Back** button. From the panel you can:

- **Channels:** tap a channel to see its invite link, then **⭐ Make active** or
  **🗑 Remove** (with a confirmation step)
- **Redirect post:** **↻ Refresh links**, change the line count with **− / +**
  (default-format posts), or **⏹ Stop monitoring**
- **Status:** full overview, with **↻ Update**

Only the owner can use the buttons.

## Commands (owner DM only)

The panel covers all of these; the commands still work if you prefer typing.

| command | |
|---|---|
| `/start` | open the control panel |
| `/login` | connect a user account for reliable link checks |
| `/logout` | disconnect the user account |
| `/scan` | check every channel right now |
| `/cancel` | stop whatever is in progress |
| `/load` | verify and load backup channels |
| `/status` | pool, active channel, post location |
| `/refresh` | re-render the post right now |
| `/active <id>` | make that channel the one shown |
| `/slots <n>` | change how many link lines the post shows |
| `/remove <id>` | drop a channel from the pool |
| `/preview` | send sample notifications |
| `/unset` | stop monitoring the post |

## Notifications

Every DM uses the same card layout: a colour-coded title, a details panel, and
at most one line telling you what happens next or what to do.

| dot | meaning |
|---|---|
| 🟢 | done: channel connected, post linked |
| 🔵 | info: startup, status |
| 🟠 | needs attention: missing permission, channel disconnected |
| 🔴 | critical: channel offline, no live links left |

```
🔴 Channel offline
┃ Channel  Movies Backup
┃ ID       -1001234567890
┃ Pool     2 active
┃ Post     Updated
```

When the pool runs dry you get a 🔴 *Action required · no live links* card.
Paste a new channel link in the chat and the post is fixed immediately. Run
`/preview` to see samples.

## `.env`

| key | default | |
|---|---|---|
| `BOT_TOKEN` | — | from @BotFather |
| `OWNER_ID` | — | your numeric id (@userinfobot) |
| `POST_SLOTS` | `5` | link lines in the post |
| `POST_TITLE` | `none` | bold line above the links (plain `.set` only); `none` = no title |
| `CHECK_INTERVAL` | `300` | seconds between health checks |
| `HTTP_TIMEOUT` | `20` | seconds to wait for Telegram before a request counts as timed out |
| `API_ID` / `API_HASH` | — | optional, from my.telegram.org; lets `/login` skip straight to the phone number |
| `REQUIRE_USERBOT` | `true` | pause monitoring until a user account is connected |
| `DEATH_STRIKES` | `3` | failed checks in a row before a channel leaves the post |
| `MIN_REQUEST_GAP` | `3` | minimum seconds between user-account requests |
| `LINK_MODE` | `single` | `single` = every line shows the active channel, rest held in reserve · `spread` = rotate lines across the whole pool |
| `AUTO_ADD` | `true` | auto-pool any channel the bot is promoted in |
| `REPOST_IF_DELETED` | `false` | if the post is deleted: send a new one (`true`) or ask you to `.set` again (`false`) |
| `EXPANDABLE_QUOTE` | `false` | collapsible quote box (plain `.set` only) |
| `MIN_CHANNELS` | `1` | beg the owner below this many live channels |

State lives in `data.json`, logs in `bot.log`.

## Files

```
setup.py      colorama installer / screen launcher
bot.py        handlers, watchdog job, owner pings
core.py       link parsing, admin checks, post rendering + editing
template.py   clones a post's formatting and swaps only its links
ui.py         owner notification cards
menu.py       inline-button control panel
userbot.py    optional user account (/login) that verifies links with Telegram
storage.py    JSON state
config.py     .env loader
```
