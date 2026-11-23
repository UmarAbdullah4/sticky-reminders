"""The assistant: every provider against a simulated server, then every
feature inside the real app with a scripted model.

No real AI service is contacted and no key is used, so this costs nothing.
It proves the app sends what each provider's documentation asks for and
handles the documented replies; it cannot prove what a live model will say.
"""
import ctypes
import importlib.machinery
import importlib.util
import json
import os
import shutil
import sys
import threading
import time
import traceback
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from queue import Queue
from urllib.parse import urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "out_ai")
shutil.rmtree(OUT, ignore_errors=True)
os.makedirs(OUT)

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
except Exception:
    pass

loader = importlib.machinery.SourceFileLoader("sticky_ai_test", sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(HERE), "sticky_reminders.pyw"))
spec = importlib.util.spec_from_loader(loader.name, loader)
mod = importlib.util.module_from_spec(spec)
loader.exec_module(mod)

mod.log.handlers.clear()
mod.DATA_FILE = os.path.join(OUT, "notes.json")
mod.CONFIG_FILE = os.path.join(OUT, "config.json")
mod.LOG_FILE = os.path.join(OUT, "test.log")
mod.BACKUP_DIR = os.path.join(OUT, "backups")
mod.SUGGEST_FILE = os.path.join(OUT, "suggestions.json")

results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond)))
    print(("PASS  " if cond else "FAIL  ") + name + (f"   [{detail}]" if detail and not cond else ""))


def raises(fn, fragment):
    """Does fn() raise AIError mentioning `fragment`? Returns (ok, message)."""
    try:
        fn()
    except mod.AIError as e:
        return fragment.lower() in str(e).lower(), str(e)
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"
    return False, "no error"


class Http:
    def __init__(self, route):
        self.calls = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def _do(self):
                raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                try:
                    body = json.loads(raw or b"{}")
                except ValueError:
                    body = raw.decode()
                call = {"method": self.command, "path": urlparse(self.path).path,
                        "url": self.path, "body": body,
                        "headers": {k.lower(): v for k, v in self.headers.items()}}
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
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()


SYS, SCHEMA = mod.AI_JOBS["capture"]
ANSWER = {"actionable": True, "note": "Send Sam the report", "due": "", "priority": "normal",
          "tasks": [], "why": "direct request"}

try:
    # ───────────────────────── helpers ─────────────────────────
    print("-- parsing --")
    pj = mod.parse_json
    check("plain JSON", pj('{"a": 1}') == {"a": 1})
    check("JSON inside a code fence and chatter",
          pj('Sure!\n```json\n{"a": {"b": [1, 2]}}\n```\nHope that helps') == {"a": {"b": [1, 2]}})
    check("garbage, arrays and empty answers are refused",
          all(raises(lambda t=t: pj(t), "expected format")[0] for t in ("nope", "[1, 2]", "", "{broken")))
    now = datetime(2026, 11, 23, 14, 0)
    cd = mod.clean_due
    check("due dates: full, ISO, bare date",
          cd("2026-11-25 09:30", now) == "2026-11-25T09:30:00"
          and cd("2026-11-25T09:30:00", now) == "2026-11-25T09:30:00"
          and cd("2026-11-25", now) == "2026-11-25T17:00:00")
    check("due dates: 'today' after hours still counts; past, absurd and junk dropped",
          cd("2026-11-23", datetime(2026, 11, 23, 19, 0)) == "2026-11-23T23:59:00"
          and cd("2026-11-01 09:00", now) == "" and cd("2099-01-01", now) == ""
          and cd("next friday", now) == "" and cd("", now) == "" and cd(None, now) == "")
    check("every job has instructions that fence off untrusted text, and a strict schema",
          all("<<<" in s and sc["additionalProperties"] is False
              and sc["required"] == list(sc["properties"])
              for n, (s, sc) in mod.AI_JOBS.items() if n != "ping"))

    # ───────────────────────── Claude (official SDK) ─────────────────────────
    print("-- Claude --")
    cl = {"mode": "ok", "stop": "end_turn", "text": json.dumps(ANSWER)}

    def cl_route(call):
        err = lambda kind, msg: {"type": "error", "error": {"type": kind, "message": msg}}
        if cl["mode"] == "401":
            return 401, err("authentication_error", "invalid x-api-key"), None
        if call["path"].endswith("/v1/models"):
            return 200, {"data": [{"id": i, "type": "model", "display_name": i,
                                   "created_at": "2026-01-01T00:00:00Z"}
                                  for i in ("claude-opus-5-5", "claude-haiku-5-5")],
                         "has_more": False, "first_id": "claude-opus-5-5",
                         "last_id": "claude-haiku-5-5"}, None
        rich = "output_config" in call["body"]
        if cl["mode"] == "404":
            return 404, err("not_found_error", "model: nope"), None
        if cl["mode"] == "429":
            return 429, err("rate_limit_error", "slow down"), {"retry-after": "0"}
        if cl["mode"] == "old-model" and rich:
            return 400, err("invalid_request_error", "output_config: not supported"), None
        if cl["mode"] == "400":
            return 400, err("invalid_request_error", "Your credit balance is too low"), None
        return 200, {"id": "msg_1", "type": "message", "role": "assistant",
                     "model": call["body"].get("model"), "stop_reason": cl["stop"],
                     "stop_sequence": None,
                     "content": [{"type": "thinking", "thinking": "", "signature": "s"},
                                 {"type": "text", "text": cl["text"]}],
                     "usage": {"input_tokens": 10, "output_tokens": 5}}, None

    cl_http = Http(cl_route)

    class Claude(mod.ClaudeProvider):
        BASE_URL = cl_http.url

    CCFG = {"ai_claude_key": "sk-ant-test", "ai_claude_model": ""}
    c = Claude(CCFG)
    check("defaults to claude-opus-5-5", c.model == "claude-opus-5-5" and c.configured()
          and not Claude({"ai_claude_key": ""}).configured())
    got = c.ask(SYS, "the prompt", SCHEMA)
    call = cl_http.calls[-1]
    b = call["body"]
    check("answer parsed, thinking blocks skipped", got == ANSWER, str(got))
    # which header carries the key is the SDK's business; it must be a header, not the URL
    check("sent through the SDK, key in a header and never in the address",
          "sk-ant-test" in (call["headers"].get("x-api-key", "") + call["headers"].get("authorization", ""))
          and "anthropic-version" in call["headers"] and "x-stainless-lang" in call["headers"]
          and call["path"] == "/v1/messages" and "sk-ant-test" not in call["url"],
          f"url={call['url']} headers={sorted(call['headers'])}")
    check("request body", b["model"] == "claude-opus-5-5" and b["max_tokens"] == 16000
          and b["system"] == SYS and b["messages"] == [{"role": "user", "content": "the prompt"}],
          str({k: b[k] for k in b if k != "system"})[:300])
    check("structured output + low effort requested",
          b["output_config"] == {"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}})
    check("refusal fallback opted in",
          b.get("fallbacks") == "default"
          and "server-side-fallback-2026-07-01" in call["headers"].get("anthropic-beta", ""),
          f"{b.get('fallbacks')} / {call['headers'].get('anthropic-beta')}")
    check("no thinking, temperature or prefill sent",
          not any(k in b for k in ("thinking", "temperature", "top_p", "top_k"))
          and b["messages"][-1]["role"] == "user")

    cl["mode"] = "old-model"
    n0 = len(cl_http.calls)
    c2 = Claude({**CCFG, "ai_claude_model": "claude-haiku-4-5"})
    check("a model without the new options is retried plainly",
          c2.ask(SYS, "p", SCHEMA) == ANSWER and len(cl_http.calls) == n0 + 2
          and "output_config" not in cl_http.calls[-1]["body"]
          and "fallbacks" not in cl_http.calls[-1]["body"] and c2._plain)
    n0 = len(cl_http.calls)
    c2.ask(SYS, "p", SCHEMA)
    check("...and plainly from then on (one request, not two)", len(cl_http.calls) == n0 + 1)

    cl["mode"] = "400"
    c3 = Claude(CCFG)
    ok, msg = raises(lambda: c3.ask(SYS, "p", SCHEMA), "credit balance")
    check("a real 400 surfaces the provider's message and does not switch modes",
          ok and not c3._plain, msg)
    for mode, frag in (("401", "rejected the API key"), ("404", "does not know the model"),
                       ("429", "limit reached")):
        cl["mode"] = mode
        ok, msg = raises(lambda: Claude(CCFG).ask(SYS, "p", SCHEMA), frag)
        check(f"HTTP {mode} explained", ok, msg)
    cl["mode"] = "ok"
    for stop, frag in (("refusal", "declined"), ("max_tokens", "cut short")):
        cl["stop"] = stop
        ok, msg = raises(lambda: Claude(CCFG).ask(SYS, "p", SCHEMA), frag)
        check(f"stop_reason {stop} is checked before the text is used", ok, msg)
    cl["stop"] = "end_turn"
    check("model list", Claude(CCFG).models() == ["claude-opus-5-5", "claude-haiku-5-5"])
    cl["mode"] = "401"
    ok, msg = raises(lambda: Claude(CCFG).models(), "rejected the API key")
    check("model list with a bad key", ok, msg)
    cl["mode"] = "ok"

    # ───────────────────────── OpenAI ─────────────────────────
    print("-- OpenAI --")
    oa = {"mode": "ok", "content": json.dumps(ANSWER)}

    def oa_route(call):
        if call["path"].endswith("/models"):
            return 200, {"object": "list", "data": [{"id": "gpt-6-luna"}, {"id": "gpt-6.2-astra"}]}, None
        err = lambda msg: {"error": {"message": msg, "type": "invalid_request_error"}}
        if oa["mode"] in ("401", "404", "429", "500"):
            return int(oa["mode"]), err("the provider said no"), None
        if oa["mode"] == "strict-server" and ("response_format" in call["body"]
                                              or call["body"]["messages"][0]["role"] != "system"):
            return 400, err("unknown role 'developer'"), None
        if oa["mode"] == "400":
            return 400, err("model is overloaded with regret"), None
        return 200, {"id": "c1", "choices": [{"index": 0, "finish_reason": "stop",
                                              "message": {"role": "assistant",
                                                          "content": oa["content"]}}]}, None

    oa_http = Http(oa_route)

    class OpenAI(mod.OpenAIProvider):
        BASE_URL = oa_http.url + "/v1"

    OCFG = {"ai_openai_key": "sk-test", "ai_openai_model": "gpt-6-luna"}
    o = OpenAI(OCFG)
    got = o.ask(SYS, "the prompt", SCHEMA)
    call = oa_http.calls[-1]
    b = call["body"]
    check("answer parsed", got == ANSWER)
    check("bearer auth to /v1/chat/completions",
          call["headers"].get("authorization") == "Bearer sk-test"
          and call["path"] == "/v1/chat/completions" and call["method"] == "POST")
    check("developer + user messages, JSON mode, no token/temperature settings",
          b == {"model": "gpt-6-luna", "response_format": {"type": "json_object"},
                "messages": [{"role": "developer", "content": SYS},
                             {"role": "user", "content": "the prompt"}]}, str(b)[:300])
    oa["content"] = "```json\n" + json.dumps(ANSWER) + "\n```"
    check("fenced answer still parsed", o.ask(SYS, "p", SCHEMA) == ANSWER)
    oa["content"] = [{"type": "text", "text": json.dumps(ANSWER)}]
    check("content given as parts", o.ask(SYS, "p", SCHEMA) == ANSWER)
    oa["content"] = ""
    ok, msg = raises(lambda: o.ask(SYS, "p", SCHEMA), "no answer")
    check("empty answer reported", ok, msg)
    oa["content"] = json.dumps(ANSWER)

    oa["mode"] = "strict-server"
    o2, n0 = OpenAI(OCFG), len(oa_http.calls)
    check("a server that rejects the newer options gets the plain request",
          o2.ask(SYS, "p", SCHEMA) == ANSWER and len(oa_http.calls) == n0 + 2 and o2._simple
          and oa_http.calls[-1]["body"]["messages"][0]["role"] == "system"
          and "response_format" not in oa_http.calls[-1]["body"])
    oa["mode"] = "400"
    ok, msg = raises(lambda: OpenAI(OCFG).ask(SYS, "p", SCHEMA), "overloaded with regret")
    check("a real 400 surfaces the provider's message", ok, msg)
    for mode, frag in (("401", "rejected the API key"), ("404", "not found"), ("429", "quota")):
        oa["mode"] = mode
        ok, msg = raises(lambda: OpenAI(OCFG).ask(SYS, "p", SCHEMA), frag)
        check(f"HTTP {mode} explained", ok, msg)
    oa["mode"] = "500"
    try:
        OpenAI(OCFG).ask(SYS, "p", SCHEMA)
        check("a server error is not blamed on the user", False)
    except mod.AIError:
        check("a server error is not blamed on the user", False)
    except RuntimeError:
        check("a server error is not blamed on the user", True)
    oa["mode"] = "ok"
    check("model list", OpenAI(OCFG).models() == ["gpt-6.2-astra", "gpt-6-luna"])

    # ───────────────────────── open-source / custom ─────────────────────────
    print("-- custom --")
    cu = mod.CustomProvider({"ai_custom_url": oa_http.url + "/v1/chat/completions/",
                             "ai_custom_key": "", "ai_custom_model": "llama3.3"})
    check("address tidied; usable without a key", cu.base == oa_http.url + "/v1" and cu.configured())
    check("answers through the same code path", cu.ask(SYS, "p", SCHEMA) == ANSWER)
    call = oa_http.calls[-1]
    check("no Authorization header without a key; plain 'system' role",
          "authorization" not in call["headers"] and call["body"]["messages"][0]["role"] == "system"
          and call["body"]["model"] == "llama3.3")
    check("not configured without a model",
          not mod.CustomProvider({"ai_custom_url": "http://localhost:11434/v1"}).configured())
    far = mod.CustomProvider({"ai_custom_url": "http://models.example.com/v1",
                              "ai_custom_key": "k", "ai_custom_model": "m"})
    ok, msg = raises(lambda: far.ask(SYS, "p", SCHEMA), "https://")
    check("refuses to send messages over plain http to the internet", ok, msg)
    ok, msg = raises(lambda: mod.CustomProvider({"ai_custom_url": "ftp://x/v1", "ai_custom_model": "m"})
                     .ask(SYS, "p", SCHEMA), "http")
    check("nonsense address refused", ok, msg)
    priv = mod.CustomProvider._private
    check("local and LAN addresses recognised",
          all(priv(h) for h in ("localhost", "127.0.0.1", "192.168.1.20", "10.0.0.5", "172.20.1.1", "box.local"))
          and not any(priv(h) for h in ("example.com", "8.8.8.8", "172.32.0.1", "")))

    # ───────────────────────── Gemini ─────────────────────────
    print("-- Gemini --")
    ge = {"mode": "ok"}

    def ge_route(call):
        if call["path"].endswith("/models"):
            return 200, {"models": [
                {"name": "models/gemini-3.5-flash-lite", "supportedGenerationMethods": ["generateContent"]},
                {"name": "models/gemini-4-carbon", "supportedGenerationMethods": ["generateContent", "countTokens"]},
                {"name": "models/embedding-001", "supportedGenerationMethods": ["embedContent"]}]}, None
        if ge["mode"] == "badkey":
            return 400, {"error": {"code": 400, "status": "INVALID_ARGUMENT",
                                   "message": "API key not valid. Please pass a valid API key."}}, None
        if ge["mode"] == "404":
            return 404, {"error": {"code": 404, "status": "NOT_FOUND", "message": "models/x is not found"}}, None
        if ge["mode"] == "429":
            return 429, {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "message": "quota"}}, None
        if ge["mode"] == "blocked":
            return 200, {"promptFeedback": {"blockReason": "SAFETY"}}, None
        if ge["mode"] == "empty":
            return 200, {"candidates": [{"finishReason": "MAX_TOKENS", "content": {"parts": []}}]}, None
        return 200, {"candidates": [{"finishReason": "STOP", "content": {"role": "model", "parts": [
            {"text": "pondering...", "thought": True},
            {"text": json.dumps(ANSWER)[:20]}, {"text": json.dumps(ANSWER)[20:]}]}}]}, None

    ge_http = Http(ge_route)

    class Gemini(mod.GeminiProvider):
        BASE_URL = ge_http.url + "/v1beta"

    GCFG = {"ai_gemini_key": "AIza-test", "ai_gemini_model": "models/gemini-3.5-flash-lite"}
    g = Gemini(GCFG)
    got = g.ask(SYS, "the prompt", SCHEMA)
    call = ge_http.calls[-1]
    check("answer joined from parts, thought parts skipped", got == ANSWER, str(got))
    check("key in a header, never in the address",
          call["headers"].get("x-goog-api-key") == "AIza-test" and "AIza" not in call["url"]
          and call["path"] == "/v1beta/models/gemini-3.5-flash-lite:generateContent")
    check("request body", call["body"] == {
        "systemInstruction": {"parts": [{"text": SYS}]},
        "contents": [{"role": "user", "parts": [{"text": "the prompt"}]}],
        "generationConfig": {"responseMimeType": "application/json"}}, str(call["body"])[:300])
    for mode, frag in (("badkey", "rejected the API key"), ("404", "does not know the model"),
                       ("429", "quota"), ("blocked", "blocked this request (SAFETY)"),
                       ("empty", "MAX_TOKENS")):
        ge["mode"] = mode
        ok, msg = raises(lambda: Gemini(GCFG).ask(SYS, "p", SCHEMA), frag)
        check(f"Gemini '{mode}' explained", ok, msg)
    ge["mode"] = "ok"
    check("model list: text models only, prefix removed",
          Gemini(GCFG).models() == ["gemini-4-carbon", "gemini-3.5-flash-lite"])

    # ───────────────────────── the engine ─────────────────────────
    print("-- engine --")

    class Scripted(mod.AIProvider):
        """Answers each job from a table; records what it was asked."""
        key, label, FIELDS = "fake", "Scripted", ()
        answers, asked, fail = {}, [], None

        def configured(self):
            return True

        def ask(self, system, prompt, schema):
            name = next(n for n, (s, _) in mod.AI_JOBS.items() if s == system)
            Scripted.asked.append((name, prompt))
            if Scripted.fail:
                raise Scripted.fail
            reply = Scripted.answers[name]
            return reply(prompt) if callable(reply) else reply

    mod.AI_BY_KEY["fake"] = Scripted

    class StubApp:
        def __init__(self):
            self.config = {"ai_provider": "", "ai_daily_limit": 3}
            self.inbox = Queue()

        def post(self, fn):
            self.inbox.put(fn)

        def _paint_status(self):
            pass

    stub = StubApp()
    eng = mod.AIEngine(stub)

    def run(job):
        out = []
        eng.submit(job, lambda r, e: out.append((r, e)))
        end = time.time() + 4
        while not out and time.time() < end:
            try:
                stub.inbox.get(timeout=0.05)()
            except Exception:
                pass
        return out[0] if out else ("timeout", "timeout")

    check("off until a provider is chosen", not eng.ready() and not eng.on("ai_capture"))
    r, e = run(lambda ask: ask("ping", "x"))
    check("asking with no provider is an error, not a crash", r is None and "not set up" in e, str(e))
    stub.config.update(ai_provider="fake", ai_capture=True)
    Scripted.answers = {"ping": {"ok": True}}
    check("ready once chosen; feature switches respected",
          eng.ready() and eng.on("ai_capture") and not eng.on("ai_reply"))
    check("job result delivered", run(lambda ask: ask("ping", "x")) == ({"ok": True}, None))
    Scripted.fail = mod.AIError("Claude rejected the API key")
    r, e = run(lambda ask: ask("ping", "x"))
    check("a user-facing failure is passed on as written and remembered",
          r is None and e == "Claude rejected the API key" and eng.problem == e)
    Scripted.fail = ValueError("secret-internal-detail")
    r, e = run(lambda ask: ask("ping", "x"))
    check("an unexpected failure gives a generic message", r is None and "secret" not in e, str(e))
    Scripted.fail = None
    r, e = run(lambda ask: ask("ping", "x"))
    check("the daily limit stops further requests", r is None and "limit of 3" in e, str(e))
    check("...and counts every request, failed ones included", eng.used == 3)
    eng.day = None
    check("a new day resets it, and a good answer clears the old problem",
          run(lambda ask: ask("ping", "x")) == ({"ok": True}, None) and eng.problem == "")

    # ══════════════════════ inside the app ══════════════════════
    print("-- app --")

    def _no_tray(self, app):
        self.app, self.icon, self.available = app, None, False

    mod.Tray.__init__ = _no_tray
    soon = (datetime.now() + timedelta(hours=3)).replace(second=0, microsecond=0)
    with open(mod.CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump({"mattermost_enabled": False, "start_minimized": False, "sound_enabled": False,
                   "reminders_enabled": True, "reminder_interval_minutes": 600,
                   "ai_provider": "fake", "ai_capture": True,
                   "ai_context": True, "ai_focus": True, "ai_reply": True}, f)
    with open(mod.DATA_FILE, "w", encoding="utf-8") as f:
        json.dump([{"id": i, "text": t, "priority": p, "due": d, "created_at": "2026-11-01T09:00:00"}
                   for i, (t, p, d) in enumerate([
                       ("Renew the domain", 2, soon.isoformat(timespec="seconds")),
                       ("Write the launch post", 1, ""), ("Call the bank", 1, ""),
                       ("Tidy the backlog", 0, ""), ("Plan the offsite", 1, "")], start=1)], f)

    app = mod.StickyNotesApp()
    tk = mod.tk
    app.root.geometry("1000x680+40+30")

    def pump(ms=300, until=None):
        end = time.time() + ms / 1000
        while time.time() < end:
            app.root.update()
            if until and until():
                return True
            time.sleep(0.01)
        return bool(until and until())

    def walk(w):
        yield w
        for ch in w.winfo_children():
            yield from walk(ch)

    def labels(w, text):
        return [x for x in walk(w) if isinstance(x, tk.Label) and x.cget("text") == text]

    def shot(win, name):
        from PIL import ImageGrab
        win.attributes("-topmost", True)
        win.lift()
        pump(350)
        x, y, w, h = win.winfo_rootx(), win.winfo_rooty(), win.winfo_width(), win.winfo_height()
        ImageGrab.grab(bbox=(x, y, x + w, y + h)).save(os.path.join(OUT, name + ".png"))
        print(f"SHOT  {name}  {w}x{h}")

    class FakeSlack(mod.SlackConnector):
        talk = [("Sam", "The Q3 numbers are finally in."),
                ("Sam", "Can you turn them into the board report by Thursday 5pm?"),
                ("Alex", "Sure")]

        def __init__(self):
            super().__init__({"trigger_word": "@reminder"}, Queue())
            self.sent, self.running = [], True

        def thread(self, note):
            return list(self.talk)

        def reply(self, note, text):
            self.sent.append(text)
            return True

    pump(1700)                # let startup finish: it resets the connector list at 1.2 s
    slack = FakeSlack()
    app.connectors["slack"] = slack
    pump(300)
    for t in list(app.toasts):
        t.destroy()
    app.toasts.clear()
    Scripted.asked.clear()

    # 1. catch the ask ----------------------------------------------------
    app._sync_capture()
    check("connectors are told the assistant wants untagged messages", slack.capture is True)
    check("no suggestions button while nothing is waiting", not app.suggest_btn.winfo_manager())
    thu = (datetime.now() + timedelta(days=3)).replace(hour=17, minute=0, second=0, microsecond=0)
    Scripted.answers.update(
        capture=lambda p: ({"actionable": True, "note": "Board report from the Q3 numbers for Sam",
                            "due": f"{thu:%Y-%m-%d %H:%M}", "priority": "high",
                            "tasks": ["Pull the Q3 numbers", "Draft the report"],
                            "why": "Sam asks for a deliverable with a deadline."}
                           if "board report" in p else
                           {"actionable": False, "note": "", "due": "", "priority": "normal",
                            "tasks": [], "why": "Small talk."}),
        context={"summary": "Sam needs the Q3 figures turned into the board report; you agreed."})
    ask_msg = {"type": "candidate", "source": "slack", "sender": "Sam", "channel": "finance",
               "channel_id": "C1", "post_id": "55.1", "thread_id": "55.0", "timestamp": "02:10 PM",
               "text": "Can you turn them into the board report by Thursday 5pm?"}
    app.msg_queue.put(dict(ask_msg))
    app.msg_queue.put(dict(ask_msg))                               # delivered twice
    app.msg_queue.put({**ask_msg, "post_id": "56.1", "text": "lol nice one, see you at lunch"})
    app._poll_queues()
    check("suggestion created", pump(4000, lambda: len(app.suggestions) == 1), str(len(app.suggestions)))
    pump(500)
    caps = [p for n, p in Scripted.asked if n == "capture"]
    check("one request per message, duplicates not re-sent, chatter rejected",
          len(caps) == 2 and len(app.suggestions) == 1, str(len(caps)))
    check("the message is fenced off as material; the model is told the time",
          "<<<\nCan you turn them into the board report by Thursday 5pm?\n>>>" in caps[0]
          and "Current time:" in caps[0] and "From: Sam" in caps[0])
    s = app.suggestions[0]
    check("suggestion carries the note, due date, priority, tasks and origin",
          (s["text"], s["due"], s["priority"], s["tasks"], s["sender"], s["post_id"], s["thread_id"]) ==
          ("Board report from the Q3 numbers for Sam", thu.isoformat(timespec="seconds"), 2,
           ["Pull the Q3 numbers", "Draft the report"], "Sam", "55.1", "55.0"), str(s))
    check("it is NOT a note yet", len(app.notes) == 5)
    check("the button appears with the count, and a toast announces it",
          app.suggest_btn.winfo_manager() == "pack" and app.suggest_btn.cget("text") == "✨  1"
          and len(app.toasts) == 1)
    check("suggestions survive a restart",
          json.load(open(mod.SUGGEST_FILE, encoding="utf-8"))[0]["text"] == s["text"]
          and mod.load_suggestions()[0]["id"] == s["id"])
    app.render_notes()
    pump(200)
    shot(app.root, "30_main_with_suggestion")

    app.open_suggestions()
    pump(400)
    app.open_suggestions()
    win = app._suggest_win
    check("one suggestions window", len([w for w in app.root.winfo_children()
                                         if isinstance(w, tk.Toplevel) and w.title() == "Suggested notes"]) == 1)
    win.geometry("+150+40")
    shot(win, "31_suggestions")

    # 2. accept -> note, with thread context ------------------------------
    Scripted.asked.clear()
    app._accept_suggestion(s["id"])
    note = app.notes[-1]
    check("accepting makes a real note",
          len(app.notes) == 6 and note["text"] == s["text"] and note["due"] == s["due"]
          and note["priority"] == 2 and [t["title"] for t in note["tasks"]] == s["tasks"]
          and note["source"] == "slack" and note["post_id"] == "55.1" and note["thread_id"] == "55.0"
          and note["origin"] == ask_msg["text"], str({k: note[k] for k in ("text", "due", "source", "origin")}))
    check("...and clears the suggestion and its button",
          app.suggestions == [] and not app.suggest_btn.winfo_manager()
          and json.load(open(mod.SUGGEST_FILE, encoding="utf-8")) == [])
    check("thread context added in the background",
          pump(4000, lambda: note.get("context")) and note["context"].startswith("Sam needs the Q3"))
    ctx = [p for n, p in Scripted.asked if n == "context"]
    check("the whole thread is sent, oldest first, untruncated",
          len(ctx) == 1 and "Sam: The Q3 numbers are finally in.\nSam: Can you turn them" in ctx[0]
          and "Alex: Sure" in ctx[0])
    slack.talk = [("Sam", "just this")]
    Scripted.asked.clear()
    app._add_context(note)
    pump(800)
    check("a lone message needs no summary (no request made)", Scripted.asked == [])
    slack.talk = FakeSlack.talk
    win.destroy()
    pump(200)

    app.msg_queue.put({**ask_msg, "post_id": "57.1", "text": "Please also send the board report to Dana"})
    app._poll_queues()
    pump(4000, lambda: len(app.suggestions) == 1)
    app._dismiss_suggestion(app.suggestions[0]["id"])
    check("dismissing drops it without making a note", app.suggestions == [] and len(app.notes) == 6)

    # 3. the note window + suggest tasks ----------------------------------
    for t in list(app.toasts):
        t.destroy()
    app.toasts.clear()
    app.open_note(note["id"])
    pump(400)
    editor = [w for w in app.root.winfo_children() if isinstance(w, tk.Toplevel) and w.title() == "Note"][0]
    editor.geometry("+150+10")
    check("the note shows what was actually said, and the background",
          labels(editor, "“" + ask_msg["text"] + "”") and labels(editor, note["context"]))
    tab = [x for x in walk(editor) if isinstance(x, tk.Label) and str(x.cget("text")).startswith("Tasks")][0]
    tab.event_generate("<Button-1>")
    pump(300)
    button = labels(editor, "✨ Suggest")
    check("Suggest button present in the Tasks tab", len(button) == 1)
    shot(editor, "32_note_from_suggestion")
    Scripted.answers["breakdown"] = {"tasks": [
        {"title": "Pull the Q3 numbers", "stages": []},                 # already there
        {"title": "Check figures with finance", "stages": []},
        {"title": "Get sign-off", "stages": ["Draft", "Review", "Approved"]},
        {"title": "", "stages": []}, "junk"]}
    Scripted.asked.clear()
    button[0].event_generate("<Button-1>")
    picker = lambda: [w for w in editor.winfo_children()
                      if isinstance(w, tk.Toplevel) and w.title() == "Suggested tasks"]
    check("proposals shown for review", pump(4000, lambda: picker()))
    bd = [p for n, p in Scripted.asked if n == "breakdown"][0]
    check("the model is given the note, its background and the existing tasks",
          "Board report from the Q3 numbers" in bd and "Background: Sam needs" in bd
          and "- Pull the Q3 numbers\n- Draft the report" in bd)
    dlg = picker()[0]
    dlg.attributes("-topmost", True)
    shot(dlg, "33_suggested_tasks")
    boxes = [x for x in walk(dlg) if isinstance(x, tk.Checkbutton)]
    check("duplicates and junk filtered out before you see them", len(boxes) == 2, str(len(boxes)))
    [x for x in walk(dlg) if isinstance(x, tk.Button) and x.cget("text") == "Add tasks"][0].invoke()
    pump(300)
    titles = [t["title"] for t in note["tasks"]]
    check("chosen tasks added",
          titles == ["Pull the Q3 numbers", "Draft the report", "Check figures with finance", "Get sign-off"],
          str(titles))
    check("a task that came with stages is tracked through them",
          note["tasks"][3]["mode"] == "stages" and note["tasks"][3]["stages"] == ["Draft", "Review", "Approved"]
          and note["tasks"][2]["mode"] == "percent" and len({t["id"] for t in note["tasks"]}) == 4)
    editor.destroy()
    pump(300)

    # 4. focus now --------------------------------------------------------
    Scripted.answers["focus"] = {"focus": [
        {"id": 1, "why": "Due in three hours."}, {"id": note["id"], "why": "Sam is waiting on it."},
        {"id": 999, "why": "does not exist"}, {"id": 1, "why": "repeat"}]}
    Scripted.asked.clear()
    app.show_reminder_popup()
    pop = app.reminder_popup
    # checked before the event loop runs again, i.e. before any answer can land
    check("popup opens at once with a placeholder", labels(pop, "Working out what matters most…"))
    pop.geometry("+60+40")
    check("focus picks appear", pump(4000, lambda: any(
        str(x.cget("text")).startswith("Renew the domain") for x in walk(pop) if isinstance(x, tk.Label))))
    rows = [x.cget("text") for x in walk(pop) if isinstance(x, tk.Label) and "\n" in str(x.cget("text"))]
    check("only real notes, no repeats, with reasons",
          rows == ["Renew the domain\nDue in three hours.",
                   "Board report from the Q3 numbers for Sam\nSam is waiting on it."], str(rows))
    fp = [p for n, p in Scripted.asked if n == "focus"][0]
    check("the model sees each note's priority, deadline, sender, progress and age",
          "priority High" in fp and "asked by Sam" in fp and "tasks 0/4 done" in fp
          and "days old" in fp and f"due {soon:%a %d %b %H:%M}" in fp, fp[:400])
    shot(pop, "34_focus_now")
    Scripted.asked.clear()
    app.show_reminder_popup()
    pump(500)
    check("reopening reuses the recent answer (no second request)",
          not [1 for n, _ in Scripted.asked if n == "focus"]
          and any(str(x.cget("text")).startswith("Renew the domain")
                  for x in walk(app.reminder_popup) if isinstance(x, tk.Label)))
    app._close_reminder_popup()

    # 5. drafted reply ----------------------------------------------------
    note["tasks"][0]["percent"] = 100
    note["tasks"][1]["percent"] = 100
    note["tasks"][1]["points"] = ["sent as PDF to the board list"]
    hold = threading.Event()              # keep the draft back until the first checks are done
    Scripted.answers["reply"] = lambda p: (hold.wait(5), {
        "reply": "The board report is done and sent as a PDF to the board list."})[1]
    Scripted.asked.clear()
    app.mark_done(note["id"])
    pump(200)
    rep = lambda: [w for w in app.root.winfo_children() if isinstance(w, tk.Toplevel) and w.title() == "Reply"]
    check("marking done opens the reply for review; nothing is sent by itself",
          note["done"] and len(rep()) == 1 and slack.sent == [])
    d = rep()[0]
    d.geometry("+200+120")
    box = [x for x in walk(d) if isinstance(x, tk.Text)][0]
    check("the standard reply is there while the draft is written",
          box.get("1.0", "end-1c") == "✅ *Done* (completed by me): " + note["text"])
    hold.set()
    check("draft arrives", pump(4000, lambda: box.get("1.0", "end-1c").startswith("✅ The board report")))
    rp = [p for n, p in Scripted.asked if n == "reply"][0]
    check("the draft is based on the request, the tasks and your notes on them",
          ask_msg["text"] in rp and "- Draft the report (100%)\n    note: sent as PDF to the board list" in rp
          and "Background: Sam needs" in rp, rp[:500])
    shot(d, "35_reply_draft")
    [x for x in walk(d) if isinstance(x, tk.Button) and x.cget("text") == "Send"][0].invoke()
    check("Send posts exactly what is in the box",
          pump(3000, lambda: slack.sent == ["✅ The board report is done and sent as a PDF to the board list."]),
          str(slack.sent))

    # typing before the draft arrives must win
    app.reopen_note(note["id"])
    gate = threading.Event()
    Scripted.answers["reply"] = lambda p: (gate.wait(5), {"reply": "late draft"})[1]
    app.mark_done(note["id"])
    pump(200)
    d = rep()[0]
    box = [x for x in walk(d) if isinstance(x, tk.Text)][0]
    box.delete("1.0", "end")
    box.insert("1.0", "my own words")
    gate.set()
    pump(1200)
    check("a late draft never overwrites what you typed", box.get("1.0", "end-1c") == "my own words")
    [x for x in walk(d) if isinstance(x, tk.Button) and x.cget("text") == "Don't reply"][0].invoke()
    pump(200)
    check("'Don't reply' sends nothing", len(slack.sent) == 1 and not rep())

    app.config["ai_reply"] = False
    app.reopen_note(note["id"])
    app.mark_done(note["id"])
    check("with drafting off, the standard reply goes out as before",
          pump(2000, lambda: len(slack.sent) == 2) and slack.sent[1].startswith("✅ *Done*") and not rep())

    # 6. failures stay quiet but visible ----------------------------------
    for t in list(app.toasts):
        t.destroy()
    app.toasts.clear()
    Scripted.fail = mod.AIError("Claude rejected the API key")
    app.msg_queue.put({**ask_msg, "post_id": "60.1", "text": "Could you review my pull request today?"})
    app._poll_queues()
    pump(2500, lambda: app.ai.problem)
    pump(400)
    bar = [x.cget("text") for x in app.conn_bar.winfo_children()]
    check("a failing key shows in the status bar instead of popping up dialogs",
          app.ai.problem == "Claude rejected the API key" and "●  AI: Scripted" in bar
          and app.suggestions == [] and len(app.toasts) == 0, str(bar))
    red = [x for x in app.conn_bar.winfo_children() if x.cget("text") == "●  AI: Scripted"][0]
    check("...in red", red.cget("fg").lower() == mod.T.err.lower())
    Scripted.fail = None

    app.config["ai_capture"] = False
    app._sync_capture()
    n_before = len(Scripted.asked)
    app.msg_queue.put({**ask_msg, "post_id": "61.1", "text": "Please prepare the slides for Monday"})
    app._poll_queues()
    pump(600)
    check("with suggestions off, nothing is sent to the model and connectors stop passing messages on",
          len(Scripted.asked) == n_before and slack.capture is False)

    # 7. settings ---------------------------------------------------------
    app.open_settings("AI")
    pump(400)
    sw, book, pages = app._settings
    sw.geometry("+160+10")
    check("AI tab exists and opens", book.tab(book.select(), "text") == "AI")
    for name in ("Claude", "Open-source / custom"):
        labels(pages["AI"], name)[0].event_generate("<Button-1>")
        pump(300)
        shot(sw, "36_settings_ai_" + name.split()[0].lower().replace("-", ""))
    labels(pages["AI"], "Claude")[0].event_generate("<Button-1>")
    pump(200)
    key_entry = [x for x in walk(pages["AI"]) if isinstance(x, tk.Entry) and x.winfo_ismapped()
                 and x.cget("show")][0]
    key_entry.insert(0, "sk-ant-secret-123")
    [x for x in walk(sw) if isinstance(x, tk.Button) and x.cget("text") == "Save"][0].invoke()
    pump(400)
    disk = open(mod.CONFIG_FILE, encoding="utf-8").read()
    check("provider and key saved; the key is encrypted on disk",
          app.config["ai_provider"] == "claude" and app.config["ai_claude_key"] == "sk-ant-secret-123"
          and "sk-ant-secret-123" not in disk and json.loads(disk)["ai_claude_key"].startswith("dpapi:"))
    check("default Claude model kept", app.config["ai_claude_model"] == "claude-opus-5-5")
    check("the engine picks up the new provider without a restart",
          isinstance(app.ai.provider(), mod.ClaudeProvider) and app.ai.provider().api_key == "sk-ant-secret-123")
    app.config["ai_provider"] = ""
    check("switching it off disables every feature at once",
          not app.ai.ready() and not app.ai.on("ai_focus") and not app.ai.on("ai_context"))
except Exception:
    traceback.print_exc()
    results.append(("harness crashed", False))
finally:
    try:
        app.root.destroy()
    except Exception:
        pass

bad = [r for r in results if not r[1]]
print(f"\n{len(results) - len(bad)}/{len(results)} checks passed")
sys.exit(1 if bad else 0)
