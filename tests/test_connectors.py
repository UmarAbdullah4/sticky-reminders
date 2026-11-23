"""Protocol tests for the connectors against simulated services.

No real service is contacted and no credentials are used: each connector is
pointed at an HTTP + websocket server on 127.0.0.1 that speaks the documented
wire format. This proves the code handles that format; it cannot prove the
real services behave as documented.
"""
import asyncio
import importlib.machinery
import importlib.util
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from queue import Empty, Queue
from urllib.parse import parse_qs, urlparse

import websockets

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "out_conn")
os.makedirs(OUT, exist_ok=True)

loader = importlib.machinery.SourceFileLoader("sticky_conn_test", sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(HERE), "sticky_reminders.pyw"))
spec = importlib.util.spec_from_loader(loader.name, loader)
mod = importlib.util.module_from_spec(spec)
loader.exec_module(mod)
mod.log.handlers.clear()
import logging
logfile = os.path.join(OUT, "connectors.log")
open(logfile, "w").close()
h = logging.FileHandler(logfile, encoding="utf-8")
h.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
mod.log.addHandler(h)

results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond)))
    print(("PASS  " if cond else "FAIL  ") + name + (f"   [{detail}]" if detail and not cond else ""))


# ───────────────────────── simulated servers ─────────────────────────

class Http:
    """Records every request; `route(method, path, headers, body)` answers."""

    def __init__(self, route):
        self.calls = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def _do(self):
                raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                ctype = self.headers.get("Content-Type", "")
                if "json" in ctype:
                    body = json.loads(raw or b"{}")
                elif "form" in ctype:
                    body = {k: v[0] for k, v in parse_qs(raw.decode()).items()}
                else:
                    body = raw.decode()
                path = urlparse(self.path).path
                call = {"method": self.command, "path": path, "body": body,
                        "headers": {k.lower(): v for k, v in self.headers.items()},
                        "ctype": ctype}
                outer.calls.append(call)
                status, payload, extra = route(call)
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                for k, v in (extra or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = _do

            def log_message(self, *a):
                pass

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.srv.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def named(self, fragment):
        return [c for c in self.calls if fragment in c["path"]]

    def close(self):
        self.srv.shutdown()


class Ws:
    """A websocket server. `script(ws, self)` runs for each connection."""

    def __init__(self, script):
        self.script = script
        self.conns = []
        self.got = []                     # every JSON frame clients sent
        self.ready = threading.Event()
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self._run, daemon=True).start()
        self.ready.wait(5)

    def _run(self):
        asyncio.set_event_loop(self.loop)

        async def handler(ws):
            self.conns.append(ws)
            try:
                await self.script(ws, self)
            except websockets.ConnectionClosed:
                pass

        async def main():
            self.server = await websockets.serve(handler, "127.0.0.1", 0)
            self.port = self.server.sockets[0].getsockname()[1]
            self.url = f"ws://127.0.0.1:{self.port}"
            self.ready.set()
            await asyncio.Future()

        try:
            self.loop.run_until_complete(main())
        except Exception:
            pass

    def send(self, obj, conn=-1):
        fut = asyncio.run_coroutine_threadsafe(self.conns[conn].send(json.dumps(obj)), self.loop)
        fut.result(5)

    def close_conn(self, code=1000, conn=-1):
        asyncio.run_coroutine_threadsafe(self.conns[conn].close(code, "test"), self.loop).result(5)


async def pump_frames(ws, srv, on_frame=None):
    async for raw in ws:
        msg = json.loads(raw)
        srv.got.append(msg)
        if on_frame:
            await on_frame(msg)


def wait(pred, secs=6.0):
    end = time.time() + secs
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.03)
    return False


def drain(q):
    out = []
    while True:
        try:
            out.append(q.get_nowait())
        except Empty:
            return out


class Box:
    """Collects what a connector reports."""

    def __init__(self):
        self.q = Queue()
        self.status, self.notes = [], []

    def pull(self):
        for m in drain(self.q):
            (self.status if m["type"] == "status" else self.notes).append(m)

    def last_status(self):
        self.pull()
        return self.status[-1] if self.status else {"text": "", "ok": None}

    def wait_status(self, fragment, secs=6.0):
        return wait(lambda: fragment in self.last_status()["text"], secs)

    def wait_notes(self, n, secs=5.0):
        def ok():
            self.pull()
            return len(self.notes) >= n
        return wait(ok, secs)

    def settle(self, secs=0.5):
        time.sleep(secs)
        self.pull()


CFG = {"trigger_word": "@reminder"}

# ───────────────────────────── helpers ─────────────────────────────

c = mod.Connector(CFG, Queue())
check("mention matches whole names only",
      c.mentions("ping @sam now", "@sam") and c.mentions("thanks @sam.", "@sam")
      and not c.mentions("ping @samantha", "@sam") and not c.mentions("ping @sam.lee", "@sam")
      and not c.mentions("ping @sam-2", "@sam"))
check("clean strips trigger (any case, every time) and own mention",
      c.clean("@Reminder  call @sam about @REMINDER x", "@sam") == "call about x",
      repr(c.clean("@Reminder  call @sam about @REMINDER x", "@sam")))
check("clean leaves a longer name alone", c.clean("@reminder ask @samantha", "@sam") == "ask @samantha")
check("priority markers", [c.priority_from(x) for x in ("a !urgent", "#HIGH b", "c !low", "d")] == [3, 2, 0, 1])
check("done text, mine vs someone's",
      c.done_text({"text": "x", "is_self": True}) == "✅ **Done:** x"
      and c.done_text({"text": "x", "is_self": False}) == "✅ **Done** (completed by me): x")

# ───────────────────────────── Mattermost ─────────────────────────────
print("\n-- Mattermost --")
mm_state = {"logins": 0, "fail_first_ws": False, "login_status": 200}


def mm_route(call):
    if call["path"].endswith("/users/login"):
        mm_state["logins"] += 1
        if mm_state["login_status"] != 200:
            return mm_state["login_status"], {"id": "api.user.login.invalid_credentials_email_username",
                                              "message": "Enter a valid email or username and/or password."}, None
        return 200, {"id": "me1", "username": "alex"}, {"Token": f"tok{mm_state['logins']}"}
    if call["path"].endswith("/users/me"):
        if call["headers"].get("authorization") != "Bearer pat-123":
            return 401, {"message": "Invalid or expired session, please login again."}, None
        return 200, {"id": "me1", "username": "alex"}, None
    if call["path"].endswith("/posts"):
        return 201, {"id": "newpost"}, None
    return 404, {"message": "nope"}, None


async def mm_script(ws, srv):
    first = json.loads(await ws.recv())
    srv.got.append(first)
    if mm_state["fail_first_ws"]:
        mm_state["fail_first_ws"] = False
        await ws.send(json.dumps({"status": "FAIL", "seq_reply": 1,
                                  "error": {"id": "api.web_socket_router.bad_seq.app_error"}}))
        await ws.close()
        return
    await ws.send(json.dumps({"status": "OK", "seq_reply": 1}))
    await ws.send(json.dumps({"event": "hello", "data": {}}))
    await pump_frames(ws, srv)


mm_http, mm_ws = Http(mm_route), Ws(mm_script)
asked = []


class MM(mod.MattermostConnector):
    def open(self, url, **kw):
        asked.append(url)
        return super().open(mm_ws.url, **kw)      # same host in real life; two test servers here


def mm_post(text, user="me1", pid="p1", root="", sender="@alex", chan="Town Square"):
    mm_ws.send({"event": "posted", "data": {
        "sender_name": sender, "channel_display_name": chan,
        "post": json.dumps({"id": pid, "user_id": user, "channel_id": "ch1",
                            "message": text, "root_id": root})}})


box = Box()
mm = MM({**CFG, "mattermost_url": mm_http.url + "/", "username": "alex@example.com",
         "password": "pw"}, box.q)
check("configured() needs url + credentials",
      MM.configured({"mattermost_url": "http://x", "username": "a", "password": "b"})
      and MM.configured({"mattermost_url": "http://x", "access_token": "t"})
      and not MM.configured({"mattermost_url": "http://x", "username": "a"})
      and not MM.configured({"username": "a", "password": "b"}))
t0 = time.time()
mm.start()
check("start() returns at once (login is off the UI thread)", time.time() - t0 < 0.2)
check("connects and reports who it is", box.wait_status("Connected as @alex") and box.last_status()["ok"] is True,
      str(box.last_status()))
login = mm_http.named("/users/login")[0]
check("password login sent as JSON", login["body"] == {"login_id": "alex@example.com", "password": "pw"})
check("websocket address derived from the server URL (plain ws for http)",
      asked and asked[0] == f"ws://127.0.0.1:{mm_http.port}/api/v4/websocket", str(asked))
check("auth challenge carries the session token",
      mm_ws.got[0] == {"seq": 1, "action": "authentication_challenge", "data": {"token": "tok1"}}, str(mm_ws.got[:1]))

mm_post("@reminder ship the build !high", pid="p1")
check("my own message becomes a note", box.wait_notes(1))
n = box.notes[0]
check("note fields", (n["text"], n["is_self"], n["priority"], n["channel"], n["sender"], n["post_id"],
                      n["thread_id"], n["source"]) ==
      ("ship the build !high", True, 2, "Town Square", "alex", "p1", "p1", "mattermost"), str(n))
mm_post("hey @alex @reminder review PR", user="other", pid="p2", sender="@sara")
mm_post("@reminder @alex_morgan look", user="other", pid="p3", sender="@sara")
mm_post("@reminder nobody in particular", user="other", pid="p4", sender="@sara")
mm_post("thanks @alex. @reminder cake", user="other", pid="p5", sender="@sara")
mm_post("no trigger word here", pid="p6")
mm_post("@reminder reply inside a thread", pid="p7", root="rootpost")
box.wait_notes(4)
box.settle()
texts = [x["text"] for x in box.notes]
check("mention of me counts; longer names and no-mention do not",
      texts == ["ship the build !high", "hey review PR", "thanks . cake", "reply inside a thread"], str(texts))
check("someone else's message is not 'self'", box.notes[1]["is_self"] is False and box.notes[1]["sender"] == "sara")
check("thread reply remembers the thread root", box.notes[3]["thread_id"] == "rootpost" and box.notes[3]["post_id"] == "p7")
ok = mm.reply({"channel_id": "ch1", "post_id": "p7", "thread_id": "rootpost"}, "✅ **Done:** x")
post = mm_http.named("/posts")[-1]
check("reply goes to the thread root with the session token",
      ok and post["body"] == {"channel_id": "ch1", "message": "✅ **Done:** x", "root_id": "rootpost"}
      and post["headers"]["authorization"] == "Bearer tok1", str(post))
check("old notes without thread_id still reply to their post",
      mm.reply({"channel_id": "ch1", "post_id": "p1"}, "x") and mm_http.named("/posts")[-1]["body"]["root_id"] == "p1")

t0 = time.time()
mm.stop()
check("stop() ends the thread promptly", wait(lambda: not mm.worker.is_alive(), 3), f"{time.time() - t0:.1f}s")
box.settle(0.3)
n_status = len(box.status)
box.settle(0.5)
check("a stopped connector reports nothing more", len(box.status) == n_status)

# session refused -> signs in again
mm_state["logins"], mm_state["fail_first_ws"] = 0, True
box = Box()
mm = MM({**CFG, "mattermost_url": mm_http.url, "username": "u", "password": "pw"}, box.q)
mm.start()
check("refused session triggers a fresh sign-in", box.wait_status("Connected as @alex", 9) and mm_state["logins"] == 2,
      f"logins={mm_state['logins']} {box.last_status()}")
mm.stop()

# wrong password -> stop, do not hammer the server
mm_state["logins"], mm_state["login_status"] = 0, 401
box = Box()
mm = MM({**CFG, "mattermost_url": mm_http.url, "username": "u", "password": "bad"}, box.q)
mm.start()
check("wrong password is reported", box.wait_status("Login failed") and box.last_status()["ok"] is False,
      str(box.last_status()))
time.sleep(4)
check("...and not retried (no account lockout)", mm_state["logins"] == 1 and not mm.worker.is_alive(),
      f"logins={mm_state['logins']}")
mm_state["login_status"] = 200

# token mode + test()
check("test() with a personal access token",
      MM.test({**CFG, "mattermost_url": mm_http.url, "access_token": "pat-123"}) == "Signed in as @alex")
try:
    MM.test({**CFG, "mattermost_url": mm_http.url, "access_token": "wrong"})
    check("bad token raises", False)
except mod.ConnectorError as e:
    check("bad token raises ConnectorError", "Token rejected" in str(e), str(e))
try:
    MM.test({**CFG, "mattermost_url": "mattermost.example.com", "access_token": "x"})
    check("URL without scheme rejected", False)
except mod.ConnectorError as e:
    check("URL without scheme rejected", "https://" in str(e))

# ───────────────────────────── Slack ─────────────────────────────
print("\n-- Slack --")
sl_state = {"auth_error": "", "opens": 0}


def sl_route(call):
    m = call["path"].rsplit("/", 1)[-1]
    if m == "auth.test":
        if sl_state["auth_error"]:
            return 200, {"ok": False, "error": sl_state["auth_error"]}, None
        return 200, {"ok": True, "url": "https://acme.slack.com/", "team": "Acme", "user": "stickybot",
                     "team_id": "T1", "user_id": "UBOT", "bot_id": "B1"}, None
    if m == "apps.connections.open":
        sl_state["opens"] += 1
        return 200, {"ok": True, "url": sl_ws.url + "/link/?ticket=1&app_id=2"}, None
    if m == "users.info":
        names = {"UME": {"real_name": "Alex M", "profile": {"display_name": "alex"}},
                 "U2": {"real_name": "Bob Stone", "profile": {"display_name": ""}}}
        u = names.get(call["body"].get("user"))
        return 200, ({"ok": True, "user": u} if u else {"ok": False, "error": "user_not_found"}), None
    if m == "conversations.info":
        return 200, {"ok": True, "channel": {"id": call["body"].get("channel"), "name": "launch"}}, None
    if m == "chat.postMessage":
        return 200, {"ok": True, "ts": "9.9"}, None
    return 200, {"ok": False, "error": "unknown_method"}, None


async def sl_script(ws, srv):
    await ws.send(json.dumps({"type": "hello", "num_connections": 1,
                              "connection_info": {"app_id": "A1"}}))
    await pump_frames(ws, srv)


sl_http, sl_ws = Http(sl_route), Ws(sl_script)


class SL(mod.SlackConnector):
    API = sl_http.url


env_no = [0]


def sl_event(ev, kind="events_api"):
    env_no[0] += 1
    sl_ws.send({"envelope_id": f"env{env_no[0]}", "type": kind, "accepts_response_payload": False,
                "payload": {"type": "event_callback", "team_id": "T1", "event": ev}})
    return f"env{env_no[0]}"


def sl_msg(text, user="UME", ts="1.1", **kw):
    return {"type": "message", "channel": "C1", "user": user, "text": text, "ts": ts,
            "channel_type": "channel", **kw}


SLCFG = {**CFG, "slack_app_token": "xapp-1", "slack_bot_token": "xoxb-1", "slack_user_id": "ume"}
box = Box()
sl = SL(SLCFG, box.q)
check("configured() needs both tokens",
      SL.configured(SLCFG) and not SL.configured({"slack_app_token": "x"}))
sl.start()
check("connects and names the workspace", box.wait_status("Connected to Acme") and box.last_status()["ok"] is True,
      str(box.last_status()))
a, o = sl_http.named("auth.test")[0], sl_http.named("apps.connections.open")[0]
check("bot token for auth.test, app token to open the socket",
      a["headers"]["authorization"] == "Bearer xoxb-1" and o["headers"]["authorization"] == "Bearer xapp-1"
      and a["method"] == "POST" and o["method"] == "POST")

e1 = sl_event(sl_msg("<@UME> @reminder ship it with <@U2> &amp; co <https://x.y/z|the site> in <#C9|ops>", ts="10.1"))
check("my message becomes a note", box.wait_notes(1))
check("every envelope is acknowledged", wait(lambda: {"envelope_id": e1} in sl_ws.got), str(sl_ws.got))
n = box.notes[0]
check("Slack markup turned into readable text",
      n["text"] == "ship it with @Bob Stone & co the site in #ops", repr(n["text"]))
check("names resolved, form-encoded lookups",
      n["sender"] == "alex" and n["channel"] == "launch"
      and sl_http.named("users.info")[0]["body"] == {"user": "U2"}
      and "form" in sl_http.named("conversations.info")[0]["ctype"], str(n))
check("note identity", (n["post_id"], n["thread_id"], n["channel_id"], n["is_self"], n["source"]) ==
      ("10.1", "10.1", "C1", True, "slack"), str(n))

sl_event({"type": "app_mention", "channel": "C1", "user": "U2", "text": "<@UBOT> @reminder twice", "ts": "11.1"})
sl_event(sl_msg("<@UBOT> @reminder twice", user="U2", ts="11.1"))
sl_event(sl_msg("@reminder not for anyone", user="U2", ts="12.1"))
sl_event(sl_msg("@reminder edited", ts="13.1", subtype="message_changed"))
sl_event(sl_msg("@reminder from a bot", ts="14.1", bot_id="B7"))
sl_event(sl_msg("no trigger", ts="15.1"))
sl_event(sl_msg("@reminder in a thread", ts="16.2", thread_ts="16.0"))
sl_event({"type": "message", "channel": "D1", "user": "U2", "text": "@reminder call me",
          "ts": "17.1", "channel_type": "im"})
e_other = sl_event({"command": "/x"}, kind="slash_commands")
box.wait_notes(4)
box.settle(0.6)
texts = [(x["text"], x["is_self"], x["channel"]) for x in box.notes]
check("mention of the app counts once; edits, bots, strangers and no-trigger are ignored",
      texts == [("ship it with @Bob Stone & co the site in #ops", True, "launch"),
                ("twice", False, "launch"), ("in a thread", True, "launch"),
                ("call me", False, "direct message")], str(texts))
check("thread_ts kept for replies", box.notes[2]["thread_id"] == "16.0")
check("non-event envelopes are acknowledged too", {"envelope_id": e_other} in sl_ws.got)

ok = sl.reply(box.notes[2], sl.done_text(box.notes[2]))
pm = sl_http.named("chat.postMessage")[-1]
check("reply posts into the thread, Slack-style bold",
      ok and pm["body"] == {"channel": "C1", "text": "✅ *Done:* in a thread", "thread_ts": "16.0"}
      and pm["headers"]["authorization"] == "Bearer xoxb-1", str(pm["body"]))

opens = sl_state["opens"]
sl_ws.send({"type": "disconnect", "reason": "refresh_requested"})
check("reconnects when Slack recycles the connection",
      wait(lambda: sl_state["opens"] == opens + 1 and len(sl_ws.conns) >= 2, 8), f"opens={sl_state['opens']}")
box.wait_status("Connected to Acme", 6)
sl_ws.send({"type": "disconnect", "reason": "link_disabled"})
check("Socket Mode switched off is reported, not retried",
      box.wait_status("Socket Mode is switched off") and wait(lambda: not sl.worker.is_alive(), 3))

sl_state["auth_error"] = "invalid_auth"
box = Box()
sl = SL(SLCFG, box.q)
sl.start()
check("bad token reported", box.wait_status("Slack rejected the bot token (invalid_auth)")
      and box.last_status()["ok"] is False, str(box.last_status()))
sl_state["auth_error"] = ""
check("test() describes the connection", SL.test(SLCFG) == "Connected to Acme as stickybot")

# without a member ID only the app's own mentions / DMs count
box = Box()
sl = SL({**SLCFG, "slack_user_id": ""}, box.q)
sl.start()
box.wait_status("Connected to Acme")
sl_event(sl_msg("@reminder mine but unknown", user="UME", ts="20.1"))
sl_event(sl_msg("<@UBOT> @reminder addressed to the app", user="UME", ts="21.1"))
box.wait_notes(1)
box.settle(0.5)
check("without a member ID, only mentions of the app count",
      [x["text"] for x in box.notes] == ["addressed to the app"] and box.notes[0]["is_self"] is False,
      str([x["text"] for x in box.notes]))
sl.stop()

# ───────────────────────────── Discord ─────────────────────────────
print("\n-- Discord --")
dc_state = {"ack": True, "close_after_identify": 0, "beats": 0}


def dc_route(call):
    if call["path"].endswith("/users/@me"):
        if call["headers"].get("authorization") != "Bot good-token":
            return 401, {"message": "401: Unauthorized", "code": 0}, None
        return 200, {"id": "9001", "username": "sticky"}, None
    if "/messages" in call["path"]:
        return 200, {"id": "m9"}, None
    if call["path"].endswith("/channels/C404"):
        return 200, {"id": "C404", "name": "looked-up"}, None
    return 404, {"message": "Unknown"}, None


async def dc_script(ws, srv):
    await ws.send(json.dumps({"op": 10, "d": {"heartbeat_interval": 300}}))

    async def on_frame(msg):
        if msg["op"] == 2:
            if dc_state["close_after_identify"]:
                await ws.close(dc_state["close_after_identify"], "nope")
                return
            await ws.send(json.dumps({"op": 0, "t": "READY", "s": 1, "d": {
                "v": 10, "user": {"id": "9001", "username": "sticky"}, "session_id": "s1",
                "resume_gateway_url": "wss://x"}}))
            await ws.send(json.dumps({"op": 0, "t": "GUILD_CREATE", "s": 2, "d": {
                "id": "G1", "name": "Guild", "channels": [{"id": "C1", "name": "general"}]}}))
        elif msg["op"] == 1:
            dc_state["beats"] += 1
            if dc_state["ack"]:
                await ws.send(json.dumps({"op": 11}))

    await pump_frames(ws, srv, on_frame)


dc_http, dc_ws = Http(dc_route), Ws(dc_script)


class DC(mod.DiscordConnector):
    API = dc_http.url
    GATEWAY = dc_ws.url + "/?v=10&encoding=json"


seq = [10]


def dc_msg(content, author="1001", mid="m1", guild="G1", channel="C1", mentions=(), bot=False, name="Alex"):
    seq[0] += 1
    d = {"id": mid, "channel_id": channel, "content": content,
         "author": {"id": author, "username": name.lower(), "global_name": name, "bot": bot},
         "mentions": list(mentions)}
    if guild:
        d["guild_id"] = guild
    dc_ws.send({"op": 0, "t": "MESSAGE_CREATE", "s": seq[0], "d": d})


DCCFG = {**CFG, "discord_bot_token": "good-token", "discord_user_id": "1001"}
box = Box()
dc = DC(DCCFG, box.q)
dc.start()
check("connects and names the bot", box.wait_status("Connected as sticky") and box.last_status()["ok"] is True,
      str(box.last_status()))
ident = next(m for m in dc_ws.got if m["op"] == 2)["d"]
check("identify payload", ident["token"] == "good-token" and ident["intents"] == 37377
      and set(ident["properties"]) == {"os", "browser", "device"}, str(ident))
check("heartbeats flow and carry the last sequence number",
      wait(lambda: dc_state["beats"] >= 3, 4) and [m for m in dc_ws.got if m["op"] == 1][-1]["d"] is not None,
      f"beats={dc_state['beats']}")

bob = {"id": "222", "username": "bob", "global_name": "Bob"}
dc_msg("@reminder deploy with <@222> and <@!9001>", mentions=[bob, {"id": "9001", "username": "sticky"}], mid="m1")
check("my message becomes a note", box.wait_notes(1))
n = box.notes[0]
check("mentions rewritten, channel named from the guild listing",
      (n["text"], n["channel"], n["sender"], n["is_self"], n["post_id"], n["channel_id"], n["source"]) ==
      ("deploy with @Bob and", "general", "Alex", True, "m1", "C1", "discord"), str(n))
dc_msg("<@9001> @reminder for the bot", author="222", name="Bob", mentions=[{"id": "9001", "username": "sticky"}], mid="m2")
dc_msg("<@1001> @reminder for the human", author="222", name="Bob", mentions=[{"id": "1001", "username": "alex"}], mid="m3")
dc_msg("@reminder for nobody", author="222", name="Bob", mid="m4")
dc_msg("@reminder from a bot", author="333", bot=True, mid="m5")
dc_msg("@reminder dm me", author="222", name="Bob", guild="", channel="D9", mid="m6")
dc_msg("no trigger", mid="m7")
dc_msg("@reminder unknown channel", channel="C404", mid="m8")
box.wait_notes(5)
box.settle(0.6)
got = [(x["text"], x["is_self"], x["channel"]) for x in box.notes]
check("mentions of the bot or of me and DMs count; bots, strangers and no-trigger do not",
      got == [("deploy with @Bob and", True, "general"), ("for the bot", False, "general"),
              ("for the human", False, "general"), ("dm me", False, "direct message"),
              ("unknown channel", True, "looked-up")], str(got))

ok = dc.reply(box.notes[0], dc.done_text(box.notes[0]))
pm = dc_http.named("/channels/C1/messages")[-1]
check("reply references the message and pings nobody",
      ok and pm["body"] == {"content": "✅ **Done:** deploy with @Bob and",
                            "allowed_mentions": {"parse": []},
                            "message_reference": {"message_id": "m1", "fail_if_not_exists": False}}, str(pm["body"]))
check("REST calls carry Bot auth and Discord's User-Agent shape",
      pm["headers"]["authorization"] == "Bot good-token"
      and pm["headers"]["user-agent"].startswith("DiscordBot (") and pm["method"] == "POST",
      pm["headers"].get("user-agent"))

conns = len(dc_ws.conns)
dc_state["ack"] = False
check("an unacknowledged heartbeat makes it reconnect", wait(lambda: len(dc_ws.conns) > conns, 9),
      f"conns {conns} -> {len(dc_ws.conns)}")
dc_state["ack"] = True
box.wait_status("Connected as sticky", 6)
conns = len(dc_ws.conns)
dc_ws.send({"op": 7, "d": None})
check("op 7 (reconnect) is obeyed", wait(lambda: len(dc_ws.conns) > conns, 9))
dc.stop()
check("stop() ends the thread", wait(lambda: not dc.worker.is_alive(), 3))

for code, expect in ((4004, "Discord rejected the bot token"), (4014, "Message Content Intent")):
    dc_state["close_after_identify"] = code
    box = Box()
    dc = DC(DCCFG, box.q)
    dc.start()
    check(f"close code {code} is explained and not retried",
          box.wait_status(expect) and box.last_status()["ok"] is False
          and wait(lambda: not dc.worker.is_alive(), 3), str(box.last_status()))
dc_state["close_after_identify"] = 0
check("test() validates the token", DC.test(DCCFG) == "Bot 'sticky' is valid")
try:
    DC.test({**DCCFG, "discord_bot_token": "bad"})
    check("bad token raises", False)
except mod.ConnectorError as e:
    check("bad Discord token raises ConnectorError", "rejected" in str(e))

# ───────────────────────────── Telegram ─────────────────────────────
print("\n-- Telegram --")
TOKEN = "123456:SECRET-TOKEN"
tg_state = {"queue": [], "mode": "ok", "offsets": []}


def tg_route(call):
    if f"/bot{TOKEN}/" not in call["path"]:
        return 401, {"ok": False, "error_code": 401, "description": "Unauthorized"}, None
    m = call["path"].rsplit("/", 1)[-1]
    if m == "getMe":
        return 200, {"ok": True, "result": {"id": 1, "is_bot": True, "first_name": "Sticky",
                                            "username": "stickybot"}}, None
    if m == "getUpdates":
        tg_state["offsets"].append(call["body"].get("offset"))
        if tg_state["mode"] == "conflict":
            return 409, {"ok": False, "error_code": 409,
                         "description": "Conflict: terminated by other getUpdates request"}, None
        for _ in range(10):
            if tg_state["queue"]:
                break
            time.sleep(0.03)
        ups, tg_state["queue"] = tg_state["queue"], []
        return 200, {"ok": True, "result": ups}, None
    if m == "sendMessage":
        return 200, {"ok": True, "result": {"message_id": 99}}, None
    return 404, {"ok": False, "error_code": 404, "description": "Not Found"}, None


tg_http = Http(tg_route)


class TG(mod.TelegramConnector):
    API = tg_http.url
    POLL = 1


uid = [100]


def tg_msg(text, sender=777, chat=None, kind="private", title=None, username="alex_m", mid=None):
    uid[0] += 1
    chat = chat if chat is not None else sender
    m = {"message_id": mid or uid[0], "date": 0, "text": text,
         "from": {"id": sender, "is_bot": False, "first_name": "Alex" if sender == 777 else "Sam",
                  "username": username if sender == 777 else "sam"},
         "chat": {"id": chat, "type": kind}}
    if title:
        m["chat"]["title"] = title
    tg_state["queue"].append({"update_id": uid[0], "message": m})
    return uid[0]


box = Box()
tg = TG({**CFG, "telegram_bot_token": TOKEN}, box.q)        # not paired yet
tg.start()
check("unpaired bot asks to be messaged", box.wait_status("Message @stickybot to get your user ID")
      and box.last_status()["ok"] is None, str(box.last_status()))
tg_msg("hello?")
check("first contact is answered with the sender's ID",
      wait(lambda: tg_http.named("sendMessage")) and "777" in tg_http.named("sendMessage")[0]["body"]["text"]
      and tg_http.named("sendMessage")[0]["body"]["chat_id"] == 777)
tg_msg("again")
box.settle(0.8)
check("...once, and nothing becomes a note before pairing",
      len(tg_http.named("sendMessage")) == 1 and box.notes == [])
tg.stop()
wait(lambda: not tg.worker.is_alive(), 4)

tg_http.calls.clear()
tg_state["offsets"].clear()
box = Box()
tg = TG({**CFG, "telegram_bot_token": TOKEN, "telegram_user_id": "777"}, box.q)
tg.start()
check("paired bot connects", box.wait_status("Connected as @stickybot") and box.last_status()["ok"] is True)
first = tg_msg("buy milk !urgent")
check("a private message from me becomes a note, no trigger needed", box.wait_notes(1))
n = box.notes[0]
check("note fields", (n["text"], n["is_self"], n["channel"], n["sender"], n["channel_id"], n["priority"],
                      n["source"], n["post_id"]) ==
      ("buy milk !urgent", True, "direct message", "Alex", "777", 3, "telegram", str(first)), str(n))
check("updates are confirmed by asking for the next offset",
      wait(lambda: (first + 1) in tg_state["offsets"], 4), str(tg_state["offsets"][-4:]))
tg_msg("/start")
tg_msg("stranger danger", sender=555)
tg_msg("group chatter", chat=-5, kind="supergroup", title="Team")
tg_msg("@reminder book the venue @stickybot", chat=-5, kind="supergroup", title="Team")
tg_msg("@reminder @alex_m please review", sender=555, chat=-5, kind="supergroup", title="Team")
tg_msg("@reminder not addressed", sender=555, chat=-5, kind="supergroup", title="Team")
box.wait_notes(3)
box.settle(0.8)
got = [(x["text"], x["is_self"], x["channel"]) for x in box.notes]
check("groups need the trigger; strangers need to mention me; DMs from strangers never count",
      got == [("buy milk !urgent", True, "direct message"), ("book the venue", True, "Team"),
              ("please review", False, "Team")], str(got))
sent = [c["body"]["text"] for c in tg_http.named("sendMessage")]
check("/start is greeted, a stranger is told the bot is private",
      any("becomes a note" in s for s in sent) and any("555" in s and "private" in s for s in sent), str(sent))
ok = tg.reply(box.notes[1], tg.done_text(box.notes[1]))
sm = tg_http.named("sendMessage")[-1]["body"]
check("reply quotes the original message, plain text",
      ok and sm["chat_id"] == "-5" and sm["text"] == "✅ Done: book the venue"
      and sm["reply_parameters"]["message_id"] == int(box.notes[1]["post_id"])
      and sm["reply_parameters"]["allow_sending_without_reply"] is True, str(sm))
tg.stop()
check("stop() ends the polling thread", wait(lambda: not tg.worker.is_alive(), 4))

box = Box()
tg = TG({**CFG, "telegram_bot_token": "999:WRONG", "telegram_user_id": "777"}, box.q)
tg.start()
check("bad token reported, not retried", box.wait_status("Telegram rejected the bot token")
      and wait(lambda: not tg.worker.is_alive(), 3), str(box.last_status()))

tg_state["mode"] = "conflict"
box = Box()
tg = TG({**CFG, "telegram_bot_token": TOKEN, "telegram_user_id": "777"}, box.q)
tg._nap = lambda s: time.sleep(0.05)
tg.start()
check("a second reader of the same bot is diagnosed", box.wait_status("Something else is reading this bot", 6),
      str(box.last_status()))
tg_state["mode"] = "ok"
check("test() validates the token", TG.test({**CFG, "telegram_bot_token": TOKEN}) == "Bot @stickybot is valid")

box.pull()
everything = " ".join(m["text"] for m in box.status) + open(logfile, encoding="utf-8").read()
check("the bot token never appears in a status message or the log",
      TOKEN not in everything and "SECRET-TOKEN" not in everything)

# ───────────────────────── unreachable server ─────────────────────────
print("\n-- network failure --")
box = Box()
dead = MM({**CFG, "mattermost_url": "http://127.0.0.1:9", "username": "u", "password": "p"}, box.q)
mod.MattermostConnector.open  # (unchanged class used below for a pure failure path)
dead.start()
check("an unreachable server is retried with back-off, not treated as fatal",
      box.wait_status("Disconnected - retrying in 3s", 8) and dead.worker.is_alive(), str(box.last_status()))
dead.stop()
check("...and stops cleanly while waiting to retry", wait(lambda: not dead.worker.is_alive(), 3))

# ───────────────────────── assistant hooks ─────────────────────────
# With the assistant's "suggest notes" switched on, connectors also pass on
# messages addressed to the user that carry no trigger word, and they can
# fetch the conversation around a message.
print("\n-- untagged messages and thread history --")


def kinds(box):
    box.pull()
    return [(m["type"], m["text"]) for m in box.notes]


# Mattermost: what is offered
mm_state.update(logins=0, fail_first_ws=False, login_status=200)
box = Box()
mm = MM({**CFG, "mattermost_url": mm_http.url, "username": "u", "password": "pw"}, box.q)
mm.start()
box.wait_status("Connected as @alex")


def mm_raw(text, user="other", pid="x1", ctype="O", **post_extra):
    mm_ws.send({"event": "posted", "data": {
        "sender_name": "@sara", "channel_display_name": "Town Square", "channel_type": ctype,
        "post": json.dumps({"id": pid, "user_id": user, "channel_id": "ch1", "message": text,
                            "root_id": "", **post_extra})}})


mm_raw("hey @alex can you check the invoice numbers?", pid="c1")
box.settle(0.5)
check("assistant off: an untagged message is ignored, as before", kinds(box) == [])
mm.capture = True
mm_raw("hey @alex can you check the invoice numbers?", pid="c2")
mm_raw("could you send me the contract draft please", pid="c3", ctype="D")
mm_raw("general chatter, nobody is addressed here", pid="c4")
mm_raw("@alex thinking out loud, this is my own post", user="me1", pid="c5")
mm_raw("@alex joined the channel.", pid="c6", type="system_join_channel")
mm_raw("@alex alert raised by a monitoring webhook", pid="c7", props={"from_webhook": "true"})
mm_raw("@alex ok", pid="c8")
mm_raw("@reminder @alex tagged ones still become notes", pid="c9")
box.wait_notes(3)
box.settle(0.6)
check("assistant on: mentions and DMs from others are offered; my own, system, webhook, "
      "tiny and unaddressed posts are not; tagged posts are still notes",
      kinds(box) == [("candidate", "hey can you check the invoice numbers?"),
                     ("candidate", "could you send me the contract draft please"),
                     ("reminder", "tagged ones still become notes")], str(kinds(box)))
cand = box.notes[0]
check("an offered message says where it came from, so a note can reply there",
      (cand["sender"], cand["channel"], cand["channel_id"], cand["post_id"], cand["thread_id"],
       cand["source"]) == ("sara", "Town Square", "ch1", "c2", "c2", "mattermost"), str(cand))
mm.stop()

# Mattermost: thread history
th = {"single": False}
POSTS = {"a": {"id": "a", "user_id": "u1", "message": "Numbers are in", "create_at": 1},
         "b": {"id": "b", "user_id": "u2", "message": "Great, report by Thursday?", "create_at": 2},
         "s": {"id": "s", "user_id": "u1", "message": "sam joined", "create_at": 3,
               "type": "system_join_channel"}}


def th_route(call):
    p = call["path"]
    if p.endswith("/posts/root1/thread"):
        return 200, {"order": ["b", "a"],
                     "posts": {"b": POSTS["b"]} if th["single"] else POSTS}, None
    if p.endswith("/channels/ch1/posts"):
        return 200, {"order": ["z"], "posts": {"z": {"id": "z", "user_id": "u1",
                                                     "message": "Earlier context", "create_at": 0}}}, None
    if p.endswith("/users/ids"):
        return 200, [{"id": "u1", "username": "sam"}, {"id": "u2", "username": "alex"}], None
    return 404, {"message": "nope"}, None


th_http = Http(th_route)
c = mod.MattermostConnector({**CFG, "mattermost_url": th_http.url, "access_token": "t"}, Queue())
c.token = "t"
note = {"thread_id": "root1", "post_id": "b", "channel_id": "ch1"}
check("Mattermost thread: oldest first, names resolved, system posts dropped",
      c.thread(note) == [("sam", "Numbers are in"), ("alex", "Great, report by Thursday?")],
      str(c.thread(note)))
check("names are looked up in one call, ids sent as a JSON list",
      th_http.named("/users/ids")[-1]["body"] == ["u1", "u2"]
      and th_http.named("/users/ids")[-1]["headers"]["authorization"] == "Bearer t")
th["single"] = True
check("Mattermost, not a thread: the messages leading up to it are used instead",
      c.thread(note) == [("sam", "Earlier context"), ("alex", "Great, report by Thursday?")],
      str(c.thread(note)))
check("no token, no lookup", mod.MattermostConnector(
    {**CFG, "mattermost_url": th_http.url}, Queue()).thread(note) == [])

# Slack: what is offered
box = Box()
sl = SL(SLCFG, box.q)
sl.start()
box.wait_status("Connected to Acme")
sl_event(sl_msg("<@UME> can you look at the pricing page today?", user="U2", ts="30.1"))
box.settle(0.5)
check("Slack, assistant off: untagged message ignored", kinds(box) == [])
sl.capture = True
sl_event(sl_msg("<@UME> can you look at the pricing page today?", user="U2", ts="31.1"))
sl_event(sl_msg("thinking out loud about pricing myself", user="UME", ts="32.1"))
sl_event(sl_msg("nobody is addressed in this one at all", user="U2", ts="33.1"))
sl_event({"type": "message", "channel": "D1", "user": "U2", "ts": "34.1", "channel_type": "im",
          "text": "are you free to review my doc?"})
sl_event(sl_msg("<@UME> automated digest from a bot", user="U2", ts="35.1", bot_id="B9"))
box.wait_notes(2)
box.settle(0.6)
check("Slack, assistant on: mentions and DMs from others are offered, nothing else",
      kinds(box) == [("candidate", "can you look at the pricing page today?"),
                     ("candidate", "are you free to review my doc?")], str(kinds(box)))
check("Slack candidate keeps channel and thread",
      (box.notes[0]["channel"], box.notes[0]["post_id"], box.notes[1]["channel"]) ==
      ("launch", "31.1", "direct message"))
sl.stop()

# Slack: thread history
slt = {"thread": True}


def slt_route(call):
    m = call["path"].rsplit("/", 1)[-1]
    if m == "conversations.replies":
        msgs = [{"user": "U2", "text": "Numbers are in <@UME>", "ts": "1"},
                {"user": "UME", "text": "on it &amp; thanks", "ts": "2"}]
        return 200, {"ok": True, "messages": msgs if slt["thread"] else msgs[:1]}, None
    if m == "conversations.history":
        return 200, {"ok": True, "messages": [{"user": "U2", "text": "newest", "ts": "3"},
                                              {"user": "U2", "text": "older", "ts": "2"}]}, None
    if m == "users.info":
        name = {"U2": "Bob Stone", "UME": "Alex M"}.get(call["body"].get("user"))
        return 200, {"ok": True, "user": {"real_name": name, "profile": {}}}, None
    return 200, {"ok": False, "error": "unknown_method"}, None


slt_http = Http(slt_route)


class SLT(mod.SlackConnector):
    API = slt_http.url


s = SLT(SLCFG, Queue())
snote = {"channel_id": "C1", "thread_id": "16.0", "post_id": "16.2"}
check("Slack thread: readable text, names resolved",
      s.thread(snote) == [("Bob Stone", "Numbers are in "), ("Alex M", "on it & thanks")],
      str(s.thread(snote)))
check("Slack thread request", slt_http.named("conversations.replies")[-1]["body"] ==
      {"channel": "C1", "ts": "16.0", "limit": "15"})
slt["thread"] = False
check("Slack, not a thread: recent channel history, put back in order",
      s.thread(snote) == [("Bob Stone", "older"), ("Bob Stone", "newest")]
      and slt_http.named("conversations.history")[-1]["body"] ==
      {"channel": "C1", "latest": "16.2", "inclusive": "true", "limit": "6"}, str(s.thread(snote)))

# Discord: what is offered
box = Box()
dc = DC(DCCFG, box.q)
dc.start()
box.wait_status("Connected as sticky")
me_ping = [{"id": "1001", "username": "alex"}]
dc_msg("<@1001> could you merge my branch today?", author="222", name="Bob", mentions=me_ping, mid="o1")
box.settle(0.5)
check("Discord, assistant off: untagged message ignored", kinds(box) == [])
dc.capture = True
dc_msg("<@1001> could you merge my branch today?", author="222", name="Bob", mentions=me_ping, mid="o2")
dc_msg("just talking to myself in the channel", author="1001", mid="o3")
dc_msg("nobody addressed in this message", author="222", name="Bob", mid="o4")
dc_msg("can you DM me the invite link?", author="222", name="Bob", guild="", channel="D9", mid="o5")
box.wait_notes(2)
box.settle(0.6)
check("Discord, assistant on: mentions and DMs from others are offered, nothing else",
      kinds(box) == [("candidate", "could you merge my branch today?"),
                     ("candidate", "can you DM me the invite link?")], str(kinds(box)))
dc.stop()

# Discord: history
def dct_route(call):
    if call["path"].endswith("/channels/C1/messages"):
        return 200, [{"author": {"username": "bob", "global_name": "Bob"}, "content": "second"},
                     {"author": {"username": "amy"}, "content": "first"},
                     {"author": {"username": "amy"}, "content": ""}], None
    return 404, {"message": "Unknown"}, None


dct_http = Http(dct_route)


class DCT(mod.DiscordConnector):
    API = dct_http.url


dn = {"channel_id": "C1", "post_id": "m9", "sender": "Bob", "text": "worded by the assistant",
      "origin": "what Bob actually wrote"}
check("Discord history: put in order, empty messages dropped, ending with the message itself",
      DCT(DCCFG, Queue()).thread(dn) == [("amy", "first"), ("Bob", "second"),
                                         ("Bob", "what Bob actually wrote")],
      str(DCT(DCCFG, Queue()).thread(dn)))
check("Telegram cannot fetch history, and says so by returning nothing",
      TG({**CFG, "telegram_bot_token": TOKEN}, Queue()).thread(dn) == [])

# Telegram: what is offered
tg_http.calls.clear()
box = Box()
tg = TG({**CFG, "telegram_bot_token": TOKEN, "telegram_user_id": "777"}, box.q)
tg.start()
box.wait_status("Connected as @stickybot")
tg_msg("@reminder kick-off notes", chat=-5, kind="supergroup", title="Team")   # teaches it my @name
box.wait_notes(1)
tg_msg("@alex_m could you update the roadmap doc?", sender=555, chat=-5, kind="supergroup", title="Team")
box.settle(0.8)
check("Telegram, assistant off: untagged group message ignored", len(box.notes) == 1)
tg.capture = True
tg_msg("@alex_m could you update the roadmap doc?", sender=555, chat=-5, kind="supergroup", title="Team")
tg_msg("nobody is addressed in this group message", sender=555, chat=-5, kind="supergroup", title="Team")
tg_msg("please add me, this is a stranger writing", sender=555)
box.wait_notes(2)
box.settle(0.8)
check("Telegram, assistant on: a group message that names me is offered; strangers' DMs never are",
      kinds(box) == [("reminder", "kick-off notes"),
                     ("candidate", "could you update the roadmap doc?")], str(kinds(box)))
tg.stop()

bad = [r for r in results if not r[1]]
print(f"\n{len(results) - len(bad)}/{len(results)} checks passed")
sys.exit(1 if bad else 0)
