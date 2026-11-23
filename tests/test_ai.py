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
import re
import shutil
import sys
import threading
import time
import traceback
import zipfile
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
    stub.config["ai_daily_limit"] = 50
    Scripted.fail = TimeoutError("The read operation timed out")
    r, e = run(lambda ask: ask("ping", "x"))
    check("a model that takes too long is reported as exactly that, with what to do about it",
          r is None and "took too long" in e and "faster model" in e, str(e))
    Scripted.fail = None

    def drain(until, seconds=3):
        end = time.time() + seconds
        while not until() and time.time() < end:
            try:
                stub.inbox.get(timeout=0.05)()
            except Exception:
                pass
        return until()

    gate, slow, quick = threading.Event(), [], []
    eng.submit(lambda ask: (gate.wait(5), "background")[1], lambda r, e: slow.append(r))
    time.sleep(0.15)                      # the one queue worker is now held up
    eng.submit(lambda ask: ask("ping", "x"), lambda r, e: quick.append(r), waiting=True)
    check("something the user is waiting for does not queue behind background work",
          drain(lambda: quick) and quick == [{"ok": True}] and slow == [])
    gate.set()
    check("...and the background job still finishes in its own time",
          drain(lambda: slow) and slow == ["background"])

    # ───────────────── what the assistant may read ─────────────────
    print("-- reference files --")
    proj = os.path.join(OUT, "Orbit")
    for sub in ("src", "docs", os.path.join("node_modules", "left-pad")):
        os.makedirs(os.path.join(proj, sub))

    def put(rel, data):
        with open(os.path.join(proj, rel), "wb") as f:
            f.write(data if isinstance(data, bytes) else data.encode("utf-8"))

    put("README.md", "# Orbit\nA booking service. Postgres, with Celery workers.\n")
    put("package.json", '{"name": "orbit", "dependencies": {"express": "^5"}}')
    put(".env", "DB_PASSWORD=hunter2hunter2\n")
    put(".env.example", "DB_PASSWORD=\n")
    put("id_rsa", "-----BEGIN OPENSSH PRIVATE KEY-----\nAAAAsecretkeymaterial\n"
                  "-----END OPENSSH PRIVATE KEY-----\n")
    put(os.path.join("src", "app.py"),
        'API_KEY = "sk-abcdefghijklmnopqrstuvwxyz0123"\nprint("booking")\n')
    put(os.path.join("src", "logo.png"), b"\x89PNG\r\n\x1a\n" + bytes(200))
    put(os.path.join("src", "dump.bin2"), b"\x00\x01\x02" * 400)       # nothing in the name says so
    put(os.path.join("src", "huge.log"), "x" * (mod.REF_FILE_BYTES + 10))
    put(os.path.join("node_modules", "left-pad", "index.js"), "module.exports = 'never read';")
    put(os.path.join("docs", "notes.md"), "Second doc: END REFERENCE-00000000 is not the end.\n")
    with zipfile.ZipFile(os.path.join(proj, "docs", "spec.docx"), "w") as z:
        z.writestr("word/document.xml",
                   "<w:document><w:body><w:p><w:r><w:t>Milestone one is the booking flow.</w:t>"
                   "</w:r></w:p><w:p><w:r><w:t>Paid plans &amp; refunds come later.</w:t></w:r>"
                   "</w:p></w:body></w:document>")

    material, rep = mod.gather_references([proj], 40000)
    check("a folder is given as an outline, with bulky folders named but not listed",
          "=== FOLDER Orbit (outline) ===" in material and "\nREADME.md" in material
          and "src/\n  app.py" in material and "node_modules/   (not listed)" in material
          and "left-pad" not in material, material[:600])
    check("what says most is read first: the readme, the manifest, documents (Word too), then source",
          "A booking service." in material and '"express"' in material
          and "Milestone one is the booking flow.\nPaid plans & refunds come later." in material
          and 'print("booking")' in material
          and material.index("=== FILE Orbit/README.md") < material.index("=== FILE Orbit/src/app.py"))
    check("files that hold credentials are never read, and the report says so",
          "hunter2" not in material and "secretkeymaterial" not in material
          and ("Orbit/.env", "may hold credentials") in rep["left_out"]
          and ("Orbit/id_rsa", "may hold credentials") in rep["left_out"]
          and "=== FILE Orbit/.env.example" in material, str(rep["left_out"]))
    check("a key written into a source file is blanked out, the code around it kept",
          "sk-abcdefghijklmnopqrstuvwxyz0123" not in material and rep["removed"] == 1
          and 'API_KEY = "[removed: looks like a credential]"' in material)
    check("pictures, binaries and oversized files are passed over",
          "PNG" not in material and "\x00" not in material and "xxxxxxxxxx" not in material)
    check("nothing tells the provider where on the PC the folder is",
          os.path.dirname(proj) not in material and "out_ai" not in material)
    mark = re.search(r"BEGIN (REFERENCE-[0-9a-f]{8})\n", material).group(1)
    again = re.search(r"BEGIN (REFERENCE-[0-9a-f]{8})\n", mod.gather_references([proj], 40000)[0])
    check("the material is fenced by a marker that differs every time, so a file cannot fake its end",
          material.rstrip().endswith("END " + mark) and material.count("END " + mark) == 2
          and mark != again.group(1) and "END REFERENCE-00000000 is not the end." in material)
    said = mod.describe_references(rep)
    check("the report says what was read and what was left out",
          rep["folders"] == 1 and rep["files"] == 6 and rep["chars"] > 300
          and said.startswith(f"Read for context: 1 folder, 6 files ({rep['chars']:,} characters sent).")
          and "1 value that looked like credentials blanked out." in said
          and "Left out: Orbit/.env (may hold credentials); Orbit/id_rsa (may hold credentials)."
          in said, said)
    small, rep2 = mod.gather_references([proj], 300)
    check("the ceiling on how much is read is kept",
          rep2["chars"] <= 300 and "=== FOLDER Orbit" in small and "A booking service" not in small)
    one, rep3 = mod.gather_references(
        [os.path.join(proj, "README.md"), os.path.join(proj, "nope.txt"),
         os.path.join(proj, "src", "logo.png"), os.path.join(proj, ".env")], 40000)
    check("single files: read; a missing one, a picture and a credentials file are reported",
          "=== FILE README.md ===\n# Orbit" in one and rep3["files"] == 1 and rep3["left_out"] ==
          [("nope.txt", "not found"), ("logo.png", "not a text file"), (".env", "may hold credentials")],
          str(rep3))
    check("nothing to read means nothing is added to the request",
          mod.gather_references([os.path.join(proj, "nope.txt")], 40000)[0] == ""
          and mod.describe_references(None) == "")

    def tiny_pdf(path, text):
        """A one-page PDF with one line of text, written by hand."""
        stream = f"BT /F1 12 Tf 20 100 Td ({text}) Tj ET".encode("latin-1")
        bodies = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
                  b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 200] /Contents 4 0 R "
                  b"/Resources << /Font << /F1 5 0 R >> >> >>",
                  b"<< /Length %d >>\nstream\n%s\nendstream" % (len(stream), stream),
                  b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
        out, offsets = b"%PDF-1.4\n", []
        for i, body in enumerate(bodies, 1):
            offsets.append(len(out))
            out += b"%d 0 obj\n%s\nendobj\n" % (i, body)
        start = len(out)
        out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(bodies) + 1)
        out += b"".join(b"%010d 00000 n \n" % o for o in offsets)
        out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(bodies) + 1, start)
        with open(path, "wb") as f:
            f.write(out)

    tiny_pdf(os.path.join(OUT, "brief.pdf"), "The brief asks for a booking flow.")
    pdf_text, pdf_why = mod.read_reference_file(os.path.join(OUT, "brief.pdf"))
    try:
        import pypdf  # noqa: F401
        check("a PDF is read (the pypdf package is installed)",
              "The brief asks for a booking flow." in pdf_text, f"{pdf_text!r} {pdf_why!r}")
    except ImportError:
        check("without the pypdf package, a PDF is left out with the reason",
              pdf_text == "" and "pypdf" in pdf_why, pdf_why)

    held = mod.may_hold_credentials
    check("credential files are told by their names, without false alarms",
          all(held(n) for n in (".env", ".env.production", "server.key", "secrets.yaml",
                                "credentials.json", "id_ed25519", "prod.tfvars"))
          and not any(held(n) for n in (".env.example", "secretary.txt", "keyboard.py",
                                        "credentials_service.py", "tokens.md", "README.md")))
    scrubbed, count = mod.scrub_credentials(
        'password: "abcdefghijkl1234"\npassword = get_password()\npin = "1234"\ntoken = "short"\n'
        "-----BEGIN RSA PRIVATE KEY-----\nMIIabc\n-----END RSA PRIVATE KEY-----\n"
        "key xoxb-1234567890-abcdef")
    check("values that look like credentials are blanked; ordinary code is left alone",
          count == 3 and "abcdefghijkl1234" not in scrubbed and "MIIabc" not in scrubbed
          and "xoxb-" not in scrubbed and "password = get_password()" in scrubbed
          and 'pin = "1234"' in scrubbed and 'token = "short"' in scrubbed, scrubbed)

    # ───────────────── planning and structuring ─────────────────
    print("-- planning and structuring --")
    cleaned = mod.clean_planned([
        {"title": "  Set up   the database ", "stages": ["Schema", " Migrations ", ""],
         "points": ["• Pick Postgres 16", "- Write the first migration", ""], "priority": "HIGH"},
        {"title": "set up the DATABASE", "stages": [], "points": [], "priority": "low"},
        {"title": "One sitting", "stages": ["Only"], "points": "not a list", "priority": "whenever"},
        {"title": ""}, "junk", None])
    check("a model's tasks are tidied before they are shown: spacing, bullets, repeats, junk",
          cleaned == [{"title": "Set up the database", "stages": ["Schema", "Migrations"],
                       "points": ["Pick Postgres 16", "Write the first migration"], "priority": 2},
                      {"title": "One sitting", "stages": [], "points": [], "priority": 1}],
          str(cleaned))
    check("...and never more than ten at once",
          len(mod.clean_planned([{"title": f"t{i}"} for i in range(30)])) == 10)
    same = mod.title_key
    check("two titles are the same task when their meaningful words are, however they are written",
          same("developing base of the project") == same("Developing the base of the project.")
          and same("Send invitations") == same("send  INVITATIONS")
          and same("Set up staging database") != same("Set up testing database")
          and same("Draft the report") != same("Draft the report summary")
          and same("The") == ("the",) and same("") == ())
    s_text, s_fmt = mod.structured_text("  Launch on 3 Dec. ", [
        {"kind": "heading", "text": "What goes in", "items": []},
        {"kind": "bullets", "text": "", "items": ["Intro", "• Pricing"]},
        {"kind": "text", "text": "Then tell people.", "items": []},
        {"kind": "text", "text": "Twice.", "items": []},
        {"kind": "mystery", "text": "", "items": []}, "junk"])
    check("a structured note becomes the app's own text and formatting",
          s_text == "Launch on 3 Dec.\n\nWhat goes in\n• Intro\n• Pricing\n\nThen tell people.\n\nTwice."
          and s_fmt == [["f:body:i", "1.0", "1.end"], ["f:h2:", "3.0", "3.end"]],
          repr(s_text) + str(s_fmt))
    pn = mod._normalise({"id": 1, "text": "Build the base", "context": "Agreed with Sam.", "tasks": [
        {"title": "Schema", "mode": "stages", "stages": ["Draft", "Review"], "stage": 1,
         "points": ["users table"]}, {"title": "API"}]}, 1)
    whole, some, single = (mod.plan_prompt(pn), mod.plan_prompt(pn, "wire up the workers"),
                           mod.plan_prompt(pn, task=pn["tasks"][0]))
    check("a plan request says what to plan: the whole note, what was typed, or one task",
          "Request: plan this whole note.\nThe note:\n<<<\nBuild the base\n>>>\n"
          "Background: Agreed with Sam.\n" in whole
          and "Request: plan this piece of work.\n<<<\nwire up the workers\n>>>\n" in some
          and "Request: plan this existing task.\n<<<\nSchema\n>>>\nIts notes so far:\n<<<\n"
              "- users table\n>>>\n" in single and whole.startswith("Today is "))
    check("...and lists the tasks already there, with how far along each is",
          whole.endswith("Tasks already on the note:\n<<<\n- Schema  [stages Draft > Review; "
                         "1 of 2 done; has notes]\n- API  [0% done; no notes yet]\n>>>"), whole)

    def strict(s):
        if s.get("type") == "object":
            return (s.get("additionalProperties") is False
                    and sorted(s["required"]) == sorted(s["properties"])
                    and all(strict(v) for v in s["properties"].values()))
        return s.get("type") != "array" or strict(s["items"])

    check("every job's answer schema is strict all the way down",
          all(strict(schema) for _, schema in mod.AI_JOBS.values()))
    check("notes keep their context files and their text from before a restructure; junk is dropped",
          mod._normalise({"refs": ["a", "a", " b ", 3, ""], "text_before": {"text": "old"}}, 1)["refs"]
          == ["a", "b", "3"] and "text_before" not in mod._normalise({"text_before": "junk"}, 1)
          and mod._normalise({"text_before": {"text": "old"}}, 1)["text_before"] == {"text": "old"})

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

    # 3. the note window: planning tasks -----------------------------------
    for t in list(app.toasts):
        t.destroy()
    app.toasts.clear()
    told = []                             # message boxes, which would otherwise wait for a click
    mod.messagebox.showinfo = lambda title, message="", **k: told.append((title, message))
    mod.messagebox.showwarning = lambda title, message="", **k: told.append((title, message))

    def dialogs(win, title):
        return [w for w in win.winfo_children()
                if isinstance(w, tk.Toplevel) and w.title() == title]

    def buttons(win, text):
        return [x for x in walk(win) if isinstance(x, tk.Button) and x.cget("text") == text]

    def press(win, text):
        buttons(win, text)[0].invoke()
        pump(300)

    def click(widget):
        widget.event_generate("<Button-1>")
        pump(250)

    def tasks_tab(win):
        click([x for x in walk(win) if isinstance(x, tk.Label)
               and str(x.cget("text")).startswith("Tasks")][0])

    def sheet(win):
        return "\n".join(x.get("1.0", "end-1c") for x in walk(win) if isinstance(x, tk.Text))

    def asked(job):
        return [p for n, p in Scripted.asked if n == job]

    def suggest(win):
        """Press Suggest and wait for the proposals."""
        Scripted.asked.clear()
        click(labels(win, "✨ Suggest")[0])
        pump(4000, lambda: dialogs(win, "Suggested tasks"))
        return (dialogs(win, "Suggested tasks") or [None])[0]

    app.open_note(note["id"])
    pump(400)
    editor = app._editors[note["id"]]
    editor.geometry("+150+10")
    check("the note shows what was actually said, and the background",
          labels(editor, "“" + ask_msg["text"] + "”") and labels(editor, note["context"]))
    tasks_tab(editor)
    check("Suggest button present in the Tasks tab", len(labels(editor, "✨ Suggest")) == 1)
    shot(editor, "32_note_from_suggestion")
    Scripted.answers["plan"] = {"tasks": [
        {"title": "Pull the Q3 numbers", "stages": [], "points": [], "priority": "normal"},  # there already
        {"title": "Check figures with finance", "stages": [],
         "points": ["Send finance the draft table", "• Agree the rounding"], "priority": "high"},
        {"title": "Get sign-off", "stages": ["Draft", "Review", "Approved"], "points": [],
         "priority": "normal"},
        {"title": "check FIGURES with finance", "stages": [], "points": [], "priority": "low"},
        {"title": "", "stages": [], "points": [], "priority": "normal"}, "junk"],
        "comment": "Assumed the report goes out as a PDF."}
    dlg = suggest(editor)
    check("proposals shown for review", dlg is not None)
    bd = asked("plan")[0]
    check("with the box empty the whole note is planned: the model gets the note, its "
          "background and the tasks so far",
          "Request: plan this whole note." in bd and "Board report from the Q3 numbers" in bd
          and "Background: Sam needs" in bd
          and "- Pull the Q3 numbers  [0% done; no notes yet]\n"
              "- Draft the report  [0% done; no notes yet]" in bd, bd)
    dlg.attributes("-topmost", True)
    shot(dlg, "33_suggested_tasks")
    boxes = [x for x in walk(dlg) if isinstance(x, tk.Checkbutton)]
    check("repeats and junk are filtered out before you see them", len(boxes) == 2, str(len(boxes)))
    check("each proposal shows its plan, and the assistant's own remark is passed on",
          "Check figures with finance    High\n•  Send finance the draft table\n"
          "•  Agree the rounding" in sheet(dlg) and "Draft  →  Review  →  Approved" in sheet(dlg)
          and labels(dlg, "“Assumed the report goes out as a PDF.”"), sheet(dlg))
    press(dlg, "Add tasks")
    titles = [t["title"] for t in note["tasks"]]
    check("chosen tasks added",
          titles == ["Pull the Q3 numbers", "Draft the report", "Check figures with finance", "Get sign-off"],
          str(titles))
    check("...in full: with their steps and priority, and tracked through stages where they have them",
          note["tasks"][2]["points"] == ["Send finance the draft table", "Agree the rounding"]
          and note["tasks"][2]["priority"] == 2 and note["tasks"][2]["mode"] == "percent"
          and note["tasks"][3]["mode"] == "stages"
          and note["tasks"][3]["stages"] == ["Draft", "Review", "Approved"]
          and len({t["id"] for t in note["tasks"]}) == 4)
    editor.destroy()
    pump(300)

    # 3b. what is typed, tasks that exist already, one task, and context files
    # (The engine paces itself to a dozen requests a minute. These steps make
    # more than that in a few seconds, so the count is cleared between them.)
    app.ai.recent.clear()
    trip = app.get_note(5)
    gone = os.path.join(OUT, "gone.txt")
    trip["tasks"] = [mod._normalise_task({"id": 1, "title": "Book the venue"}, 1),
                     mod._normalise_task({"id": 2, "title": "Send invitations", "collapsed": True}, 2)]
    trip["refs"] = [proj, gone]
    app.open_note(5)
    pump(400)
    ed = app._editors[5]
    ed.geometry("+120+10")
    tasks_tab(ed)
    box = labels(ed, mod.TaskPanel.ADD_HINT_AI)[0].master      # the "add a task" field
    Scripted.answers["plan"] = {"tasks": [
        {"title": "book the VENUE", "stages": ["Shortlist", "Visit", "Sign"],
         "points": ["Room for 40", "Near the station"], "priority": "urgent"},
        {"title": "Arrange transport", "stages": [], "points": ["Coach from the office"],
         "priority": "normal"}], "comment": ""}
    dlg = suggest(ed)
    whole = asked("plan")[0]
    check("files named in the note's Context are read and sent along, fenced off as material",
          "BEGIN REFERENCE-" in whole and "A booking service." in whole and "hunter2" not in whole
          and whole.index("Tasks already on the note") < whole.index("BEGIN REFERENCE-"))
    check("...and the answer comes with what was read and what was not",
          [x for x in walk(dlg) if isinstance(x, tk.Label)
           and str(x.cget("text")).startswith("Read for context: 1 folder, 6 files")
           and "gone.txt (not found)" in x.cget("text")])
    check("a task returned under a title you already have fills that task in, not a second one",
          "Book the venue    fills in the task you have\nShortlist  →  Visit  →  Sign\n"
          "•  Room for 40" in sheet(dlg), sheet(dlg))
    dlg.attributes("-topmost", True)
    shot(dlg, "37_plan_fills_in")
    press(dlg, "Apply")
    venue = trip["tasks"][0]
    check("filling in sets stages and steps, keeps the task's own title and priority; new tasks are added",
          [t["title"] for t in trip["tasks"]] == ["Book the venue", "Send invitations", "Arrange transport"]
          and venue["mode"] == "stages" and venue["stages"] == ["Shortlist", "Visit", "Sign"]
          and venue["points"] == ["Room for 40", "Near the station"] and venue["priority"] == 1
          and trip["tasks"][2]["points"] == ["Coach from the office"], str(trip["tasks"]))

    box.insert(0, "send invitations to everyone, with the agenda, two weeks ahead")
    typed = box.get()
    Scripted.answers["plan"] = {"tasks": [                     # the title comes back reworded
        {"title": "Send the invitations.", "stages": ["Draft", "Send", "Chase"],
         "points": ["Attach the agenda", "Send two weeks ahead"], "priority": "high"}], "comment": ""}
    dlg = suggest(ed)
    check("what is typed in the box is what gets planned",
          f"Request: plan this piece of work.\n<<<\n{typed}\n>>>" in asked("plan")[0])
    press(dlg, "Apply")
    invite = trip["tasks"][1]
    check("...and when that is a task you already have, it is planned in full instead of duplicated",
          len(trip["tasks"]) == 3 and invite["mode"] == "stages"
          and invite["stages"] == ["Draft", "Send", "Chase"]
          and invite["points"] == ["Attach the agenda", "Send two weeks ahead"]
          and invite["collapsed"] is False, str(invite))
    check("the box is cleared once its text has become a plan", box.get() == "")

    box.insert(0, "order name badges")
    Scripted.answers["plan"] = {"tasks": [
        {"title": "Order name badges", "stages": [],
         "points": ["40 badges", "Names from the invitation list"], "priority": "low"}],
        "comment": "Assumed one badge per invitee."}
    press(suggest(ed), "Add tasks")
    badges = trip["tasks"][-1]
    check("something new that is typed becomes a new task, planned",
          len(trip["tasks"]) == 4 and badges["title"] == "Order name badges"
          and badges["points"] == ["40 badges", "Names from the invitation list"]
          and badges["priority"] == 0 and box.get() == "", str(badges))

    told.clear()
    Scripted.answers["plan"] = {"tasks": [], "comment": "Everything in this note is already planned."}
    click(labels(ed, "✨ Suggest")[0])
    check("when there is nothing to add, the assistant's reason is shown, with what it read",
          pump(4000, lambda: told) and told[0][0] == "Suggest tasks" and told[0][1].startswith(
              "Everything in this note is already planned.\n\nRead for context:"), str(told))

    hold = threading.Event()              # a model that takes its time
    Scripted.answers["plan"] = lambda p: (hold.wait(8), {"tasks": [], "comment": "Nothing more."})[1]
    told.clear()
    click(labels(ed, "✨ Suggest")[0])
    check("while the assistant works, the button counts the seconds",
          pump(5000, lambda: labels(ed, "Thinking… 2s") or labels(ed, "Thinking… 3s")))
    hold.set()
    check("...and is itself again once the answer is in",
          pump(4000, lambda: told and labels(ed, "✨ Suggest")) and told[0][1].startswith("Nothing more."))

    venue["stage"] = 1                    # this task is now under way
    check("an open task has its own Plan button", len(labels(ed, "✨ Plan")) == 2)
    Scripted.answers["plan"] = {"tasks": [
        {"title": "Reserve the hall", "stages": ["A", "B"],
         "points": ["Near the station", "Ask about catering"], "priority": "low"}], "comment": ""}
    Scripted.asked.clear()
    click(labels(ed, "✨ Plan")[0])
    pump(4000, lambda: dialogs(ed, "Suggested tasks"))
    dlg = dialogs(ed, "Suggested tasks")[0]
    check("Plan on a task asks about that task, and passes on the notes it has",
          "Request: plan this existing task.\n<<<\nBook the venue\n>>>\nIts notes so far:\n<<<\n"
          "- Room for 40\n- Near the station\n>>>" in asked("plan")[0], asked("plan")[0])
    check("only what is new is offered, and a task that is under way keeps its stages",
          "Book the venue    fills in the task you have\n•  Ask about catering" in sheet(dlg)
          and "A  →  B" not in sheet(dlg) and "Near the station" not in sheet(dlg), sheet(dlg))
    press(dlg, "Apply")
    check("...so its progress is not disturbed",
          venue["stages"] == ["Shortlist", "Visit", "Sign"] and venue["stage"] == 1
          and venue["points"] == ["Room for 40", "Near the station", "Ask about catering"]
          and venue["title"] == "Book the venue", str(venue))

    context = [w for w in walk(ed) if hasattr(w, "context")][0].context
    check("the Context row names what the assistant may read",
          "Orbit" in context["summary"].cget("text") and "gone.txt" in context["summary"].cget("text"),
          context["summary"].cget("text"))
    context["remove"](gone)
    saved_refs = lambda: [n for n in json.load(open(mod.DATA_FILE, encoding="utf-8"))
                          if n["id"] == 5][0]["refs"]
    check("a file or folder can be added and removed; a wrong path is refused, a repeat ignored",
          context["add"](os.path.join(OUT, "no-such-folder")) == "That file or folder was not found."
          and context["add"](proj) == "" and trip["refs"] == [proj]
          and context["add"](os.path.join(OUT, "brief.pdf")) == ""
          and trip["refs"] == [proj, os.path.join(OUT, "brief.pdf")] and saved_refs() == trip["refs"],
          str(trip["refs"]))
    shot(ed, "38_note_with_context")
    ed.destroy()
    pump(300)

    blank = mod._normalise({"id": 99, "text": ""}, 99)
    app.notes.append(blank)
    app.open_note(99)
    pump(400)
    tasks_tab(app._editors[99])
    told.clear()
    Scripted.asked.clear()
    click(labels(app._editors[99], "✨ Suggest")[0])
    pump(400)
    check("with nothing typed and nothing in the note, nothing is sent - the app says what to do",
          Scripted.asked == [] and len(told) == 1 and "Type what needs doing" in told[0][1], str(told))
    app._editors[99].destroy()
    app.notes.remove(blank)
    pump(300)

    # 3c. the Note tab: structure the note ---------------------------------
    app.ai.recent.clear()
    post = app.get_note(2)
    original = ("launch post. needs intro about the new sync, pricing unchanged $9, quote from "
                "Dana, publish 3 Dec. also tweet + newsletter")
    post["text"] = original
    post["refs"] = [os.path.join(proj, "README.md")]
    app.open_note(2)
    pump(400)
    ep = app._editors[2]
    ep.geometry("+140+10")
    strip = labels(ep, "Restore the original")[0].master     # the strip that offers the old text
    check("the Note tab has a Structure button; Restore waits until there is something to restore",
          len(buttons(ep, "✨ Structure")) == 1 and not strip.winfo_manager())
    Scripted.answers["structure"] = {
        "summary": "Write and publish the launch post on 3 Dec.",
        "blocks": [{"kind": "heading", "text": "What goes in", "items": []},
                   {"kind": "bullets", "text": "", "items": [
                       "Intro about the new sync", "Pricing unchanged at $9", "Quote from Dana"]},
                   {"kind": "heading", "text": "After publishing", "items": []},
                   {"kind": "text", "text": "Tweet it and put it in the newsletter.", "items": []}],
        "tasks": [{"title": "Draft the post", "stages": ["Outline", "Draft", "Edit"],
                   "points": ["Open with the new sync"], "priority": "normal"}],
        "comment": ""}
    Scripted.asked.clear()
    buttons(ep, "✨ Structure")[0].invoke()
    check("the structured version is shown for review",
          pump(4000, lambda: dialogs(ep, "Structured note")))
    check("the model is given the note and its Context file",
          f"The note:\n<<<\n{original}\n>>>" in asked("structure")[0]
          and "=== FILE README.md ===" in asked("structure")[0])
    dlg = dialogs(ep, "Structured note")[0]
    expected = ("Write and publish the launch post on 3 Dec.\n\nWhat goes in\n"
                "• Intro about the new sync\n• Pricing unchanged at $9\n• Quote from Dana\n\n"
                "After publishing\nTweet it and put it in the newsletter.")
    check("the preview shows the new text with its headings, and the tasks it would add - "
          "and nothing has changed yet",
          any(v.get("1.0", "end-1c") == expected and "f:h2:" in v.tag_names("3.0")
              for v in walk(dlg) if isinstance(v, tk.Text))
          and "Draft the post\nOutline  →  Draft  →  Edit" in sheet(dlg)
          and post["text"] == original and post["tasks"] == [], sheet(dlg))
    dlg.attributes("-topmost", True)
    shot(dlg, "39_structured_note")
    press(dlg, "Apply")
    check("applying replaces the text, formatting included, and adds the tasks",
          post["text"] == expected
          and {("f:body:i", "1.0"), ("f:h2:", "3.0"), ("f:h2:", "8.0")} <= {(t, a) for t, a, _ in post["fmt"]}
          and [t["title"] for t in post["tasks"]] == ["Draft the post"]
          and post["tasks"][0]["stages"] == ["Outline", "Draft", "Edit"], str(post["fmt"]))
    check("the text from before is kept, and Restore is offered under the toolbar",
          post["text_before"]["text"] == original and strip.winfo_manager() == "pack")
    shot(ep, "40_note_structured")
    click(labels(ep, "Restore the original")[0])
    check("Restore brings the original back, and then steps aside again",
          post["text"] == original and "text_before" not in post and not strip.winfo_manager()
          and [n for n in json.load(open(mod.DATA_FILE, encoding="utf-8"))
               if n["id"] == 2][0]["text"] == original)
    buttons(ep, "✨ Structure")[0].invoke()
    pump(4000, lambda: dialogs(ep, "Structured note"))
    dlg = dialogs(ep, "Structured note")[0]
    check("asked again, the task it already added is not offered a second time",
          not [x for x in walk(dlg) if isinstance(x, tk.Checkbutton) and not x.cget("text")])
    press(dlg, "Apply")
    click([x for x in strip.winfo_children() if x.cget("text") == "✕"][0])
    check("dismissing the strip keeps the new version and lets the original go",
          post["text"] == expected and "text_before" not in post and not strip.winfo_manager()
          and len(post["tasks"]) == 1)
    ep.destroy()
    pump(300)
    post["text"], post["fmt"] = "Write the launch post", []   # one line again, for the steps below
    app.ai.recent.clear()

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
    # (the line break is what tells a focus pick, "title / reason", from the
    # same note's row in the plain list underneath)
    check("focus picks appear", pump(4000, lambda: any(
        str(x.cget("text")).startswith("Renew the domain\n") for x in walk(pop) if isinstance(x, tk.Label))))
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
    check("the ceiling on context text is saved as a number", app.config["ai_ref_chars"] == 40000)
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
