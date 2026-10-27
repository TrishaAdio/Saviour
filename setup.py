"""Interactive installer for Saviour.

Asks for everything the userbot needs (api credentials, phone, login code,
2FA password, owner id), writes .env, performs the Telegram login so the
session file is ready, and finishes with the command to start the bot.

    python3 setup.py
"""

import asyncio
import getpass
import importlib.util
import os
import re
import subprocess
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
REQUIREMENTS = BASE_DIR / "requirements.txt"
ENV_FILE = BASE_DIR / ".env"
VENV = BASE_DIR / "venv"


# --------------------------------------------------------------- bootstrap
def ensure_dependencies() -> None:
    """Install the pip packages; fall back to a local venv if the system
    Python refuses them, restarting setup inside that venv."""
    missing = [m for m in ("telethon", "dotenv", "colorama")
               if importlib.util.find_spec(m) is None]
    if not missing:
        return
    print(f"Installing dependencies ({', '.join(missing)})…")
    rc = subprocess.call(
        [sys.executable, "-m", "pip", "install", "-r", str(REQUIREMENTS)])
    if rc == 0:
        return

    venv_python = VENV / "bin" / "python3"
    if Path(sys.executable).resolve() == venv_python.resolve():
        raise SystemExit("Could not install the dependencies even inside the "
                         "venv - check your internet connection and pip output above.")
    print("The system Python won't take packages - creating a local venv…")
    if not venv_python.exists():
        subprocess.check_call([sys.executable, "-m", "venv", str(VENV)])
    print("Restarting setup inside the venv…\n")
    os.execv(str(venv_python), [str(venv_python), str(Path(__file__).resolve())])


ensure_dependencies()

from colorama import Fore, Style, init as colorama_init  # noqa: E402

colorama_init()

BANNER = r"""
 .oooooo..o                      o8o
d8P'    `Y8                      `"'
Y88bo.       .oooo.   oooo    ooooooo   .ooooo.  oooo d8b
 `"Y8888o.  `P  )88b   `88.  .8' `888' d88' `88b `888""8P
     `"Y88b  .oP"888    `88..8'   888  888   888  888
oo     .d8P d8(  888     `888'    888  888   888  888
8""88888P'  `Y888""8o     `8'    o888o `Y8bod8P' d888b
"""

C = Fore.CYAN + Style.BRIGHT
G = Fore.GREEN + Style.BRIGHT
Y = Fore.YELLOW + Style.BRIGHT
R = Fore.RED + Style.BRIGHT
W = Style.BRIGHT
DIM = Style.DIM
X = Style.RESET_ALL


def step(text: str) -> None:
    print(f"\n{C}▸{X} {W}{text}{X}")


def ok(text: str) -> None:
    print(f"  {G}✔{X} {text}")


def warn(text: str) -> None:
    print(f"  {Y}!{X} {text}")


def bad(text: str) -> None:
    print(f"  {R}✘{X} {text}")


def hint(text: str) -> None:
    print(f"  {DIM}{text}{X}")


def ask(prompt: str, default: str = "", secret: bool = False) -> str:
    """One validated prompt. Ctrl+C / EOF exits cleanly."""
    suffix = f" [{default}]" if default else ""
    while True:
        try:
            if secret:
                raw = getpass.getpass(f"  {W}›{X} {prompt}{suffix}: ")
            else:
                raw = input(f"  {W}›{X} {prompt}{suffix}: ")
        except (EOFError, KeyboardInterrupt):
            print(f"\n{R}Setup cancelled.{X}")
            raise SystemExit(1)
        value = raw.strip() or default
        if value:
            return value
        bad("A value is required.")


# ------------------------------------------------------------------- .env
def existing_env() -> dict:
    values = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text("utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, _, val = line.partition("=")
                values[key.strip()] = val.strip()
    return values


def write_env(values: dict) -> None:
    lines = [
        "# Written by setup.py - edit by hand or re-run setup.",
        f"API_ID={values['API_ID']}",
        f"API_HASH={values['API_HASH']}",
        f"OWNER_ID={values['OWNER_ID']}",
        f"POST_SLOTS={values['POST_SLOTS']}",
        "",
        "# Optional overrides:",
        "# POST_TITLE=none",
        "# CHECK_INTERVAL=300",
        "# REQUEST_GAP=3",
        "# DEATH_STRIKES=3",
        "# SYNC_INTERVAL=21600",
        "# REPOST_IF_DELETED=true",
        "# SESSION_NAME=saviour",
        "",
    ]
    ENV_FILE.write_text("\n".join(lines), "utf-8")
    os.chmod(ENV_FILE, 0o600)


# ------------------------------------------------------------------ login
async def telegram_login(api_id: int, api_hash: str, session_path: Path) -> None:
    """Full interactive Telegram login: phone -> code -> optional 2FA."""
    from telethon import TelegramClient
    from telethon.errors import (
        FloodWaitError,
        PasswordHashInvalidError,
        PhoneCodeExpiredError,
        PhoneCodeInvalidError,
        PhoneNumberInvalidError,
        SessionPasswordNeededError,
    )

    client = TelegramClient(str(session_path), api_id, api_hash)
    try:
        await client.connect()
    except (ConnectionError, OSError) as exc:
        bad(f"Could not reach Telegram: {exc}")
        hint("Check the internet connection and re-run setup.")
        raise SystemExit(1)

    if await client.is_user_authorized():
        me = await client.get_me()
        ok(f"Session already logged in as @{me.username or me.id} - skipping login.")
    else:
        step("Phone number (the account that will BE the userbot)")
        hint("Use a spare account if you can - the session is full access.")
        while True:
            phone = ask("Phone, with country code (e.g. +15551234567)")
            phone = re.sub(r"[\s\-()]", "", phone)
            if not phone.startswith("+"):
                phone = "+" + phone
            if not re.fullmatch(r"\+\d{7,15}", phone):
                bad("That doesn't look like a phone number - digits only, with country code.")
                continue
            try:
                sent = await client.send_code_request(phone)
                break
            except PhoneNumberInvalidError:
                bad("Telegram says that number is invalid - check it.")
            except FloodWaitError as exc:
                bad(f"Telegram asks to wait {exc.seconds}s before trying again.")
                raise SystemExit(1)
        ok(f"Code sent to {phone} (check Telegram, not SMS).")

        while True:
            code = ask("Login code")
            code = re.sub(r"[\s\-]", "", code)
            try:
                await client.sign_in(phone, code,
                                     phone_code_hash=sent.phone_code_hash)
                break
            except PhoneCodeInvalidError:
                bad("Wrong code - try again.")
            except PhoneCodeExpiredError:
                bad("That code expired. Re-run setup to get a new one.")
                raise SystemExit(1)
            except SessionPasswordNeededError:
                step("Two-step verification is on - password needed")
                while True:
                    password = ask("2FA password", secret=True)
                    try:
                        await client.sign_in(password=password)
                        break
                    except PasswordHashInvalidError:
                        bad("Wrong password - try again.")
                break
            except FloodWaitError as exc:
                bad(f"Telegram asks to wait {exc.seconds}s before trying again.")
                raise SystemExit(1)

        me = await client.get_me()
        ok(f"Logged in as @{me.username or me.id} ({me.first_name or ''})")

    # Make sure only this user can read the session file.
    for suffix in ("", "-journal"):
        f = session_path.with_suffix(f".session{suffix}")
        if f.exists():
            os.chmod(f, 0o600)

    # ------------------------------------------------ owner id + test DM
    step("Owner - your main account (gets every notification)")
    hint("Numeric id from @userinfobot, or your @username.")
    from telethon import types

    env = existing_env()
    owner = None
    while owner is None:
        raw = ask("Owner id or @username", default=env.get("OWNER_ID", ""))
        try:
            if raw.lstrip("-").isdigit():
                owner = int(raw)
                try:
                    await client.get_entity(owner)
                except ValueError:
                    warn("Can't see that user yet - keeping the id anyway.")
                    hint("After starting the bot, send /start to it once from that account.")
            else:
                entity = await client.get_entity(raw.lstrip("@"))
                if not isinstance(entity, types.User):
                    bad("That's a channel/group - the owner must be a user account.")
                    continue
                owner = entity.id
        except ValueError:
            bad("Could not find that user - check the username/id.")
        except Exception as exc:  # flood etc.
            bad(f"Lookup failed: {exc}")

    try:
        entity = await client.get_entity(owner)
        await client.send_message(entity, "👋 Saviour setup complete - this chat is my command channel.")
        ok("Test message sent to the owner - the DM channel works.")
    except Exception:
        warn("Could not message the owner yet.")
        hint("Open a DM with the userbot account from your main account and send /start once.")

    await client.disconnect()
    return owner


# -------------------------------------------------------------------- main
def main() -> None:
    print(f"{C}{BANNER}{X}")
    print(f"{W}  Saviour setup{X} {DIM}· redirect userbot installer{X}")
    print(f"{DIM}  Ctrl+C at any prompt cancels safely.{X}")

    step("Checking Python")
    if sys.version_info < (3, 8):
        bad(f"Python {sys.version.split()[0]} is too old - need 3.8+.")
        raise SystemExit(1)
    ok(f"Python {sys.version.split()[0]}")

    env = existing_env()
    if ENV_FILE.exists():
        warn("An existing .env was found - its values are the defaults below.")

    step("API credentials")
    hint("https://my.telegram.org/apps → API development tools")
    api_id = ""
    while not api_id.isdigit():
        api_id = ask("API_ID", default=env.get("API_ID", ""))
        if not api_id.isdigit():
            bad("API_ID is a number.")
    api_hash = ""
    while not re.fullmatch(r"[0-9a-f]{32}", api_hash):
        api_hash = ask("API_HASH", default=env.get("API_HASH", ""))
        if not re.fullmatch(r"[0-9a-f]{32}", api_hash):
            bad("API_HASH is 32 hex characters - copy it exactly.")

    step("Redirect post")
    slots = ask("Link lines in the post (POST_SLOTS)",
                default=env.get("POST_SLOTS", "5"))
    if not slots.isdigit() or not 1 <= int(slots) <= 20:
        warn("Invalid number - using 5.")
        slots = "5"

    # ---------------------------------------------------------------- login
    import config  # safe now: .env may not exist yet, defaults are fine
    session_path = BASE_DIR / (env.get("SESSION_NAME") or config.SESSION_NAME)
    owner = asyncio.run(telegram_login(int(api_id), api_hash, session_path))

    write_env({"API_ID": api_id, "API_HASH": api_hash,
               "OWNER_ID": str(owner), "POST_SLOTS": slots})
    ok(f".env written (owner-only permissions): {ENV_FILE}")

    # -------------------------------------------------------------- done
    venv_python = VENV / "bin" / "python3"
    runner = "venv/bin/python3" if venv_python.exists() else "python3"

    print(f"\n{G}{'─' * 56}{X}")
    print(f"{G}{Style.BRIGHT}  🎉 Setup complete!{X}\n")
    print(f"  {W}Start the bot:{X}")
    print(f"    {C}{runner} main.py{X}\n")
    print(f"  {W}Keep it running across reboots:{X}")
    print(f"    {DIM}cp saviour.service /etc/systemd/system/")
    print(f"    systemctl daemon-reload && systemctl enable --now saviour{X}\n")
    print(f"  {W}Then:{X}")
    print(f"    1. Add the userbot account to a backup channel and promote it")
    print(f"       → you get 🟢 Channel synced")
    print(f"    2. Add it to the redirect channel, post {W}.set{X} there")
    print(f"       (it waits for admin if it has to, then posts by itself)")
    print(f"    3. Commands live in the DM with the userbot: /status /sync /scan …")
    print(f"{G}{'─' * 56}{X}\n")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print(f"\n{R}Setup cancelled.{X}")
        raise SystemExit(1)
