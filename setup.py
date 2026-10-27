"""Installer for Saviour.

Asks for everything the account needs, logs it in to Telegram, writes .env
and finishes with the command that starts the bot.

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

HERE = Path(__file__).resolve().parent
ENV = HERE / ".env"
VENV = HERE / "venv"


def install_deps():
    """Install the packages, falling back to a local venv if pip refuses."""
    missing = [m for m in ("telethon", "dotenv", "colorama")
               if importlib.util.find_spec(m) is None]
    if not missing:
        return
    print(f"Installing dependencies ({', '.join(missing)})…")
    if subprocess.call([sys.executable, "-m", "pip", "install", "-r",
                        str(HERE / "requirements.txt")]) == 0:
        return
    python = VENV / "bin" / "python3"
    if Path(sys.executable).resolve() == python.resolve():
        raise SystemExit("Couldn't install the dependencies even in the venv — "
                         "check the pip output above.")
    print("This Python won't take packages — making a local venv…")
    if not python.exists():
        subprocess.check_call([sys.executable, "-m", "venv", str(VENV)])
    print("Restarting setup inside the venv…\n")
    os.execv(str(python), [str(python), str(Path(__file__).resolve())])


install_deps()

from colorama import Fore, Style, init  # noqa: E402

init()

BANNER = r"""
 .oooooo..o                      o8o
d8P'    `Y8                      `"'
Y88bo.       .oooo.   oooo    ooooooo   .ooooo.  oooo d8b
 `"Y8888o.  `P  )88b   `88.  .8' `888' d88' `88b `888""8P
     `"Y88b  .oP"888    `88..8'   888  888   888  888
oo     .d8P d8(  888     `888'    888  888   888  888
8""88888P'  `Y888""8o     `8'    o888o `Y8bod8P' d888b
"""

C, G, Y, R, W = (Fore.CYAN + Style.BRIGHT, Fore.GREEN + Style.BRIGHT,
                 Fore.YELLOW + Style.BRIGHT, Fore.RED + Style.BRIGHT, Style.BRIGHT)
DIM, X = Style.DIM, Style.RESET_ALL


def step(text):
    print(f"\n{C}▸{X} {W}{text}{X}")


def good(text):
    print(f"  {G}✔{X} {text}")


def warn(text):
    print(f"  {Y}!{X} {text}")


def bad(text):
    print(f"  {R}✘{X} {text}")


def hint(text):
    print(f"  {DIM}{text}{X}")


def ask(prompt, default="", secret=False):
    tail = f" [{default}]" if default else ""
    while True:
        try:
            raw = (getpass.getpass(f"  {W}›{X} {prompt}{tail}: ") if secret
                   else input(f"  {W}›{X} {prompt}{tail}: "))
        except (EOFError, KeyboardInterrupt):
            print(f"\n{R}Setup cancelled.{X}")
            raise SystemExit(1)
        value = raw.strip() or default
        if value:
            return value
        bad("That can't be empty.")


def read_env():
    out = {}
    if ENV.exists():
        for line in ENV.read_text("utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, _, value = line.partition("=")
                out[key.strip()] = value.strip()
    return out


def write_env(values):
    ENV.write_text("\n".join([
        "# Written by setup.py — edit by hand or run setup again.",
        f"API_ID={values['API_ID']}",
        f"API_HASH={values['API_HASH']}",
        f"OWNER_ID={values['OWNER_ID']}",
        f"POST_SLOTS={values['POST_SLOTS']}",
        "",
        "# Optional:",
        "# POST_TITLE=none        # bold line above the links; none = no title",
        "# CHECK_EVERY=900        # seconds between \".\" tests (15 min)",
        "# SYNC_EVERY=900         # seconds between searches for new channels",
        "# GAP=3                  # seconds between two channels in a round",
        "# MAX_WAIT=300           # longest flood-wait to sit out",
        "# STRIKES=3              # failed rounds before a channel leaves the post",
        "# REPOST_IF_DELETED=true",
        "# SESSION_NAME=saviour",
        "",
    ]), "utf-8")
    os.chmod(ENV, 0o600)


async def sign_in(api_id, api_hash, session):
    """Phone → code → 2FA, then find out who the owner is."""
    from telethon import TelegramClient, types
    from telethon.errors import (FloodWaitError, PasswordHashInvalidError,
                                 PhoneCodeExpiredError, PhoneCodeInvalidError,
                                 PhoneNumberInvalidError, SessionPasswordNeededError)

    client = TelegramClient(str(session), api_id, api_hash)
    try:
        await client.connect()
    except (ConnectionError, OSError) as exc:
        bad(f"Couldn't reach Telegram: {exc}")
        raise SystemExit(1)

    if await client.is_user_authorized():
        me = await client.get_me()
        good(f"Already signed in as @{me.username or me.id} — skipping the login.")
    else:
        step("The account that will BE the bot")
        hint("Use a spare account if you can: the session is full access.")
        while True:
            phone = re.sub(r"[\s\-()]", "", ask("Phone number with country code"))
            phone = phone if phone.startswith("+") else "+" + phone
            if not re.fullmatch(r"\+\d{7,15}", phone):
                bad("That doesn't look like a phone number.")
                continue
            try:
                sent = await client.send_code_request(phone)
                break
            except PhoneNumberInvalidError:
                bad("Telegram says that number is invalid.")
            except FloodWaitError as exc:
                bad(f"Telegram wants you to wait {exc.seconds}s first.")
                raise SystemExit(1)
        good(f"Code sent to {phone} — look in Telegram, not SMS.")

        while True:
            code = re.sub(r"[\s\-]", "", ask("Login code"))
            try:
                await client.sign_in(phone, code, phone_code_hash=sent.phone_code_hash)
                break
            except PhoneCodeInvalidError:
                bad("Wrong code — try again.")
            except PhoneCodeExpiredError:
                bad("That code expired. Run setup again for a fresh one.")
                raise SystemExit(1)
            except SessionPasswordNeededError:
                step("Two-step verification is on")
                while True:
                    try:
                        await client.sign_in(password=ask("2FA password", secret=True))
                        break
                    except PasswordHashInvalidError:
                        bad("Wrong password — try again.")
                break
            except FloodWaitError as exc:
                bad(f"Telegram wants you to wait {exc.seconds}s first.")
                raise SystemExit(1)
        me = await client.get_me()
        good(f"Signed in as @{me.username or me.id}")

    for extra in ("", "-journal"):
        f = session.with_suffix(f".session{extra}")
        if f.exists():
            os.chmod(f, 0o600)

    step("You — the account that gets every notification")
    hint("Your numeric id (ask @userinfobot) or your @username.")
    hint("It must be a different account from the one above.")
    saved, owner = read_env(), None
    while owner is None:
        raw = ask("Your id or @username", default=saved.get("OWNER_ID", ""))
        try:
            if raw.lstrip("-").isdigit():
                owner = int(raw)
                try:
                    await client.get_entity(owner)
                except ValueError:
                    warn("I can't see that account yet — keeping the id anyway.")
                    hint("Message the bot account once from it after starting.")
            else:
                found = await client.get_entity(raw.lstrip("@"))
                if not isinstance(found, types.User):
                    bad("That's a chat, not a person.")
                    continue
                owner = found.id
        except ValueError:
            bad("Couldn't find that account.")
        except Exception as exc:
            bad(f"Lookup failed: {exc}")

    if owner == (await client.get_me()).id:
        warn("That's the bot's own account. Notifications will land in Saved "
             "Messages and pasted links won't work.")
    try:
        await client.send_message(await client.get_entity(owner),
                                  "👋 Saviour setup finished — this chat is where "
                                  "I'll report in.")
        good("Test message delivered — the notification chat works.")
    except Exception:
        warn("Couldn't message you yet.")
        hint("Open a chat with the bot account from your own and say hi once.")

    await client.disconnect()
    return owner


def main():
    print(f"{C}{BANNER}{X}")
    print(f"{W}  Saviour setup{X} {DIM}· redirect userbot installer{X}")
    print(f"{DIM}  Ctrl+C at any question cancels safely.{X}")

    step("Checking Python")
    if sys.version_info < (3, 8):
        bad(f"Python {sys.version.split()[0]} is too old — 3.8 or newer, please.")
        raise SystemExit(1)
    good(f"Python {sys.version.split()[0]}")

    saved = read_env()
    if ENV.exists():
        warn("Found an existing .env — its values are the defaults below.")

    step("API credentials")
    hint("https://my.telegram.org/apps → API development tools")
    api_id = ""
    while not api_id.isdigit():
        api_id = ask("API_ID", default=saved.get("API_ID", ""))
        if not api_id.isdigit():
            bad("API_ID is a number.")
    api_hash = ""
    while not re.fullmatch(r"[0-9a-f]{32}", api_hash):
        api_hash = ask("API_HASH", default=saved.get("API_HASH", ""))
        if not re.fullmatch(r"[0-9a-f]{32}", api_hash):
            bad("API_HASH is 32 characters of lowercase hex.")

    step("The redirect post")
    slots = ask("How many link lines should it have?",
                default=saved.get("POST_SLOTS", "5"))
    if not slots.isdigit() or not 1 <= int(slots) <= 20:
        warn("Not a number between 1 and 20 — using 5.")
        slots = "5"

    import config
    session = HERE / (saved.get("SESSION_NAME") or config.SESSION)
    owner = asyncio.run(sign_in(int(api_id), api_hash, session))

    write_env({"API_ID": api_id, "API_HASH": api_hash,
               "OWNER_ID": str(owner), "POST_SLOTS": slots})
    good(f".env written, readable only by you: {ENV}")

    runner = "venv/bin/python3" if (VENV / "bin" / "python3").exists() else "python3"
    print(f"\n{G}{'─' * 58}{X}")
    print(f"{G}{W}  🎉 All set!{X}\n")
    print(f"  {W}Start it:{X}")
    print(f"    {C}{runner} saviour.py{X}\n")
    print(f"  {W}Keep it alive across reboots:{X}")
    print(f"    {DIM}cp saviour.service /etc/systemd/system/")
    print(f"    systemctl daemon-reload && systemctl enable --now saviour{X}\n")
    print(f"  {W}Then, in Telegram:{X}")
    print("    1. Add this account to a backup channel and promote it to admin")
    print(f"       {DIM}→ you get \"🟢 Channel synced\"{X}")
    print(f"    2. Add it to your public channel and post {W}.set{X} there")
    print(f"       {DIM}→ not admin yet? it waits, then posts the moment you promote it{X}")
    print(f"    3. Talk to it in your own chat: {W}/status{X}, {W}/scan{X}, {W}/help{X}")
    print(f"{G}{'─' * 58}{X}\n")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print(f"\n{R}Setup cancelled.{X}")
        raise SystemExit(1)
