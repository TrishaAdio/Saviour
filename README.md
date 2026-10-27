# Saviour — redirect userbot

A **pure userbot** (one Telegram user account, Telethon — no bot token, no Bot
API) that keeps a *Redirecting…* post in your public channel always pointing at
a **live** backup channel.

You add the account to your channels and promote it. It does the rest:

- notices the promotion instantly and DMs you **🟢 Channel synced**
- waits patiently if it can't act yet (joined but not admin, `.set` before
  admin) and tells you the moment it can
- **rewrites every channel's bio with a random line every check round** — the
  channels always look fresh, and a failed bio write is the first alarm bell
- replaces a dead invite link with a fresh, verified one by itself
- **never trusts a "channel deleted" verdict from indirect checks** — before
  believing it, it proves death with direct actions only a live channel
  allows: renaming the channel with a `.` and reverting, or posting a message
  and deleting it within a second
- edits the same redirect post in place when links change — no reposts, no
  formatting loss

## Install

```bash
python3 setup.py
```

The installer checks Python, installs the dependencies (falling back to a
local `venv/` if the system Python refuses them), then asks for:

- **API_ID / API_HASH** — [my.telegram.org](https://my.telegram.org/apps) →
  *API development tools*
- **POST_SLOTS** — link lines in the redirect post (default 5)
- **Phone number** of the account that will *be* the userbot, then the
  **login code** Telegram sends it, then the **2FA password** if the account
  has two-step verification
- **Owner** — your main account's numeric id or @username

It writes `.env` (owner-only permissions), completes the Telegram login and
saves the session, sends the owner a test DM to prove the command channel
works, and finishes by printing the command to start the bot:

```bash
python3 main.py        # or venv/bin/python3 main.py if a venv was created
```

Prefer manual? `cp .env.example .env`, fill it in, and run `main.py` — the
first run asks for phone + code interactively.

The session is saved to `saviour.session` — it grants full access to the
account, so **use a spare account** and never commit or share the file
(it's in `.gitignore`).

## How it works

### 1. Adding backup channels
Add the account to a channel and promote it to admin (it needs **Invite Users
via Link**; **Change Info** enables the bio updates). You get:

```
🟢 Channel synced: Movies Backup
ID: -1001234567890
Invite: t.me/+AbCdEf123456
Bio updates: on
Pool: 3 active
```

- Promoted without *Invite Users via Link* → it tells you exactly which right
  to tick, and adds the channel by itself once you do.
- Demoted or kicked → the channel is parked offline instantly, the post is
  fixed on the spot, and you're told. Re-promote and it comes back by itself.
- Added to a **group** instead of a channel → ignored.
- The channel hosting the redirect post is never pooled as its own target.

Missed a promotion (account offline at that moment)? `/sync` (or the automatic
sync every `SYNC_INTERVAL` seconds) walks the account's own dialogs — a
userbot *can* list its channels — and connects everything it admins.

### 2. The redirect post
Add the account to your redirect channel and post **`.set`** there (or
`.set 3` for 3 lines). Two cases:

- **Already admin** → the `.set` message is deleted and the redirect post
  appears immediately:

  ```
  https://t.me/+pbib86sOwyU1NjRh
  https://t.me/+pbib86sOwyU1NjRh
  https://t.me/+pbib86sOwyU1NjRh
  https://t.me/+pbib86sOwyU1NjRh
  https://t.me/+pbib86sOwyU1NjRh
  ```

- **Not admin yet** → nothing happens visibly; the account remembers the
  request and waits. The moment you promote it, the post goes up and you get
  **✅ Redirect post is live**. (It needs *Post Messages*; *Edit Messages* and
  *Delete Messages* are needed to maintain it.)

### 3. Keeping every link alive
Every `CHECK_INTERVAL` seconds (default 300), for each pooled channel:

1. open the channel and confirm the account is still admin
2. **write a random line into the channel bio** — proof the channel is
   writable, and it keeps the channels looking alive on its own
3. ask Telegram directly whether the invite link still works
   (`messages.checkChatInvite` — a real answer, not a web-page guess)
4. if the invite was revoked: **mint a fresh link and verify it** before
   saving it (`🔁 … a fresh one was created and verified`)

If steps 1–2 say the channel is gone, that verdict is **never believed
directly**. The account first tries, in order:

1. **read the channel's full info** — needs no rights and has no side effects;
   a deleted or banned channel refuses this outright
2. **rename the channel** (add a `.` to the title, revert it a second later)
3. **post a silent message and delete it within a second**

Any one succeeding means the "deleted" was a false alarm and nothing changes.
Only when all three fail does the round count as *suspect*.

### 4. Why a working channel is never dropped
1. **Only direct action may say "dead".** Inconclusive answers — a network
   drop, a Telegram server error, a flood-wait, an unreadable reply — count
   as *unknown* and change nothing, not even a strike.
2. **One bad round is not enough.** A channel must fail `DEATH_STRIKES`
   rounds in a row (default 3) before it leaves the post. Any success resets
   the count, and you get a 🟠 warning on the first failure.
3. **Nothing is ever deleted.** A confirmed-dead channel is *parked offline*:
   kept in the file, kept out of the post, re-checked every round. If it
   answers again it returns on its own with a 🟢 DM.

Pool empty → 🆘 *No live links left* — paste new channel links in the DM and
the post is fixed immediately.

## Commands (owner DM only)

| command | |
|---|---|
| `/status` | pool, post location, last check |
| `/channels` | list every channel with its state |
| `/scan` | check all channels right now |
| `/sync` | find channels the account admins and connect them |
| `/load` | paste links after this to add channels manually |
| `/active <id>` | channel shown in the post |
| `/slots <n>` | link lines in the post (1–20) |
| `/refresh` | re-render the post now |
| `/remove <id>` | drop a channel from the pool |
| `/unset` | forget the redirect post |
| `/cancel` | cancel link input |
| `.clearall` | wipe the account (see below) |

In a channel: `.set` / `.set 3` claims the redirect post.

### `.clearall` — wipe the account
Nuclear option for starting over (or retiring the account). Send `.clearall`
in the DM to see what it does, then `.clearall confirm` to run it:

- **deletes** every group and channel the account created
- **leaves** every other group and channel — including the whole link pool
  and the redirect channel
- **blocks** every DM and **deletes its history** (revoked both sides where
  Telegram allows)

Only the command chat and Saved Messages survive, and the redirect state
(pool, post, pending `.set`) is wiped. The userbot keeps running afterwards,
so you can rebuild immediately. A progress message counts the dialogs as it
goes and ends with a full summary.

## Keep it running

`setup.py` already logged the account in (the session file is ready), so:

```bash
cp saviour.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now saviour
systemctl status saviour        # should say "active (running)"
```

`/status` shows **Last check** — if that's more than a few minutes old, the
userbot isn't running. A lock file refuses to start a second copy (two copies
sharing one session corrupt it); stop leftovers with `pkill -f main.py`.

## .env

| key | default | |
|---|---|---|
| `API_ID` / `API_HASH` | — | from my.telegram.org |
| `OWNER_ID` | — | your numeric id |
| `POST_SLOTS` | `5` | link lines in the post |
| `POST_TITLE` | `none` | bold line above the links; `none` = no title |
| `CHECK_INTERVAL` | `300` | seconds between health rounds (and bio rewrites) |
| `REQUEST_GAP` | `3` | minimum seconds between two channels in one round |
| `MAX_FLOOD_WAIT` | `300` | longest flood-wait to sit out; longer ones end the round as unverified instead of freezing the bot |
| `DEATH_STRIKES` | `3` | failed rounds in a row before a channel leaves the post |
| `SYNC_INTERVAL` | `900` | seconds between automatic dialog scans — the safety net for a promotion whose live update was missed (`0` = off) |
| `REPOST_IF_DELETED` | `true` | automatically repost if the redirect post is deleted |
| `SESSION_NAME` | `saviour` | Telethon session file name |

State lives in `data.json`, logs in `bot.log`.

## Files

```
main.py     entry point: handlers (joins, promotions, .set, commands), loops
core.py     pool, bio/invite probes, deep verify, post rendering
storage.py  JSON state
texts.py    random bios + owner message templates
config.py   .env loader
```
