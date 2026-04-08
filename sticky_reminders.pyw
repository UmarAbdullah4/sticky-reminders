"""
Sticky Notes Reminder + Mattermost WebSocket
==============================================
Polished desktop app with:
  - Click to open/expand notes
  - "Done" button sends confirmation back to Mattermost channel
  - Hourly reminder popups
  - Auto-start on Windows login

Requirements:  python -m pip install websockets
Config:        Edit config.json (same folder)
"""

import tkinter as tk
from tkinter import font as tkfont
import json
import os
import sys
import ssl
import threading
import asyncio
import winsound
from datetime import datetime
from queue import Queue
from urllib.request import Request, urlopen
from urllib.error import HTTPError

# ─── Paths ───
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(SCRIPT_DIR, "config.json")
DATA_FILE = os.path.join(os.path.expanduser("~"), ".sticky_reminders.json")

# ─── Note Colors ───
COLORS = [
    {"name": "Yellow",  "bg": "#FFF9B1", "header": "#F5E647", "text": "#1A1A1A", "hover": "#FFF38A"},
    {"name": "Green",   "bg": "#D5F5D5", "header": "#7ED957", "text": "#1A1A1A", "hover": "#C0F0C0"},
    {"name": "Pink",    "bg": "#FFD6E8", "header": "#FF6FB5", "text": "#1A1A1A", "hover": "#FFC0DD"},
    {"name": "Purple",  "bg": "#E8D5FF", "header": "#B47AEA", "text": "#1A1A1A", "hover": "#DDC8FF"},
    {"name": "Blue",    "bg": "#D5EAFF", "header": "#5EB3F6", "text": "#1A1A1A", "hover": "#C0DEFF"},
    {"name": "Orange",  "bg": "#FFE0B2", "header": "#FFA726", "text": "#1A1A1A", "hover": "#FFD499"},
]

DONE_COLOR = {"bg": "#E0E0E0", "header": "#999999", "text": "#888888", "hover": "#D5D5D5"}
MM_COLOR = 3  # Purple


def load_config():
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"[Config] Error: {e}")
        return {}


def load_notes():
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return [
        {"id": 1, "text": "Start working on the report", "color": 0, "source": "local", "done": False},
        {"id": 2, "text": "Review pull requests", "color": 4, "source": "local", "done": False},
        {"id": 3, "text": "Reply to client emails", "color": 2, "source": "local", "done": False},
    ]


def save_notes(notes):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(notes, f, indent=2)


def save_config(config):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=4)


# ═══════════════════════════════════════════════════════
#  Mattermost Client
# ═══════════════════════════════════════════════════════

class MattermostClient:
    def __init__(self, config, message_queue):
        self.url = config.get("mattermost_url", "").rstrip("/")
        self.username = config.get("username", "")
        self.password = config.get("password", "")
        self.trigger = config.get("trigger_word", "@reminder")
        self.queue = message_queue
        self.token = None
        self.user_id = None
        self.my_username = None
        self.running = False

        self.ssl_ctx = ssl.create_default_context()
        self.ssl_ctx.check_hostname = False
        self.ssl_ctx.verify_mode = ssl.CERT_NONE

    def login(self):
        try:
            url = f"{self.url}/api/v4/users/login"
            payload = json.dumps({
                "login_id": self.username,
                "password": self.password,
            }).encode("utf-8")
            req = Request(url, data=payload, headers={"Content-Type": "application/json"})

            try:
                resp = urlopen(req, context=self.ssl_ctx, timeout=15)
            except HTTPError as e:
                body = e.read().decode("utf-8")
                try:
                    err_data = json.loads(body)
                    err_msg = err_data.get("message", body[:200])
                    if "too_many" in err_data.get("id", ""):
                        err_msg += "\n\u2192 Reset login attempts in System Console"
                except Exception:
                    err_msg = body[:200]
                self.queue.put({"type": "status", "text": f"Login failed: {err_msg}", "ok": False})
                return False

            self.token = resp.headers.get("Token")
            data = json.loads(resp.read().decode("utf-8"))
            self.user_id = data.get("id")
            self.my_username = data.get("username", "")

            print(f"[MM] Logged in as @{self.my_username} (id: {self.user_id})")
            self.queue.put({"type": "status", "text": f"Logged in as @{self.my_username}", "ok": True})
            return True

        except Exception as e:
            self.queue.put({"type": "status", "text": f"Connection error: {e}", "ok": False})
            return False

    def send_message(self, channel_id, message, root_id=None):
        """Post a message to a Mattermost channel."""
        try:
            url = f"{self.url}/api/v4/posts"
            payload = {
                "channel_id": channel_id,
                "message": message,
            }
            if root_id:
                payload["root_id"] = root_id

            data = json.dumps(payload).encode("utf-8")
            req = Request(url, data=data, headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.token}",
            })
            resp = urlopen(req, context=self.ssl_ctx, timeout=10)
            print(f"[MM] Sent reply to channel {channel_id}")
            return True
        except Exception as e:
            print(f"[MM] Failed to send message: {e}")
            return False

    def _should_create_note(self, message, sender_id):
        msg_lower = message.lower()
        if self.trigger.lower() not in msg_lower:
            return False
        if sender_id == self.user_id:
            return True
        if self.my_username and f"@{self.my_username.lower()}" in msg_lower:
            return True
        return False

    def _extract_reminder_text(self, message):
        text = message
        idx = text.lower().find(self.trigger.lower())
        if idx != -1:
            text = text[:idx] + text[idx + len(self.trigger):]
        if self.my_username:
            at_me = f"@{self.my_username}"
            lower_text = text.lower()
            me_idx = lower_text.find(at_me.lower())
            if me_idx != -1:
                text = text[:me_idx] + text[me_idx + len(at_me):]
        return text.strip()

    async def _listen_ws(self):
        import websockets
        ws_scheme = "wss" if self.url.startswith("https") else "ws"
        host = self.url.replace("https://", "").replace("http://", "")
        ws_url = f"{ws_scheme}://{host}/api/v4/websocket"
        retry_delay = 3

        while self.running:
            try:
                print(f"[MM] Connecting to WebSocket: {ws_url}")
                async with websockets.connect(ws_url, ssl=self.ssl_ctx) as ws:
                    auth = json.dumps({
                        "seq": 1,
                        "action": "authentication_challenge",
                        "data": {"token": self.token}
                    })
                    await ws.send(auth)
                    auth_resp = await ws.recv()
                    auth_data = json.loads(auth_resp)
                    if auth_data.get("status") == "OK":
                        print("[MM] WebSocket authenticated")

                    self.queue.put({
                        "type": "status",
                        "text": f"Connected as @{self.my_username} \u2714",
                        "ok": True
                    })
                    retry_delay = 3

                    async for raw in ws:
                        try:
                            event = json.loads(raw)
                            if event.get("event") != "posted":
                                continue

                            event_data = event.get("data", {})
                            post = json.loads(event_data.get("post", "{}"))
                            message = post.get("message", "")
                            sender_id = post.get("user_id", "")

                            if not self._should_create_note(message, sender_id):
                                continue

                            reminder_text = self._extract_reminder_text(message)
                            if not reminder_text:
                                continue

                            sender_name = event_data.get("sender_name", "").lstrip("@") or "Unknown"
                            channel_display = event_data.get("channel_display_name", "") or \
                                              event_data.get("channel_name", "Unknown")
                            channel_id = post.get("channel_id", "")
                            post_id = post.get("id", "")
                            is_self = sender_id == self.user_id

                            print(f"[MM] Reminder from @{sender_name} in #{channel_display}: {reminder_text}")

                            self.queue.put({
                                "type": "reminder",
                                "text": reminder_text,
                                "sender": sender_name,
                                "channel": channel_display,
                                "channel_id": channel_id,
                                "post_id": post_id,
                                "is_self": is_self,
                                "timestamp": datetime.now().strftime("%I:%M %p"),
                            })
                        except json.JSONDecodeError:
                            pass
                        except Exception as e:
                            print(f"[MM] Event error: {e}")

            except Exception as e:
                self.queue.put({
                    "type": "status",
                    "text": f"Disconnected, retrying in {retry_delay}s...",
                    "ok": False
                })
                print(f"[MM] WebSocket error: {e}")
                await asyncio.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, 30)

    def start(self):
        if not self.token:
            if not self.login():
                return
        self.running = True

        def run_loop():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.run_until_complete(self._listen_ws())

        t = threading.Thread(target=run_loop, daemon=True)
        t.start()

    def stop(self):
        self.running = False


# ═══════════════════════════════════════════════════════
#  Sticky Notes GUI
# ═══════════════════════════════════════════════════════

class StickyNotesApp:
    def __init__(self):
        self.notes = load_notes()
        # Ensure all notes have 'done' field
        for n in self.notes:
            if "done" not in n:
                n["done"] = False

        self.next_id = max((n["id"] for n in self.notes), default=0) + 1
        self.config = load_config()
        self.msg_queue = Queue()
        self.mm_client = None

        self.root = tk.Tk()
        self.root.title("\U0001f4dd Sticky Notes + Mattermost")
        self.root.configure(bg="#1A1A1A")
        self.root.geometry("820x640")
        self.root.minsize(580, 440)

        # ─── Fonts ───
        self.font_title = ("Segoe UI", 14, "bold")
        self.font_body = ("Segoe UI", 11)
        self.font_small = ("Segoe UI", 9)
        self.font_tiny = ("Segoe UI", 8)
        self.font_btn = ("Segoe UI", 10, "bold")

        # ─── Top Bar ───
        topbar = tk.Frame(self.root, bg="#2A2A2A", pady=10, padx=16)
        topbar.pack(fill="x")

        tk.Label(topbar, text="\U0001f4dd Sticky Notes", font=self.font_title,
                 bg="#2A2A2A", fg="#FFFFFF").pack(side="left")

        self.countdown_label = tk.Label(topbar, text="", font=self.font_small,
                                        bg="#2A2A2A", fg="#777")
        self.countdown_label.pack(side="left", padx=20)

        # Right buttons
        tk.Button(topbar, text="+ New Note", font=self.font_btn,
                  bg="#FFD54F", fg="#1A1A1A", relief="flat", padx=14, pady=5,
                  command=self.add_note, cursor="hand2",
                  activebackground="#FFC107").pack(side="right")

        tk.Button(topbar, text="\u2699", font=("Segoe UI", 13),
                  bg="#3A3A3A", fg="#AAA", relief="flat", padx=8, pady=3,
                  command=self.open_settings, cursor="hand2",
                  activebackground="#4A4A4A").pack(side="right", padx=6)

        tk.Button(topbar, text="\U0001f514", font=("Segoe UI", 11),
                  bg="#3A3A3A", fg="#AAA", relief="flat", padx=8, pady=3,
                  command=self.show_reminder_popup, cursor="hand2",
                  activebackground="#4A4A4A").pack(side="right", padx=2)

        # ─── Mattermost Status Bar ───
        mm_bar = tk.Frame(self.root, bg="#222", pady=4, padx=16)
        mm_bar.pack(fill="x")

        self.mm_indicator = tk.Label(mm_bar, text="\u25cf", font=("Segoe UI", 9),
                                     bg="#222", fg="#555")
        self.mm_indicator.pack(side="left")

        self.mm_status = tk.Label(mm_bar, text="Mattermost: Not connected",
                                   font=self.font_tiny, bg="#222", fg="#777")
        self.mm_status.pack(side="left", padx=6)

        trigger = self.config.get("trigger_word", "@reminder")
        tk.Label(mm_bar, text=f"Trigger: {trigger}",
                 font=self.font_tiny, bg="#222", fg="#555").pack(side="right")

        tk.Button(mm_bar, text="Reconnect", font=self.font_tiny,
                  bg="#333", fg="#999", relief="flat", padx=6, pady=1,
                  command=self.connect_mattermost, cursor="hand2").pack(side="right", padx=8)

        # ─── Notes Counter ───
        counter_bar = tk.Frame(self.root, bg="#1A1A1A", pady=6, padx=18)
        counter_bar.pack(fill="x")

        self.counter_label = tk.Label(counter_bar, text="", font=self.font_small,
                                       bg="#1A1A1A", fg="#666")
        self.counter_label.pack(side="left")

        self.filter_var = tk.StringVar(value="all")
        for val, label in [("all", "All"), ("active", "Active"), ("done", "Done"), ("mm", "Mattermost")]:
            tk.Radiobutton(counter_bar, text=label, variable=self.filter_var, value=val,
                           font=self.font_tiny, bg="#1A1A1A", fg="#888",
                           selectcolor="#333", activebackground="#1A1A1A",
                           activeforeground="#FFD54F", indicatoron=0,
                           padx=10, pady=3, relief="flat", bd=0,
                           cursor="hand2", command=self.render_notes).pack(side="right", padx=2)

        # ─── Scrollable Notes Area ───
        container = tk.Frame(self.root, bg="#1A1A1A")
        container.pack(fill="both", expand=True)

        self.canvas = tk.Canvas(container, bg="#1A1A1A", highlightthickness=0)
        scrollbar = tk.Scrollbar(container, orient="vertical", command=self.canvas.yview,
                                 bg="#333", troughcolor="#1A1A1A")
        self.scroll_frame = tk.Frame(self.canvas, bg="#1A1A1A")

        self.scroll_frame.bind("<Configure>",
                               lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.create_window((0, 0), window=self.scroll_frame, anchor="nw")
        self.canvas.configure(yscrollcommand=scrollbar.set)

        self.canvas.pack(side="left", fill="both", expand=True, padx=14, pady=8)
        scrollbar.pack(side="right", fill="y")
        self.canvas.bind_all("<MouseWheel>",
                             lambda e: self.canvas.yview_scroll(-1 * (e.delta // 120), "units"))

        self.render_notes()
        self.update_countdown()
        self.check_reminder()
        self.poll_queue()

        if self.config.get("mattermost_url") and self.config.get("username"):
            self.root.after(1500, self.connect_mattermost)

        self.root.protocol("WM_DELETE_WINDOW", self.minimize_to_taskbar)

    # ─── Mattermost ───

    def connect_mattermost(self):
        if self.mm_client:
            self.mm_client.stop()
        self.mm_status.config(text="Mattermost: Connecting...", fg="#FFD54F")
        self.mm_indicator.config(fg="#FFD54F")
        self.mm_client = MattermostClient(self.config, self.msg_queue)
        self.mm_client.start()

    def poll_queue(self):
        while not self.msg_queue.empty():
            msg = self.msg_queue.get_nowait()
            if msg["type"] == "status":
                ok = msg.get("ok", False)
                self.mm_status.config(text=f"Mattermost: {msg['text']}",
                                       fg="#7ED957" if ok else "#FF6B6B")
                self.mm_indicator.config(fg="#7ED957" if ok else "#FF6B6B")
            elif msg["type"] == "reminder":
                self.add_mattermost_note(msg)
        self.root.after(500, self.poll_queue)

    def add_mattermost_note(self, msg):
        note = {
            "id": self.next_id,
            "text": msg["text"],
            "color": MM_COLOR,
            "source": "mattermost",
            "done": False,
            "sender": msg.get("sender", "Unknown"),
            "channel": msg.get("channel", "Unknown"),
            "channel_id": msg.get("channel_id", ""),
            "post_id": msg.get("post_id", ""),
            "is_self": msg.get("is_self", False),
            "received_at": msg.get("timestamp", ""),
        }
        self.next_id += 1
        self.notes.append(note)
        save_notes(self.notes)
        self.render_notes()

        try:
            winsound.MessageBeep(winsound.MB_ICONASTERISK)
        except Exception:
            pass

        self.root.deiconify()
        self.root.lift()
        self.root.attributes("-topmost", True)
        self.root.after(3000, lambda: self.root.attributes("-topmost", False))

        label = "Self-reminder" if msg.get("is_self") else f"@{msg['sender']} in #{msg['channel']}"
        self.show_toast(f"\U0001f4e8 {label}", msg["text"])

    def mark_done(self, note_id):
        """Mark a note as done and send confirmation to Mattermost if applicable."""
        for n in self.notes:
            if n["id"] == note_id:
                n["done"] = True

                # Send reply to Mattermost
                if n.get("source") == "mattermost" and n.get("channel_id") and self.mm_client:
                    reminder_text = n.get("text", "")
                    sender = n.get("sender", "")
                    is_self = n.get("is_self", False)

                    if is_self:
                        reply = f"\u2705 **Done:** {reminder_text}"
                    else:
                        reply = f"\u2705 **Done** (completed by @{self.mm_client.my_username}): {reminder_text}"

                    # Send in background thread
                    def send():
                        self.mm_client.send_message(
                            n["channel_id"], reply, root_id=n.get("post_id")
                        )

                    threading.Thread(target=send, daemon=True).start()
                    self.show_toast("\u2705 Marked as done", f"Reply sent to #{n.get('channel', '')}")

                break

        save_notes(self.notes)
        self.render_notes()

    def delete_note(self, nid):
        self.notes = [n for n in self.notes if n["id"] != nid]
        save_notes(self.notes)
        self.render_notes()

    def show_toast(self, title, body):
        toast = tk.Toplevel(self.root)
        toast.overrideredirect(True)
        toast.attributes("-topmost", True)
        toast.configure(bg="#333")
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        toast.geometry(f"350x80+{sw - 370}+{sh - 130}")

        # Left accent bar
        tk.Frame(toast, bg="#7ED957", width=4).pack(side="left", fill="y")

        content = tk.Frame(toast, bg="#333", padx=12)
        content.pack(side="left", fill="both", expand=True)

        tk.Label(content, text=title, font=("Segoe UI", 9, "bold"),
                 bg="#333", fg="#FFF", anchor="w").pack(fill="x", pady=(10, 0))
        tk.Label(content, text=body[:100], font=("Segoe UI", 9),
                 bg="#333", fg="#CCC", anchor="w", wraplength=300).pack(fill="x", pady=(2, 10))

        toast.after(5000, toast.destroy)

    # ─── Note Detail Popup ───

    def open_note(self, note_id):
        """Open a note in a detailed popup window."""
        note = None
        for n in self.notes:
            if n["id"] == note_id:
                note = n
                break
        if not note:
            return

        is_mm = note.get("source") == "mattermost"
        is_done = note.get("done", False)
        c = DONE_COLOR if is_done else COLORS[note.get("color", 0) % len(COLORS)]

        popup = tk.Toplevel(self.root)
        popup.title("Note")
        popup.configure(bg="#2A2A2A")
        popup.geometry("480x420+{}+{}".format(
            self.root.winfo_x() + 120, self.root.winfo_y() + 80
        ))
        popup.attributes("-topmost", True)
        popup.resizable(True, True)
        popup.minsize(380, 300)

        # ─── Header ───
        header = tk.Frame(popup, bg=c["header"], pady=12, padx=18)
        header.pack(fill="x")

        header_left = tk.Frame(header, bg=c["header"])
        header_left.pack(side="left", fill="x", expand=True)

        if is_mm:
            source_text = "Self-reminder" if note.get("is_self") else f"From @{note.get('sender', '?')} in #{note.get('channel', '?')}"
            tk.Label(header_left, text="\U0001f4e8 Mattermost Reminder",
                     font=("Segoe UI", 12, "bold"), bg=c["header"], fg="#FFF").pack(anchor="w")
            tk.Label(header_left, text=source_text,
                     font=("Segoe UI", 9), bg=c["header"], fg="#EEE").pack(anchor="w", pady=(2, 0))
            if note.get("received_at"):
                tk.Label(header_left, text=f"Received at {note['received_at']}",
                         font=("Segoe UI", 8), bg=c["header"], fg="#DDD").pack(anchor="w")
        else:
            tk.Label(header_left, text="\U0001f4dd Note",
                     font=("Segoe UI", 12, "bold"), bg=c["header"], fg="#FFF").pack(anchor="w")

        if is_done:
            tk.Label(header_left, text="\u2705 Completed",
                     font=("Segoe UI", 9, "bold"), bg=c["header"], fg="#90EE90").pack(anchor="w", pady=(2, 0))

        # ─── Status badge ───
        if is_done:
            tk.Label(header, text="\u2705", font=("Segoe UI", 18),
                     bg=c["header"]).pack(side="right")

        # ─── Bottom buttons (pack FIRST so they stay visible) ───
        btn_frame = tk.Frame(popup, bg="#2A2A2A", pady=10, padx=18)
        btn_frame.pack(side="bottom", fill="x")

        # Done button (prominent for active notes)
        if not is_done:
            def do_done():
                self.mark_done(note_id)
                popup.destroy()

            tk.Button(btn_frame, text="\u2705 Mark as Done", font=self.font_btn,
                      bg="#4CAF50", fg="#FFF", relief="flat", padx=16, pady=6,
                      command=do_done, cursor="hand2",
                      activebackground="#45A049").pack(side="left")

        # Delete button
        def do_delete():
            self.delete_note(note_id)
            popup.destroy()

        tk.Button(btn_frame, text="\U0001f5d1 Delete", font=("Segoe UI", 10),
                  bg="#C62828", fg="#FFF", relief="flat", padx=14, pady=6,
                  command=do_delete, cursor="hand2",
                  activebackground="#B71C1C").pack(side="left", padx=8)

        # Reopen (for done notes)
        if is_done:
            def reopen():
                note["done"] = False
                save_notes(self.notes)
                popup.destroy()
                self.render_notes()

            tk.Button(btn_frame, text="\U0001f504 Reopen", font=("Segoe UI", 10),
                      bg="#FF9800", fg="#FFF", relief="flat", padx=14, pady=6,
                      command=reopen, cursor="hand2",
                      activebackground="#F57C00").pack(side="left")

        # Close button
        tk.Button(btn_frame, text="Close", font=("Segoe UI", 10),
                  bg="#444", fg="#CCC", relief="flat", padx=14, pady=6,
                  command=popup.destroy, cursor="hand2").pack(side="right")

        # ─── Color picker (pack before body, local notes only) ───
        if not is_mm and not is_done:
            color_frame = tk.Frame(popup, bg="#2A2A2A", pady=8, padx=18)
            color_frame.pack(side="bottom", fill="x")

            tk.Label(color_frame, text="Color:", font=self.font_tiny,
                     bg="#2A2A2A", fg="#888").pack(side="left", padx=(0, 8))

            for i, col in enumerate(COLORS):
                def make_color_cmd(ci=i):
                    def cmd():
                        note["color"] = ci
                        save_notes(self.notes)
                        popup.destroy()
                        self.render_notes()
                        self.open_note(note_id)
                    return cmd

                dot = tk.Label(color_frame, text="\u25cf", font=("Segoe UI", 14),
                               bg="#2A2A2A", fg=col["header"], cursor="hand2")
                dot.pack(side="left", padx=3)
                dot.bind("<Button-1>", lambda e, cmd=make_color_cmd(): cmd())

        # ─── Note body (fills remaining space) ───
        body_frame = tk.Frame(popup, bg=c["bg"], padx=18, pady=14)
        body_frame.pack(fill="both", expand=True)

        text_w = tk.Text(body_frame, wrap="word", font=("Segoe UI", 12),
                         bg=c["bg"], fg=c["text"], relief="flat",
                         insertbackground=c["text"], bd=0, padx=8, pady=8)
        text_w.insert("1.0", note.get("text", ""))
        text_w.pack(fill="both", expand=True)

        if is_done:
            text_w.config(state="disabled")

        def on_text_change(e=None):
            note["text"] = text_w.get("1.0", "end-1c")
            save_notes(self.notes)

        text_w.bind("<KeyRelease>", on_text_change)

    # ─── Notes Grid ───

    def minimize_to_taskbar(self):
        self.root.iconify()

    def _get_filtered_notes(self):
        f = self.filter_var.get()
        if f == "active":
            return [n for n in self.notes if not n.get("done", False)]
        elif f == "done":
            return [n for n in self.notes if n.get("done", False)]
        elif f == "mm":
            return [n for n in self.notes if n.get("source") == "mattermost"]
        return self.notes

    def render_notes(self):
        for w in self.scroll_frame.winfo_children():
            w.destroy()

        filtered = self._get_filtered_notes()
        active_count = sum(1 for n in self.notes if not n.get("done", False))
        done_count = sum(1 for n in self.notes if n.get("done", False))
        mm_count = sum(1 for n in self.notes if n.get("source") == "mattermost")
        self.counter_label.config(text=f"{len(self.notes)} notes \u2022 {active_count} active \u2022 {done_count} done \u2022 {mm_count} from MM")

        cols = 3
        for i in range(cols):
            self.scroll_frame.columnconfigure(i, weight=1, uniform="col")

        row, col = 0, 0
        for note in filtered:
            is_done = note.get("done", False)
            is_mm = note.get("source") == "mattermost"
            c = DONE_COLOR if is_done else COLORS[note.get("color", 0) % len(COLORS)]
            nid = note["id"]

            # Outer wrapper for consistent sizing
            wrapper = tk.Frame(self.scroll_frame, bg="#1A1A1A")
            wrapper.grid(row=row, column=col, padx=6, pady=6, sticky="nsew")
            self.scroll_frame.rowconfigure(row, minsize=150)

            # Card
            card = tk.Frame(wrapper, bg=c["bg"], bd=0, cursor="hand2")
            card.pack(fill="both", expand=True)

            # Bind click to open on card
            def bind_click(widget, note_id=nid):
                widget.bind("<Button-1>", lambda e: self.open_note(note_id))

            bind_click(card)

            # Header strip
            hdr = tk.Frame(card, bg=c["header"], height=5)
            hdr.pack(fill="x")
            hdr.pack_propagate(False)
            bind_click(hdr)

            # Top row: badges + delete
            top_row = tk.Frame(card, bg=c["bg"])
            top_row.pack(fill="x", padx=8, pady=(6, 0))
            bind_click(top_row)

            badge_frame = tk.Frame(top_row, bg=c["bg"])
            badge_frame.pack(side="left")

            if is_mm:
                lbl = tk.Label(badge_frame, text="\U0001f4e8", font=("Segoe UI", 8),
                               bg=c["bg"], fg=c["text"])
                lbl.pack(side="left")
            if is_done:
                lbl = tk.Label(badge_frame, text="\u2705", font=("Segoe UI", 8),
                               bg=c["bg"])
                lbl.pack(side="left", padx=2)

            # Delete X
            tk.Button(top_row, text="\u2715", font=("Segoe UI", 9), bg=c["bg"],
                      fg=c["text"], relief="flat", padx=2,
                      command=lambda n=nid: self.delete_note(n), cursor="hand2").pack(side="right")

            # Note text preview
            preview_text = note.get("text", "").strip()
            if len(preview_text) > 100:
                preview_text = preview_text[:100] + "..."
            if not preview_text:
                preview_text = "(click to add text)"

            note_font = ("Segoe UI", 11, "overstrike") if is_done else self.font_body
            text_label = tk.Label(card, text=preview_text, font=note_font,
                                  bg=c["bg"], fg=c["text"], wraplength=220,
                                  justify="left", anchor="nw", padx=10, pady=6)
            text_label.pack(fill="both", expand=True)
            bind_click(text_label)

            # Footer for MM notes
            if is_mm:
                sender = note.get("sender", "")
                channel = note.get("channel", "")
                footer_text = ""
                if sender:
                    footer_text = f"@{sender}"
                if channel:
                    footer_text += f" \u2022 #{channel}"

                if footer_text:
                    ft = tk.Label(card, text=footer_text, font=self.font_tiny,
                                  bg=c["bg"], fg="#8B6BB5", anchor="w", padx=10)
                    ft.pack(fill="x", pady=(0, 6))
                    bind_click(ft)

            col += 1
            if col >= cols:
                col, row = 0, row + 1

        # Add note card (+)
        wrapper = tk.Frame(self.scroll_frame, bg="#1A1A1A")
        wrapper.grid(row=row, column=col, padx=6, pady=6, sticky="nsew")
        self.scroll_frame.rowconfigure(row, minsize=150)

        add_card = tk.Frame(wrapper, bg="#2A2A2A", bd=0, cursor="hand2")
        add_card.pack(fill="both", expand=True)

        add_label = tk.Label(add_card, text="+\nNew Note", font=("Segoe UI", 16),
                             bg="#2A2A2A", fg="#555", cursor="hand2")
        add_label.pack(expand=True)

        def on_add_enter(e):
            add_card.config(bg="#333")
            add_label.config(bg="#333", fg="#FFD54F")

        def on_add_leave(e):
            add_card.config(bg="#2A2A2A")
            add_label.config(bg="#2A2A2A", fg="#555")

        add_card.bind("<Enter>", on_add_enter)
        add_card.bind("<Leave>", on_add_leave)
        add_card.bind("<Button-1>", lambda e: self.add_note())
        add_label.bind("<Button-1>", lambda e: self.add_note())

    def add_note(self):
        new_note = {"id": self.next_id, "text": "", "color": 0, "source": "local", "done": False}
        self.notes.append(new_note)
        self.next_id += 1
        save_notes(self.notes)
        self.render_notes()
        self.open_note(new_note["id"])

    def cycle_color(self, nid):
        for n in self.notes:
            if n["id"] == nid:
                n["color"] = (n.get("color", 0) + 1) % len(COLORS)
        save_notes(self.notes)
        self.render_notes()

    def save_text(self, nid, text):
        for n in self.notes:
            if n["id"] == nid:
                n["text"] = text
        save_notes(self.notes)

    # ─── Settings ───

    def open_settings(self):
        settings = tk.Toplevel(self.root)
        settings.title("\u2699 Settings")
        settings.configure(bg="#2A2A2A")
        settings.geometry("400x360+{}+{}".format(
            self.root.winfo_x() + 150, self.root.winfo_y() + 100
        ))
        settings.attributes("-topmost", True)
        settings.resizable(False, False)

        header = tk.Frame(settings, bg="#333", pady=10, padx=16)
        header.pack(fill="x")
        tk.Label(header, text="\u2699 Settings", font=self.font_title,
                 bg="#333", fg="#FFF").pack(anchor="w")

        body = tk.Frame(settings, bg="#2A2A2A", padx=20, pady=16)
        body.pack(fill="both", expand=True)

        # Interval
        tk.Label(body, text="Reminder Interval", font=("Segoe UI", 10, "bold"),
                 bg="#2A2A2A", fg="#DDD").pack(anchor="w", pady=(0, 6))

        interval_var = tk.IntVar(value=self.config.get("reminder_interval_minutes", 60))

        preset_frame = tk.Frame(body, bg="#2A2A2A")
        preset_frame.pack(fill="x", pady=(0, 10))

        for val in [15, 30, 60, 120]:
            label = f"{val}m" if val < 60 else f"{val // 60}h"
            tk.Radiobutton(preset_frame, text=label, variable=interval_var, value=val,
                           font=self.font_small, bg="#2A2A2A", fg="#DDD",
                           selectcolor="#444", activebackground="#2A2A2A",
                           activeforeground="#FFD54F", indicatoron=0,
                           padx=14, pady=5, relief="flat", bd=0,
                           cursor="hand2").pack(side="left", padx=3)

        tk.Label(body, text="Custom (minutes):", font=self.font_tiny,
                 bg="#2A2A2A", fg="#888").pack(anchor="w", pady=(0, 4))

        custom_entry = tk.Entry(body, font=self.font_body, bg="#444", fg="#FFF",
                                insertbackground="#FFF", relief="flat", width=8)
        custom_entry.pack(anchor="w", pady=(0, 14))
        custom_entry.insert(0, str(self.config.get("reminder_interval_minutes", 60)))

        def on_custom(e=None):
            try:
                v = int(custom_entry.get())
                if v > 0:
                    interval_var.set(v)
            except ValueError:
                pass

        custom_entry.bind("<KeyRelease>", on_custom)

        # Trigger
        tk.Label(body, text="Trigger Word", font=("Segoe UI", 10, "bold"),
                 bg="#2A2A2A", fg="#DDD").pack(anchor="w", pady=(0, 4))

        trigger_entry = tk.Entry(body, font=self.font_body, bg="#444", fg="#FFF",
                                 insertbackground="#FFF", relief="flat", width=18)
        trigger_entry.pack(anchor="w", pady=(0, 14))
        trigger_entry.insert(0, self.config.get("trigger_word", "@reminder"))

        # Buttons
        btn_frame = tk.Frame(settings, bg="#2A2A2A", pady=10, padx=20)
        btn_frame.pack(fill="x")

        def save_settings():
            val = interval_var.get()
            try:
                custom = int(custom_entry.get())
                if custom > 0:
                    val = custom
            except ValueError:
                pass

            self.config["reminder_interval_minutes"] = max(1, val)
            self.config["trigger_word"] = trigger_entry.get().strip() or "@reminder"
            save_config(self.config)
            settings.destroy()

        tk.Button(btn_frame, text="Save", font=self.font_btn,
                  bg="#FFD54F", fg="#1A1A1A", relief="flat", padx=18, pady=5,
                  command=save_settings, cursor="hand2").pack(side="right")

        tk.Button(btn_frame, text="Cancel", font=("Segoe UI", 10),
                  bg="#444", fg="#CCC", relief="flat", padx=14, pady=5,
                  command=settings.destroy, cursor="hand2").pack(side="right", padx=8)

    # ─── Reminder System ───

    def update_countdown(self):
        now = datetime.now()
        interval = self.config.get("reminder_interval_minutes", 60)
        if interval >= 60:
            m, s = 59 - now.minute, 59 - now.second
        else:
            mins_into = now.minute % interval
            m = interval - mins_into - 1
            s = 59 - now.second
        self.countdown_label.config(text=f"Next in {m:02d}:{s:02d}")
        self.root.after(1000, self.update_countdown)

    def check_reminder(self):
        now = datetime.now()
        interval = self.config.get("reminder_interval_minutes", 60)
        active = [n for n in self.notes if not n.get("done", False)]
        if now.minute % interval == 0 and now.second < 2 and active:
            self.show_reminder_popup()
        self.root.after(1000, self.check_reminder)

    def show_reminder_popup(self):
        try:
            winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
        except Exception:
            pass

        self.root.deiconify()
        self.root.lift()

        active_notes = [n for n in self.notes if not n.get("done", False)]

        popup = tk.Toplevel(self.root)
        popup.title("\U0001f514 Reminder")
        popup.configure(bg="#2A2A2A")
        popup.geometry("480x520+{}+{}".format(
            self.root.winfo_screenwidth() // 2 - 240,
            self.root.winfo_screenheight() // 2 - 260
        ))
        popup.attributes("-topmost", True)
        popup.focus_force()
        popup.lift()
        try:
            popup.grab_set()
        except Exception:
            pass
        popup.after(100, lambda: popup.attributes("-topmost", True))
        popup.after(200, lambda: popup.focus_force())

        # Header
        header = tk.Frame(popup, bg="#FFB300", pady=14, padx=20)
        header.pack(fill="x")
        tk.Label(header, text="\U0001f514 Reminder",
                 font=("Segoe UI", 16, "bold"), bg="#FFB300", fg="#1A1A1A").pack(anchor="w")
        tk.Label(header,
                 text=f"{datetime.now().strftime('%I:%M %p')} \u2014 {len(active_notes)} active notes",
                 font=("Segoe UI", 10), bg="#FFB300", fg="#333").pack(anchor="w", pady=(4, 0))

        # Scrollable notes
        pcontainer = tk.Frame(popup, bg="#2A2A2A")
        pcontainer.pack(fill="both", expand=True)

        pcanvas = tk.Canvas(pcontainer, bg="#2A2A2A", highlightthickness=0)
        pscroll = tk.Scrollbar(pcontainer, orient="vertical", command=pcanvas.yview)
        pframe = tk.Frame(pcanvas, bg="#2A2A2A")

        pframe.bind("<Configure>", lambda e: pcanvas.configure(scrollregion=pcanvas.bbox("all")))
        pcanvas.create_window((0, 0), window=pframe, anchor="nw")
        pcanvas.configure(yscrollcommand=pscroll.set)
        pcanvas.pack(side="left", fill="both", expand=True, padx=16, pady=12)
        pscroll.pack(side="right", fill="y")

        for note in active_notes:
            c = COLORS[note.get("color", 0) % len(COLORS)]
            is_mm = note.get("source") == "mattermost"

            nf = tk.Frame(pframe, bg=c["bg"], padx=14, pady=10, cursor="hand2")
            nf.pack(fill="x", pady=4)

            tk.Frame(nf, bg=c["header"], width=4).pack(side="left", fill="y", padx=(0, 10))

            content = tk.Frame(nf, bg=c["bg"])
            content.pack(side="left", fill="x", expand=True)

            if is_mm:
                sender = note.get("sender", "")
                tk.Label(content, text=f"\U0001f4e8 @{sender}", font=("Segoe UI", 8, "bold"),
                         bg=c["bg"], fg=c["header"]).pack(anchor="w")

            text = note.get("text", "").strip()[:150] or "(empty)"
            tk.Label(content, text=text, font=("Segoe UI", 11), bg=c["bg"],
                     fg=c["text"], wraplength=350, justify="left").pack(anchor="w")

            # Click to open in detail
            nid = note["id"]
            for widget in [nf, content]:
                widget.bind("<Button-1>", lambda e, n=nid, p=popup: (p.destroy(), self.open_note(n)))

        # Button
        btn_frame = tk.Frame(popup, bg="#2A2A2A", pady=12)
        btn_frame.pack(fill="x")
        tk.Button(btn_frame, text="Got it!", font=self.font_btn,
                  bg="#FFD54F", fg="#1A1A1A", relief="flat", padx=24, pady=6,
                  command=popup.destroy, cursor="hand2").pack()

    def run(self):
        self.root.mainloop()


# ─── Main ───
if __name__ == "__main__":
    app = StickyNotesApp()
    if "--reminder" in sys.argv:
        app.root.after(500, app.show_reminder_popup)
    app.run()
