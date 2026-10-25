"""Optional user-account helper, logged in with /login.

Why this exists
---------------
A bot account cannot ask Telegram whether an invite link still works. The web
check (opening t.me) is a guess and gets it wrong. A *user* account can call
``messages.checkChatInvite`` and ``contacts.resolveUsername`` directly, so
Telegram itself tells us whether a link is alive, revoked or gone.

The session is stored in ``session.json`` (chmod 600) next to the bot. Treat
that file like a password: it grants full access to the account.
"""

import asyncio
import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import config

log = logging.getLogger(__name__)

try:
    from telethon import TelegramClient, errors, functions, types
    from telethon.sessions import StringSession
    AVAILABLE = True
except ImportError:  # telethon not installed yet
    AVAILABLE = False
    TelegramClient = None  # type: ignore

SESSION_FILE = config.BASE_DIR / "session.json"

# t.me/+hash, t.me/joinchat/hash  ->  private invite
_INVITE_RE = re.compile(r"(?:t(?:elegram)?\.me|telegram\.dog)/(?:\+|joinchat/)([\w-]+)", re.I)
# t.me/username  ->  public channel
_PUBLIC_RE = re.compile(r"(?:t(?:elegram)?\.me|telegram\.dog)/([A-Za-z]\w{3,31})/?$", re.I)

# Verdicts
LIVE, DEAD, UNKNOWN = True, False, None

_client: Optional["TelegramClient"] = None
_lock = asyncio.Lock()
_broken = False  # session rejected by Telegram; stop trying until re-login


# ------------------------------------------------------------- storage
def load() -> Dict[str, Any]:
    if not SESSION_FILE.exists():
        return {}
    try:
        return json.loads(SESSION_FILE.read_text("utf-8")) or {}
    except (json.JSONDecodeError, OSError) as exc:
        log.error("session.json unreadable: %s", exc)
        return {}


def save(data: Dict[str, Any]) -> None:
    tmp = SESSION_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2), "utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(SESSION_FILE)
    os.chmod(SESSION_FILE, 0o600)


def logged_in() -> bool:
    return AVAILABLE and not _broken and bool(load().get("session"))


def account() -> str:
    data = load()
    name = data.get("username")
    return f"@{name}" if name else (data.get("phone") or "user account")


def api_credentials() -> Tuple[Optional[int], Optional[str]]:
    """API id/hash from .env, or from the last successful login."""
    data = load()
    api_id = config.API_ID or data.get("api_id")
    api_hash = config.API_HASH or data.get("api_hash")
    return (int(api_id) if api_id else None), (api_hash or None)


# -------------------------------------------------------------- client
def _build(api_id: int, api_hash: str, session: str = "") -> "TelegramClient":
    return TelegramClient(
        StringSession(session), api_id, api_hash,
        # Only outgoing requests - we never need incoming updates here.
        receive_updates=False,
        device_model="Saviour", system_version="1.0", app_version="1.0",
        connection_retries=2, request_retries=2, flood_sleep_threshold=0,
    )


async def connected() -> Optional["TelegramClient"]:
    """The shared, connected client, or None when not logged in."""
    global _client, _broken
    if not logged_in():
        return None
    async with _lock:
        if _client is not None and _client.is_connected():
            return _client
        data = load()
        api_id, api_hash = api_credentials()
        if not api_id or not api_hash:
            log.error("userbot session present but API id/hash missing")
            return None
        try:
            _client = _build(api_id, api_hash, data["session"])
            await _client.connect()
            if not await _client.is_user_authorized():
                raise errors.AuthKeyUnregisteredError(None)
        except Exception as exc:  # noqa: BLE001 - any failure means unusable
            log.error("userbot cannot connect: %s: %s", type(exc).__name__, exc)
            if isinstance(exc, (errors.AuthKeyUnregisteredError,
                                errors.AuthKeyDuplicatedError,
                                errors.UserDeactivatedError,
                                errors.SessionRevokedError)):
                _broken = True
            _client = None
            return None
        return _client


async def disconnect() -> None:
    global _client
    if _client is not None:
        try:
            await _client.disconnect()
        except Exception:  # noqa: BLE001
            pass
        _client = None


async def logout() -> bool:
    """Sign the account out of Telegram and delete the stored session."""
    global _broken
    client = await connected()
    ok = True
    if client is not None:
        try:
            await client.log_out()
        except Exception as exc:  # noqa: BLE001
            log.warning("log_out failed: %s", exc)
            ok = False
    await disconnect()
    data = load()
    keep = {k: data[k] for k in ("api_id", "api_hash") if k in data}
    save(keep)
    _broken = False
    return ok


# --------------------------------------------------------- link checks
def _dead_detail(exc: Exception) -> str:
    if isinstance(exc, errors.InviteHashExpiredError):
        return "Invite link was revoked or expired"
    if isinstance(exc, (errors.InviteHashInvalidError, errors.InviteHashEmptyError)):
        return "Invite link is not valid any more"
    if isinstance(exc, errors.ChannelPrivateError):
        return "Channel is banned, deleted or no longer reachable"
    if isinstance(exc, (errors.UsernameNotOccupiedError, errors.UsernameInvalidError)):
        return "Channel username no longer exists"
    return str(exc)


def _chat_is_gone(chat: Any) -> bool:
    """A banned/deleted channel can still come back as a stub object."""
    if chat is None:
        return True
    if isinstance(chat, types.ChannelForbidden):
        return True
    return bool(getattr(chat, "deactivated", False)
                or getattr(chat, "left", False) and getattr(chat, "broadcast", False) is None)


async def check_link(url: str) -> Tuple[Optional[bool], str]:
    """Ask Telegram whether a link works. Returns (LIVE/DEAD/UNKNOWN, detail)."""
    client = await connected()
    if client is None:
        return UNKNOWN, "userbot unavailable"

    invite = _INVITE_RE.search(url)
    public = _PUBLIC_RE.search(url.rstrip("/"))
    try:
        if invite:
            result = await client(functions.messages.CheckChatInviteRequest(invite.group(1)))
            if isinstance(result, types.ChatInviteAlready):
                return (DEAD, "Channel is banned or deleted") if _chat_is_gone(result.chat) \
                    else (LIVE, "Already a member")
            if isinstance(result, types.ChatInvitePeek):
                return (DEAD, "Channel is banned or deleted") if _chat_is_gone(result.chat) \
                    else (LIVE, "Invite works")
            if isinstance(result, types.ChatInvite):
                return LIVE, f"Invite works ({result.title})"
            return UNKNOWN, f"Unexpected reply: {type(result).__name__}"

        if public:
            result = await client(functions.contacts.ResolveUsernameRequest(public.group(1)))
            chats = list(getattr(result, "chats", []) or [])
            if not chats:
                return DEAD, "Channel username no longer exists"
            return (DEAD, "Channel is banned or deleted") if _chat_is_gone(chats[0]) \
                else (LIVE, "Channel is reachable")

        return UNKNOWN, "not a t.me channel link"

    except errors.FloodWaitError as exc:
        log.warning("userbot flood wait %ss on %s", exc.seconds, url)
        return UNKNOWN, f"rate limited for {exc.seconds}s"
    except (errors.InviteHashExpiredError, errors.InviteHashInvalidError,
            errors.InviteHashEmptyError, errors.ChannelPrivateError,
            errors.UsernameNotOccupiedError, errors.UsernameInvalidError) as exc:
        return DEAD, _dead_detail(exc)
    except (errors.AuthKeyUnregisteredError, errors.SessionRevokedError,
            errors.UserDeactivatedError) as exc:
        global _broken
        _broken = True
        await disconnect()
        log.error("userbot session is no longer valid: %s", exc)
        return UNKNOWN, "session no longer valid - /login again"
    except Exception as exc:  # noqa: BLE001 - never let a check crash the job
        log.warning("userbot check of %s failed: %s: %s", url, type(exc).__name__, exc)
        return UNKNOWN, f"{type(exc).__name__}"


# ------------------------------------------------------- login journey
class Login:
    """Step-by-step /login flow. One instance per attempt."""

    def __init__(self) -> None:
        self.api_id: Optional[int] = None
        self.api_hash: Optional[str] = None
        self.phone: Optional[str] = None
        self.hash: Optional[str] = None
        self.client: Optional["TelegramClient"] = None
        env_id, env_hash = api_credentials()
        if env_id and env_hash:
            self.api_id, self.api_hash = env_id, env_hash
        self.step = "phone" if self.api_id else "api_id"

    # each step returns (next_step, message_for_owner)
    async def feed(self, text: str) -> Tuple[str, str]:
        text = (text or "").strip()
        if self.step == "api_id":
            if not text.isdigit():
                return "api_id", "That isn't a number. Send the numeric <b>api_id</b> from my.telegram.org."
            self.api_id = int(text)
            self.step = "api_hash"
            return "api_hash", "Now send the <b>api_hash</b> (32 characters)."

        if self.step == "api_hash":
            if not re.fullmatch(r"[0-9a-fA-F]{32}", text):
                return "api_hash", "That doesn't look like an api_hash. It's 32 letters and digits."
            self.api_hash = text
            self.step = "phone"
            return "phone", ("Send the <b>phone number</b> of the account, with country "
                             "code, e.g. <code>+919876543210</code>.")

        if self.step == "phone":
            if not re.fullmatch(r"\+?\d{6,15}", text.replace(" ", "")):
                return "phone", "That isn't a phone number. Include the country code, e.g. <code>+919876543210</code>."
            self.phone = text.replace(" ", "")
            if not self.phone.startswith("+"):
                self.phone = "+" + self.phone
            return await self._send_code()

        if self.step == "code":
            return await self._sign_in(text)

        if self.step == "password":
            return await self._password(text)

        return self.step, "Send /login to start again."

    async def _send_code(self) -> Tuple[str, str]:
        try:
            self.client = _build(self.api_id, self.api_hash)
            await self.client.connect()
            sent = await self.client.send_code_request(self.phone)
            self.hash = sent.phone_code_hash
        except errors.ApiIdInvalidError:
            await self.close()
            return "failed", "Telegram rejected that <b>api_id / api_hash</b> pair. Check them and send /login again."
        except errors.PhoneNumberInvalidError:
            self.step = "phone"
            return "phone", "Telegram says that phone number is invalid. Send it again with the country code."
        except errors.PhoneNumberBannedError:
            await self.close()
            return "failed", "That number is banned by Telegram. Use another account."
        except errors.FloodWaitError as exc:
            await self.close()
            return "failed", f"Telegram is rate limiting this account. Try again in {exc.seconds}s."
        except Exception as exc:  # noqa: BLE001
            await self.close()
            log.error("send_code_request failed: %s: %s", type(exc).__name__, exc)
            return "failed", f"Couldn't request the code: <code>{type(exc).__name__}</code>"
        self.step = "code"
        return "code", ("Telegram sent a <b>login code</b> to that account. Send it here.\n"
                        "If you typed it in Telegram's own login box it stops working, so "
                        "copy it from the message.")

    async def _sign_in(self, code: str) -> Tuple[str, str]:
        code = re.sub(r"\D", "", code)
        if not code:
            return "code", "Send just the digits of the login code."
        try:
            await self.client.sign_in(self.phone, code, phone_code_hash=self.hash)
        except errors.SessionPasswordNeededError:
            self.step = "password"
            return "password", "This account has <b>two-step verification</b>. Send its password."
        except (errors.PhoneCodeInvalidError, errors.PhoneCodeEmptyError):
            return "code", "That code is wrong. Send it again."
        except errors.PhoneCodeExpiredError:
            await self.close()
            return "failed", "That code expired. Send /login to start again."
        except errors.FloodWaitError as exc:
            await self.close()
            return "failed", f"Too many attempts. Try again in {exc.seconds}s."
        except Exception as exc:  # noqa: BLE001
            await self.close()
            log.error("sign_in failed: %s: %s", type(exc).__name__, exc)
            return "failed", f"Sign-in failed: <code>{type(exc).__name__}</code>"
        return await self._finish()

    async def _password(self, password: str) -> Tuple[str, str]:
        try:
            await self.client.sign_in(password=password)
        except errors.PasswordHashInvalidError:
            return "password", "Wrong password. Send it again."
        except errors.FloodWaitError as exc:
            await self.close()
            return "failed", f"Too many attempts. Try again in {exc.seconds}s."
        except Exception as exc:  # noqa: BLE001
            await self.close()
            log.error("2FA sign_in failed: %s: %s", type(exc).__name__, exc)
            return "failed", f"Sign-in failed: <code>{type(exc).__name__}</code>"
        return await self._finish()

    async def _finish(self) -> Tuple[str, str]:
        global _broken
        try:
            me = await self.client.get_me()
            session = self.client.session.save()
        except Exception as exc:  # noqa: BLE001
            await self.close()
            return "failed", f"Signed in but couldn't save the session: <code>{type(exc).__name__}</code>"
        save({"api_id": self.api_id, "api_hash": self.api_hash, "phone": self.phone,
              "session": session, "user_id": me.id, "username": me.username})
        _broken = False
        await self.close(disconnect_only=True)
        await disconnect()  # drop any old cached client so the new session is used
        name = f"@{me.username}" if me.username else (me.first_name or str(me.id))
        return "done", name

    async def close(self, disconnect_only: bool = False) -> None:
        if self.client is not None:
            try:
                await self.client.disconnect()
            except Exception:  # noqa: BLE001
                pass
            self.client = None
        if not disconnect_only:
            self.step = "failed"
