# Handoff: Saviour (Telegram redirect-link bot)

You are taking over an existing, working project. Read this fully before
changing anything. Repo: https://github.com/TrishaAdio/Saviour (default branch
`main`, everything up to PR #12 is merged, no open PRs).

## What the bot does

The owner runs a public Telegram channel with one **redirect post**: a quote box
of invite links (e.g. 5 × `t.me/+abc…`) pointing to a backup channel. Backup
channels get banned or deleted. The bot keeps that post pointing at a live
channel, **by editing the same post**, never by posting a new one.

1. Owner makes the bot admin in backup channels → bot auto-connects them, mints
   an invite link for each (the "pool").
2. Owner replies `.set` to the redirect post → bot adopts that exact post,
   clones its formatting (text + entities), deletes the `.set` reply.
3. Every `CHECK_INTERVAL` the bot verifies each channel. A dead one is taken out
   of the post and the post is edited to the next live link, keeping formatting.
4. Owner gets a DM for every event. Pool empty → DM asking for new links.

## Owner and environment

- Owner is non-native English, writes short and informal. Answer in plain,
  simple English. Lead with what happened and what they must run.
- Server: VPS, `/root/Saviour`, **Python 3.12**, venv at `venv/`.
- The owner wants the bot run in **screen, not systemd**:
  ```bash
  screen -dmS saviour bash -c 'while true; do venv/bin/python3 bot.py; sleep 5; done'
  ```
  `saviour.service` is still in the repo as an option; don't push it on them.
- Update steps you give them are always: merge PR → `git pull origin main` →
  `screen -S saviour -X quit` → start command above.
- They paste tracebacks from the server. Take them literally.

## Files

| file | role |
|---|---|
| `bot.py` | handlers, health-check job, `/sync`, panel callbacks, startup, instance lock |
| `core.py` | link parsing, admin checks, invite links, health check per channel, post render/edit, retries, `sync_channels` |
| `userbot.py` | Telethon user account: `/login` flow, `check_link`, `find_bot_channels`, pacing + flood-wait |
| `template.py` | snapshot a post's text+entities, re-render with new links (UTF-16 offsets) |
| `storage.py` | `data.json` state: channels (online/offline/strikes), post, flags, `known_chats` |
| `ui.py` | every owner-facing message ("cards") |
| `menu.py` | inline-button control panel (`/start`), one message edited in place |
| `icons.py` / `icon_data.py` | Premium custom-emoji icons + plain fallback; `icon_data.py` is generated |
| `safebot.py` | `ExtBot` subclass: resend without icons if Telegram refuses them |
| `config.py` | `.env` loader (see README for every key) |
| `setup.py` | colorama installer: deps, prompts, writes `.env`, starts in screen |
| `tools/gen_icon_data.py` | regenerates `icon_data.py` from the pack JSON |

Owner commands: `start help load login logout cancel scan sync icons status
refresh remove active slots preview unset`. Channel command: `.set` (reply).

## Rules learned the hard way — do not break these

1. **Never decide a channel is dead from the t.me web page.** It reports live
   channels as missing. Only the `/login` user account (Telethon
   `checkChatInvite` / `resolveUsername`) may answer "dead"; a negative web
   result is "unknown". This caused real money loss before.
2. **One bad answer never removes a channel.** `DEATH_STRIKES` (3) failures in a
   row, then the channel is *parked offline* (kept, re-checked, auto-restored).
   Channels are never deleted by a check. Demotion/removal events are the only
   instant removal.
3. **Monitoring is paused without `/login`** (`REQUIRE_USERBOT`).
4. **Protect the user account:** read-only requests only, one at a time,
   `MIN_REQUEST_GAP` between them, pace per Telethon dialog *batch* (100), and a
   flood-wait pauses every request until it expires.
5. **Edit, never send** for the redirect post and for the panel. Button presses
   must not create new messages.
6. **The channel hosting the post is never a link target.**
7. **Answer a callback query first**, before any work. If Telegram says the
   query is too old, the press is stale (replayed after downtime) and must be
   ignored — it could be a confirmed Remove.
8. **Only one bot instance**: `bot.lock` flock; `Conflict` errors are reported once.
9. **Network errors are not bugs:** retry via `core.with_retry`; `BadRequest`
   subclasses `NetworkError` in PTB, so always catch `(BadRequest, Forbidden)`
   *before* `core.TRANSIENT`.
10. **`core.TRANSIENT` is a tuple.** Write `except (BadRequest, *core.TRANSIENT)`,
    never `except (BadRequest, core.TRANSIENT)` (that crashed in production).
11. **Premium icons need the bot owner to have Premium.** Everything degrades to
    plain emoji via `icons.enabled()`; `safebot.py` resends without icons. Never
    hand-type a `custom_emoji_id`; add it to `tools/gen_icon_data.py`.
12. Telegram limits: 4096 chars and 100 entities per message, 64-byte
    `callback_data`. Long lists are capped in `ui.py`.
13. Escape every channel title with `ui.esc`; parse mode is HTML everywhere.

## What is NOT verified against real Telegram

All testing was done with fakes of PTB/Telethon objects. Never confirmed live:
- `/login` with a real account, and `/sync` finding the owner's real channels
  (the owner reported 12 channels with the bot as admin that it didn't know).
- Premium icons rendering, and auto-switch-off when the owner lacks Premium.
- The userbot verdicts on the owner's actual banned/deleted channels.

If the owner reports something odd in these areas, believe the report over the
tests.

## Open items

- **`setup.py`:** the owner once said "not that setup.py, I want a running
  setup.py that asks api id, hash, bot token and starts the bot, not the setup
  commands one". The current one asks for token, owner id, settings and
  optional api_id/api_hash, then starts the bot in screen, and still has
  `--start/--stop/...` flags. Never confirmed this satisfies them — ask.
- **No tests in the repo.** Every past test script was deleted after running.
  A `tests/` suite (pytest, fakes for `Bot` and the Telethon client) would stop
  regressions; worth adding before big changes.
- `python-telegram-bot` is pinned at 21.6, which has no button
  `icon_custom_emoji_id`/`style` fields; they go through `api_kwargs`.

## How to work in this repo

- Branch from latest `main`, one PR per change, descriptive commit message.
  If the PR you were adding to got merged meanwhile, open a new PR.
- PRs via REST: `gh api repos/TrishaAdio/Saviour/pulls -f title=… -f head=… -f base=main -f body=…`.
- Test on **Python 3.12** (server version). `/root/.pyenv/versions/3.12.13/bin/python -m venv`.
- Testing pitfalls hit before: monkeypatching `asyncio.sleep` with a lambda that
  calls `asyncio.sleep` recurses — keep a reference to the real one first;
  PTB `Message` objects need `set_bot()` before `reply_text`; frozen PTB objects
  can't have methods reassigned.
- Run `python3 -m py_compile *.py` before committing; never commit `.env`,
  `data.json`, `session.json`, `bot.lock`, `bot.log`, venvs.
- Update `README.md` with any user-visible change.
