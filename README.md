# Saviour

A redirect keeper that is **one Telegram user account and nothing else** — no
bot token, no Bot API. It keeps a post in your public channel always pointing
at a backup channel that is actually alive.

You add the account to your channels and promote it. It does the rest.

## Install

```bash
python3 setup.py
```

It installs the dependencies, asks for your **API_ID / API_HASH**
([my.telegram.org](https://my.telegram.org/apps) → *API development tools*),
then the account's **phone number**, the **login code**, and the **2FA
password** if it has one. Finally it asks which account is *you* (the owner),
sends you a test message, and prints the start command:

```bash
python3 saviour.py        # or venv/bin/python3 saviour.py
```

> The session file is full access to that account — **use a spare one** and
> never share `saviour.session` (it's gitignored).

## How it works

### 1. Adding a backup channel
Add the account, promote it to admin. It needs **Invite Users via Link**;
give it **Change Info** too so it can keep the bio fresh. The moment you
promote it:

```
🟢 Channel synced: Movies Backup
ID: -1001234567890
Invite: t.me/+AbCdEf123456
Bio updates: on
Pool: 3 live
```

- Promoted without *Invite Users via Link* → it tells you which right is
  missing and adds the channel itself once you tick it.
- Demoted or kicked → parked immediately, the post is fixed, you're told.
  Re-promote it and it comes back on its own.
- Missed a promotion (bot was down)? It re-scans its own chat list every
  `SYNC_EVERY` seconds, or run `/find`.

### 2. The redirect post
Add the account to your public channel and post **`.set`** there (or `.set 3`
for 3 lines).

- **Already admin** → your `.set` is deleted and the post appears at once.
- **Not admin yet** → *nothing happens*. It quietly remembers, tells you it's
  waiting, and keeps retrying in the background. The second you promote it,
  the post goes up and you get **✅ The redirect post is up**.

```
https://t.me/+pbib86sOwyU1NjRh
https://t.me/+pbib86sOwyU1NjRh
https://t.me/+pbib86sOwyU1NjRh
https://t.me/+pbib86sOwyU1NjRh
https://t.me/+pbib86sOwyU1NjRh
```

Every line shows the channel that is currently live. When it dies, the **same
post is edited** to show the next one — never reposted.

### 3. Random bios, and links that fix themselves
Every `CHECK_EVERY` seconds (default 300), for each channel:

1. open it and confirm we're still admin
2. **write a new random line into its bio** — the channel keeps looking
   active, and it proves we can still act in there
3. ask Telegram directly whether the invite link still works

If the **bio can't be written**, the channel gets a **brand new invite link**.
If the **link itself is dead** but the channel is fine, the old invite was
revoked — so a fresh one is made and *verified* before it's used.

### 4. Nothing is believed without proof
If anything suggests a channel is deleted, that is **never acted on
directly**. The account first tries to:

1. **read the channel's full info** — needs no rights, changes nothing
2. **add a `.` to its name** and take it straight back off
3. **post a message and delete it** a second later

Any one of those working means the channel is alive and the alarm was false.

### 5. Why you can't lose a working channel
- **Unclear is not dead.** A timeout, a server error, a flood-wait or a reply
  we can't read counts as *unsure* — no strike, no change.
- **One bad round is never enough.** A channel must fail `STRIKES` rounds in
  a row (default 3). Any success resets the count, and you get a 🟠 warning
  on the very first failure.
- **Nothing is ever deleted.** A channel that fails is *parked*: out of the
  post, still in the list, still checked every round. If it answers again it
  returns by itself with a 🟢 message.
- Pool empty → 🆘 you're asked for links. Paste them in the chat and the post
  is fixed immediately.

## Commands (your chat with the account)

| command | |
|---|---|
| `/status` | pool, post, last check |
| `/channels` | every channel and its state |
| `/links` | every invite link |
| `/scan` | check everything right now |
| `/find` | look for channels it's admin in |
| `/use <id>` | show this channel in the post |
| `/slots <n>` | how many link lines the post has |
| `/redraw` | rewrite the post now |
| `/forget <id>` | drop a channel from the list |
| `/unset` | forget the redirect post |
| `.clearall` | wipe the account (asks first) |

Paste channel links any time (`@name`, `t.me/name`, `+invite`, `-100…`) and
they're checked and added. In a channel: `.set` claims the redirect post.

### `.clearall`
For starting over or retiring the account. `.clearall` shows what it will do;
`.clearall confirm` does it: **deletes** every group/channel the account
created, **leaves** every other one, and **blocks + wipes** every private
chat. Your own chat and Saved Messages are kept, and the pool and post reset.

## Keep it running

```bash
cp saviour.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now saviour
systemctl status saviour
```

`/status` shows **Last check** — if that's more than a few minutes old, it
isn't running. A lock file stops a second copy (two copies would corrupt the
session); clean up with `pkill -f saviour.py`.

## .env

| key | default | |
|---|---|---|
| `API_ID` / `API_HASH` | — | from my.telegram.org |
| `OWNER_ID` | — | your own account, not the bot's |
| `POST_SLOTS` | `5` | link lines in the post |
| `POST_TITLE` | `none` | bold line above the links |
| `CHECK_EVERY` | `300` | seconds between health rounds |
| `SYNC_EVERY` | `900` | seconds between searches for new channels (`0` = off) |
| `GAP` | `3` | seconds between two channels in one round |
| `MAX_WAIT` | `300` | longest flood-wait to sit out |
| `STRIKES` | `3` | failed rounds in a row before a channel is parked |
| `REPOST_IF_DELETED` | `true` | replace the post if it gets deleted |

State lives in `data.json`, logs in `saviour.log`.

## Files

```
saviour.py   the bot: events, commands, the check loop
engine.py    all Telegram work: probes, bios, links, the post
state.py     the JSON file it remembers things in
config.py    reads .env
setup.py     the installer
```
