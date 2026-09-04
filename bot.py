#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════╗
║   ⚡ TELEGRAM HOSTING BOT ⚡                  ║
║   Render Free Tier (512 MB) Optimized        ║
║                                              ║
║   • Owner approval system (button w/ ID)     ║
║   • Channel join gate (bot as channel admin) ║
║   • Smart auto dependency installer          ║
║     (requirements.txt / package.json / .zip) ║
║   • Hacker-style terminal                    ║
║   • Background process manager               ║
║     (/run, /ps, /logs, /kill)                ║
║   • Flask keep-alive for UptimeRobot 24x7    ║
╚══════════════════════════════════════════════╝
"""

import os
import re
import json
import html
import random
import shlex
import signal
import logging
import asyncio
import threading
from datetime import datetime
from functools import wraps

from flask import Flask
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# =====================================================================
# CONFIGURATION  (hard-coded — Render pe env var daalne ki zaroorat nahi)
# Env var set karoge to woh override karega, warna neeche wali values chalengi.
# =====================================================================
BOT_TOKEN   = os.environ.get("BOT_TOKEN", "8790039582:AAFmeWBAFKMzSbBpipcHJNBMJb9X7y4NT_c").strip()
_owner_raw  = os.environ.get("OWNER_ID", "5206554804").strip()
OWNER_ID    = int(_owner_raw) if _owner_raw.isdigit() else None
CHANNEL_ENV = os.environ.get("CHANNEL_ID", "@tech_zone_dev").strip() or None  # join-gate channel (ya /setchannel)
PORT        = int(os.environ.get("PORT", "8080"))                # Render PORT deta hai

BASE_DIR      = os.path.abspath("workspaces")   # har user ka apna folder
DATA_DIR      = os.path.abspath("data")         # approval list / settings
USERS_FILE    = os.path.join(DATA_DIR, "allowed_users.json")
SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")

CMD_TIMEOUT     = 60    # normal command ka max time (seconds)
INSTALL_TIMEOUT = 300   # pip / npm install ka max time
MAX_MSG         = 3500  # Telegram message limit se safe (4096)

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("hosting-bot")

# =====================================================================
# IN-MEMORY STATE
# =====================================================================
allowed_users: set = set()          # approved user IDs
settings: dict = {"channel_id": CHANNEL_ENV}
sessions: dict = {}                 # user_id -> {"cwd": ...}
processes: dict = {}                # bg proc id -> info
proc_counter = 0
notified: set = set()               # owner ko request bheja ja chuka? (spam guard)


# =====================================================================
# PERSISTENCE
# =====================================================================
def load_data():
    global allowed_users
    os.makedirs(DATA_DIR, exist_ok=True)
    try:
        with open(USERS_FILE, "r") as f:
            allowed_users = {int(x) for x in json.load(f)}
    except Exception:
        allowed_users = set()

    # Env se bhi pre-approved users: ALLOWED_USERS="111,222,333"
    for x in os.environ.get("ALLOWED_USERS", "").replace(";", ",").split(","):
        x = x.strip()
        if x.isdigit():
            allowed_users.add(int(x))

    try:
        with open(SETTINGS_FILE, "r") as f:
            s = json.load(f)
            if s.get("channel_id"):
                settings["channel_id"] = s["channel_id"]
    except Exception:
        pass


def save_users():
    with open(USERS_FILE, "w") as f:
        json.dump(sorted(allowed_users), f)


def save_settings():
    with open(SETTINGS_FILE, "w") as f:
        json.dump(settings, f)


# =====================================================================
# WORKSPACE HELPERS  (har user sirf apne folder me kaam kar sakta hai)
# =====================================================================
def ensure_workspace(uid: int) -> str:
    p = os.path.realpath(os.path.join(BASE_DIR, str(uid)))
    os.makedirs(p, exist_ok=True)
    return p


def bound(uid: int) -> str:
    """
    Jis folder ke andar user ka ghera hai.
    Owner  → poora workspaces/ root (sab users ki files dekh/manage kar sakta hai)
    Baaki  → apna hi workspaces/<id>/ folder (doosron ki files NAHI dekh sakta)
    """
    os.makedirs(BASE_DIR, exist_ok=True)
    return BASE_DIR if (OWNER_ID is not None and uid == OWNER_ID) else ensure_workspace(uid)


def get_cwd(uid: int) -> str:
    if uid not in sessions:
        sessions[uid] = {"cwd": bound(uid)}
    return sessions[uid]["cwd"]


def resolve_cwd(uid: int, target: str):
    """
    Naya cwd set karta hai — lekin user ke ghere (bound) ke andar hi.
    Returns:  path (OK) | False (dir nahi mila) | None (ghere ke bahar)
    """
    ws = bound(uid)
    if not target or target == "~":
        new = ws
    elif target.startswith("/"):
        new = os.path.realpath(target)
    else:
        new = os.path.realpath(os.path.join(get_cwd(uid), target))

    if new != ws and not new.startswith(ws + os.sep):
        return None                      # ghere ke bahar → deny
    if not os.path.isdir(new):
        return False                     # folder exist nahi karta
    sessions[uid]["cwd"] = new
    return new


def short_path(uid: int) -> str:
    ws = bound(uid)
    cur = get_cwd(uid)
    rel = os.path.relpath(cur, ws)
    if rel == ".":
        return "~"
    return "~/" + rel


def safe_env(uid: int) -> dict:
    """
    User commands ke liye minimal environment.
    BOT_TOKEN / OWNER_ID etc. user commands se CHHUPAYE jaate hain —
    approved user bhi apne shell me bot token nahi dekh sakta.
    """
    home = os.path.join("/tmp", f"home_{uid}")
    os.makedirs(home, exist_ok=True)
    return {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": home,
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PYTHONUNBUFFERED": "1",
        "PIP_NO_CACHE_DIR": "1",
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        # 512 MB RAM me Node heap ko cap karo taaki container OOM-kill na ho
        "NODE_OPTIONS": "--max-old-space-size=256",
        "npm_config_cache": "/tmp/.npm-cache",
    }


# =====================================================================
# CHANNEL MEMBERSHIP
# =====================================================================
def channel_url(ch):
    if ch and ch.startswith("@"):
        return "https://t.me/" + ch[1:]
    return None


async def is_member(bot, uid: int) -> bool:
    ch = settings.get("channel_id")
    if not ch:
        return True                                    # koi channel set nahi
    if OWNER_ID is not None and uid == OWNER_ID:
        return True                                    # owner hamesha allowed
    try:
        m = await bot.get_chat_member(chat_id=ch, user_id=uid)
        if m.status in ("member", "administrator", "creator"):
            return True
        if m.status == "restricted" and getattr(m, "is_member", False):
            return True
        return False
    except Exception as e:
        # Bot channel me admin nahi hai → check fail (fail-closed)
        logger.warning("Channel membership check failed (%s): %s", ch, e)
        return False


# =====================================================================
# ACCESS DECORATORS
# =====================================================================
async def send_approval_request(update: Update, context: ContextTypes.DEFAULT_TYPE, attempt: str = "bot use"):
    user = update.effective_user
    uid = user.id
    uname = f"@{user.username}" if user.username else "—"

    await update.effective_message.reply_html(
        "🚫 <b>ACCESS DENIED — Approval pending</b>\n\n"
        f"• Tumhari ID: <code>{uid}</code>\n"
        f"• Tumne try kiya: <b>{html.escape(attempt)}</b>\n\n"
        "⏳ Owner ke paas approval request chali gayi hai.\n"
        "Jaise hi owner approve karenge, tum file upload / command chala paoge.\n"
        "<i>Channel join hona bhi zaroori hai.</i>"
    )

    # Owner ko request (spam guard — ek pending request, har naye attempt pe refresh)
    if OWNER_ID is not None:
        notified.add(uid)
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Approve", callback_data=f"appr:{uid}"),
            InlineKeyboardButton("❌ Deny",   callback_data=f"deny:{uid}"),
        ]])
        try:
            await context.bot.send_message(
                OWNER_ID,
                "🔔 <b>ACCESS REQUEST — user action karna chahta hai</b>\n\n"
                f"👤 Naam: {html.escape(user.full_name)}\n"
                f"🔖 Username: {uname}\n"
                f"🆔 ID: <code>{uid}</code>\n"
                f"⚙️ Attempt: <b>{html.escape(attempt)}</b>\n"
                f"🕒 Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
                "Approve karne ke liye button dabao:",
                parse_mode=ParseMode.HTML,
                reply_markup=kb,
            )
        except Exception as e:
            logger.warning("Owner notify failed: %s", e)


async def send_channel_gate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ch = settings.get("channel_id") or ""
    row = []
    url = channel_url(ch)
    if url:
        row.append(InlineKeyboardButton("📢 Channel Join Karo", url=url))
    row.append(InlineKeyboardButton("✅ Join Liya", callback_data="recheck"))

    await update.effective_message.reply_html(
        "⚠️ <b>ACTION REQUIRED — Channel Join</b>\n\n"
        f"Pehle yeh channel join karo: <b>{html.escape(str(ch))}</b>\n"
        "Phir neeche ka button dabao.\n\n"
        "<i>(Note: Bot channel me admin hona chahiye.)</i>",
        reply_markup=InlineKeyboardMarkup([row]),
    )


def restricted(action: str = "bot use"):
    """
    Action commands ke liye gate (file upload / command run / background process).
    Pehle APPROVAL check (nahi to owner ko request jati hai), phir CHANNEL join.
    /start pe ye gate nahi lagta — wahan sirf channel gate + fake panel dikhta hai.
    """
    def deco(func):
        @wraps(func)
        async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
            user = update.effective_user
            msg = update.effective_message
            if user is None or msg is None:
                return
            uid = user.id

            if OWNER_ID is None:
                await msg.reply_html("⚠️ Owner ID set nahi hai — <code>OWNER_ID</code> daalo.")
                return

            # 1) APPROVAL CHECK  — yahi se owner ke paas request jati hai
            if uid != OWNER_ID and uid not in allowed_users:
                # attempt ka chhota label
                attempt = action
                if action == "command run" and msg.text:
                    attempt = "command: " + msg.text[:40]
                elif action == "file upload" and msg.document:
                    attempt = "file upload: " + (msg.document.file_name or "file")
                await send_approval_request(update, context, attempt=attempt)
                return

            # 2) CHANNEL JOIN CHECK
            if not await is_member(context.bot, uid):
                await send_channel_gate(update, context)
                return

            return await func(update, context, *args, **kwargs)
        return wrapper
    return deco


def owner_only(func):
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        if not update.effective_user or update.effective_user.id != OWNER_ID:
            await update.effective_message.reply_html(
                "<pre>⛔ Owner access required — yeh command sirf owner chala sakta hai.</pre>"
            )
            return
        return await func(update, context, *args, **kwargs)
    return wrapper


# =====================================================================
# SHELL EXECUTION  (async — bot block nahi hota)
# =====================================================================
async def run_shell(cmd: str, cwd: str, uid: int, timeout: int = CMD_TIMEOUT):
    proc = await asyncio.create_subprocess_shell(
        cmd,
        cwd=cwd,
        executable="/bin/bash",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,          # output order sahi rehta hai
        stdin=asyncio.subprocess.DEVNULL,
        env=safe_env(uid),
        start_new_session=True,                    # taaki poora process-group kill kar sakein
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return proc.returncode, out.decode(errors="replace")
    except asyncio.TimeoutError:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except Exception:
            pass
        await proc.wait()
        return -1, f"\n[!] Timeout — command {timeout}s baad kill ho gayi.\n    Lambi/server wali process ke liye /run use karo."


def chunk_text(text: str, limit: int = MAX_MSG):
    chunks = []
    while text:
        if len(text) <= limit:
            chunks.append(text)
            break
        split = text.rfind("\n", 0, limit)
        if split <= 0:
            split = limit
        chunks.append(text[:split])
        text = text[split:].lstrip("\n")
    return chunks


async def send_output(msg, header: str, body: str, code=None):
    text = header
    text += "\n" + (body.strip("\n") if body and body.strip() else "(no output)")
    if code not in (0, None):
        text += f"\n[!] exit code: {code}"
    for chunk in chunk_text(text):
        await msg.reply_html(f"<pre>{html.escape(chunk)}</pre>")


# =====================================================================
# FAKE KALI SERVER ENGINE  🐉  (prank specs — sab dikhawa hai)
# =====================================================================
FAKE_USER = "root"
FAKE_HOST = "kali-hacking-server"
FAKE_IP   = "104.28.12.77"


def fake_neofetch(uid: int) -> str:
    logo = [
        r"   .';;;;;;;;;;'.",
        r"  .;;;;;;;;;;;;;;'",
        r"  ;;;;;;;;;;;;;;;;;",
        r" ;;;;;;;;'  ';;;;;;;",
        r" ;;;;;;'      ';;;;;",
        r".;;;;;.        ;;;;;",
        r".;;;;;         ;;;;;",
        r".;;;;;         ;;;;;",
        r".;;;;;.       .;;;;;",
        r".;;;;;;.     .;;;;;;",
        r".;;;;;;;._.;;;;;;;;;",
        r" ';;;;;;;;;;;;;;;;;'",
        r"  ';;;;;;;;;;;;;;;'",
        r"    ';;;;;;;;;;;'",
    ]
    host = FAKE_HOST
    who = "root"
    info = [
        f"{who}@{host}",
        "-" * (len(who) + 1 + len(host)),
        "OS: Kali GNU/Linux Rolling x86_64",
        "Host: Dedicated Root Server Pro",
        "Kernel: 6.8.0-kali1-amd64",
        "Uptime: 37 days, 14 hours, 22 mins",
        "Packages: 4821 (dpkg), 1204 (snap)",
        "Shell: zsh 5.9",
        "Resolution: 7680x4320",
        "DE: Xfce 4.18",
        "WM: Xfwm4",
        "CPU: AMD Ryzen 9 7950X (32) @ 5.7GHz",
        "GPU: NVIDIA GeForce RTX 4090",
        "Memory: 318.42 GiB / 512 GiB",
        "Disk: 184 GB / 600 GB SSD (NVMe Gen5)",
        "",
        f"Public IP: {FAKE_IP}",
        "Ping: 3 ms | Location: Mumbai, IN",
    ]
    width = max(len(l) for l in logo) + 2
    out = []
    for i in range(max(len(logo), len(info))):
        l = logo[i].ljust(width) if i < len(logo) else " " * width
        r = info[i] if i < len(info) else ""
        # dragon ko light-blue jaisa look (plain text me best effort — colors chhod dete)
        out.append(l + r)
    return "\n" + "\n".join(out)


def fake_ping(cmd: str) -> str:
    # count
    count = 4
    cm = re.search(r"-c\s*(\d+)", cmd)
    if cm:
        try:
            count = min(int(cm.group(1)), 20)
        except ValueError:
            pass
    # target: saare flags/options hata ke last non-option token
    tokens = cmd.split()
    target = "google.com"
    for i, t in enumerate(tokens[1:], start=1):
        if t == "-c":
            continue
        if re.fullmatch(r"-c\d+", t) or re.fullmatch(r"-\w+", t):
            continue
        if tokens[i - 1] == "-c":
            continue
        target = t
    if target in ("8.8.8.8",):
        target = "dns.google"
    times = [round(random.uniform(1.0, 20.0), 1) for _ in range(count)]
    lines = [f"PING {target} ({FAKE_IP}) 56(84) bytes of data."]
    for i, t in enumerate(times, 1):
        lines.append(f"64 bytes from {FAKE_IP}: icmp_seq={i} ttl=64 time={t} ms")
    avg = round(sum(times) / len(times), 1)
    lines += [
        "",
        f"--- {target} ping statistics ---",
        f"{count} packets transmitted, {count} received, 0% packet loss, time {count}005ms",
        f"rtt min/avg/max/mdev = {min(times)}/{avg}/{max(times)}/1.2 ms",
    ]
    return "\n".join(lines)


def fake_uname(cmd: str) -> str:
    base = f"Linux {FAKE_HOST} 6.8.0-kali1-amd64 #1 SMP PREEMPT_DYNAMIC Kali 6.8.11-1kali1 x86_64 GNU/Linux"
    if "-a" in cmd:
        return base
    # split flags
    flags = cmd.replace("uname", "").replace("-", "").strip()
    if not flags:
        return "Linux"
    parts = []
    if "s" in flags: parts.append("Linux")
    if "n" in flags: parts.append(FAKE_HOST)
    if "r" in flags: parts.append("6.8.0-kali1-amd64")
    if "v" in flags: parts.append("#1 SMP PREEMPT_DYNAMIC Kali 6.8.11-1kali1")
    if "m" in flags or "p" in flags: parts.append("x86_64")
    if "o" in flags: parts.append("GNU/Linux")
    if "a" in flags: return base
    return " ".join(parts) if parts else base


OS_RELEASE = (
    'PRETTY_NAME="Kali GNU/Linux Rolling"\n'
    'NAME="Kali GNU/Linux"\n'
    'VERSION="2024.3"\n'
    'VERSION_ID="2024.3"\n'
    'ID=kali\n'
    'ID_LIKE=debian\n'
    'HOME_URL="https://www.kali.org/"\n'
    'SUPPORT_URL="https://forums.kali.org/"\n'
    'BUG_REPORT_URL="https://bugs.kali.org/"\n'
    'ANSI_COLOR="36;1"\n'
    'LOGO=kali-linux'
)


def fake_top() -> str:
    load = round(random.uniform(0.3, 1.2), 2)
    return (
        f"top - 13:37:00 up 37 days, 14:22,  1 user,  load average: {load}, {round(load+0.2,2)}, {round(load+0.4,2)}\n"
        "Tasks: 214 total,   1 running, 213 sleeping,   0 stopped,   0 zombie\n"
        "%Cpu(s):  2.1 us,  0.8 sy,  0.0 ni, 96.9 id,  0.2 wa,  0.0 hi,  0.0 si,  0.0 st\n"
        "MiB Mem :  524288.0 total,  515062.4 free,    8421.6 used,     804.0 buff/cache\n"
        "MiB Swap:       0.0 total,       0.0 free,       0.0 used.  520100.0 avail Mem\n\n"
        "  PID USER      PR  NI    VIRT    RES    SHR S  %CPU  %MEM     TIME+ COMMAND\n"
        " 1024 root      20   0 1843200 120.4m  48.2m S   1.3   0.0   12:04.22 nginx\n"
        " 2048 root      20   0  968400  86.1m  31.0m S   0.7   0.0    8:51.07 python3"
    )


def fake_speedtest() -> str:
    down = round(random.uniform(900, 980), 1)
    up = round(random.uniform(850, 950), 1)
    p = round(random.uniform(1, 12), 1)
    return (
        "   Speedtest by Ookla\n"
        "      Server: Mumbai Datacenter (id=12345)\n"
        "         ISP: Root Hosting Ltd.\n"
        f"    Latency:     {p} ms   (0.2 ms jitter)\n"
        f"   Download:   {down} Mbps  (data used: 428.6 MB)\n"
        f"     Upload:   {up} Mbps  (data used: 198.2 MB)\n"
        "Packet Loss:     0.0%\n"
        " Result URL: https://www.speedtest.net/result/c/fake-kali-1337"
    )


def fake_ip() -> str:
    return (
        "1: lo: <LOOPBACK,UP,LOWER_UP> mtu 65536 qdisc noqueue state UNKNOWN\n"
        "    link/loopback 00:00:00:00:00:00 brd 00:00:00:00:00:00\n"
        "    inet 127.0.0.1/8 scope host lo\n"
        "2: eth0: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500 qdisc mq state UP\n"
        f"    link/ether 00:16:3e:{random.randint(0,255):02x}:{random.randint(0,255):02x}:{random.randint(0,255):02x}\n"
        f"    inet {FAKE_IP}/24 brd 104.28.12.255 scope global eth0\n"
        "3: tun0: <POINTOPOINT,MULTICAST,NOARP,UP,LOWER_UP> mtu 1500 (VPN tunnel)"
    )


def fake_lscpu() -> str:
    return (
        "Architecture:            x86_64\n"
        "  CPU op-mode(s):        32-bit, 64-bit\n"
        "  Address sizes:         48 bits physical, 48 bits virtual\n"
        "  CPU(s):                32\n"
        "  Vendor ID:              AuthenticAMD\n"
        "  Model name:             AMD Ryzen 9 7950X 16-Core Processor\n"
        "    CPU family:          25\n"
        "    CPU MHz:             5701.000\n"
        "    CPU max MHz:         5700.0000\n"
        "    L1d cache:            512 KiB (32 instances)\n"
        "    L2 cache:             16 MiB (16 instances)\n"
        "    L3 cache:             64 MiB (2 instances)\n"
        "    NUMA node(s):         1"
    )


def fake_env() -> str:
    return (
        f"USER=root\nLOGNAME=root\nHOME=/root\nSHELL=/bin/zsh\nTERM=xterm-256color\n"
        f"PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin\n"
        f"HOSTNAME={FAKE_HOST}\nLANG=C.UTF-8\n"
        f"SSH_CONNECTION={FAKE_IP} 51234 10.0.0.1 22\n"
        "SHELL_SESSION_ID=kali-root-1337"
    )


def fake_cmd_output(cmd: str, uid: int):
    """Agar command fake-spec list me hai to (header, output) return; warna None."""
    c = cmd.strip()
    cl = c.lower()

    # pipes / redirects / chains ko fake engine me nahi lete (real shell)
    if any(sym in c for sym in ("|", ">", ">>", "&&", ";", "||", "$(", "`")):
        # ping ko chain me bhi fake nahi karte
        pass

    first = cl.split()[0] if cl.split() else ""
    full = cl

    # neofetch / screenfetch
    if first in ("neofetch", "screenfetch", "fastfetch", "fetch"):
        return fake_neofetch(uid)

    # ping (fake fast latency)
    if first == "ping":
        return fake_ping(c)

    # free
    if first == "free":
        if "-h" in c or "--human" in c:
            return ("               total        used        free      shared  buff/cache   available\n"
                    "Mem:           512Gi       8.2Gi       491Gi       0.4Gi        12Gi       503Gi\n"
                    "Swap:          8.0Gi          0B       8.0Gi")
        return ("               total        used        free      shared  buff/cache   available\n"
                "Mem:      536870912     8619490   515062410      419430    12687768   527426410\n"
                "Swap:      8388608           0     8388608")

    # df
    if first == "df":
        if "-h" in c:
            return ("Filesystem      Size  Used Avail Use% Mounted on\n"
                    "/dev/nvme0n1p2  600G  184G  416G  31% /\n"
                    "tmpfs           256G  1.2M  256G   1% /dev/shm\n"
                    "/dev/nvme1n1p1  2.0T   96G  1.9T   5% /data")
        return ("/dev/nvme0n1p2 629145600 192937984 436207616  31% /\n"
                "tmpfs            268435456     1228 268434228   1% /dev/shm")

    # uname
    if first == "uname":
        return fake_uname(c)

    # uptime
    if first == "uptime":
        return f" 13:37:00 up 37 days, 14:22,  1 user,  load average: 0.68, 0.74, 0.81"

    # hostname
    if first == "hostname":
        if "ctl" in c:
            return ("   Static hostname: " + FAKE_HOST + "\n"
                    "         Icon name: computer-server\n"
                    "           Chassis: server\n"
                    "        Machine ID: 3f9a2c8b1d7e4a0f9c2e5b8d1f4a7c3e\n"
                    "           Boot ID: 8a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d\n"
                    "    Virtualization: kvm\n"
                    "  Operating System: Kali GNU/Linux Rolling\n"
                    "            Kernel: Linux 6.8.0-kali1-amd64\n"
                    "      Architecture: x86-64\n"
                    "   Hardware Vendor: Root Hosting Pro\n"
                    "    Hardware Model: Dedicated RX-9900")
        return FAKE_HOST

    # whoami / id
    if first == "whoami":
        return FAKE_USER
    if first == "id":
        return "uid=0(root) gid=0(root) groups=0(root),20(dialout),27(sudo),100(users)"

    # nproc
    if first == "nproc":
        return "32"

    # lscpu
    if first == "lscpu":
        return fake_lscpu()

    # os-release / kali-release / lsb_release
    if "os-release" in c or "kali-release" in c or first == "lsb_release":
        if first == "lsb_release":
            return "Distributor ID: Kali\nDescription:    Kali GNU/Linux Rolling\nRelease:        2024.3\nCodename:       kali-rolling"
        return OS_RELEASE

    # speedtest
    if "speedtest" in c:
        return fake_speedtest()

    # ifconfig / ip
    if first == "ifconfig" or (first == "ip" and ("addr" in c or "a" in c.split()[1:2] or c.strip() == "ip a")):
        return fake_ip()

    # top / htop (static one-shot)
    if first in ("top", "htop", "btop"):
        return fake_top() + "\n\n[i] (static snapshot — interactive mode Telegram me supported nahi)"

    # env / printenv
    if first in ("env", "printenv"):
        if c.strip() in ("env", "printenv"):
            return fake_env()

    return None


# =====================================================================
# TERMINAL  (hacker-style prompt)
# =====================================================================
@restricted("command run")
async def terminal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    cmd = update.effective_message.text.strip()
    if not cmd:
        return

    cwd = get_cwd(uid)
    prompt = f"root@{FAKE_HOST}:{short_path(uid)}#"

    # --- `cd` ko Python me handle karte hain (har command naya shell hota hai) ---
    if cmd == "cd" or re.match(r"^cd(\s|$)", cmd):
        try:
            parts = shlex.split(cmd)
        except ValueError:
            parts = cmd.split()
        target = parts[1] if len(parts) > 1 else "~"

        if target == "-":
            await update.effective_message.reply_html("<pre>[!] 'cd -' supported nahi hai.</pre>")
            return

        res = resolve_cwd(uid, target)
        if res is None:
            await update.effective_message.reply_html(
                "<pre>[!] DENIED — apne folder ke bahar jaana allowed nahi.</pre>"
            )
        elif res is False:
            await update.effective_message.reply_html(
                f"<pre>[!] Directory not found: {html.escape(target)}</pre>"
            )
        else:
            await update.effective_message.reply_html(f"<pre>{prompt} </pre>")
        return

    # --- FAKE KALI SPECS interception (neofetch/ping/free/df etc.) ---
    try:
        fake = fake_cmd_output(cmd, uid)
    except Exception:
        fake = None
    if fake is not None:
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
        await asyncio.sleep(0.3)
        await send_output(update.effective_message, f"{prompt} {cmd}", fake, code=0)
        return

    # --- REAL command ---
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    code, out = await run_shell(cmd, cwd, uid)
    await send_output(update.effective_message, f"{prompt} {cmd}", out, code)


# =====================================================================
# FILE UPLOAD + SMART AUTO DEPENDENCY INSTALLER
# =====================================================================
@restricted("file upload")
async def upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    doc = update.effective_message.document
    if not doc:
        return

    cwd = get_cwd(uid)
    ws = bound(uid)

    # filename se path-traversal rok do (../../etc/passwd type attacks)
    fname = os.path.basename(doc.file_name or "uploaded_file")
    dest = os.path.realpath(os.path.join(cwd, fname))
    if dest != ws and not dest.startswith(ws + os.sep):
        await update.effective_message.reply_html("<pre>[!] Invalid file path.</pre>")
        return

    status = await update.effective_message.reply_html(
        f"<pre>[⇩] File receive ho rahi hai: {html.escape(fname)} ...</pre>"
    )

    try:
        tg_file = await context.bot.get_file(doc.file_id)
        await tg_file.download_to_drive(dest)
    except Exception as e:
        await status.edit_html(
            f"<pre>[!] Upload failed: {html.escape(str(e))}\n"
            f"[i] Telegram bot download limit ~20MB hai.</pre>"
        )
        return

    async def finish(text: str):
        chunks = chunk_text(text)
        await status.edit_html(f"<pre>{html.escape(chunks[0])}</pre>")
        for c in chunks[1:]:
            await status.reply_html(f"<pre>{html.escape(c)}</pre>")

    # ---------------- SMART INSTALLER ----------------
    try:
        if fname == "requirements.txt":
            await status.edit_html("<pre>[⚙] requirements.txt detected — pip install chalu...</pre>")
            code, out = await run_shell(
                "pip3 install --no-cache-dir -r requirements.txt", cwd, uid, timeout=INSTALL_TIMEOUT
            )
            tail = "\n".join(out.strip().splitlines()[-15:])
            ok = "[✓] Python dependencies install ho gayi." if code == 0 else "[!] pip install me ERROR — upar output dekho."
            await finish(f"[✓] File saved: {fname}\n{ok}\n[i] Ab chalao: /run python3 app.py\n\n{tail}")

        elif fname == "package.json":
            await status.edit_html("<pre>[⚙] package.json detected — npm install chalu (Render optimized)...</pre>")
            code, out = await run_shell(
                "npm install --no-audit --no-fund", cwd, uid, timeout=INSTALL_TIMEOUT
            )
            tail = "\n".join(out.strip().splitlines()[-15:])
            ok = "[✓] Node.js modules install ho gaye." if code == 0 else "[!] npm install me ERROR — upar output dekho."
            hint = "[i] Note: Render Python image me Node nahi hota — npm ke liye Dockerfile wala deploy dekho (README)."
            await finish(f"[✓] File saved: {fname}\n{ok}\n{hint}\n\n{tail}")

        elif fname.endswith(".zip"):
            await status.edit_html(f"<pre>[⚙] ZIP file detected — extract ho rahi hai: {html.escape(fname)}</pre>")
            code, out = await run_shell(
                f"python3 -m zipfile -e {shlex.quote(fname)} .", cwd, uid, timeout=120
            )
            ok = "[✓] ZIP extract ho gayi." if code == 0 else "[!] ZIP extract fail hui."
            await finish(f"[✓] File saved: {fname}\n{ok}\n\n{out[-800:]}")

        else:
            await status.edit_html(
                f"<pre>[✓] File saved: {html.escape(fname)}\n"
                f"[i] Location: {short_path(uid)}\n"
                f"[i] Koi auto-dependency nahi mili.\n"
                f"[i] Tip: requirements.txt / package.json / .zip bhejo to auto-setup hoga.</pre>"
            )
    except Exception as e:
        await status.edit_html(f"<pre>[!] Installer error: {html.escape(str(e))}</pre>")


# =====================================================================
# BACKGROUND PROCESS MANAGER  (/run, /ps, /logs, /kill)
# =====================================================================
async def watch_proc(pid_key: int, proc, uid: int, context: ContextTypes.DEFAULT_TYPE):
    code = await proc.wait()
    info = processes.get(pid_key)
    if info:
        info["alive"] = False
        info["exit_code"] = code
    try:
        await context.bot.send_message(
            uid,
            f"<pre>[!] Background process #{pid_key} band ho gayi (exit code {code}).\n"
            f"Cmd: {html.escape(info['cmd'] if info else '?')}</pre>",
            parse_mode=ParseMode.HTML,
        )
    except Exception:
        pass


@restricted("background app run")
async def cmd_run(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global proc_counter
    uid = update.effective_user.id
    cmd = " ".join(context.args).strip()
    if not cmd:
        await update.message.reply_html(
            "<pre>Usage: /run python3 app.py\n\n"
            "Background (24x7) chalane ke liye — server/bot type programs isi se chalu karo.\n"
            "/ps list dekho | /logs &lt;id&gt; output | /kill &lt;id&gt; band karo.</pre>"
        )
        return

    cwd = get_cwd(uid)
    log_dir = os.path.join(cwd, ".bg_logs")
    os.makedirs(log_dir, exist_ok=True)

    proc_counter += 1
    key = proc_counter
    log_path = os.path.join(log_dir, f"{key}.log")
    logf = open(log_path, "ab")

    try:
        proc = await asyncio.create_subprocess_shell(
            cmd,
            cwd=cwd,
            executable="/bin/bash",
            stdout=logf,
            stderr=asyncio.subprocess.STDOUT,
            stdin=asyncio.subprocess.DEVNULL,
            env=safe_env(uid),
            start_new_session=True,
        )
    except Exception as e:
        logf.close()
        await update.message.reply_html(f"<pre>[!] Process start fail: {html.escape(str(e))}</pre>")
        return

    processes[key] = {
        "pid": proc.pid,
        "cmd": cmd,
        "uid": uid,
        "log": log_path,
        "started": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "alive": True,
    }

    asyncio.create_task(watch_proc(key, proc, uid, context))

    await update.message.reply_html(
        f"<pre>[+] Background process START\n"
        f"    ID : #{key}     PID: {proc.pid}\n"
        f"    Cmd: {html.escape(cmd)}\n"
        f"    Dir: {short_path(uid)}\n"
        f"[i] /logs {key}  → output dekho\n"
        f"[i] /kill {key}  → band karo\n"
        f"[i] /ps         → sab processes</pre>"
    )


@restricted("view processes")
async def cmd_ps(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    rows = []
    for k, v in sorted(processes.items()):
        if v["uid"] != uid and uid != OWNER_ID:
            continue
        # liveness double-check
        alive = v["alive"]
        if alive:
            try:
                os.kill(v["pid"], 0)
            except (ProcessLookupError, PermissionError):
                alive = False
                v["alive"] = False
        mark = "🟢 RUNNING" if alive else "🔴 STOPPED"
        rows.append(f"#{k}  {mark}  pid={v['pid']}\n    {v['cmd'][:60]}\n    started {v['started']}")
    body = "\n".join(rows) if rows else "(koi background process nahi hai — /run &lt;cmd&gt; se chalu karo)"
    await update.message.reply_html(f"<pre>{html.escape(body)}</pre>")


@restricted("view logs")
async def cmd_logs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_html("<pre>Usage: /logs &lt;id&gt; [lines]\nExample: /logs 1 50</pre>")
        return
    key = int(context.args[0])
    info = processes.get(key)
    if not info or (info["uid"] != uid and uid != OWNER_ID):
        await update.message.reply_html(f"<pre>[!] Process #{key} nahi mili.</pre>")
        return

    lines = 40
    if len(context.args) > 1 and context.args[1].isdigit():
        lines = int(context.args[1])

    try:
        with open(info["log"], "rb") as f:
            data = f.read()[-8000:]
        tail = "\n".join(data.decode(errors="replace").splitlines()[-lines:])
    except Exception as e:
        tail = f"[!] Log read fail: {e}"

    await send_output(update.message, f"logs #{key} — {info['cmd'][:50]}", tail)


@restricted("kill process")
async def cmd_kill(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_html("<pre>Usage: /kill &lt;id&gt;</pre>")
        return
    key = int(context.args[0])
    info = processes.get(key)
    if not info or (info["uid"] != uid and uid != OWNER_ID):
        await update.message.reply_html(f"<pre>[!] Process #{key} nahi mili.</pre>")
        return
    if not info["alive"]:
        await update.message.reply_html(f"<pre>[i] Process #{key} pehle se band hai.</pre>")
        return

    try:
        pgid = os.getpgid(info["pid"])
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    await asyncio.sleep(2)
    try:
        os.killpg(os.getpgid(info["pid"]), signal.SIGKILL)
    except ProcessLookupError:
        pass
    except Exception:
        pass

    info["alive"] = False
    await update.message.reply_html(
        f"<pre>[✓] Process #{key} (pid {info['pid']}) kill kar di gayi.\n    {html.escape(info['cmd'][:60])}</pre>"
    )


# =====================================================================
# OWNER COMMANDS
# =====================================================================
@owner_only
async def cmd_approve(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or not context.args[0].strip().lstrip("-").isdigit():
        await update.message.reply_html(
            "<pre>Usage: /approve &lt;user_id&gt;\n"
            "User /start karega to owner ko Approve button bhi milta hai.</pre>"
        )
        return
    target = int(context.args[0])
    allowed_users.add(target)
    save_users()
    notified.discard(target)
    await update.message.reply_html(f"<pre>[✓] User {target} APPROVED — ab bot use kar sakta hai.</pre>")
    try:
        await context.bot.send_message(
            target,
            "<pre>[✓] Owner ne approve kar diya!\nAb /start karo — channel join (agar set hai) ke baad bot chal jayega.</pre>",
        )
    except Exception:
        pass


@owner_only
async def cmd_unapprove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or not context.args[0].strip().lstrip("-").isdigit():
        await update.message.reply_html("<pre>Usage: /unapprove &lt;user_id&gt;</pre>")
        return
    target = int(context.args[0])
    allowed_users.discard(target)
    save_users()
    await update.message.reply_html(f"<pre>[✓] User {target} ka access hata diya gaya.</pre>")


@owner_only
async def cmd_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lines = [f"👑 OWNER : {OWNER_ID}", f"✅ Approved users ({len(allowed_users)}):"]
    lines += [f"   • {u}" for u in sorted(allowed_users)] or ["   (koi nahi)"]
    ch = settings.get("channel_id") or "set nahi"
    lines.append(f"\n📢 Channel: {ch}")
    await update.message.reply_html(f"<pre>{html.escape(chr(10).join(lines))}</pre>")


@owner_only
async def cmd_setchannel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    arg = " ".join(context.args).strip()
    if not arg:
        await update.message.reply_html(
            "<pre>Usage: /setchannel @yourchannel\n"
            "Ya link: /setchannel https://t.me/yourchannel\n\n"
            "Zaroori: bot ko us channel me ADMIN banao — tabhi membership check hota hai.</pre>"
        )
        return

    m = re.search(r"(?:t\.me/|telegram\.me/)([A-Za-z0-9_]+)", arg)
    if m:
        ch = "@" + m.group(1)
    elif arg.lstrip("-").isdigit():
        ch = arg
    else:
        ch = arg if arg.startswith("@") else "@" + arg.lstrip("@")

    try:
        chat = await context.bot.get_chat(ch)
    except Exception as e:
        await update.message.reply_html(
            f"<pre>[!] Bot is channel ko dekh nahi pa raha: {html.escape(str(e))}\n"
            f"[!] Pehle bot ko channel me ADMIN banao, phir /setchannel karo.</pre>"
        )
        return

    settings["channel_id"] = ch
    save_settings()
    title = getattr(chat, "title", ch)
    await update.message.reply_html(
        f"<pre>[✓] Channel set ho gaya: {html.escape(title)} ({html.escape(ch)})\n"
        f"[i] Ab sirf channel join karne wale approved users bot use kar payenge.\n"
        f"[i] Bot channel me admin hona chahiye.</pre>"
    )


@owner_only
async def cmd_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ch = settings.get("channel_id")
    if ch:
        await update.message.reply_html(f"<pre>📢 Current channel: {html.escape(str(ch))}\nHataane ke liye: /setchannel off</pre>")
    else:
        await update.message.reply_html("<pre>Koi channel set nahi.\nSet karo: /setchannel @yourchannel</pre>")


# =====================================================================
# BUTTON CALLBACKS  (Approve / Deny / Recheck)
# =====================================================================
async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    data = q.data or ""
    uid = q.from_user.id

    if data.startswith(("appr:", "deny:")):
        if uid != OWNER_ID:
            await q.answer("⛔ Sirf owner approve/deny kar sakta hai.", show_alert=True)
            return
        action, target_s = data.split(":", 1)
        target = int(target_s)

        if action == "appr":
            allowed_users.add(target)
            save_users()
            notified.discard(target)
            await q.answer("✅ Approved!")
            try:
                await q.edit_message_text(
                    f"✅ User <code>{target}</code> APPROVED.",
                    parse_mode=ParseMode.HTML,
                )
                await context.bot.send_message(
                    target,
                    "✅ Aapko approve kar diya gaya hai! Ab /start karo.\n"
                    "(Agar channel set hai to join karna bhi zaroori hai.)",
                )
            except Exception:
                pass
        else:
            await q.answer("❌ Denied")
            try:
                await q.edit_message_text(
                    f"❌ User <code>{target}</code> ko deny kar diya gaya.",
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                pass

    elif data == "recheck":
        if await is_member(context.bot, uid):
            await q.answer("✅ Channel verified — server connected!", show_alert=True)
            try:
                await q.edit_message_text("✅ Channel join verified — server connected.")
            except Exception:
                pass

            # join ke baad fake panel + upload prompt (same as /start)
            if uid == OWNER_ID:
                await q.message.reply_html(
                    f"<pre>{html.escape(PANEL_APPROVED)}</pre>\n"
                    f"👑 <b>OWNER MODE — full access.</b>\n\n{HELP_USER}{HELP_OWNER}"
                )
            elif uid in allowed_users:
                await q.message.reply_html(
                    f"<pre>{html.escape(PANEL_APPROVED)}</pre>\n"
                    "✅ <b>Connected — tum approved ho.</b>\n"
                    "📁 File upload karo ya command bhejo.\n"
                    "💻 Try: <code>neofetch</code> · <code>free -h</code> · <code>ping google.com</code>"
                )
            else:
                await q.message.reply_html(
                    f"<pre>{html.escape(PANEL_APPROVED)}</pre>\n"
                    "👋 <b>Server connected!</b>\n\n"
                    "📁 Ab file upload karo ya command bhejo.\n"
                    "⚠️ Pehli file/command pe owner ki approval lagegi — request auto chali jayegi.\n\n"
                    "Try karo: <code>neofetch</code> ya file bhejo 👇"
                )
        else:
            await q.answer(
                "⚠️ Abhi tak join nahi dikh raha.\n"
                "1) Channel join karo\n2) Bot channel me admin hai?",
                show_alert=True,
            )


# =====================================================================
# /start, /help, /id
# =====================================================================
BANNER = (
    "╔════════════════════════════╗\n"
    "   ⚡ HOSTING BOT ⚡\n"
    "   Render 512MB • 24x7 ON\n"
    "╚════════════════════════════╝"
)

HELP_USER = (
    "💻 <b>Terminal:</b> koi bhi command seedha bhejo — "
    "<code>ls -la</code>, <code>python3 main.py</code>, <code>pip install flask</code> etc.\n"
    "📁 <b>Upload:</b> file bhejo — <code>requirements.txt</code>, <code>package.json</code> aur "
    "<code>.zip</code> ka setup AUTO hota hai.\n"
    "🚀 <b>Background (24x7):</b>\n"
    "   /run &lt;cmd&gt; — server/bot background me chalu\n"
    "   /ps — running processes\n"
    "   /logs &lt;id&gt; — output dekho\n"
    "   /kill &lt;id&gt; — band karo\n"
    "📂 <code>cd &lt;folder&gt;</code> — directory change (sirf apne folder ke andar)\n"
    "🖥️ /server — server specs (Kali / RAM / SSD / ping)\n"
    "🆔 /id — apni Telegram ID dekho\n"
    "❓ /help — yeh message"
)

HELP_OWNER = (
    "\n\n👑 <b>Owner commands:</b>\n"
    "/approve &lt;id&gt; — user approve karo (button bhi aata hai)\n"
    "/unapprove &lt;id&gt; — access wapas lo\n"
    "/users — approved users list\n"
    "/setchannel @channel — channel gate lagao (bot admin hona chahiye)\n"
    "/channel — current channel dekho"
)


PANEL_APPROVED = (
    "╔══════════════════════════════╗\n"
    "   ⚡ KALI ROOT SERVER ⚡\n"
    "   Online • 24x7 • Loaded\n"
    "╚══════════════════════════════╝\n"
    "OS    : Kali GNU/Linux Rolling\n"
    "RAM   : 512 GB DDR5 ECC\n"
    "DISK  : 600 GB NVMe SSD Gen5\n"
    "CPU   : Ryzen 9 7950X (32 threads)\n"
    "NET   : 10 Gbps • Ping 1–20 ms\n"
    f"IP    : " + FAKE_IP + "\n"
    "STATUS: 🟢 CONNECTED"
)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id

    if OWNER_ID is None:
        await update.message.reply_html("⚠️ Pehle owner set karo — <code>OWNER_ID</code> daalo.")
        return

    # ---- CHANNEL GATE: /start pe yahi pehla gate hai (approval popup nahi) ----
    if not await is_member(context.bot, uid):
        await send_channel_gate(update, context)
        return

    # ---- Owner ----
    if uid == OWNER_ID:
        await update.message.reply_html(
            f"<pre>{html.escape(PANEL_APPROVED)}</pre>\n"
            "👑 <b>OWNER MODE — full access.</b>\n\n"
            f"{HELP_USER}{HELP_OWNER}\n\n"
            "📂 Tum (owner) <code>workspaces/</code> ke andar sab users ki files dekh sakte ho — "
            "<code>ls</code> se har user ka folder dikhega."
        )
        return

    # ---- Channel joined. Approved? ----
    if uid in allowed_users:
        await update.message.reply_html(
            f"<pre>{html.escape(PANEL_APPROVED)}</pre>\n"
            "✅ <b>Connected — tum approved ho.</b>\n\n"
            "📁 <b>File upload karo</b> (requirements.txt / package.json / .zip auto-setup hoga)\n"
            "💻 Ya koi command bhejo: <code>neofetch</code>, <code>free -h</code>, <code>ping google.com</code>, <code>ls</code>\n"
            "🚀 Background app: <code>/run python3 app.py</code>\n\n"
            f"{HELP_USER}"
        )
        return

    # ---- Joined, but abhi tak approved nahi ----
    await update.message.reply_html(
        f"<pre>{html.escape(PANEL_APPROVED)}</pre>\n"
        "👋 <b>Server connected!</b>\n\n"
        "📁 Ab tum <b>file upload</b> kar sakte ho ya command bhej sakte ho.\n"
        "⚠️ Lekin pehli file upload / command pe <b>owner ki approval</b> lagegi —\n"
        "jaise hi tum kuch karoge, owner ke paas request chali jayegi.\n\n"
        "Try karo: <code>neofetch</code> ya koi file bhejo 👇"
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    owner_extra = HELP_OWNER if uid == OWNER_ID else ""
    await update.message.reply_html(f"{HELP_USER}{owner_extra}")


async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    await update.message.reply_html(
        f"👤 <b>Your ID:</b> <code>{u.id}</code>\n"
        f"💬 <b>Chat ID:</b> <code>{update.effective_chat.id}</code>\n"
        f"🔖 <b>Username:</b> {('@' + u.username) if u.username else '—'}"
    )


async def cmd_server(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Fake server specs — sirf channel join chahiye (approval nahi)."""
    uid = update.effective_user.id
    if not await is_member(context.bot, uid):
        await send_channel_gate(update, context)
        return
    await update.message.reply_html(f"<pre>{html.escape(fake_neofetch(uid))}</pre>")


# =====================================================================
# KEEP-ALIVE WEB SERVER  (UptimeRobot is URL ko ping karega)
# =====================================================================
flask_app = Flask(__name__)


@flask_app.route("/")
def home():
    return "🤖 Hosting Bot is ONLINE — 24x7 active. (UptimeRobot yahi URL ping kare.)"


@flask_app.route("/health")
def health():
    return {"status": "ok", "time": datetime.now().isoformat(), "users": len(allowed_users)}


def run_flask():
    try:
        flask_app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)
    except Exception as e:
        logger.error("Flask/keep-alive server error: %s", e)


# =====================================================================
# MAIN
# =====================================================================
def main():
    if not BOT_TOKEN:
        raise SystemExit("❌ BOT_TOKEN missing hai — Render → Environment Variables me set karo.")

    load_data()
    os.makedirs(BASE_DIR, exist_ok=True)

    # Keep-alive server background thread me
    threading.Thread(target=run_flask, daemon=True).start()
    logger.info("Keep-alive web server port %s par chalu", PORT)

    app = Application.builder().token(BOT_TOKEN).build()

    # General
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(CommandHandler(["server", "specs", "neofetch"], cmd_server))

    # Owner
    app.add_handler(CommandHandler("approve", cmd_approve))
    app.add_handler(CommandHandler("unapprove", cmd_unapprove))
    app.add_handler(CommandHandler("users", cmd_users))
    app.add_handler(CommandHandler("setchannel", cmd_setchannel))
    app.add_handler(CommandHandler("channel", cmd_channel))

    # Background processes
    app.add_handler(CommandHandler("run", cmd_run))
    app.add_handler(CommandHandler("ps", cmd_ps))
    app.add_handler(CommandHandler("logs", cmd_logs))
    app.add_handler(CommandHandler("kill", cmd_kill))

    # Buttons
    app.add_handler(CallbackQueryHandler(on_callback))

    # Files & terminal (private chat only)
    app.add_handler(MessageHandler(filters.Document.ALL & filters.ChatType.PRIVATE, upload))
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, terminal
    ))

    logger.info("✅ Bot is running...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
