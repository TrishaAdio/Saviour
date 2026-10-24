#!/usr/bin/env python3
"""Interactive installer + screen launcher for the redirect bot.

    python3 setup.py            # install, configure, start in screen
    python3 setup.py --start    # start in screen
    python3 setup.py --stop     # kill the screen session
    python3 setup.py --restart  # stop + start
    python3 setup.py --logs     # tail the log file
    python3 setup.py --attach   # how to attach to the screen
    python3 setup.py --status   # is it running?
"""

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

try:
    from colorama import Fore, Style, init
except ImportError:  # bootstrap colorama, then re-exec
    print("[*] installing colorama …")
    subprocess.run([sys.executable, "-m", "pip", "install", "-q", "colorama"], check=False)
    try:
        from colorama import Fore, Style, init
    except ImportError:
        class _Dummy:
            def __getattr__(self, _):
                return ""

        Fore = Style = _Dummy()

        def init(*_a, **_kw):
            return None

init(autoreset=True)

BASE = Path(__file__).resolve().parent
ENV = BASE / ".env"
SESSION = "redirectbot"
LOG = BASE / "bot.log"

C = Fore.CYAN + Style.BRIGHT
G = Fore.GREEN + Style.BRIGHT
Y = Fore.YELLOW + Style.BRIGHT
R = Fore.RED + Style.BRIGHT
M = Fore.MAGENTA + Style.BRIGHT
W = Fore.WHITE + Style.BRIGHT
D = Style.DIM
X = Style.RESET_ALL

BANNER = r"""
  ____          _ _               _     ____        _
 |  _ \ ___  __| (_)_ __ ___  ___| |_  | __ )  ___ | |_
 | |_) / _ \/ _` | | '__/ _ \/ __| __| |  _ \ / _ \| __|
 |  _ <  __/ (_| | | | |  __/ (__| |_  | |_) | (_) | |_
 |_| \_\___|\__,_|_|_|  \___|\___|\__| |____/ \___/ \__|
"""


def banner() -> None:
    print(C + BANNER)
    print(M + "        auto-healing channel redirect bot" + X)
    print(D + "        " + "-" * 42 + X + "\n")


def ok(msg):
    print(f"{G}  [✓] {X}{msg}")


def info(msg):
    print(f"{C}  [*] {X}{msg}")


def warn(msg):
    print(f"{Y}  [!] {X}{msg}")


def err(msg):
    print(f"{R}  [✗] {X}{msg}")


def step(n, total, msg):
    print(f"\n{W}[{n}/{total}]{X} {M}{msg}{X}")


def ask(label, default=None, secret=False, cast=None):
    suffix = f" {D}[{default}]{X}" if default not in (None, "") else ""
    while True:
        raw = input(f"{Y}  ?{X} {label}{suffix}: ").strip()
        if not raw and default not in (None, ""):
            raw = str(default)
        if not raw:
            err("this one is required")
            continue
        if cast:
            try:
                return cast(raw)
            except ValueError:
                err("invalid value, try again")
                continue
        return raw


# ----------------------------------------------------------- utilities
def run(cmd, **kw):
    return subprocess.run(cmd, **kw)


def has(binary):
    return shutil.which(binary) is not None


def screen_running() -> bool:
    if not has("screen"):
        return False
    out = subprocess.run(["screen", "-ls"], capture_output=True, text=True).stdout
    return f".{SESSION}\t" in out or f".{SESSION} " in out


def install_screen() -> bool:
    if has("screen"):
        ok("screen is installed")
        return True
    warn("screen not found — trying to install it")
    sudo = ["sudo"] if os.geteuid() != 0 and has("sudo") else []
    for mgr, args in (
        ("apt-get", ["install", "-y", "screen"]),
        ("dnf", ["install", "-y", "screen"]),
        ("yum", ["install", "-y", "screen"]),
        ("apk", ["add", "screen"]),
        ("pacman", ["-Sy", "--noconfirm", "screen"]),
    ):
        if not has(mgr):
            continue
        if mgr == "apt-get":
            run(sudo + [mgr, "update", "-qq"], check=False)
        res = run(sudo + [mgr] + args, check=False)
        if res.returncode == 0 and has("screen"):
            ok("screen installed")
            return True
    err("could not install screen automatically")
    print(f"{D}      install it manually, e.g.: sudo apt install screen{X}")
    return False


def install_requirements() -> bool:
    req = BASE / "requirements.txt"
    if not req.exists():
        err("requirements.txt missing")
        return False
    info("installing python packages (this takes a moment) …")
    res = run(
        [sys.executable, "-m", "pip", "install", "-q", "--upgrade", "-r", str(req)],
        check=False,
    )
    if res.returncode != 0:
        warn("pip returned an error — retrying with --break-system-packages")
        res = run(
            [sys.executable, "-m", "pip", "install", "-q", "--upgrade",
             "--break-system-packages", "-r", str(req)],
            check=False,
        )
    if res.returncode == 0:
        ok("dependencies ready")
        return True
    err("dependency install failed")
    return False


def read_env() -> dict:
    data = {}
    if ENV.exists():
        for line in ENV.read_text("utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                data[key.strip()] = value.strip()
    return data


def configure() -> dict:
    current = read_env()
    if current.get("BOT_TOKEN") and current.get("OWNER_ID"):
        print(f"{D}      existing config found — press Enter to keep each value{X}")

    cfg = {}
    cfg["BOT_TOKEN"] = ask("Bot token (from @BotFather)", current.get("BOT_TOKEN"))
    cfg["OWNER_ID"] = str(ask("Your numeric Telegram user id (@userinfobot)",
                              current.get("OWNER_ID"), cast=int))
    cfg["POST_SLOTS"] = str(ask("How many link lines in the post",
                                current.get("POST_SLOTS", "5"), cast=int))
    cfg["POST_TITLE"] = ask("Title line above the links ('none' for no title)",
                            current.get("POST_TITLE", "none"))
    cfg["CHECK_INTERVAL"] = str(ask("Health-check interval in seconds",
                                    current.get("CHECK_INTERVAL", "300"), cast=int))
    print(f"{D}      link mode: 'single' = all lines show the active channel"
          f" (like your screenshot){X}")
    print(f"{D}                 'spread' = rotate lines across every loaded channel{X}")
    mode = ask("Link mode (single/spread)", current.get("LINK_MODE", "single")).lower()
    cfg["LINK_MODE"] = mode if mode in ("single", "spread") else "single"
    cfg["EXPANDABLE_QUOTE"] = current.get("EXPANDABLE_QUOTE", "false")
    cfg["MIN_CHANNELS"] = current.get("MIN_CHANNELS", "1")

    ENV.write_text(
        "# generated by setup.py\n"
        + "\n".join(f"{k}={v}" for k, v in cfg.items())
        + "\n",
        "utf-8",
    )
    os.chmod(ENV, 0o600)
    ok(f"config written to {ENV.name}")
    return cfg


def start() -> bool:
    if not has("screen"):
        err("screen is not installed — run: python3 setup.py")
        return False
    if screen_running():
        warn(f"session '{SESSION}' is already running")
        return True
    if not ENV.exists():
        err("no .env — run: python3 setup.py")
        return False

    run(["screen", "-dmS", SESSION, sys.executable, str(BASE / "bot.py")],
        cwd=str(BASE), check=False)
    time.sleep(3)

    if screen_running():
        ok(f"bot started in screen session {W}{SESSION}{X}")
        return True
    err("bot did not stay up — last log lines:")
    tail_log(20)
    return False


def stop() -> bool:
    if not screen_running():
        warn("not running")
        return True
    run(["screen", "-S", SESSION, "-X", "quit"], check=False)
    time.sleep(1)
    if screen_running():
        err("could not stop the session")
        return False
    ok("stopped")
    return True


def tail_log(lines=40) -> None:
    if not LOG.exists():
        warn("no log file yet")
        return
    content = LOG.read_text("utf-8", errors="replace").splitlines()[-lines:]
    print(D + "  " + "-" * 60 + X)
    for line in content:
        print("  " + line)
    print(D + "  " + "-" * 60 + X)


def status() -> None:
    if screen_running():
        ok(f"running in screen session '{SESSION}'")
    else:
        err("not running")
    env = read_env()
    if env:
        token = env.get("BOT_TOKEN", "")
        masked = token[:8] + "…" + token[-4:] if len(token) > 14 else "(set)" if token else "(missing)"
        print(f"{D}      token: {masked}   owner: {env.get('OWNER_ID', '?')}   "
              f"slots: {env.get('POST_SLOTS', '?')}{X}")


def outro() -> None:
    print(f"\n{G}  Done.{X}\n")
    print(f"{W}  Manage it{X}")
    print(f"    {C}screen -r {SESSION}{X}      attach to the live console  "
          f"{D}(detach: Ctrl+A then D){X}")
    print(f"    {C}python3 setup.py --logs{X}     tail the log file")
    print(f"    {C}python3 setup.py --restart{X}  restart")
    print(f"    {C}python3 setup.py --stop{X}     stop\n")
    print(f"{W}  Next in Telegram{X}")
    print(f"    {Y}1.{X} make the bot admin in your backup channels")
    print(f"    {Y}2.{X} DM the bot {C}/load{X} and paste the channel links "
          f"→ {G}access verified{X}")
    print(f"    {Y}3.{X} make it admin in your public channel and post {C}.set{X} there")
    print(f"    {Y}4.{X} it posts the redirect message and keeps the links alive\n")


def full_setup() -> None:
    banner()
    total = 4
    step(1, total, "Checking system")
    print(f"{D}      python {sys.version.split()[0]}{X}")
    if sys.version_info < (3, 9):
        err("python 3.9+ required")
        sys.exit(1)
    ok("python version fine")
    install_screen()

    step(2, total, "Installing dependencies")
    if not install_requirements():
        sys.exit(1)

    step(3, total, "Configuration")
    configure()

    step(4, total, "Launching")
    if not start():
        sys.exit(1)
    outro()


def main() -> None:
    arg = sys.argv[1] if len(sys.argv) > 1 else ""
    if arg in ("--start", "-s"):
        banner()
        start()
    elif arg in ("--stop", "-k"):
        banner()
        stop()
    elif arg in ("--restart", "-r"):
        banner()
        stop()
        start()
    elif arg in ("--logs", "-l"):
        banner()
        tail_log(60)
    elif arg in ("--status",):
        banner()
        status()
    elif arg in ("--attach", "-a"):
        banner()
        info(f"run: screen -r {SESSION}   (detach with Ctrl+A then D)")
    elif arg in ("--help", "-h"):
        banner()
        print(__doc__)
    else:
        full_setup()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print(f"\n{R}  aborted{X}")
        sys.exit(130)
