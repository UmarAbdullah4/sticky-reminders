"""
Sticky Reminders  -  v2.0
=========================
A desktop sticky-notes + reminder app with Mattermost integration.

What changed in v2:
  * Reminder engine rewritten (absolute schedule, no minute-modulo bug)
  * Mute / Do Not Disturb + Snooze + Quiet Hours
  * System tray icon (close to tray, quit properly)
  * Single instance lock  ->  never two copies running
  * Priorities, due dates, tags, search, sort, archive
  * Menu bar, keyboard shortcuts, status bar, export
  * Atomic saves with backup, rotating log file

Requirements:  python -m pip install websockets pystray pillow
Config:        config.json (same folder)
Data:          ~/.sticky_reminders.json
Log:           ~/.sticky_reminders.log
"""

import asyncio
import json
import logging
import os
import shutil
import socket
import ssl
import subprocess
import sys
import threading
import winsound
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler
from queue import Queue, Empty
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

APP_NAME = "Sticky Reminders"
APP_VERSION = "2.0"

# ─── Paths ───
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(SCRIPT_DIR, "config.json")
HOME = os.path.expanduser("~")
DATA_FILE = os.path.join(HOME, ".sticky_reminders.json")
LOG_FILE = os.path.join(HOME, ".sticky_reminders.log")
STARTUP_DIR = os.path.join(HOME, "AppData", "Roaming", "Microsoft", "Windows",
                           "Start Menu", "Programs", "Startup")
SHORTCUT_PATH = os.path.join(STARTUP_DIR, "StickyReminders.lnk")

# The real interpreter, NOT the WindowsApps alias (the alias spawns a second
# process that lingers - that is why two pythonw.exe used to show up).
PYTHONW = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")

SINGLE_INSTANCE_PORT = 49731

# ─── Logging ───
log = logging.getLogger("sticky")
log.setLevel(logging.INFO)
try:
    _h = RotatingFileHandler(LOG_FILE, maxBytes=512 * 1024, backupCount=2, encoding="utf-8")
    _h.setFormatter(logging.Formatter("%(asctime)s  %(levelname)-7s  %(message)s"))
    log.addHandler(_h)
except Exception:
    pass


# ─── Theme ───
class T:
    bg = "#16181D"
    panel = "#1E2128"
    panel2 = "#272B34"
    line = "#333844"
    fg = "#E8EAF0"
    fg_dim = "#9AA1B0"
    fg_faint = "#666D7C"
    accent = "#FFC94D"
    accent_dk = "#E0A800"
    ok = "#4CC38A"
    warn = "#F5A524"
    err = "#F05252"
    info = "#6AA9F4"


COLORS = [
    {"name": "Yellow", "bg": "#FFF6C2", "header": "#F2D024", "text": "#1A1A1A"},
    {"name": "Green",  "bg": "#D8F5DE", "header": "#4CC38A", "text": "#1A1A1A"},
    {"name": "Pink",   "bg": "#FFDCE9", "header": "#F5679E", "text": "#1A1A1A"},
    {"name": "Purple", "bg": "#E9DDFF", "header": "#A97BEA", "text": "#1A1A1A"},
    {"name": "Blue",   "bg": "#D9EAFF", "header": "#5B9DF5", "text": "#1A1A1A"},
    {"name": "Orange", "bg": "#FFE5C2", "header": "#F5A524", "text": "#1A1A1A"},
]
DONE_COLOR = {"bg": "#2C3038", "header": "#4A505C", "text": "#7E8695"}
MM_COLOR = 3

# (label, colour, short badge)
PRIORITIES = [
    ("Low",    "#5B9DF5", "↓"),
    ("Normal", "#7E8695", ""),
    ("High",   "#F5A524", "↑"),
    ("Urgent", "#F05252", "!"),
]

SNOOZE_CHOICES = [("5 minutes", 5), ("15 minutes", 15), ("30 minutes", 30),
                  ("1 hour", 60), ("3 hours", 180)]
MUTE_CHOICES = [("30 minutes", 30), ("1 hour", 60), ("2 hours", 120),
                ("4 hours", 240), ("8 hours", 480), ("Until tomorrow", -1),
                ("Until I turn it back on", -2)]

DEFAULT_CONFIG = {
    "mattermost_url": "",
    "username": "",
    "password": "",
    "access_token": "",
    "mattermost_enabled": True,
    "trigger_word": "@reminder",
    "reminder_interval_minutes": 60,
    "reminders_enabled": True,
    "sound_enabled": True,
    "popup_style": "toast",          # "toast" | "window"
    "focus_steal": False,            # pull the main window forward on new MM note
    "muted_until": "",               # "" | ISO datetime | "forever"
    "quiet_hours_enabled": False,
    "quiet_start": "22:00",
    "quiet_end": "08:00",
    "start_minimized": True,
    "close_to_tray": True,
    "due_reminders": True,
    "window_geometry": "",
}


# ═══════════════════════════════════════════════════════
#  Persistence
# ═══════════════════════════════════════════════════════

def load_config():
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            cfg.update(json.load(f))
    except FileNotFoundError:
        pass
    except Exception as e:
        log.error("Config load failed: %s", e)
    return cfg


def save_config(cfg):
    try:
        _atomic_write(CONFIG_FILE, json.dumps(cfg, indent=4))
    except Exception as e:
        log.error("Config save failed: %s", e)


def _atomic_write(path, text):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    if os.path.exists(path):
        try:
            shutil.copy2(path, path + ".bak")
        except Exception:
            pass
    os.replace(tmp, path)


def _normalise(n, fallback_id):
    """Fill in fields added in v2 so old notes keep working."""
    n.setdefault("id", fallback_id)
    n.setdefault("text", "")
    n.setdefault("color", 0)
    n.setdefault("source", "local")
    n.setdefault("done", False)
    n.setdefault("archived", False)
    n.setdefault("priority", 1)
    n.setdefault("due", "")
    n.setdefault("tags", [])
    n.setdefault("fmt", [])
    n.setdefault("snooze_until", "")
    n.setdefault("due_notified", False)
    n.setdefault("created_at", datetime.now().isoformat(timespec="seconds"))
    n.setdefault("done_at", "")
    n.setdefault("pinned", False)
    return n


def load_notes():
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                notes = json.load(f)
            return [_normalise(n, i + 1) for i, n in enumerate(notes)]
        except Exception as e:
            log.error("Notes load failed, trying backup: %s", e)
            try:
                with open(DATA_FILE + ".bak", "r", encoding="utf-8") as f:
                    notes = json.load(f)
                return [_normalise(n, i + 1) for i, n in enumerate(notes)]
            except Exception:
                log.error("Backup unusable, starting empty")
    return []


def save_notes(notes):
    try:
        _atomic_write(DATA_FILE, json.dumps(notes, indent=2))
    except Exception as e:
        log.error("Notes save failed: %s", e)


# ═══════════════════════════════════════════════════════
#  Helpers
# ═══════════════════════════════════════════════════════

def parse_dt(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return None


def parse_hhmm(s, default=(0, 0)):
    try:
        h, m = s.split(":")
        return int(h), int(m)
    except Exception:
        return default


def humanise_delta(td):
    total = int(td.total_seconds())
    if total < 0:
        return "overdue"
    if total < 60:
        return f"{total}s"
    m, s = divmod(total, 60)
    if m < 60:
        return f"{m:02d}:{s:02d}"
    h, m = divmod(m, 60)
    if h < 24:
        return f"{h}h {m:02d}m"
    d, h = divmod(h, 24)
    return f"{d}d {h}h"


def dark_titlebar(win):
    """Paint the native title bar dark so it matches the app."""
    try:
        import ctypes
        win.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(win.winfo_id())
        val = ctypes.c_int(1)
        for attr in (20, 19):  # DWMWA_USE_IMMERSIVE_DARK_MODE, older build id
            if ctypes.windll.dwmapi.DwmSetWindowAttribute(
                    hwnd, attr, ctypes.byref(val), ctypes.sizeof(val)) == 0:
                break
    except Exception:
        pass


def force_foreground(win):
    """Raise `win` to the *front*, not merely un-minimise it.

    Windows refuses SetForegroundWindow to a process that does not own the
    current foreground window, so deiconify()+lift()+focus_force() restores the
    window behind whatever the user is looking at - which reads as "clicking the
    icon did nothing". Attaching to the foreground thread's input queue lifts
    that restriction; the brief -topmost flick is the fallback when it does not.
    """
    try:
        win.deiconify()
        win.state("normal")
        win.lift()
    except Exception:
        return
    try:
        import ctypes
        u = ctypes.windll.user32
        win.update_idletasks()
        hwnd = u.GetParent(win.winfo_id()) or win.winfo_id()
        u.ShowWindow(hwnd, 9)  # SW_RESTORE
        fg = u.GetForegroundWindow()
        cur = u.GetWindowThreadProcessId(fg, None)
        own = ctypes.windll.kernel32.GetCurrentThreadId()
        attached = cur and cur != own and u.AttachThreadInput(cur, own, True)
        try:
            u.BringWindowToTop(hwnd)
            u.SetForegroundWindow(hwnd)
        finally:
            if attached:
                u.AttachThreadInput(cur, own, False)
    except Exception:
        pass
    # Fallback for the cases Windows still blocks: a momentary topmost flick
    # pulls the window out from behind other apps without pinning it there.
    try:
        win.attributes("-topmost", True)
        win.after(250, lambda: win.attributes("-topmost", False))
        win.focus_force()
    except Exception:
        pass


def run_hidden(args):
    """Run a command with no console flash."""
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    return subprocess.run(args, startupinfo=si, capture_output=True, text=True,
                          creationflags=0x08000000)


def startup_enabled():
    return os.path.exists(SHORTCUT_PATH)


def set_startup(enable):
    """Create/remove the login shortcut. Always targets the REAL interpreter so
    only one process is created."""
    try:
        if not enable:
            if os.path.exists(SHORTCUT_PATH):
                os.remove(SHORTCUT_PATH)
            return True, "Startup entry removed."
        script = os.path.join(SCRIPT_DIR, os.path.basename(__file__))
        ps = (
            "$s=(New-Object -COM WScript.Shell).CreateShortcut('{lnk}');"
            "$s.TargetPath='{exe}';"
            "$s.Arguments='\"{script}\"';"
            "$s.WorkingDirectory='{wd}';"
            "$s.WindowStyle=7;"
            "$s.Description='Sticky Reminders';"
            "$s.Save()"
        ).format(lnk=SHORTCUT_PATH.replace("'", "''"),
                 exe=PYTHONW.replace("'", "''"),
                 script=script.replace("'", "''"),
                 wd=SCRIPT_DIR.replace("'", "''"))
        r = run_hidden(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps])
        if r.returncode != 0:
            return False, (r.stderr or "PowerShell failed").strip()[:200]
        return True, "Starts automatically at login (single process)."
    except Exception as e:
        return False, str(e)


class SingleInstance:
    """Bind a loopback port. Second launch just tells the first one to show."""

    def __init__(self, on_show):
        self.on_show = on_show
        self.sock = None

    def acquire(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.bind(("127.0.0.1", SINGLE_INSTANCE_PORT))
            s.listen(5)
        except OSError:
            s.close()
            return False
        self.sock = s
        threading.Thread(target=self._serve, daemon=True).start()
        return True

    def _serve(self):
        while True:
            try:
                conn, _ = self.sock.accept()
                conn.close()
                self.on_show()
            except Exception:
                return

    @staticmethod
    def signal_existing():
        try:
            with socket.create_connection(("127.0.0.1", SINGLE_INSTANCE_PORT), timeout=2):
                pass
            return True
        except Exception:
            return False


# ═══════════════════════════════════════════════════════
#  Rich text for the note editor
# ═══════════════════════════════════════════════════════
#
# Formatting is stored per note as `fmt`: a list of [tag, start, end] index
# triples. `text` stays plain, so search, card previews and export are
# unaffected by any styling.
#
# Font-ish tags are mutually exclusive and named  f:<base>:<mods>  where base
# is body/h1/h2/code and mods is any of "b"/"i". Only ONE may cover a range,
# because Tk resolves the `font` option by tag priority - so a toggle rewrites
# the whole combination rather than layering tags. Underline and strikethrough
# use separate Tk options, so they layer freely on top.

FONT_BASES = ("body", "h1", "h2", "code")
_BASE_SPEC = {"body": ("Segoe UI", 11, False),
              "h1":   ("Segoe UI", 17, True),
              "h2":   ("Segoe UI", 13, True),
              "code": ("Consolas", 10, False)}


def font_tag(base, bold, ital):
    return f"f:{base}:{'b' if bold else ''}{'i' if ital else ''}"


def shade(hex_colour, factor):
    """Lighten (factor > 1) or darken (factor < 1) a #rrggbb colour."""
    try:
        h = hex_colour.lstrip("#")
        r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
        r, g, b = (max(0, min(255, int(c * factor))) for c in (r, g, b))
        return f"#{r:02X}{g:02X}{b:02X}"
    except Exception:
        return hex_colour


def configure_rich_tags(txt, fg, bg="#FFFFFF"):
    code_bg = shade(bg, 0.90)
    for base in FONT_BASES:
        fam, size, always_bold = _BASE_SPEC[base]
        for bold in (False, True):
            for ital in (False, True):
                styles = []
                if bold or always_bold:
                    styles.append("bold")
                if ital:
                    styles.append("italic")
                cfg = {"font": (fam, size, " ".join(styles)) if styles else (fam, size)}
                if base == "code":
                    cfg["background"] = code_bg
                if base in ("h1", "h2"):
                    cfg["spacing1"] = 8
                    cfg["spacing3"] = 3
                txt.tag_configure(font_tag(base, bold, ital), **cfg)
    txt.tag_configure("u", underline=True)
    txt.tag_configure("s", overstrike=True)
    txt.tag_configure("hl", background="#FFE066")
    txt.tag_configure("dim", foreground=shade(fg, 2.2))


def font_state_at(txt, index):
    """(base, bold, italic) of the font tag covering `index`."""
    for t in txt.tag_names(index):
        if t.startswith("f:"):
            try:
                _, base, mods = t.split(":")
                return base, "b" in mods, "i" in mods
            except ValueError:
                pass
    return "body", False, False


def apply_font(txt, a, b, base, bold, ital):
    for t in txt.tag_names():
        if t.startswith("f:"):
            txt.tag_remove(t, a, b)
    txt.tag_add(font_tag(base, bold, ital), a, b)


def dump_fmt(txt):
    out = []
    for t in txt.tag_names():
        if t == "sel":
            continue
        r = txt.tag_ranges(t)
        for i in range(0, len(r), 2):
            out.append([t, str(r[i]), str(r[i + 1])])
    return out


def load_fmt(txt, fmt):
    for item in fmt or []:
        try:
            tag, a, b = item
            txt.tag_add(tag, a, b)
        except Exception:
            continue


class Tooltip:
    """Small hover label. Text can be changed later via set()."""

    def __init__(self, widget, text, delay=450):
        self.widget = widget
        self.text = text
        self.delay = delay
        self.tip = None
        self.job = None
        widget.bind("<Enter>", self._enter, add="+")
        widget.bind("<Leave>", self._leave, add="+")
        widget.bind("<ButtonPress>", self._leave, add="+")

    def set(self, text):
        self.text = text

    def _enter(self, _=None):
        self._cancel()
        self.job = self.widget.after(self.delay, self._show)

    def _leave(self, _=None):
        self._cancel()
        self._hide()

    def _cancel(self):
        if self.job:
            try:
                self.widget.after_cancel(self.job)
            except Exception:
                pass
            self.job = None

    def _show(self):
        if self.tip or not self.text:
            return
        try:
            x = self.widget.winfo_rootx() + self.widget.winfo_width() // 2
            y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
            t = tk.Toplevel(self.widget)
            t.overrideredirect(True)
            t.attributes("-topmost", True)
            t.configure(bg=T.line)
            tk.Label(t, text=self.text, font=("Segoe UI", 8), bg="#0E1014",
                     fg=T.fg, padx=8, pady=4).pack(padx=1, pady=1)
            t.update_idletasks()
            t.geometry(f"+{max(0, x - t.winfo_width() // 2)}+{y}")
            self.tip = t
        except Exception:
            self.tip = None

    def _hide(self):
        if self.tip:
            try:
                self.tip.destroy()
            except Exception:
                pass
            self.tip = None


# ═══════════════════════════════════════════════════════
#  Mattermost
# ═══════════════════════════════════════════════════════

class MattermostClient:
    def __init__(self, config, message_queue):
        self.url = config.get("mattermost_url", "").rstrip("/")
        self.username = config.get("username", "")
        self.password = config.get("password", "")
        self.token_cfg = config.get("access_token", "")
        self.trigger = config.get("trigger_word", "@reminder")
        self.queue = message_queue
        self.token = None
        self.user_id = None
        self.my_username = None
        self.running = False

        self.ssl_ctx = ssl.create_default_context()
        self.ssl_ctx.check_hostname = False
        self.ssl_ctx.verify_mode = ssl.CERT_NONE

    # ── auth ──
    def login(self):
        if self.token_cfg:
            self.token = self.token_cfg
            try:
                req = Request(f"{self.url}/api/v4/users/me",
                              headers={"Authorization": f"Bearer {self.token}"})
                data = json.loads(urlopen(req, context=self.ssl_ctx, timeout=15).read())
                self.user_id = data.get("id")
                self.my_username = data.get("username", "")
                self._status(f"Signed in as @{self.my_username}", True)
                return True
            except Exception as e:
                self._status(f"Token rejected: {e}", False)
                return False

        if not (self.url and self.username and self.password):
            self._status("Not configured", False)
            return False

        try:
            payload = json.dumps({"login_id": self.username,
                                  "password": self.password}).encode("utf-8")
            req = Request(f"{self.url}/api/v4/users/login", data=payload,
                          headers={"Content-Type": "application/json"})
            try:
                resp = urlopen(req, context=self.ssl_ctx, timeout=15)
            except HTTPError as e:
                body = e.read().decode("utf-8", "replace")
                try:
                    err = json.loads(body)
                    msg = err.get("message", body[:200])
                    if "too_many" in err.get("id", ""):
                        msg += " (reset login attempts in System Console)"
                except Exception:
                    msg = body[:200]
                self._status(f"Login failed: {msg}", False)
                return False

            self.token = resp.headers.get("Token")
            data = json.loads(resp.read().decode("utf-8"))
            self.user_id = data.get("id")
            self.my_username = data.get("username", "")
            log.info("Mattermost login OK as @%s", self.my_username)
            self._status(f"Signed in as @{self.my_username}", True)
            return True
        except Exception as e:
            self._status(f"Connection error: {e}", False)
            return False

    def _status(self, text, ok):
        self.queue.put({"type": "status", "text": text, "ok": ok})

    # ── posting ──
    def send_message(self, channel_id, message, root_id=None):
        try:
            payload = {"channel_id": channel_id, "message": message}
            if root_id:
                payload["root_id"] = root_id
            req = Request(f"{self.url}/api/v4/posts",
                          data=json.dumps(payload).encode("utf-8"),
                          headers={"Content-Type": "application/json",
                                   "Authorization": f"Bearer {self.token}"})
            urlopen(req, context=self.ssl_ctx, timeout=10)
            return True
        except Exception as e:
            log.error("Send failed: %s", e)
            return False

    # ── matching ──
    def _should_create_note(self, message, sender_id):
        low = message.lower()
        if self.trigger.lower() not in low:
            return False
        if sender_id == self.user_id:
            return True
        if self.my_username and f"@{self.my_username.lower()}" in low:
            return True
        return False

    def _extract(self, message):
        text = message
        i = text.lower().find(self.trigger.lower())
        if i != -1:
            text = text[:i] + text[i + len(self.trigger):]
        if self.my_username:
            at = f"@{self.my_username}"
            j = text.lower().find(at.lower())
            if j != -1:
                text = text[:j] + text[j + len(at):]
        return text.strip()

    @staticmethod
    def _priority_from(message):
        low = message.lower()
        if "!urgent" in low or "#urgent" in low:
            return 3
        if "!high" in low or "#high" in low:
            return 2
        if "!low" in low or "#low" in low:
            return 0
        return 1

    # ── websocket ──
    async def _listen(self):
        import websockets
        scheme = "wss" if self.url.startswith("https") else "ws"
        host = self.url.replace("https://", "").replace("http://", "")
        ws_url = f"{scheme}://{host}/api/v4/websocket"
        delay = 3

        while self.running:
            try:
                async with websockets.connect(ws_url, ssl=self.ssl_ctx) as ws:
                    await ws.send(json.dumps({"seq": 1,
                                              "action": "authentication_challenge",
                                              "data": {"token": self.token}}))
                    await ws.recv()
                    self._status(f"Connected as @{self.my_username}", True)
                    delay = 3

                    async for raw in ws:
                        if not self.running:
                            break
                        try:
                            ev = json.loads(raw)
                            if ev.get("event") != "posted":
                                continue
                            d = ev.get("data", {})
                            post = json.loads(d.get("post", "{}"))
                            message = post.get("message", "")
                            sender_id = post.get("user_id", "")
                            if not self._should_create_note(message, sender_id):
                                continue
                            text = self._extract(message)
                            if not text:
                                continue
                            self.queue.put({
                                "type": "reminder",
                                "text": text,
                                "priority": self._priority_from(message),
                                "sender": (d.get("sender_name", "") or "Unknown").lstrip("@"),
                                "channel": d.get("channel_display_name") or d.get("channel_name") or "Unknown",
                                "channel_id": post.get("channel_id", ""),
                                "post_id": post.get("id", ""),
                                "is_self": sender_id == self.user_id,
                                "timestamp": datetime.now().strftime("%I:%M %p"),
                            })
                        except json.JSONDecodeError:
                            pass
                        except Exception as e:
                            log.error("WS event error: %s", e)
            except Exception as e:
                if not self.running:
                    return
                self._status(f"Disconnected - retrying in {delay}s", False)
                log.warning("WS error: %s", e)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 60)

    def start(self):
        if not self.token and not self.login():
            return
        self.running = True

        def runner():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                loop.run_until_complete(self._listen())
            except Exception as e:
                log.error("WS loop died: %s", e)

        threading.Thread(target=runner, daemon=True).start()

    def stop(self):
        self.running = False


# ═══════════════════════════════════════════════════════
#  Tray icon
# ═══════════════════════════════════════════════════════

class Tray:
    def __init__(self, app):
        self.app = app
        self.icon = None
        self.available = False
        try:
            import pystray  # noqa
            from PIL import Image  # noqa
            self.available = True
        except Exception as e:
            log.warning("Tray unavailable: %s", e)

    def _image(self, muted):
        from PIL import Image, ImageDraw
        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        body = (110, 116, 130) if muted else (255, 201, 77)
        d.rounded_rectangle([6, 8, 58, 56], radius=9, fill=body)
        d.polygon([(58, 40), (58, 56), (42, 56)], fill=(22, 24, 29))
        for y in (22, 32, 42):
            d.line([(16, y), (48 if y < 42 else 38, y)], fill=(26, 26, 26), width=4)
        if muted:
            d.line([(10, 54), (54, 10)], fill=(240, 82, 82), width=7)
        return img

    def start(self):
        if not self.available:
            return
        import pystray
        from pystray import MenuItem as Item, Menu

        def snooze_items():
            return Menu(*[Item(lbl, (lambda m=mins: self.app.post(lambda: self.app.snooze_all(m))))
                          for lbl, mins in SNOOZE_CHOICES])

        def mute_items():
            return Menu(*[Item(lbl, (lambda m=mins: self.app.post(lambda: self.app.mute_for(m))))
                          for lbl, mins in MUTE_CHOICES])

        menu = Menu(
            Item("Open Sticky Reminders", lambda: self.app.post(self.app.show_window), default=True),
            Menu.SEPARATOR,
            Item(lambda i: f"Active notes: {self.app.active_count()}", None, enabled=False),
            Item(lambda i: self.app.mute_label(), None, enabled=False),
            Menu.SEPARATOR,
            Item("Remind me now", lambda: self.app.post(self.app.show_reminder_popup)),
            Item("Snooze", snooze_items()),
            Item("Mute", mute_items()),
            Item("Unmute", lambda: self.app.post(self.app.unmute),
                 visible=lambda i: self.app.is_muted()),
            Menu.SEPARATOR,
            Item("Settings", lambda: self.app.post(self.app.open_settings)),
            Item("Quit", lambda: self.app.post(self.app.quit_app)),
        )
        self.icon = pystray.Icon("sticky_reminders", self._image(False), APP_NAME, menu)
        threading.Thread(target=self.icon.run, daemon=True).start()

    def refresh(self):
        if self.icon:
            try:
                self.icon.icon = self._image(self.app.is_muted())
                self.icon.title = f"{APP_NAME} - {self.app.active_count()} active" + \
                                  (" (muted)" if self.app.is_muted() else "")
                self.icon.update_menu()
            except Exception:
                pass

    def stop(self):
        if self.icon:
            try:
                self.icon.stop()
            except Exception:
                pass


# ═══════════════════════════════════════════════════════
#  Main application
# ═══════════════════════════════════════════════════════

class StickyNotesApp:

    # ─────────────── setup ───────────────
    def __init__(self):
        self.config = load_config()
        self.notes = load_notes()
        self.next_id = max((n["id"] for n in self.notes), default=0) + 1
        self.msg_queue = Queue()
        self.ui_queue = Queue()
        self.mm_client = None
        self.reminder_popup = None
        self.toasts = []
        self._undo = None
        self._resize_job = None
        self._cols = 3
        self._quitting = False

        self.root = tk.Tk()
        self.root.title(f"{APP_NAME}")
        self.root.configure(bg=T.bg)
        self.root.geometry(self.config.get("window_geometry") or "980x680")
        self.root.minsize(560, 420)

        self.font_h1 = ("Segoe UI Semibold", 13)
        self.font_body = ("Segoe UI", 10)
        self.font_note = ("Segoe UI", 10)
        self.font_small = ("Segoe UI", 9)
        self.font_tiny = ("Segoe UI", 8)
        self.font_btn = ("Segoe UI Semibold", 9)

        self.search_var = tk.StringVar()
        self.filter_var = tk.StringVar(value="active")
        self.sort_var = tk.StringVar(value="Priority")

        self._style_ttk()
        self._build_menu()
        self._build_toolbar()
        self._build_notes_area()
        self._build_statusbar()
        self._bind_keys()

        self.next_reminder = self._compute_first_reminder()

        self.tray = Tray(self)
        self.tray.start()

        self.root.protocol("WM_DELETE_WINDOW", self.hide_window)

        self.render_notes()
        self.root.after(80, lambda: dark_titlebar(self.root))
        self._tick()
        self._poll_queues()

        if self.config.get("mattermost_enabled", True) and self.config.get("mattermost_url"):
            self.root.after(1200, self.connect_mattermost)
        else:
            self._set_mm_status("Mattermost disabled", None)

        if self.config.get("start_minimized", True) and self.tray.available:
            self.root.after(200, self.hide_window)

        log.info("Started v%s (pid %s)", APP_VERSION, os.getpid())

    def _style_ttk(self):
        s = ttk.Style()
        try:
            s.theme_use("clam")
        except Exception:
            pass
        s.configure("D.TCombobox", fieldbackground=T.panel2, background=T.panel2,
                    foreground=T.fg, arrowcolor=T.fg_dim, bordercolor=T.line,
                    lightcolor=T.line, darkcolor=T.line, selectbackground=T.panel2,
                    selectforeground=T.fg, padding=4)
        # `readonly` is its own ttk state - without this map the combobox falls
        # back to the light default and the text becomes unreadable.
        s.map("D.TCombobox",
              fieldbackground=[("readonly", T.panel2), ("disabled", T.panel)],
              background=[("readonly", T.panel2)],
              foreground=[("readonly", T.fg)],
              selectbackground=[("readonly", T.panel2)],
              selectforeground=[("readonly", T.fg)],
              arrowcolor=[("readonly", T.fg_dim)],
              bordercolor=[("readonly", T.line), ("focus", T.accent)])
        self.root.option_add("*TCombobox*Listbox.background", T.panel2)
        self.root.option_add("*TCombobox*Listbox.foreground", T.fg)
        self.root.option_add("*TCombobox*Listbox.selectBackground", T.accent)
        self.root.option_add("*TCombobox*Listbox.selectForeground", "#1A1A1A")
        self.root.option_add("*TCombobox*Listbox.font", self.font_small)
        s.configure("TNotebook", background=T.panel, borderwidth=0)
        s.configure("TNotebook.Tab", background=T.panel2, foreground=T.fg_dim,
                    padding=(14, 7), borderwidth=0, font=self.font_small)
        s.map("TNotebook.Tab", background=[("selected", T.panel)],
              foreground=[("selected", T.accent)])
        s.configure("Vertical.TScrollbar", background=T.panel2, troughcolor=T.bg,
                    bordercolor=T.bg, arrowcolor=T.fg_faint, darkcolor=T.panel2,
                    lightcolor=T.panel2)

    # ─────────────── chrome ───────────────
    def _build_menu(self):
        mb = tk.Menu(self.root, tearoff=0, bg=T.panel, fg=T.fg,
                     activebackground=T.accent, activeforeground="#1A1A1A", bd=0)

        def sub():
            return tk.Menu(mb, tearoff=0, bg=T.panel, fg=T.fg,
                           activebackground=T.accent, activeforeground="#1A1A1A", bd=0)

        m_file = sub()
        m_file.add_command(label="New note", accelerator="Ctrl+N", command=self.add_note)
        m_file.add_separator()
        m_file.add_command(label="Export to Markdown…", command=lambda: self.export("md"))
        m_file.add_command(label="Export to JSON…", command=lambda: self.export("json"))
        m_file.add_separator()
        m_file.add_command(label="Hide to tray", accelerator="Esc", command=self.hide_window)
        m_file.add_command(label="Quit", accelerator="Ctrl+Q", command=self.quit_app)
        mb.add_cascade(label="File", menu=m_file)

        m_view = sub()
        for label, val in [("Active", "active"), ("All", "all"), ("Done", "done"),
                           ("Overdue", "overdue"), ("From Mattermost", "mm"),
                           ("Archived", "archived")]:
            m_view.add_radiobutton(label=label, variable=self.filter_var, value=val,
                                   command=self.render_notes, selectcolor=T.accent)
        m_view.add_separator()
        m_view.add_command(label="Focus search", accelerator="Ctrl+F",
                           command=lambda: self.search_entry.focus_set())
        m_view.add_command(label="Refresh", accelerator="F5", command=self.render_notes)
        mb.add_cascade(label="View", menu=m_view)

        m_rem = sub()
        m_rem.add_command(label="Remind me now", accelerator="Ctrl+R",
                          command=self.show_reminder_popup)
        m_snooze = sub()
        for lbl, mins in SNOOZE_CHOICES:
            m_snooze.add_command(label=lbl, command=lambda m=mins: self.snooze_all(m))
        m_rem.add_cascade(label="Snooze all", menu=m_snooze)
        m_mute = sub()
        for lbl, mins in MUTE_CHOICES:
            m_mute.add_command(label=lbl, command=lambda m=mins: self.mute_for(m))
        m_rem.add_cascade(label="Mute", menu=m_mute)
        m_rem.add_command(label="Unmute", command=self.unmute)
        m_rem.add_separator()
        m_rem.add_command(label="Settings…", accelerator="Ctrl+,", command=self.open_settings)
        mb.add_cascade(label="Reminders", menu=m_rem)

        m_mm = sub()
        m_mm.add_command(label="Reconnect", command=self.connect_mattermost)
        m_mm.add_command(label="Disconnect", command=self.disconnect_mattermost)
        mb.add_cascade(label="Mattermost", menu=m_mm)

        m_help = sub()
        m_help.add_command(label="Open log file", command=self.open_log)
        m_help.add_command(label="About", command=self.about)
        mb.add_cascade(label="Help", menu=m_help)

        self.root.config(menu=mb)

    def _btn(self, parent, text, cmd, bg=None, fg=None, font=None, pad=(12, 5)):
        """Flat button whose pressed/hover colours are derived from its own
        fill - a fixed dark `activebackground` made accent buttons flash grey."""
        fill = bg or T.panel2
        text_col = fg or T.fg
        b = tk.Button(parent, text=text, command=cmd, cursor="hand2",
                      font=font or self.font_btn, relief="flat", bd=0,
                      bg=fill, fg=text_col,
                      activebackground=shade(fill, 0.86),
                      activeforeground=text_col,
                      highlightthickness=0,
                      padx=pad[0], pady=pad[1])
        # subtle hover lift, still derived from the button's own colour
        b.bind("<Enter>", lambda e: b.config(bg=shade(fill, 1.14)))
        b.bind("<Leave>", lambda e: b.config(bg=fill))
        return b

    @staticmethod
    def _readable_on(bg):
        """Black or white text, whichever contrasts better with `bg`."""
        try:
            h = bg.lstrip("#")
            r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
            # perceived luminance (ITU-R BT.601)
            return "#1A1A1A" if (0.299 * r + 0.587 * g + 0.114 * b) > 150 else "#FFFFFF"
        except Exception:
            return "#1A1A1A"

    def _chip_group(self, parent, var, options, command=None, colours=None):
        """Segmented selector.

        Tk's Radiobutton applies one foreground to both states, so a selected
        chip ends up as dim grey text on a saturated fill - unreadable. These
        labels restyle themselves on every change and pick the text colour
        from the fill's luminance.

        `colours` optionally maps a value to its selected fill.
        """
        chips = {}
        colours = colours or {}

        def refresh():
            cur = str(var.get())
            for val, lbl in chips.items():
                on = str(val) == cur
                fill = colours.get(val, T.accent)
                lbl.config(bg=fill if on else T.panel2,
                           fg=self._readable_on(fill) if on else T.fg_dim,
                           font=self.font_tiny if not on else ("Segoe UI Semibold", 8))

        def pick(val):
            var.set(val)
            refresh()
            if command:
                command()

        for val, text in options:
            c = tk.Label(parent, text=text, font=self.font_tiny, bg=T.panel2,
                         fg=T.fg_dim, padx=12, pady=5, cursor="hand2")
            c.pack(side="left", padx=2)
            c.bind("<Button-1>", lambda e, v=val: pick(v))
            chips[val] = c
        refresh()
        return refresh

    def _build_toolbar(self):
        bar = tk.Frame(self.root, bg=T.panel)
        bar.pack(fill="x")
        inner = tk.Frame(bar, bg=T.panel)
        inner.pack(fill="x", padx=14, pady=9)

        # Grid, not pack: the search column absorbs all slack so the action
        # buttons can never be pushed off the right edge on a narrow window.
        inner.columnconfigure(1, weight=1)

        tk.Label(inner, text="Sticky Reminders", font=self.font_h1,
                 bg=T.panel, fg=T.fg).grid(row=0, column=0, sticky="w")

        # search (column 1, elastic)
        sframe = tk.Frame(inner, bg=T.panel2)
        sframe.grid(row=0, column=1, sticky="ew", padx=16)
        sframe.columnconfigure(1, weight=1)
        tk.Label(sframe, text="\U0001F50D", bg=T.panel2, fg=T.fg_faint,
                 font=self.font_small).grid(row=0, column=0, padx=(8, 2))
        self.search_entry = tk.Entry(sframe, textvariable=self.search_var, width=8,
                                     bg=T.panel2, fg=T.fg, relief="flat", bd=0,
                                     insertbackground=T.fg, font=self.font_body)
        self.search_entry.grid(row=0, column=1, sticky="ew", ipady=5, padx=(0, 6))
        self.search_var.trace_add("write", lambda *a: self.render_notes())
        self.clear_search_btn = tk.Label(sframe, text="✕", bg=T.panel2, fg=T.fg_faint,
                                         font=self.font_tiny, cursor="hand2")
        self.clear_search_btn.grid(row=0, column=2, padx=(0, 8))
        self.clear_search_btn.bind("<Button-1>", lambda e: self.search_var.set(""))

        # actions (column 2, fixed)
        acts = tk.Frame(inner, bg=T.panel)
        acts.grid(row=0, column=2, sticky="e")

        b = self._btn(acts, "⏰", self.show_reminder_popup,
                      font=("Segoe UI", 11), pad=(9, 3))
        b.pack(side="left", padx=2)
        Tooltip(b, "Show reminder now  (Ctrl+R)")

        self.mute_btn = self._btn(acts, "\U0001F514", self.toggle_mute,
                                  font=("Segoe UI", 11), pad=(9, 3))
        self.mute_btn.pack(side="left", padx=2)
        self.mute_tip = Tooltip(self.mute_btn, "Mute reminders  (Ctrl+M)")

        g = self._btn(acts, "⚙", self.open_settings, font=("Segoe UI", 12), pad=(9, 2))
        g.pack(side="left", padx=6)
        Tooltip(g, "Settings  (Ctrl+,)")

        self.new_btn = self._btn(acts, "+  New note", self.add_note,
                                 bg=T.accent, fg="#1A1A1A", pad=(14, 6))
        self.new_btn.pack(side="left")
        Tooltip(self.new_btn, "New note  (Ctrl+N)")

        # filter row
        frow = tk.Frame(self.root, bg=T.bg)
        frow.pack(fill="x", padx=16, pady=(10, 4))

        self.chip_frame = tk.Frame(frow, bg=T.bg)
        self.chip_frame.pack(side="left")
        self.chips = {}
        for label, val in [("Active", "active"), ("All", "all"), ("Done", "done"),
                           ("Overdue", "overdue"), ("Mattermost", "mm"), ("Archived", "archived")]:
            c = tk.Label(self.chip_frame, text=label, font=self.font_small,
                         bg=T.panel, fg=T.fg_dim, padx=12, pady=5, cursor="hand2")
            c.pack(side="left", padx=(0, 6))
            c.bind("<Button-1>", lambda e, v=val: (self.filter_var.set(v), self.render_notes()))
            self.chips[val] = c

        tk.Label(frow, text="Sort", font=self.font_tiny, bg=T.bg,
                 fg=T.fg_faint).pack(side="right", padx=(0, 6))
        sort_cb = ttk.Combobox(frow, textvariable=self.sort_var, width=12, state="readonly",
                               style="D.TCombobox", font=self.font_small,
                               values=["Priority", "Due date", "Newest", "Oldest", "A – Z"])
        sort_cb.pack(side="right")
        sort_cb.bind("<<ComboboxSelected>>", lambda e: self.render_notes())

    def _build_notes_area(self):
        container = tk.Frame(self.root, bg=T.bg)
        container.pack(fill="both", expand=True)

        self.canvas = tk.Canvas(container, bg=T.bg, highlightthickness=0)
        sb = ttk.Scrollbar(container, orient="vertical", command=self.canvas.yview,
                           style="Vertical.TScrollbar")
        self.scroll_frame = tk.Frame(self.canvas, bg=T.bg)
        self._win = self.canvas.create_window((0, 0), window=self.scroll_frame, anchor="nw")

        self.scroll_frame.bind("<Configure>",
                               lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", self._on_canvas_resize)
        self.canvas.configure(yscrollcommand=sb.set)

        self.canvas.pack(side="left", fill="both", expand=True, padx=(14, 0), pady=6)
        sb.pack(side="right", fill="y", padx=(0, 4), pady=6)
        # kept as an attribute so dialogs can take the wheel and hand it back
        self._main_wheel = lambda e: self.canvas.yview_scroll(-1 * (e.delta // 120), "units")
        self.root.bind_all("<MouseWheel>", self._main_wheel)

    def _build_statusbar(self):
        bar = tk.Frame(self.root, bg=T.panel, height=28)
        bar.pack(fill="x", side="bottom")
        inner = tk.Frame(bar, bg=T.panel)
        inner.pack(fill="x", padx=14, pady=5)

        self.mm_dot = tk.Label(inner, text="●", bg=T.panel, fg=T.fg_faint,
                               font=self.font_tiny)
        self.mm_dot.pack(side="left")
        self.mm_status = tk.Label(inner, text="Mattermost: idle", bg=T.panel,
                                  fg=T.fg_faint, font=self.font_tiny)
        self.mm_status.pack(side="left", padx=6)

        self.counter_label = tk.Label(inner, text="", bg=T.panel, fg=T.fg_faint,
                                      font=self.font_tiny)
        self.counter_label.pack(side="right")

        self.countdown_label = tk.Label(inner, text="", bg=T.panel, fg=T.fg_dim,
                                        font=self.font_tiny)
        self.countdown_label.pack(side="right", padx=16)

    def _bind_keys(self):
        r = self.root
        r.bind("<Control-n>", lambda e: self.add_note())
        r.bind("<Control-f>", lambda e: self.search_entry.focus_set())
        r.bind("<Control-r>", lambda e: self.show_reminder_popup())
        r.bind("<Control-m>", lambda e: self.toggle_mute())
        r.bind("<Control-q>", lambda e: self.quit_app())
        r.bind("<Control-comma>", lambda e: self.open_settings())
        r.bind("<Control-z>", lambda e: self.undo())
        r.bind("<F5>", lambda e: self.render_notes())
        r.bind("<Escape>", lambda e: self.hide_window())

    def _on_canvas_resize(self, event):
        self.canvas.itemconfig(self._win, width=event.width)
        cols = max(1, min(5, event.width // 250))
        if cols != self._cols:
            self._cols = cols
            if self._resize_job:
                self.root.after_cancel(self._resize_job)
            self._resize_job = self.root.after(120, self.render_notes)

    # ─────────────── thread bridge ───────────────
    def post(self, fn):
        """Schedule `fn` on the Tk thread (safe from tray/network threads)."""
        self.ui_queue.put(fn)

    def _poll_queues(self):
        while True:
            try:
                fn = self.ui_queue.get_nowait()
            except Empty:
                break
            try:
                fn()
            except Exception as e:
                log.error("UI task failed: %s", e)

        while True:
            try:
                msg = self.msg_queue.get_nowait()
            except Empty:
                break
            if msg["type"] == "status":
                self._set_mm_status(msg["text"], msg.get("ok"))
            elif msg["type"] == "reminder":
                self.add_mattermost_note(msg)

        self.root.after(300, self._poll_queues)

    # ─────────────── window state ───────────────
    def show_window(self):
        force_foreground(self.root)

    def _remember_geometry(self):
        try:
            if self.root.state() == "normal":
                g = self.root.geometry()
                if g and g != self.config.get("window_geometry"):
                    self.config["window_geometry"] = g
                    save_config(self.config)
        except Exception:
            pass

    def hide_window(self):
        self._remember_geometry()
        if self.tray.available and self.config.get("close_to_tray", True):
            self.root.withdraw()
        else:
            self.root.iconify()

    def quit_app(self):
        if self._quitting:
            return
        self._quitting = True
        self._remember_geometry()
        log.info("Shutting down")
        if self.mm_client:
            self.mm_client.stop()
        self.tray.stop()
        try:
            self.root.destroy()
        except Exception:
            pass
        os._exit(0)

    def about(self):
        messagebox.showinfo(
            "About",
            f"{APP_NAME} {APP_VERSION}\n\n"
            f"Notes:  {DATA_FILE}\n"
            f"Config: {CONFIG_FILE}\n"
            f"Log:    {LOG_FILE}\n\n"
            f"Python: {sys.version.split()[0]}\n"
            f"PID:    {os.getpid()}",
            parent=self.root)

    def open_log(self):
        try:
            os.startfile(LOG_FILE)
        except Exception as e:
            messagebox.showerror("Log", str(e), parent=self.root)

    # ═══════════ Mute / Snooze / Quiet hours ═══════════

    def is_muted(self):
        mu = self.config.get("muted_until", "")
        if mu == "forever":
            return True
        dt = parse_dt(mu)
        if dt:
            if datetime.now() < dt:
                return True
            self.config["muted_until"] = ""
            save_config(self.config)
        return False

    def mute_label(self):
        mu = self.config.get("muted_until", "")
        if mu == "forever":
            return "Muted indefinitely"
        dt = parse_dt(mu)
        if dt and datetime.now() < dt:
            return f"Muted until {dt.strftime('%I:%M %p')}"
        if self.in_quiet_hours():
            return "Quiet hours"
        return "Reminders on"

    def mute_for(self, minutes):
        if minutes == -2:
            self.config["muted_until"] = "forever"
        elif minutes == -1:
            t = (datetime.now() + timedelta(days=1)).replace(hour=8, minute=0,
                                                             second=0, microsecond=0)
            self.config["muted_until"] = t.isoformat(timespec="seconds")
        else:
            t = datetime.now() + timedelta(minutes=minutes)
            self.config["muted_until"] = t.isoformat(timespec="seconds")
        save_config(self.config)
        self._close_reminder_popup()
        self._refresh_mute_ui()
        self.show_toast("Reminders muted", self.mute_label(), color=T.fg_faint, sound=False)

    def unmute(self):
        self.config["muted_until"] = ""
        save_config(self.config)
        self._refresh_mute_ui()
        self.show_toast("Reminders on", "You will be reminded again.",
                        color=T.ok, sound=False)

    def toggle_mute(self):
        if self.is_muted():
            self.unmute()
        else:
            self._mute_menu()

    def _mute_menu(self):
        m = tk.Menu(self.root, tearoff=0, bg=T.panel, fg=T.fg,
                    activebackground=T.accent, activeforeground="#1A1A1A", bd=0)
        m.add_command(label="Mute for…", state="disabled")
        m.add_separator()
        for lbl, mins in MUTE_CHOICES:
            m.add_command(label=lbl, command=lambda mm=mins: self.mute_for(mm))
        try:
            x = self.mute_btn.winfo_rootx()
            y = self.mute_btn.winfo_rooty() + self.mute_btn.winfo_height()
            m.tk_popup(x, y)
        finally:
            m.grab_release()

    def _refresh_mute_ui(self):
        muted = self.is_muted()
        self.mute_btn.config(text="\U0001F515" if muted else "\U0001F514",
                             bg=T.err if muted else T.panel2,
                             fg="#FFFFFF" if muted else T.fg)
        if getattr(self, "mute_tip", None):
            self.mute_tip.set(f"{self.mute_label()} — click to "
                              f"{'unmute' if muted else 'mute'}  (Ctrl+M)")
        self.tray.refresh()

    def in_quiet_hours(self):
        if not self.config.get("quiet_hours_enabled"):
            return False
        sh, sm = parse_hhmm(self.config.get("quiet_start", "22:00"), (22, 0))
        eh, em = parse_hhmm(self.config.get("quiet_end", "08:00"), (8, 0))
        now = datetime.now()
        cur = now.hour * 60 + now.minute
        start, end = sh * 60 + sm, eh * 60 + em
        if start == end:
            return False
        if start < end:
            return start <= cur < end
        return cur >= start or cur < end

    def can_notify(self):
        return (self.config.get("reminders_enabled", True)
                and not self.is_muted()
                and not self.in_quiet_hours())

    def snooze_all(self, minutes):
        self.next_reminder = datetime.now() + timedelta(minutes=minutes)
        self._close_reminder_popup()
        self.show_toast("Snoozed", f"Next reminder in {minutes} minutes.",
                        color=T.info, sound=False)
        self.tray.refresh()

    # ═══════════ Reminder engine ═══════════

    def _interval(self):
        try:
            return max(1, int(self.config.get("reminder_interval_minutes", 60)))
        except Exception:
            return 60

    def _compute_first_reminder(self):
        return datetime.now() + timedelta(minutes=self._interval())

    def _tick(self):
        """One heartbeat per second: schedule reminders, refresh status bar."""
        now = datetime.now()

        # periodic reminder
        if self.config.get("reminders_enabled", True) and now >= self.next_reminder:
            self.next_reminder = now + timedelta(minutes=self._interval())
            if self.can_notify() and self.active_count() > 0:
                self.show_reminder_popup()

        # due-date reminders
        if self.config.get("due_reminders", True):
            changed = False
            for n in self.notes:
                if n.get("done") or n.get("archived") or n.get("due_notified"):
                    continue
                due = parse_dt(n.get("due", ""))
                if due and now >= due:
                    n["due_notified"] = True
                    changed = True
                    if self.can_notify():
                        self.show_toast("Due now", (n.get("text") or "(empty note)")[:90],
                                        color=T.err)
            if changed:
                save_notes(self.notes)
                self.render_notes()

        # status bar
        if self.is_muted() or self.in_quiet_hours():
            self.countdown_label.config(text=self.mute_label(), fg=T.fg_faint)
        elif not self.config.get("reminders_enabled", True):
            self.countdown_label.config(text="Reminders off", fg=T.fg_faint)
        else:
            self.countdown_label.config(
                text=f"Next reminder in {humanise_delta(self.next_reminder - now)}",
                fg=T.fg_dim)

        self.root.after(1000, self._tick)

    def _beep(self):
        if self.config.get("sound_enabled", True):
            try:
                winsound.MessageBeep(winsound.MB_ICONASTERISK)
            except Exception:
                pass

    def _close_reminder_popup(self):
        if self.reminder_popup:
            try:
                self.reminder_popup.destroy()
            except Exception:
                pass
            self.reminder_popup = None

    def show_reminder_popup(self):
        """Non-modal reminder. Never grabs the pointer, never forces the main
        window open."""
        self._close_reminder_popup()
        active = self._reminder_notes()
        if not active:
            self.show_toast("Nothing pending", "All notes are done.",
                            color=T.ok, sound=False)
            return

        self._beep()

        p = tk.Toplevel(self.root)
        self.reminder_popup = p
        p.title("Reminder")
        p.configure(bg=T.panel)
        p.attributes("-topmost", True)
        p.resizable(False, True)

        w, h = 430, min(560, 200 + 74 * min(len(active), 5))
        sw, sh = p.winfo_screenwidth(), p.winfo_screenheight()
        if self.config.get("popup_style", "toast") == "toast":
            p.geometry(f"{w}x{h}+{sw - w - 24}+{sh - h - 70}")
        else:
            p.geometry(f"{w}x{h}+{(sw - w) // 2}+{(sh - h) // 2}")
        p.protocol("WM_DELETE_WINDOW", self._close_reminder_popup)
        p.bind("<Escape>", lambda e: self._close_reminder_popup())
        p.after(60, lambda: dark_titlebar(p))

        hdr = tk.Frame(p, bg=T.accent, padx=18, pady=12)
        hdr.pack(fill="x")
        tk.Label(hdr, text="\U0001F514  Reminder", font=("Segoe UI Semibold", 14),
                 bg=T.accent, fg="#1A1A1A").pack(anchor="w")
        tk.Label(hdr, text=f"{datetime.now().strftime('%I:%M %p')}  •  "
                           f"{len(active)} pending",
                 font=self.font_small, bg=T.accent, fg="#5A4600").pack(anchor="w", pady=(2, 0))

        # actions (packed first so they never get pushed off-screen)
        act = tk.Frame(p, bg=T.panel, padx=14, pady=12)
        act.pack(side="bottom", fill="x")

        self._btn(act, "Dismiss", self._close_reminder_popup,
                  bg=T.accent, fg="#1A1A1A", pad=(16, 6)).pack(side="right")

        mute_b = self._btn(act, "Mute ▾", lambda: self._popup_menu(mute_b, MUTE_CHOICES, self.mute_for))
        mute_b.pack(side="left")
        sn_b = self._btn(act, "Snooze ▾", lambda: self._popup_menu(sn_b, SNOOZE_CHOICES, self.snooze_all))
        sn_b.pack(side="left", padx=6)
        self._btn(act, "Open app", lambda: (self._close_reminder_popup(),
                                            self.show_window())).pack(side="left")

        # list
        wrap = tk.Frame(p, bg=T.panel)
        wrap.pack(fill="both", expand=True)
        cv = tk.Canvas(wrap, bg=T.panel, highlightthickness=0)
        sb = ttk.Scrollbar(wrap, orient="vertical", command=cv.yview,
                           style="Vertical.TScrollbar")
        fr = tk.Frame(cv, bg=T.panel)
        fr.bind("<Configure>", lambda e: cv.configure(scrollregion=cv.bbox("all")))
        cv.create_window((0, 0), window=fr, anchor="nw", width=w - 34)
        cv.configure(yscrollcommand=sb.set)
        cv.pack(side="left", fill="both", expand=True, padx=(14, 0), pady=10)
        sb.pack(side="right", fill="y", pady=10)

        for note in active:
            self._reminder_row(fr, note, p)

    def _reminder_row(self, parent, note, popup):
        c = COLORS[note.get("color", 0) % len(COLORS)]
        pr = PRIORITIES[min(3, max(0, note.get("priority", 1)))]
        row = tk.Frame(parent, bg=T.panel2, cursor="hand2")
        row.pack(fill="x", pady=3)

        tk.Frame(row, bg=c["header"], width=4).pack(side="left", fill="y")

        body = tk.Frame(row, bg=T.panel2, padx=12, pady=9)
        body.pack(side="left", fill="both", expand=True)

        meta = tk.Frame(body, bg=T.panel2)
        meta.pack(fill="x")
        if note.get("priority", 1) != 1:
            tk.Label(meta, text=f"{pr[2]} {pr[0]}", font=self.font_tiny,
                     bg=T.panel2, fg=pr[1]).pack(side="left", padx=(0, 8))
        if note.get("source") == "mattermost":
            tk.Label(meta, text=f"✉ @{note.get('sender', '?')}", font=self.font_tiny,
                     bg=T.panel2, fg=T.fg_faint).pack(side="left")
        due = parse_dt(note.get("due", ""))
        if due:
            overdue = due < datetime.now()
            tk.Label(meta, text=("Overdue • " if overdue else "Due ") +
                                due.strftime("%d %b %H:%M"),
                     font=self.font_tiny, bg=T.panel2,
                     fg=T.err if overdue else T.fg_faint).pack(side="right")

        tk.Label(body, text=(note.get("text", "").strip() or "(empty note)")[:180],
                 font=self.font_note, bg=T.panel2, fg=T.fg, wraplength=300,
                 justify="left", anchor="w").pack(fill="x", pady=(3, 0))

        acts = tk.Frame(body, bg=T.panel2)
        acts.pack(fill="x", pady=(6, 0))
        nid = note["id"]
        self._btn(acts, "Done", lambda n=nid: (self.mark_done(n), self.show_reminder_popup()),
                  bg=T.ok, fg="#0F2418", font=self.font_tiny, pad=(10, 3)).pack(side="left")
        self._btn(acts, "Snooze 1h",
                  lambda n=nid: (self.snooze_note(n, 60), self.show_reminder_popup()),
                  font=self.font_tiny, pad=(10, 3)).pack(side="left", padx=6)
        self._btn(acts, "Open",
                  lambda n=nid: (self._close_reminder_popup(), self.show_window(), self.open_note(n)),
                  font=self.font_tiny, pad=(10, 3)).pack(side="left")

    def _popup_menu(self, widget, choices, handler):
        m = tk.Menu(self.root, tearoff=0, bg=T.panel, fg=T.fg,
                    activebackground=T.accent, activeforeground="#1A1A1A", bd=0)
        for lbl, val in choices:
            m.add_command(label=lbl, command=lambda v=val: handler(v))
        try:
            m.tk_popup(widget.winfo_rootx(),
                       widget.winfo_rooty() + widget.winfo_height())
        finally:
            m.grab_release()

    def _reminder_notes(self):
        now = datetime.now()
        out = []
        for n in self.notes:
            if n.get("done") or n.get("archived"):
                continue
            sn = parse_dt(n.get("snooze_until", ""))
            if sn and now < sn:
                continue
            out.append(n)
        out.sort(key=lambda n: (-n.get("priority", 1),
                                parse_dt(n.get("due", "")) or datetime.max))
        return out

    def snooze_note(self, nid, minutes):
        for n in self.notes:
            if n["id"] == nid:
                n["snooze_until"] = (datetime.now() +
                                     timedelta(minutes=minutes)).isoformat(timespec="seconds")
        save_notes(self.notes)
        self.render_notes()

    # ═══════════ Toasts ═══════════

    def show_toast(self, title, body, color=None, sound=True):
        if sound:
            self._beep()
        color = color or T.accent
        t = tk.Toplevel(self.root)
        t.overrideredirect(True)
        t.attributes("-topmost", True)
        t.attributes("-alpha", 0.0)
        t.configure(bg=T.line)

        w, h = 340, 84
        sw, sh = t.winfo_screenwidth(), t.winfo_screenheight()
        slot = len(self.toasts)
        t.geometry(f"{w}x{h}+{sw - w - 24}+{sh - 80 - (h + 10) * (slot + 1)}")
        self.toasts.append(t)

        tk.Frame(t, bg=color, width=4).pack(side="left", fill="y")
        c = tk.Frame(t, bg=T.panel2, padx=14)
        c.pack(side="left", fill="both", expand=True)
        tk.Label(c, text=title, font=("Segoe UI Semibold", 9), bg=T.panel2,
                 fg=T.fg, anchor="w").pack(fill="x", pady=(12, 0))
        tk.Label(c, text=body[:120], font=self.font_small, bg=T.panel2, fg=T.fg_dim,
                 anchor="w", wraplength=290, justify="left").pack(fill="x", pady=(2, 12))

        def fade_in(a=0.0):
            if a < 0.97:
                try:
                    t.attributes("-alpha", a)
                    t.after(15, lambda: fade_in(a + 0.12))
                except Exception:
                    pass

        def close():
            try:
                if t in self.toasts:
                    self.toasts.remove(t)
                t.destroy()
            except Exception:
                pass

        for wdg in (t, c):
            wdg.bind("<Button-1>", lambda e: close())
        fade_in()
        t.after(6000, close)

    # ═══════════ Notes CRUD ═══════════

    def active_count(self):
        return sum(1 for n in self.notes if not n.get("done") and not n.get("archived"))

    def add_note(self):
        self.show_window()
        note = _normalise({"id": self.next_id, "text": "", "color": 0,
                           "source": "local"}, self.next_id)
        self.next_id += 1
        self.notes.append(note)
        save_notes(self.notes)
        self.render_notes()
        self.open_note(note["id"])

    def get_note(self, nid):
        for n in self.notes:
            if n["id"] == nid:
                return n
        return None

    def mark_done(self, nid):
        n = self.get_note(nid)
        if not n:
            return
        n["done"] = True
        n["done_at"] = datetime.now().isoformat(timespec="seconds")

        if n.get("source") == "mattermost" and n.get("channel_id") and \
                self.mm_client and self.mm_client.token:
            who = self.mm_client.my_username or "me"
            reply = (f"✅ **Done:** {n.get('text','')}" if n.get("is_self")
                     else f"✅ **Done** (completed by @{who}): {n.get('text','')}")
            threading.Thread(
                target=lambda: self.mm_client.send_message(n["channel_id"], reply,
                                                           root_id=n.get("post_id")),
                daemon=True).start()
            self.show_toast("Marked done", f"Replied in #{n.get('channel','')}",
                            color=T.ok, sound=False)
        save_notes(self.notes)
        self.render_notes()
        self.tray.refresh()

    def reopen_note(self, nid):
        n = self.get_note(nid)
        if n:
            n["done"] = False
            n["done_at"] = ""
            n["due_notified"] = False
            save_notes(self.notes)
            self.render_notes()
            self.tray.refresh()

    def archive_note(self, nid):
        n = self.get_note(nid)
        if n:
            n["archived"] = True
            save_notes(self.notes)
            self.render_notes()
            self.show_toast("Archived", "Ctrl+Z to undo.", color=T.info, sound=False)
            self._undo = ("archive", nid)
            self.tray.refresh()

    def delete_note(self, nid, confirm=True):
        n = self.get_note(nid)
        if not n:
            return
        if confirm and (n.get("text") or "").strip():
            if not messagebox.askyesno("Delete note",
                                       "Delete this note permanently?\n\n"
                                       "(Archive keeps it and can be undone.)",
                                       parent=self.root):
                return
        self._undo = ("delete", dict(n))
        self.notes = [x for x in self.notes if x["id"] != nid]
        save_notes(self.notes)
        self.render_notes()
        self.tray.refresh()

    def undo(self):
        if not self._undo:
            return
        kind, payload = self._undo
        if kind == "archive":
            n = self.get_note(payload)
            if n:
                n["archived"] = False
        elif kind == "delete":
            self.notes.append(payload)
        self._undo = None
        save_notes(self.notes)
        self.render_notes()
        self.show_toast("Undone", "Note restored.", color=T.ok, sound=False)

    def archive_all_done(self):
        cnt = 0
        for n in self.notes:
            if n.get("done") and not n.get("archived"):
                n["archived"] = True
                cnt += 1
        save_notes(self.notes)
        self.render_notes()
        self.show_toast("Cleaned up", f"{cnt} completed notes archived.",
                        color=T.info, sound=False)

    def add_mattermost_note(self, msg):
        note = _normalise({
            "id": self.next_id, "text": msg["text"], "color": MM_COLOR,
            "source": "mattermost", "priority": msg.get("priority", 1),
            "sender": msg.get("sender", "Unknown"),
            "channel": msg.get("channel", "Unknown"),
            "channel_id": msg.get("channel_id", ""),
            "post_id": msg.get("post_id", ""),
            "is_self": msg.get("is_self", False),
            "received_at": msg.get("timestamp", ""),
        }, self.next_id)
        self.next_id += 1
        self.notes.append(note)
        save_notes(self.notes)
        self.render_notes()
        self.tray.refresh()

        # Respect mute: a new note never yanks focus unless you asked for it.
        if self.config.get("focus_steal") and self.can_notify():
            self.show_window()
        if self.can_notify():
            label = "Self-reminder" if msg.get("is_self") else \
                    f"@{msg['sender']} in #{msg['channel']}"
            self.show_toast(f"✉  {label}", msg["text"], color=COLORS[MM_COLOR]["header"])

    # ═══════════ Rendering ═══════════

    def _visible_notes(self):
        f = self.filter_var.get()
        q = self.search_var.get().strip().lower()
        now = datetime.now()

        def keep(n):
            if f == "archived":
                if not n.get("archived"):
                    return False
            elif n.get("archived"):
                return False

            if f == "active" and n.get("done"):
                return False
            if f == "done" and not n.get("done"):
                return False
            if f == "mm" and n.get("source") != "mattermost":
                return False
            if f == "overdue":
                due = parse_dt(n.get("due", ""))
                if n.get("done") or not due or due >= now:
                    return False
            if q:
                hay = " ".join([n.get("text", ""), n.get("sender", ""),
                                n.get("channel", ""), " ".join(n.get("tags", []))]).lower()
                if q not in hay:
                    return False
            return True

        out = [n for n in self.notes if keep(n)]
        s = self.sort_var.get()
        if s == "Priority":
            out.sort(key=lambda n: (n.get("done", False), -n.get("priority", 1),
                                    parse_dt(n.get("due", "")) or datetime.max))
        elif s == "Due date":
            out.sort(key=lambda n: (parse_dt(n.get("due", "")) or datetime.max))
        elif s == "Newest":
            out.sort(key=lambda n: n.get("created_at", ""), reverse=True)
        elif s == "Oldest":
            out.sort(key=lambda n: n.get("created_at", ""))
        else:
            out.sort(key=lambda n: n.get("text", "").lower())
        return out

    def render_notes(self):
        for w in self.scroll_frame.winfo_children():
            w.destroy()

        for val, chip in self.chips.items():
            on = self.filter_var.get() == val
            chip.config(bg=T.accent if on else T.panel,
                        fg=self._readable_on(T.accent) if on else T.fg_dim,
                        font=("Segoe UI Semibold", 9) if on else self.font_small)

        live = [n for n in self.notes if not n.get("archived")]
        active = sum(1 for n in live if not n.get("done"))
        done = sum(1 for n in live if n.get("done"))
        overdue = sum(1 for n in live if not n.get("done")
                      and (parse_dt(n.get("due", "")) or datetime.max) < datetime.now())
        arch = sum(1 for n in self.notes if n.get("archived"))
        self.counter_label.config(
            text=f"{active} active  •  {done} done  •  {overdue} overdue  •  {arch} archived")
        self._refresh_mute_ui()

        notes = self._visible_notes()
        cols = self._cols
        for i in range(cols):
            self.scroll_frame.columnconfigure(i, weight=1, uniform="col")

        if not notes:
            empty = tk.Frame(self.scroll_frame, bg=T.bg, pady=60)
            empty.grid(row=0, column=0, columnspan=cols, sticky="nsew")
            tk.Label(empty, text="Nothing here", font=("Segoe UI", 13),
                     bg=T.bg, fg=T.fg_faint).pack()
            tk.Label(empty, text="Press Ctrl+N to create a note.",
                     font=self.font_small, bg=T.bg, fg=T.fg_faint).pack(pady=(4, 0))
            return

        r = c = 0
        for note in notes:
            self._render_card(note, r, c)
            c += 1
            if c >= cols:
                c, r = 0, r + 1

        # trailing "new note" tile
        wrap = tk.Frame(self.scroll_frame, bg=T.bg)
        wrap.grid(row=r, column=c, padx=6, pady=6, sticky="nsew")
        self.scroll_frame.rowconfigure(r, minsize=160)
        add = tk.Frame(wrap, bg=T.panel, cursor="hand2")
        add.pack(fill="both", expand=True)
        lbl = tk.Label(add, text="+\nNew note", font=("Segoe UI", 12), bg=T.panel,
                       fg=T.fg_faint, cursor="hand2")
        lbl.pack(expand=True)
        for wd in (add, lbl):
            wd.bind("<Button-1>", lambda e: self.add_note())
            wd.bind("<Enter>", lambda e: (add.config(bg=T.panel2), lbl.config(bg=T.panel2, fg=T.accent)))
            wd.bind("<Leave>", lambda e: (add.config(bg=T.panel), lbl.config(bg=T.panel, fg=T.fg_faint)))

    def _render_card(self, note, r, c):
        nid = note["id"]
        is_done = note.get("done", False)
        is_mm = note.get("source") == "mattermost"
        col = DONE_COLOR if is_done else COLORS[note.get("color", 0) % len(COLORS)]
        pr = PRIORITIES[min(3, max(0, note.get("priority", 1)))]
        due = parse_dt(note.get("due", ""))
        overdue = bool(due and not is_done and due < datetime.now())
        snoozed = bool(parse_dt(note.get("snooze_until", "")) and
                       datetime.now() < parse_dt(note.get("snooze_until", "")))

        wrap = tk.Frame(self.scroll_frame, bg=T.bg)
        wrap.grid(row=r, column=c, padx=6, pady=6, sticky="nsew")
        self.scroll_frame.rowconfigure(r, minsize=160)

        card = tk.Frame(wrap, bg=col["bg"], cursor="hand2")
        card.pack(fill="both", expand=True)

        def click(w):
            w.bind("<Button-1>", lambda e: self.open_note(nid))
            w.bind("<Button-3>", lambda e: self._card_menu(e, nid))

        click(card)

        strip = tk.Frame(card, bg=T.err if overdue else col["header"], height=5)
        strip.pack(fill="x")
        strip.pack_propagate(False)
        click(strip)

        top = tk.Frame(card, bg=col["bg"])
        top.pack(fill="x", padx=9, pady=(7, 0))
        click(top)

        badges = tk.Frame(top, bg=col["bg"])
        badges.pack(side="left")
        click(badges)
        if note.get("priority", 1) != 1:
            tk.Label(badges, text=pr[2] or pr[0][0], font=("Segoe UI Semibold", 8),
                     bg=pr[1], fg="#FFFFFF", padx=5).pack(side="left", padx=(0, 4))
        if is_mm:
            tk.Label(badges, text="✉", font=self.font_tiny,
                     bg=col["bg"], fg=col["text"]).pack(side="left", padx=(0, 4))
        if is_done:
            tk.Label(badges, text="✓", font=("Segoe UI Semibold", 9),
                     bg=col["bg"], fg=T.ok).pack(side="left", padx=(0, 4))
        if snoozed:
            tk.Label(badges, text="⏸", font=self.font_tiny,
                     bg=col["bg"], fg=col["text"]).pack(side="left")

        tk.Label(top, text="⋯", font=("Segoe UI", 10), bg=col["bg"], fg=col["text"],
                 cursor="hand2").pack(side="right")
        top.winfo_children()[-1].bind("<Button-1>", lambda e: self._card_menu(e, nid))

        preview = (note.get("text", "").strip() or "(click to add text)")
        if len(preview) > 130:
            preview = preview[:130].rstrip() + "…"
        font = ("Segoe UI", 10, "overstrike") if is_done else self.font_note
        body = tk.Label(card, text=preview, font=font, bg=col["bg"], fg=col["text"],
                        wraplength=205, justify="left", anchor="nw", padx=9, pady=6)
        body.pack(fill="both", expand=True)
        click(body)

        foot_bits = []
        if is_mm:
            if note.get("sender"):
                foot_bits.append(f"@{note['sender']}")
            if note.get("channel"):
                foot_bits.append(f"#{note['channel']}")
        if due:
            foot_bits.append(("Overdue " if overdue else "Due ") + due.strftime("%d %b %H:%M"))
        if note.get("tags"):
            foot_bits.append(" ".join("#" + t for t in note["tags"][:2]))
        if foot_bits:
            ft = tk.Label(card, text="  •  ".join(foot_bits), font=self.font_tiny,
                          bg=col["bg"], fg=T.err if overdue else "#7A6B4E",
                          anchor="w", padx=9, wraplength=205, justify="left")
            ft.pack(fill="x", pady=(0, 6))
            click(ft)

        if not is_done:
            qa = tk.Frame(card, bg=col["bg"])
            qa.pack(fill="x", padx=9, pady=(0, 8))
            tk.Label(qa, text="✓ Done", font=self.font_tiny, bg=col["header"],
                     fg="#1A1A1A", padx=8, pady=2, cursor="hand2").pack(side="left")
            qa.winfo_children()[-1].bind("<Button-1>", lambda e: self.mark_done(nid))

    def _card_menu(self, event, nid):
        n = self.get_note(nid)
        if not n:
            return
        m = tk.Menu(self.root, tearoff=0, bg=T.panel, fg=T.fg,
                    activebackground=T.accent, activeforeground="#1A1A1A", bd=0)
        m.add_command(label="Open", command=lambda: self.open_note(nid))
        if n.get("done"):
            m.add_command(label="Reopen", command=lambda: self.reopen_note(nid))
        else:
            m.add_command(label="Mark done", command=lambda: self.mark_done(nid))
            sn = tk.Menu(m, tearoff=0, bg=T.panel, fg=T.fg,
                         activebackground=T.accent, activeforeground="#1A1A1A", bd=0)
            for lbl, mins in SNOOZE_CHOICES:
                sn.add_command(label=lbl, command=lambda mm=mins: self.snooze_note(nid, mm))
            m.add_cascade(label="Snooze this note", menu=sn)
        pm = tk.Menu(m, tearoff=0, bg=T.panel, fg=T.fg,
                     activebackground=T.accent, activeforeground="#1A1A1A", bd=0)
        for i, (lbl, _, _) in enumerate(PRIORITIES):
            pm.add_command(label=lbl, command=lambda v=i: self.set_priority(nid, v))
        m.add_cascade(label="Priority", menu=pm)
        m.add_separator()
        if n.get("archived"):
            m.add_command(label="Restore", command=lambda: (n.update({"archived": False}),
                                                            save_notes(self.notes),
                                                            self.render_notes()))
        else:
            m.add_command(label="Archive", command=lambda: self.archive_note(nid))
        m.add_command(label="Delete…", command=lambda: self.delete_note(nid))
        try:
            m.tk_popup(event.x_root, event.y_root)
        finally:
            m.grab_release()

    def set_priority(self, nid, value):
        n = self.get_note(nid)
        if n:
            n["priority"] = value
            save_notes(self.notes)
            self.render_notes()

    # ═══════════ Note editor ═══════════

    def open_note(self, nid):
        note = self.get_note(nid)
        if not note:
            return
        is_mm = note.get("source") == "mattermost"
        is_done = note.get("done", False)
        # A copy, not the palette entry itself: recolouring mutates `col` in
        # place so every widget and closure below reads the new shade lazily.
        col = dict(DONE_COLOR if is_done else COLORS[note.get("color", 0) % len(COLORS)])

        p = tk.Toplevel(self.root)
        p.title("Note")
        p.configure(bg=T.panel)
        p.geometry("520x520+{}+{}".format(self.root.winfo_x() + 140,
                                          self.root.winfo_y() + 70))
        p.minsize(420, 380)
        p.bind("<Escape>", lambda e: p.destroy())
        p.after(60, lambda: dark_titlebar(p))
        # Deliberately NOT transient to the main window. Tk ties a transient
        # child's visibility to its parent: the note minimised with the list,
        # and re-attaching it while the list was hidden or in the tray withdrew
        # the note outright - it looked like unpinning had closed it. A note is
        # its own window instead; use the pin button to keep it in front.
        p.lift()
        p.focus_force()

        # header
        hdr = tk.Frame(p, bg=col["header"], padx=18, pady=12)
        hdr.pack(fill="x")
        left = tk.Frame(hdr, bg=col["header"])
        left.pack(side="left", fill="x", expand=True)
        if is_mm:
            src = ("Self-reminder" if note.get("is_self")
                   else f"From @{note.get('sender','?')} in #{note.get('channel','?')}")
            tk.Label(left, text="✉  Mattermost reminder", font=("Segoe UI Semibold", 12),
                     bg=col["header"], fg="#1A1A1A").pack(anchor="w")
            tk.Label(left, text=src, font=self.font_small, bg=col["header"],
                     fg="#33302A").pack(anchor="w", pady=(2, 0))
            if note.get("received_at"):
                tk.Label(left, text=f"Received {note['received_at']}", font=self.font_tiny,
                         bg=col["header"], fg="#4A463E").pack(anchor="w")
        else:
            tk.Label(left, text="\U0001F4DD  Note", font=("Segoe UI Semibold", 12),
                     bg=col["header"], fg="#1A1A1A").pack(anchor="w")
        # ── pin-on-top toggle ──
        pin_off = shade(col["header"], 0.88)
        pin_on = "#C62828"
        pinned = tk.BooleanVar(value=bool(note.get("pinned", False)))
        pin_btn = tk.Button(hdr, text="📌", cursor="hand2", relief="flat",
                            bd=0, font=("Segoe UI", 12), highlightthickness=0,
                            padx=8, pady=2)
        pin_btn._fill = pin_off
        pin_btn.pack(side="right", padx=(10, 0))
        pin_btn.bind("<Enter>", lambda e: pin_btn.config(bg=shade(pin_btn._fill, 1.12)))
        pin_btn.bind("<Leave>", lambda e: pin_btn.config(bg=pin_btn._fill))

        def apply_pin(persist=True):
            """Pinned = always-on-top, above every other application."""
            on = bool(pinned.get())
            try:
                p.attributes("-topmost", on)
                p.lift()
            except tk.TclError:
                return
            fill = pin_on if on else pin_off
            ink = "#FFFFFF" if on else "#1A1A1A"
            pin_btn._fill = fill
            pin_btn.config(bg=fill, fg=ink,
                           activebackground=shade(fill, 0.86), activeforeground=ink)
            pin_tip.set("Pinned on top - click to unpin  (Ctrl+T)" if on else
                        "Keep this note above every other window  (Ctrl+T)")
            if persist:
                note["pinned"] = on
                save_notes(self.notes)

        def toggle_pin(_=None):
            pinned.set(not pinned.get())
            apply_pin()
            return "break"

        pin_btn.config(command=toggle_pin)
        p.bind("<Control-t>", toggle_pin)
        pin_tip = Tooltip(pin_btn, "Keep this note above every other window  (Ctrl+T)")
        apply_pin(persist=False)

        if is_done:
            tk.Label(hdr, text="✓ Completed", font=("Segoe UI Semibold", 9),
                     bg=col["header"], fg="#0F4A2A").pack(side="right")

        # buttons
        btns = tk.Frame(p, bg=T.panel, padx=18, pady=12)
        btns.pack(side="bottom", fill="x")

        if not is_done:
            self._btn(btns, "✓  Mark done", lambda: (self.mark_done(nid), p.destroy()),
                      bg=T.ok, fg="#0F2418", pad=(16, 6)).pack(side="left")
        else:
            self._btn(btns, "↺  Reopen", lambda: (self.reopen_note(nid), p.destroy()),
                      bg=T.warn, fg="#1A1A1A", pad=(14, 6)).pack(side="left")

        self._btn(btns, "Archive", lambda: (self.archive_note(nid), p.destroy())
                  ).pack(side="left", padx=8)
        self._btn(btns, "Delete", lambda: (self.delete_note(nid), p.destroy()),
                  bg="#5A2222", fg="#FFD5D5").pack(side="left")
        self._btn(btns, "Close", p.destroy).pack(side="right")

        # attributes panel
        attr = tk.Frame(p, bg=T.panel, padx=18, pady=8)
        attr.pack(side="bottom", fill="x")

        row1 = tk.Frame(attr, bg=T.panel)
        row1.pack(fill="x", pady=(0, 6))
        tk.Label(row1, text="Priority", font=self.font_tiny, bg=T.panel,
                 fg=T.fg_faint, width=8, anchor="w").pack(side="left")
        pvar = tk.IntVar(value=note.get("priority", 1))
        self._chip_group(
            row1, pvar,
            [(i, lbl) for i, (lbl, _, _) in enumerate(PRIORITIES)],
            command=lambda: (note.update({"priority": int(pvar.get())}),
                             save_notes(self.notes),
                             self.render_notes()),
            colours={i: colr for i, (_, colr, _) in enumerate(PRIORITIES)})

        row2 = tk.Frame(attr, bg=T.panel)
        row2.pack(fill="x", pady=(0, 6))
        tk.Label(row2, text="Due", font=self.font_tiny, bg=T.panel,
                 fg=T.fg_faint, width=8, anchor="w").pack(side="left")
        due_var = tk.StringVar(value=(parse_dt(note.get("due", "")).strftime("%Y-%m-%d %H:%M")
                                      if parse_dt(note.get("due", "")) else ""))
        due_e = tk.Entry(row2, textvariable=due_var, font=self.font_small, width=17,
                         bg=T.panel2, fg=T.fg, relief="flat", bd=0, insertbackground=T.fg)
        due_e.pack(side="left", ipady=4, padx=(0, 6))

        def commit_due(*_):
            s = due_var.get().strip()
            if not s:
                note["due"] = ""
            else:
                for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d", "%d/%m/%Y %H:%M", "%d/%m/%Y"):
                    try:
                        note["due"] = datetime.strptime(s, fmt).isoformat(timespec="seconds")
                        break
                    except ValueError:
                        continue
                else:
                    messagebox.showwarning("Due date", "Use  YYYY-MM-DD HH:MM", parent=p)
                    return
            note["due_notified"] = False
            save_notes(self.notes)
            self.render_notes()

        due_e.bind("<FocusOut>", commit_due)
        due_e.bind("<Return>", commit_due)

        # Quick-set buttons get their own row - on a narrow window they would
        # otherwise push "Clear" off the right edge.
        row2b = tk.Frame(attr, bg=T.panel)
        row2b.pack(fill="x", pady=(0, 6))
        tk.Label(row2b, text="", font=self.font_tiny, bg=T.panel,
                 width=8).pack(side="left")

        def quick(label, dt):
            def go():
                due_var.set(dt().strftime("%Y-%m-%d %H:%M"))
                commit_due()
            self._btn(row2b, label, go, font=self.font_tiny,
                      pad=(7, 3)).pack(side="left", padx=2)

        quick("+1h", lambda: datetime.now() + timedelta(hours=1))
        quick("Today 5pm", lambda: datetime.now().replace(hour=17, minute=0, second=0, microsecond=0))
        quick("Tmrw 9am", lambda: (datetime.now() + timedelta(days=1)).replace(
            hour=9, minute=0, second=0, microsecond=0))
        quick("Next week", lambda: (datetime.now() + timedelta(days=7)).replace(
            hour=9, minute=0, second=0, microsecond=0))
        self._btn(row2b, "Clear", lambda: (due_var.set(""), commit_due()),
                  font=self.font_tiny, pad=(7, 3)).pack(side="left", padx=2)

        row3 = tk.Frame(attr, bg=T.panel)
        row3.pack(fill="x")
        tk.Label(row3, text="Tags", font=self.font_tiny, bg=T.panel,
                 fg=T.fg_faint, width=8, anchor="w").pack(side="left")
        tag_var = tk.StringVar(value=", ".join(note.get("tags", [])))
        tag_e = tk.Entry(row3, textvariable=tag_var, font=self.font_small, width=24,
                         bg=T.panel2, fg=T.fg, relief="flat", bd=0, insertbackground=T.fg)
        tag_e.pack(side="left", ipady=4, padx=(0, 10))

        def commit_tags(*_):
            note["tags"] = [t.strip().lstrip("#") for t in tag_var.get().split(",") if t.strip()]
            save_notes(self.notes)
            self.render_notes()

        tag_e.bind("<FocusOut>", commit_tags)
        tag_e.bind("<Return>", commit_tags)

        if not is_mm:
            tk.Label(row3, text="Colour", font=self.font_tiny, bg=T.panel,
                     fg=T.fg_faint).pack(side="left", padx=(6, 6))
            for i, cc in enumerate(COLORS):
                dot = tk.Label(row3, text="●", font=("Segoe UI", 13), bg=T.panel,
                               fg=cc["header"], cursor="hand2")
                dot.pack(side="left", padx=2)
                # `recolour` is defined further down, once the body widgets it
                # repaints exist - the name resolves when the click fires.
                dot.bind("<Button-1>", lambda e, ci=i: recolour(ci))

        # body
        wrap = tk.Frame(p, bg=col["bg"], padx=16, pady=12)
        wrap.pack(fill="both", expand=True)
        txt = tk.Text(wrap, wrap="word", font=("Segoe UI", 11), bg=col["bg"],
                      fg=col["text"], relief="flat", bd=0, padx=6, pady=6,
                      insertbackground=col["text"], undo=True,
                      selectbackground=col["header"], selectforeground="#1A1A1A")
        configure_rich_tags(txt, col["text"], col["bg"])
        txt.insert("1.0", note.get("text", ""))
        load_fmt(txt, note.get("fmt"))

        # Toolbar goes into `wrap` before the Text is packed, so it sits above it.
        fmt_bar = None
        if not is_done:
            fmt_bar = self._build_format_bar(wrap, txt, col, lambda: on_change())

        tscroll = tk.Scrollbar(wrap, orient="vertical", command=txt.yview,
                               relief="flat", bd=0, width=10,
                               bg=col["header"], troughcolor=col["bg"],
                               activebackground=col["header"],
                               highlightthickness=0)
        tscroll.pack(side="right", fill="y")
        txt.configure(yscrollcommand=tscroll.set)
        txt.pack(side="left", fill="both", expand=True)

        if is_done:
            txt.config(state="disabled")
        else:
            txt.focus_set()

        def on_change(_=None):
            note["text"] = txt.get("1.0", "end-1c")
            note["fmt"] = dump_fmt(txt)
            save_notes(self.notes)

        txt.bind("<KeyRelease>", on_change)
        p.protocol("WM_DELETE_WINDOW", lambda: (on_change(), commit_due(), commit_tags(),
                                                self.render_notes(), p.destroy()))

        def repaint():
            """Restyle the open note for the current `col`."""
            nonlocal pin_off
            hdr.config(bg=col["header"])
            left.config(bg=col["header"])
            for w in left.winfo_children():
                w.config(bg=col["header"])
            for w in hdr.winfo_children():
                if isinstance(w, tk.Label):
                    w.config(bg=col["header"])
            wrap.config(bg=col["bg"])
            txt.config(bg=col["bg"], fg=col["text"], insertbackground=col["text"],
                       selectbackground=col["header"])
            configure_rich_tags(txt, col["text"], col["bg"])
            tscroll.config(bg=col["header"], troughcolor=col["bg"],
                           activebackground=col["header"])
            if fmt_bar:
                fmt_bar.config(bg=col["bg"])
                ink = self._readable_on(col["header"])
                for w in fmt_bar.winfo_children():
                    if isinstance(w, tk.Button):
                        w.config(bg=col["header"], fg=ink,
                                 activebackground=shade(col["header"], 0.86),
                                 activeforeground=ink)
                    else:                      # the thin separator strips
                        w.config(bg=col["text"])
            pin_off = shade(col["header"], 0.88)
            apply_pin(persist=False)

        def recolour(ci):
            """Swap the note's colour without closing it - reopening threw away
            the cursor position, the scroll offset and the undo history."""
            note["color"] = ci
            save_notes(self.notes)
            self.render_notes()
            if not is_done:      # a completed note stays grey whatever is picked
                col.update(COLORS[ci])
                repaint()

    # ═══════════ Note formatting toolbar ═══════════

    def _build_format_bar(self, parent, txt, col, on_change):

        def sel():
            """Selection, else the word under the cursor, else None."""
            r = txt.tag_ranges("sel")
            if r:
                return str(r[0]), str(r[1])
            a, b = txt.index("insert wordstart"), txt.index("insert wordend")
            return (a, b) if txt.get(a, b).strip() else None

        def line_span():
            r = txt.tag_ranges("sel")
            if r:
                return txt.index(f"{r[0]} linestart"), txt.index(f"{r[1]} lineend")
            return txt.index("insert linestart"), txt.index("insert lineend")

        def toggle_style(which):
            s = sel()
            if not s:
                return "break"
            a, b = s
            base, bold, ital = font_state_at(txt, a)
            if which == "b":
                bold = not bold
            else:
                ital = not ital
            apply_font(txt, a, b, base, bold, ital)
            on_change()
            return "break"

        def toggle_tag(tag):
            s = sel()
            if not s:
                return "break"
            a, b = s
            # "on" only if the whole range already carries it
            on = tag in txt.tag_names(a) and tag in txt.tag_names(f"{b}-1c")
            (txt.tag_remove if on else txt.tag_add)(tag, a, b)
            on_change()
            return "break"

        def set_base(base):
            a, b = line_span()
            if a == b:
                return "break"
            cur, bold, ital = font_state_at(txt, a)
            # clicking the active heading turns it back into body text
            target = "body" if cur == base else base
            apply_font(txt, a, b, target, False if target != "body" else bold,
                       ital if target == "body" else False)
            on_change()
            return "break"

        def toggle_bullet():
            a, b = line_span()
            first = int(a.split(".")[0])
            last = int(b.split(".")[0])
            lines = [txt.get(f"{i}.0", f"{i}.end") for i in range(first, last + 1)]
            adding = not all(l.lstrip().startswith("• ") for l in lines if l.strip())
            for i in range(first, last + 1):
                line = txt.get(f"{i}.0", f"{i}.end")
                if adding:
                    if line.strip() and not line.lstrip().startswith("• "):
                        txt.insert(f"{i}.0", "• ")
                elif line.lstrip().startswith("• "):
                    j = line.index("• ")
                    txt.delete(f"{i}.{j}", f"{i}.{j + 2}")
            on_change()
            return "break"

        def clear_fmt():
            s = sel()
            if not s:
                return "break"
            a, b = s
            for t in txt.tag_names():
                if t != "sel":
                    txt.tag_remove(t, a, b)
            on_change()
            return "break"

        bar = tk.Frame(parent, bg=col["bg"])
        bar.pack(side="top", fill="x", pady=(0, 6))

        def fbtn(text, cmd, tip, font=("Segoe UI", 10), width=3):
            fill = col["header"]
            b = tk.Button(bar, text=text, command=cmd, cursor="hand2", font=font,
                          relief="flat", bd=0, width=width,
                          bg=fill, fg=self._readable_on(fill),
                          activebackground=shade(fill, 0.86),
                          activeforeground=self._readable_on(fill),
                          highlightthickness=0, padx=2, pady=2)
            # Read `col` on hover rather than closing over `fill`, so a colour
            # change does not leave the buttons flashing back to the old shade.
            b.bind("<Enter>", lambda e: b.config(bg=shade(col["header"], 1.12)))
            b.bind("<Leave>", lambda e: b.config(bg=col["header"]))
            b.pack(side="left", padx=2)
            Tooltip(b, tip)
            return b

        fbtn("B", lambda: toggle_style("b"), "Bold  (Ctrl+B)", ("Segoe UI", 10, "bold"))
        fbtn("I", lambda: toggle_style("i"), "Italic  (Ctrl+I)", ("Segoe UI", 10, "italic"))
        fbtn("U", lambda: toggle_tag("u"), "Underline  (Ctrl+U)", ("Segoe UI", 10, "underline"))
        fbtn("S", lambda: toggle_tag("s"), "Strikethrough", ("Segoe UI", 10, "overstrike"))

        tk.Frame(bar, bg=col["text"], width=1).pack(side="left", fill="y", padx=6, pady=3)

        fbtn("H1", lambda: set_base("h1"), "Heading 1  (Ctrl+1)", ("Segoe UI", 10, "bold"))
        fbtn("H2", lambda: set_base("h2"), "Heading 2  (Ctrl+2)", ("Segoe UI", 9, "bold"))
        fbtn("</>", lambda: set_base("code"), "Code", ("Consolas", 9))

        tk.Frame(bar, bg=col["text"], width=1).pack(side="left", fill="y", padx=6, pady=3)

        fbtn("•", toggle_bullet, "Bullet list  (Ctrl+L)", ("Segoe UI", 12))
        fbtn("\U0001F58D", lambda: toggle_tag("hl"), "Highlight", ("Segoe UI", 9))
        fbtn("✕", clear_fmt, "Clear formatting", ("Segoe UI", 9))

        for seq, fn in [("<Control-b>", lambda e: toggle_style("b")),
                        ("<Control-i>", lambda e: toggle_style("i")),
                        ("<Control-u>", lambda e: toggle_tag("u")),
                        ("<Control-l>", lambda e: toggle_bullet()),
                        ("<Control-Key-1>", lambda e: set_base("h1")),
                        ("<Control-Key-2>", lambda e: set_base("h2")),
                        ("<Control-Key-0>", lambda e: clear_fmt())]:
            txt.bind(seq, fn)

        return bar

    # ═══════════ Export ═══════════

    def export(self, fmt):
        ext = ".md" if fmt == "md" else ".json"
        path = filedialog.asksaveasfilename(
            parent=self.root, defaultextension=ext,
            initialfile=f"sticky-notes-{datetime.now():%Y%m%d}{ext}",
            filetypes=[("Markdown", "*.md")] if fmt == "md" else [("JSON", "*.json")])
        if not path:
            return
        try:
            if fmt == "json":
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(self.notes, f, indent=2)
            else:
                lines = [f"# Sticky Reminders  -  {datetime.now():%d %b %Y %H:%M}", ""]
                for title, pred in [("Active", lambda n: not n["done"] and not n["archived"]),
                                    ("Completed", lambda n: n["done"] and not n["archived"]),
                                    ("Archived", lambda n: n["archived"])]:
                    sel = [n for n in self.notes if pred(n)]
                    if not sel:
                        continue
                    lines.append(f"## {title} ({len(sel)})\n")
                    for n in sel:
                        box = "x" if n["done"] else " "
                        bits = []
                        if n.get("priority", 1) != 1:
                            bits.append(PRIORITIES[n["priority"]][0])
                        d = parse_dt(n.get("due", ""))
                        if d:
                            bits.append(f"due {d:%d %b %H:%M}")
                        if n.get("source") == "mattermost":
                            bits.append(f"@{n.get('sender','')} #{n.get('channel','')}")
                        if n.get("tags"):
                            bits.append(" ".join("#" + t for t in n["tags"]))
                        suffix = f"  _({' | '.join(bits)})_" if bits else ""
                        body = (n.get("text") or "(empty)").replace("\n", " ")
                        lines.append(f"- [{box}] {body}{suffix}")
                    lines.append("")
                with open(path, "w", encoding="utf-8") as f:
                    f.write("\n".join(lines))
            self.show_toast("Exported", os.path.basename(path), color=T.ok, sound=False)
        except Exception as e:
            messagebox.showerror("Export failed", str(e), parent=self.root)

    # ═══════════ Mattermost control ═══════════

    def _set_mm_status(self, text, ok):
        colour = T.fg_faint if ok is None else (T.ok if ok else T.err)
        self.mm_dot.config(fg=colour)
        self.mm_status.config(text=f"Mattermost: {text}", fg=colour)

    def connect_mattermost(self):
        if self.mm_client:
            self.mm_client.stop()
        self.config["mattermost_enabled"] = True
        self._set_mm_status("connecting…", None)
        self.mm_client = MattermostClient(self.config, self.msg_queue)
        self.mm_client.start()

    def disconnect_mattermost(self):
        if self.mm_client:
            self.mm_client.stop()
            self.mm_client = None
        self.config["mattermost_enabled"] = False
        save_config(self.config)
        self._set_mm_status("disconnected", None)

    # ═══════════ Settings ═══════════

    def open_settings(self):
        self.show_window()
        w = tk.Toplevel(self.root)
        w.title("Settings")
        w.configure(bg=T.panel)
        w.geometry("580x560+{}+{}".format(self.root.winfo_x() + 140,
                                          self.root.winfo_y() + 40))
        w.resizable(True, True)
        w.minsize(520, 460)
        w.bind("<Escape>", lambda e: w.destroy())
        w.after(60, lambda: dark_titlebar(w))
        w.bind("<Destroy>", lambda e: (self.root.bind_all("<MouseWheel>", self._main_wheel)
                                       if e.widget is w else None), add="+")
        w.transient(self.root)
        w.lift()
        w.focus_force()

        nb = ttk.Notebook(w)
        nb.pack(fill="both", expand=True, padx=12, pady=12)

        def page(name):
            """A scrollable tab - some pages are taller than the dialog."""
            outer = tk.Frame(nb, bg=T.panel)
            nb.add(outer, text=name)
            cv = tk.Canvas(outer, bg=T.panel, highlightthickness=0)
            sb = ttk.Scrollbar(outer, orient="vertical", command=cv.yview,
                               style="Vertical.TScrollbar")
            inner = tk.Frame(cv, bg=T.panel, padx=20, pady=16)
            win = cv.create_window((0, 0), window=inner, anchor="nw")
            inner.bind("<Configure>",
                       lambda e: cv.configure(scrollregion=cv.bbox("all")))
            cv.bind("<Configure>", lambda e: cv.itemconfig(win, width=e.width))
            cv.configure(yscrollcommand=sb.set)
            cv.pack(side="left", fill="both", expand=True)
            sb.pack(side="right", fill="y")

            def wheel(e):
                if cv.bbox("all") and cv.bbox("all")[3] > cv.winfo_height():
                    cv.yview_scroll(-1 * (e.delta // 120), "units")

            # take the wheel while the pointer is over this tab, hand it back after
            outer.bind("<Enter>", lambda e: self.root.bind_all("<MouseWheel>", wheel))
            outer.bind("<Leave>",
                       lambda e: self.root.bind_all("<MouseWheel>", self._main_wheel))
            return inner

        def section(parent, title):
            tk.Label(parent, text=title, font=("Segoe UI Semibold", 10),
                     bg=T.panel, fg=T.fg).pack(anchor="w", pady=(10, 4))

        def hint(parent, text):
            tk.Label(parent, text=text, font=self.font_tiny, bg=T.panel,
                     fg=T.fg_faint, justify="left", wraplength=470).pack(anchor="w", pady=(0, 6))

        def check(parent, label, key):
            v = tk.BooleanVar(value=bool(self.config.get(key)))
            tk.Checkbutton(parent, text=label, variable=v, font=self.font_small,
                           bg=T.panel, fg=T.fg_dim, selectcolor=T.panel2,
                           activebackground=T.panel, activeforeground=T.fg,
                           bd=0, highlightthickness=0, anchor="w",
                           cursor="hand2").pack(anchor="w", pady=1)
            return v

        def entry(parent, label, value, width=34, show=None):
            tk.Label(parent, text=label, font=self.font_tiny, bg=T.panel,
                     fg=T.fg_faint).pack(anchor="w", pady=(6, 2))
            var = tk.StringVar(value=value)
            e = tk.Entry(parent, textvariable=var, font=self.font_small, width=width,
                         bg=T.panel2, fg=T.fg, relief="flat", bd=0,
                         insertbackground=T.fg, show=show)
            e.pack(anchor="w", ipady=5)
            return var, e

        # ── Reminders ──
        p1 = page("Reminders")
        section(p1, "Reminder interval")
        hint(p1, "How often you get nudged about pending notes. "
                 "The timer is absolute - it counts from the last reminder, "
                 "not from the top of the hour.")
        iv = tk.StringVar(value=str(self._interval()))
        irow = tk.Frame(p1, bg=T.panel)
        irow.pack(anchor="w", pady=(0, 4))
        iv_refresh = self._chip_group(
            irow, iv, [("15", "15m"), ("30", "30m"), ("60", "1h"), ("120", "2h"),
                       ("240", "4h"), ("480", "8h"), ("600", "10h")])
        tk.Label(p1, text="Custom (minutes)", font=self.font_tiny,
                 bg=T.panel, fg=T.fg_faint).pack(anchor="w", pady=(8, 2))
        ie = tk.Entry(p1, textvariable=iv, font=self.font_small, width=8, bg=T.panel2,
                      fg=T.fg, relief="flat", bd=0, insertbackground=T.fg)
        ie.pack(anchor="w", ipady=4)
        # typing a custom value keeps the preset chips in sync
        ie.bind("<KeyRelease>", lambda e: iv_refresh())

        section(p1, "Behaviour")
        v_rem = check(p1, "Enable periodic reminders", "reminders_enabled")
        v_snd = check(p1, "Play a sound", "sound_enabled")
        v_due = check(p1, "Alert me when a note's due time arrives", "due_reminders")
        v_focus = check(p1, "Bring the window forward for new Mattermost notes", "focus_steal")

        section(p1, "Popup position")
        v_style = tk.StringVar(value=self.config.get("popup_style", "toast"))
        srow = tk.Frame(p1, bg=T.panel)
        srow.pack(anchor="w")
        self._chip_group(srow, v_style, [("toast", "Bottom-right corner"),
                                         ("window", "Centre of screen")])

        # ── Quiet hours / mute ──
        p2 = page("Do Not Disturb")
        section(p2, "Current state")
        state_lbl = tk.Label(p2, text=self.mute_label(), font=("Segoe UI Semibold", 11),
                             bg=T.panel, fg=T.accent)
        state_lbl.pack(anchor="w", pady=(0, 8))

        def do_mute(mins):
            self.mute_for(mins)
            state_lbl.config(text=self.mute_label())

        # two rows - the full list is wider than the dialog
        tk.Label(p2, text="Mute for", font=self.font_tiny, bg=T.panel,
                 fg=T.fg_faint).pack(anchor="w", pady=(0, 3))
        for chunk in (MUTE_CHOICES[:4], MUTE_CHOICES[4:]):
            mrow = tk.Frame(p2, bg=T.panel)
            mrow.pack(anchor="w", pady=1)
            for lbl, mins in chunk:
                self._btn(mrow, lbl, lambda m=mins: do_mute(m),
                          font=self.font_tiny, pad=(10, 4)).pack(side="left", padx=2)

        self._btn(p2, "Turn reminders back on",
                  lambda: (self.unmute(), state_lbl.config(text=self.mute_label())),
                  bg=T.ok, fg="#0F2418").pack(anchor="w", pady=10)

        section(p2, "Quiet hours")
        hint(p2, "Reminders are silently skipped during this window every day. "
                 "The schedule keeps running - you just aren't interrupted.")
        v_quiet = check(p2, "Enable quiet hours", "quiet_hours_enabled")
        qrow = tk.Frame(p2, bg=T.panel)
        qrow.pack(anchor="w", pady=6)
        tk.Label(qrow, text="From", font=self.font_tiny, bg=T.panel,
                 fg=T.fg_faint).pack(side="left", padx=(0, 6))
        v_qs = tk.StringVar(value=self.config.get("quiet_start", "22:00"))
        tk.Entry(qrow, textvariable=v_qs, width=7, font=self.font_small, bg=T.panel2,
                 fg=T.fg, relief="flat", bd=0, insertbackground=T.fg).pack(side="left", ipady=4)
        tk.Label(qrow, text="to", font=self.font_tiny, bg=T.panel,
                 fg=T.fg_faint).pack(side="left", padx=6)
        v_qe = tk.StringVar(value=self.config.get("quiet_end", "08:00"))
        tk.Entry(qrow, textvariable=v_qe, width=7, font=self.font_small, bg=T.panel2,
                 fg=T.fg, relief="flat", bd=0, insertbackground=T.fg).pack(side="left", ipady=4)

        # ── Mattermost ──
        p3 = page("Mattermost")
        v_mm = check(p3, "Connect to Mattermost", "mattermost_enabled")
        v_url, _ = entry(p3, "Server URL", self.config.get("mattermost_url", ""))
        v_user, _ = entry(p3, "Username / email", self.config.get("username", ""))
        v_pass, pw_entry = entry(p3, "Password", self.config.get("password", ""), show="•")
        show_var = tk.BooleanVar(value=False)
        tk.Checkbutton(p3, text="Show password", variable=show_var, font=self.font_tiny,
                       bg=T.panel, fg=T.fg_faint, selectcolor=T.panel2, bd=0,
                       highlightthickness=0, activebackground=T.panel, cursor="hand2",
                       command=lambda: pw_entry.config(show="" if show_var.get() else "•")
                       ).pack(anchor="w", pady=(2, 0))
        v_tok, _ = entry(p3, "Personal access token (preferred - leave blank to use password)",
                         self.config.get("access_token", ""), show="•")
        v_trig, _ = entry(p3, "Trigger word", self.config.get("trigger_word", "@reminder"), width=18)
        hint(p3, "Any message containing the trigger word - sent by you, or mentioning "
                 "you - becomes a note. Add !urgent / !high / !low to set its priority.")
        hint(p3, f"Credentials are stored in plain text in {CONFIG_FILE}. "
                 f"A personal access token is safer than your password: revoke it "
                 f"any time from Mattermost without changing your login.")
        self._btn(p3, "Reconnect now", self.connect_mattermost).pack(anchor="w", pady=8)

        # ── System ──
        p4 = page("System")
        section(p4, "Startup")
        su_var = tk.BooleanVar(value=startup_enabled())
        su_state = tk.Label(p4, text="", font=self.font_tiny, bg=T.panel, fg=T.fg_faint,
                            wraplength=470, justify="left")

        def toggle_startup():
            ok, msg = set_startup(su_var.get())
            su_state.config(text=msg, fg=T.ok if ok else T.err)

        tk.Checkbutton(p4, text="Start automatically when I log in", variable=su_var,
                       font=self.font_small, bg=T.panel, fg=T.fg_dim, selectcolor=T.panel2,
                       activebackground=T.panel, activeforeground=T.fg, bd=0,
                       highlightthickness=0, anchor="w", cursor="hand2",
                       command=toggle_startup).pack(anchor="w")
        su_state.pack(anchor="w", pady=(2, 0))
        hint(p4, f"Launches {os.path.basename(PYTHONW)} directly, so only ONE process runs. "
                 f"(The old shortcut used the WindowsApps alias, which spawned a second "
                 f"lingering pythonw.exe.)")

        section(p4, "Window")
        v_min = check(p4, "Start minimised to the tray", "start_minimized")
        v_tray = check(p4, "Closing the window hides to the tray instead of quitting",
                       "close_to_tray")
        if not self.tray.available:
            hint(p4, "Tray icon unavailable - install it with:  "
                     "python -m pip install pystray pillow")

        section(p4, "Maintenance")
        mrow2 = tk.Frame(p4, bg=T.panel)
        mrow2.pack(anchor="w", pady=4)
        self._btn(mrow2, "Archive completed notes", self.archive_all_done,
                  font=self.font_tiny, pad=(10, 4)).pack(side="left", padx=(0, 6))
        self._btn(mrow2, "Open log", self.open_log,
                  font=self.font_tiny, pad=(10, 4)).pack(side="left", padx=(0, 6))
        self._btn(mrow2, "Open data folder",
                  lambda: os.startfile(SCRIPT_DIR),
                  font=self.font_tiny, pad=(10, 4)).pack(side="left")

        # ── save bar ──
        bar = tk.Frame(w, bg=T.panel, padx=12, pady=10)
        bar.pack(fill="x")

        def save_all():
            try:
                iv_val = max(1, int(iv.get()))
            except ValueError:
                messagebox.showwarning("Interval", "Interval must be a whole number of minutes.",
                                       parent=w)
                return
            self.config.update({
                "reminder_interval_minutes": iv_val,
                "reminders_enabled": v_rem.get(),
                "sound_enabled": v_snd.get(),
                "due_reminders": v_due.get(),
                "focus_steal": v_focus.get(),
                "popup_style": v_style.get(),
                "quiet_hours_enabled": v_quiet.get(),
                "quiet_start": v_qs.get().strip() or "22:00",
                "quiet_end": v_qe.get().strip() or "08:00",
                "mattermost_enabled": v_mm.get(),
                "mattermost_url": v_url.get().strip(),
                "username": v_user.get().strip(),
                "password": v_pass.get(),
                "access_token": v_tok.get().strip(),
                "trigger_word": v_trig.get().strip() or "@reminder",
                "start_minimized": v_min.get(),
                "close_to_tray": v_tray.get(),
            })
            save_config(self.config)
            self.next_reminder = datetime.now() + timedelta(minutes=iv_val)
            if self.config["mattermost_enabled"]:
                self.connect_mattermost()
            else:
                self.disconnect_mattermost()
            self._refresh_mute_ui()
            self.render_notes()
            w.destroy()
            self.show_toast("Settings saved",
                            f"Reminder every {iv_val} min • {self.mute_label()}",
                            color=T.ok, sound=False)

        self._btn(bar, "Save", save_all, bg=T.accent, fg="#1A1A1A",
                  pad=(20, 6)).pack(side="right")
        self._btn(bar, "Cancel", w.destroy).pack(side="right", padx=8)

    # ─────────────── run ───────────────
    def run(self):
        self.root.mainloop()


# ═══════════════════════════════════════════════════════
#  Entry point
# ═══════════════════════════════════════════════════════

def main():
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass

    holder = {}
    guard = SingleInstance(lambda: holder["app"].post(holder["app"].show_window))
    if not guard.acquire():
        # Another copy is already running - wake it up instead of starting a second.
        SingleInstance.signal_existing()
        log.info("Another instance is running; asked it to show and exited.")
        sys.exit(0)

    app = StickyNotesApp()
    holder["app"] = app

    if "--reminder" in sys.argv:
        app.root.after(600, app.show_reminder_popup)
    if "--show" in sys.argv:
        app.root.after(300, app.show_window)

    app.run()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        log.exception("Fatal error")
        raise
