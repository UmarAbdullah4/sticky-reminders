"""v2.2 hardening + features, driven through the real app on throwaway data."""
import ctypes
import importlib.machinery
import importlib.util
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import traceback
from datetime import datetime, timedelta
from queue import Queue

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "out22")
shutil.rmtree(OUT, ignore_errors=True)
os.makedirs(OUT)

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
except Exception:
    pass

loader = importlib.machinery.SourceFileLoader("sticky_v22_test", sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(HERE), "sticky_reminders.pyw"))
spec = importlib.util.spec_from_loader(loader.name, loader)
mod = importlib.util.module_from_spec(spec)
loader.exec_module(mod)

mod.log.handlers.clear()
mod.DATA_FILE = os.path.join(OUT, "notes.json")
mod.CONFIG_FILE = os.path.join(OUT, "config.json")
mod.LOG_FILE = os.path.join(OUT, "test.log")
mod.BACKUP_DIR = os.path.join(OUT, "backups")
mod.SUGGEST_FILE = os.path.join(OUT, "suggestions.json")
# never touch the live app's instance lock or port
mod.SingleInstance.MUTEX = f"Local\\StickyRemindersTest.{os.getpid()}"
s = socket.socket()
s.bind(("127.0.0.1", 0))
mod.SINGLE_INSTANCE_PORT = s.getsockname()[1]
s.close()

results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond)))
    print(("PASS  " if cond else "FAIL  ") + name + (f"   [{detail}]" if detail and not cond else ""))


def write(path, text):
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def corrupt_copies(path):
    d, base = os.path.dirname(path), os.path.basename(path)
    return [f for f in os.listdir(d) if f.startswith(base + ".corrupt-")]


NOTE = {"id": 1, "text": "keep me", "created_at": "2026-01-01T10:00:00"}

try:
    # ── credentials at rest ──
    print("-- secrets --")
    enc = mod.protect("hunter2")
    check("protect() encrypts", enc.startswith("dpapi:") and "hunter2" not in enc)
    check("unprotect() round-trips, unicode included",
          mod.unprotect(enc) == "hunter2" and mod.unprotect(mod.protect("pä$$ wörd ✓")) == "pä$$ wörd ✓")
    check("already-encrypted and empty values pass through",
          mod.protect(enc) == enc and mod.protect("") == "" and mod.unprotect("") == "")
    check("an undecryptable value becomes empty, not a crash", mod.unprotect("dpapi:AAAA") == "")
    check("plain values still load (old config files)", mod.unprotect("plain") == "plain")

    write(mod.CONFIG_FILE, json.dumps({"password": "hunter2", "slack_bot_token": "xoxb-plain",
                                       "username": "u@example.com", "mattermost_enabled": False}))
    cfg = mod.load_config()
    check("plaintext credentials are detected on load",
          mod._plaintext_secrets and cfg["password"] == "hunter2" and cfg["slack_bot_token"] == "xoxb-plain")
    mod.save_config(cfg)
    on_disk = read(mod.CONFIG_FILE)
    check("saved config holds no plaintext secret",
          "hunter2" not in on_disk and "xoxb-plain" not in on_disk and on_disk.count("dpapi:") == 2
          and "u@example.com" in on_disk)
    cfg2 = mod.load_config()
    check("and loads back identically", cfg2["password"] == "hunter2" and cfg2["slack_bot_token"] == "xoxb-plain"
          and not mod._plaintext_secrets)
    check("new defaults present", cfg2["mattermost_verify_tls"] is True and cfg2["reply_on_done"] is True
          and cfg2["slack_enabled"] is False)

    write(mod.CONFIG_FILE, "{not json")
    cfg3 = mod.load_config()
    check("a damaged config falls back to defaults and is kept, not overwritten",
          cfg3["trigger_word"] == "@reminder" and len(corrupt_copies(mod.CONFIG_FILE)) == 1)

    check("TLS verification on by default, off only on request",
          mod.tls_context().verify_mode.name == "CERT_REQUIRED" and mod.tls_context().check_hostname
          and mod.tls_context(False).verify_mode.name == "CERT_NONE")

    # ── notes file safety ──
    print("-- notes file --")
    mod.save_notes([dict(NOTE)])
    mod.save_notes([dict(NOTE), {"id": 2, "text": "second", "created_at": "2026-01-02T10:00:00"}])
    check("save keeps the previous version as .bak", len(json.loads(read(mod.DATA_FILE + ".bak"))) == 1)
    write(mod.DATA_FILE, '[{"id": 1, "text": "trunc')
    notes = mod.load_notes()
    check("damaged notes file -> restored from .bak", [n["text"] for n in notes] == ["keep me"]
          and "restored" in (mod._load_problem or "") and ".bak" in mod._load_problem, str(mod._load_problem))
    check("the damaged file is set aside, and not yet overwritten",
          len(corrupt_copies(mod.DATA_FILE)) == 1 and read(mod.DATA_FILE).endswith("trunc"))

    os.makedirs(mod.BACKUP_DIR)
    write(os.path.join(mod.BACKUP_DIR, "notes-2026-11-20.json"), json.dumps([{"id": 9, "text": "from daily"}]))
    write(os.path.join(mod.BACKUP_DIR, "notes-2026-11-19.json"), json.dumps([{"id": 9, "text": "older"}]))
    write(mod.DATA_FILE + ".bak", "garbage")
    time.sleep(1.1)                       # distinct timestamp for the second corrupt copy
    notes = mod.load_notes()
    check(".bak damaged too -> newest daily backup is used",
          [n["text"] for n in notes] == ["from daily"] and "notes-2026-11-20.json" in mod._load_problem)
    shutil.rmtree(mod.BACKUP_DIR)
    notes = mod.load_notes()
    check("nothing readable -> starts empty and says so",
          notes == [] and "started empty" in mod._load_problem)

    mod.save_notes([dict(NOTE)])
    check("a healthy file loads with no warning", len(mod.load_notes()) == 1 and mod._load_problem is None)
    mod.daily_backup()
    today = os.path.join(mod.BACKUP_DIR, f"notes-{datetime.now():%Y-%m-%d}.json")
    check("daily backup written", os.path.exists(today) and json.loads(read(today))[0]["text"] == "keep me")
    for i in range(1, 21):
        write(os.path.join(mod.BACKUP_DIR, f"notes-2025-01-{i:02d}.json"), "[]")
    mod.daily_backup()
    kept = sorted(os.listdir(mod.BACKUP_DIR))
    check("only the newest 14 backups are kept",
          len(kept) == 14 and os.path.basename(today) in kept and "notes-2025-01-07.json" not in kept
          and "notes-2025-01-08.json" in kept, str(kept))
    os.remove(today)
    write(mod.DATA_FILE, "broken")
    mod.daily_backup()
    check("a damaged notes file is never filed as a backup", not os.path.exists(today))

    write(mod.DATA_FILE, json.dumps([{"id": 1, "text": "a"}, {"id": 1, "text": "b"}, {"id": "x", "text": "c"},
                                     "junk", {"text": "d"}]))
    ids = [n["id"] for n in mod.load_notes()]
    check("duplicate / invalid note ids are repaired", len(ids) == 4 and len(set(ids)) == 4
          and all(type(i) is int for i in ids), str(ids))
    write(mod.DATA_FILE, json.dumps({"notes": [{"id": 5, "text": "wrapped"}]}))
    check('{"notes": [...]} wrapper accepted', mod.load_notes()[0]["text"] == "wrapped")

    # ── small pure functions ──
    print("-- helpers --")
    check("window position: sane kept, off-screen and nonsense replaced",
          mod.usable_geometry("900x600+100+80") == "900x600+100+80"
          and mod.usable_geometry("900x600+-99000+-99000") == "980x680"
          and mod.usable_geometry("10x10+0+0") == "980x680"
          and mod.usable_geometry("garbage") == "980x680" and mod.usable_geometry("") == "980x680")
    fri = datetime(2026, 11, 20, 9, 0)    # a Friday
    now = datetime(2026, 11, 20, 12, 0)
    nx = mod.next_occurrence
    check("repeat: daily / weekly", nx(fri, "daily", now) == datetime(2026, 11, 21, 9, 0)
          and nx(fri, "weekly", now) == datetime(2026, 11, 27, 9, 0))
    check("repeat: weekdays skips the weekend", nx(fri, "weekdays", now) == datetime(2026, 11, 23, 9, 0))
    check("repeat: monthly keeps the day, clamps short months",
          nx(datetime(2026, 1, 31, 8, 0), "monthly", datetime(2026, 1, 31, 9, 0)) == datetime(2026, 2, 28, 8, 0)
          and nx(datetime(2026, 1, 31, 8, 0), "monthly", datetime(2026, 3, 1, 9, 0)) == datetime(2026, 3, 31, 8, 0)
          and nx(datetime(2026, 12, 15, 8, 0), "monthly", datetime(2026, 12, 15, 9, 0)) == datetime(2027, 1, 15, 8, 0))
    check("repeat: a long-overdue note jumps to the next future slot",
          nx(datetime(2020, 1, 1, 7, 0), "daily", now) == datetime(2026, 11, 21, 7, 0))
    check("repeat: none, and no due date",
          nx(fri, "", now) is None and nx(None, "daily", now) == now + timedelta(days=1))

    # ── how the app is launched and packaged ──
    print("-- packaging --")
    program, arguments, folder = mod.launch_command()
    check("from source, the app relaunches itself as pythonw + this script",
          not mod.FROZEN and program.lower().endswith("pythonw.exe")
          and arguments.startswith('"') and arguments.endswith('sticky_reminders.pyw"')
          and folder == mod.SCRIPT_DIR, f"{program} {arguments}")
    check("a copy run from source and an installed copy use different login shortcuts",
          os.path.basename(mod.SHORTCUT_PATH) == "StickyReminders.lnk")
    check("the app icon is where the app looks for it", os.path.isfile(mod.ICON_FILE), mod.ICON_FILE)
    st = os.path.join(OUT, "selftest")
    code = subprocess.run([sys.executable, "-B", mod.__file__, "--self-test", st]).returncode
    report = json.load(open(os.path.join(st, "self_test.json"), encoding="utf-8"))
    check("the built-in self-test passes (the same one the installer build runs on the .exe)",
          code == 0 and report["passed"] and report["version"] == mod.APP_VERSION
          and not report["packaged"], json.dumps(report["checks"]))

    # ── the installed program taking over from a copy run from source ──
    print("-- taking over from a copy run from source --")
    ss = mod.source_script
    check("a command line counts as the app only when the app is what it runs",
          ss(r'"C:\Py\pythonw.exe" "C:\Apps\Sticky Notes\sticky_reminders.pyw"')
          == r"C:\Apps\Sticky Notes\sticky_reminders.pyw"
          and ss(r"C:\Py\pythonw.exe C:\Apps\sticky_reminders.pyw --show") == r"C:\Apps\sticky_reminders.pyw"
          and ss(r'python -B "C:\Apps\STICKY_REMINDERS.PYW"') == r"C:\Apps\STICKY_REMINDERS.PYW"
          and ss(r"python -B tests\test_app.py C:\Apps\sticky_reminders.pyw") is None
          and ss(r"python C:\Apps\sticky_reminders.pyw --self-test C:\tmp") is None
          and ss(r"python C:\Apps\other.pyw") is None and ss("python") is None and ss("") is None)

    # From here on the "copy run from source" is a stand-in under a name of its
    # own, so that nothing below can mistake the real app for it and stop it.
    mod.SOURCE_SCRIPT = f"sticky_standin_{os.getpid()}.pyw"
    old_home = os.path.join(OUT, "old copy")                 # a folder name with a space in it
    new_home = os.path.join(OUT, "installed settings")
    logins = os.path.join(OUT, "logins")
    os.makedirs(old_home)
    os.makedirs(logins)
    standin = os.path.join(old_home, mod.SOURCE_SCRIPT)
    write(standin, "import time\ntime.sleep(300)\n")
    write(os.path.join(old_home, "config.json"),
          json.dumps({"trigger_word": "@todo", "password": mod.protect("pw-from-the-old-copy")}))
    mod.SOURCE_SHORTCUT = os.path.join(logins, "StickyReminders.lnk")
    mod.SHORTCUT_PATH = os.path.join(logins, "Sticky Reminders.lnk")
    harness_config = (mod.CONFIG_DIR, mod.CONFIG_FILE)
    mod.CONFIG_DIR, mod.CONFIG_FILE = new_home, os.path.join(new_home, "config.json")

    def old_login_shortcut():
        ps = (f"$s=(New-Object -COM WScript.Shell).CreateShortcut('{mod.SOURCE_SHORTCUT}');"
              f"$s.TargetPath='{mod.PYTHONW}';$s.Arguments='\"{standin}\"';$s.Save()")
        subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=True, capture_output=True)

    asked, looked, answer = [], [], {"yes": True}
    mod.message_box = lambda text, question=False: (asked.append((question, text)), answer["yes"])[1]
    real_find, real_stop = mod.find_source_copy, mod.stop_processes
    real_who = mod.SingleInstance.__dict__["who"]
    mod.find_source_copy = lambda: (looked.append(1), real_find())[1]
    note = os.path.join(old_home, "This copy has been replaced.txt")
    holder_script = os.path.join(OUT, "holds_the_doorbell.py")
    write(holder_script, "import socket, sys, time\ns = socket.socket()\n"
                         "s.bind(('127.0.0.1', int(sys.argv[1])))\ns.listen(1)\ntime.sleep(300)\n")

    def who_says(text):
        mod.SingleInstance.who = classmethod(lambda cls: text)

    class Guard:
        took_over = 0

        def take_over(self):
            Guard.took_over += 1
            return True

    def decision():
        return mod.load_config().get("source_copy")

    def fresh(shortcut=True):
        """An installed copy that has never been started, beside an old copy."""
        shutil.rmtree(new_home, ignore_errors=True)
        for leftover in (mod.SHORTCUT_PATH, mod.SOURCE_SHORTCUT, standin + ".old", note):
            if os.path.exists(leftover):
                os.remove(leftover)
        write(standin, "import time\ntime.sleep(300)\n")
        if shortcut:
            old_login_shortcut()
        asked.clear()
        looked.clear()
        answer["yes"] = True

    fresh(shortcut=False)
    aside = mod.set_aside_source_copy(old_home)
    check("setting the old copy aside renames its program file and leaves a note saying why",
          aside == standin + ".old" and os.path.isfile(aside) and not os.path.exists(standin)
          and "switched off" in read(note), aside)
    fresh(shortcut=False)
    os.makedirs(os.path.join(old_home, ".git"))
    check("a developer's checkout is left alone, and so is a folder that does not hold the app",
          mod.set_aside_source_copy(old_home) == "" and os.path.isfile(standin)
          and mod.set_aside_source_copy(logins) == "" and mod.set_aside_source_copy("") == "")
    os.rmdir(os.path.join(old_home, ".git"))

    fresh()
    started = [subprocess.Popen([sys.executable, standin]),
               subprocess.Popen([sys.executable, holder_script, str(mod.SINGLE_INSTANCE_PORT)])]
    old, holder = started
    try:
        time.sleep(1.5)
        found = real_find()
        check("a copy run from source is found by what it runs, and its folder with it",
              bool(found) and old.pid in found["pids"] and found["folder"] == old_home, str(found))
        check("...and a Python program answering the app's doorbell counts as one too",
              bool(found) and holder.pid in found["pids"], str(found))

        who_says("")                                          # an old copy does not know the question
        now_first = mod.settle_source_copy(Guard(), False)
        time.sleep(0.3)
        cfg = mod.load_config()
        check("on Yes the old copy is stopped and this one takes its place",
              now_first is True and Guard.took_over == 1 and old.poll() is not None
              and holder.poll() is not None and [q for q, _ in asked] == [True]
              and "running now" in asked[0][1], str(asked))
        check("...its settings and sign-ins are carried over",
              cfg["trigger_word"] == "@todo" and cfg["password"] == "pw-from-the-old-copy"
              and cfg.get("source_copy") == "replaced", str(cfg.get("source_copy")))
        check("...its start-at-login entry is replaced by this program's",
              not os.path.exists(mod.SOURCE_SHORTCUT) and os.path.exists(mod.SHORTCUT_PATH))
        check("...and it is switched off: its program file is renamed, so nothing can start it again",
              not os.path.exists(standin) and os.path.isfile(standin + ".old") and os.path.isfile(note))
        check("stopping a process that has already gone is not an error", real_stop([old.pid]) is True)

        asked.clear()
        looked.clear()
        check("after that, an ordinary start does not look or ask",
              mod.settle_source_copy(Guard(), True) is True and asked == [] and looked == [])
        who_says(f"{mod.APP_VERSION} packaged")
        check("...and a second launch of this same program is told apart without a search",
              mod.settle_source_copy(Guard(), False) is False and asked == [] and looked == [])

        # the old copy is back and running (its file restored by hand, say)
        write(standin, "import time\ntime.sleep(300)\n")
        started.append(subprocess.Popen([sys.executable, standin]))
        time.sleep(1.0)
        who_says("")
        took = Guard.took_over
        check("if the old copy turns up running again, it is stopped and set aside again, unasked",
              mod.settle_source_copy(Guard(), False) is True and started[-1].poll() is not None
              and Guard.took_over == took + 1 and asked == [] and not os.path.exists(standin))

        # a copy that cannot be stopped (started as administrator)
        fresh()
        started.append(subprocess.Popen([sys.executable, standin]))
        time.sleep(1.0)
        took = Guard.took_over
        mod.stop_processes = lambda pids, wait=5.0: False
        result = mod.settle_source_copy(Guard(), False)
        mod.stop_processes = real_stop
        check("an old copy that Windows will not let the app stop is reported, with what to do",
              result is False and Guard.took_over == took and [q for q, _ in asked] == [True, False]
              and "still running" in asked[1][1] and "will not come back" in asked[1][1]
              and decision() == "replaced" and not os.path.exists(standin)
              and started[-1].poll() is None, str(asked))
    finally:
        mod.stop_processes = real_stop
        for p in started:
            if p.poll() is None:
                p.kill()

    # the old copy is not running, only set to start at login - and the answer is No
    fresh()
    answer["yes"] = False
    first = mod.settle_source_copy(Guard(), True)
    check("on No nothing is touched, and it is never looked for or asked about again",
          first is True and len(asked) == 1 and "running now" not in asked[0][1]
          and os.path.exists(mod.SOURCE_SHORTCUT) and not os.path.exists(mod.SHORTCUT_PATH)
          and os.path.isfile(standin) and decision() == "kept"
          and mod.load_config()["trigger_word"] == "@reminder"
          and mod.settle_source_copy(Guard(), False) is False and len(asked) == 1 and looked == [1],
          str((asked, looked)))

    fresh()
    first = mod.settle_source_copy(Guard(), True)
    check("with the old copy not running, its folder is read from its login shortcut",
          first is True and mod.load_config()["trigger_word"] == "@todo" and decision() == "replaced"
          and not os.path.exists(mod.SOURCE_SHORTCUT) and os.path.exists(mod.SHORTCUT_PATH)
          and os.path.isfile(standin + ".old") and len(asked) == 1, str(asked))

    fresh(shortcut=False)
    check("a PC with no sign of an old copy is not searched, asked, or written to",
          mod.settle_source_copy(Guard(), True) is True and asked == [] and looked == []
          and not os.path.exists(mod.CONFIG_FILE))
    who_says("")
    check("something else in the app's place is searched for once, and not mistaken for an old copy",
          mod.settle_source_copy(Guard(), False) is False and asked == [] and looked == [1]
          and os.path.isfile(standin))
    setattr(mod.SingleInstance, "who", real_who)
    mod.find_source_copy = real_find
    mod.CONFIG_DIR, mod.CONFIG_FILE = harness_config

    # ── single instance ──
    print("-- single instance --")
    shown = []
    a = mod.SingleInstance(lambda: shown.append(1))
    check("first copy acquires", a.acquire() is True)
    b = mod.SingleInstance(lambda: shown.append("wrong"))
    check("second copy is refused", b.acquire() is False)
    check("second copy can ring the first", mod.SingleInstance.signal_existing() is True
          and (time.sleep(0.2) or shown == [1]), str(shown))
    with socket.create_connection(("127.0.0.1", mod.SINGLE_INSTANCE_PORT), timeout=2) as c:
        c.sendall(b"GET / HTTP/1.0\r\n\r\n")
        time.sleep(0.3)
    check("a stray connection does not pop the window", shown == [1])
    check("a running copy says what it is when asked, without showing itself",
          mod.SingleInstance.who() == f"{mod.APP_VERSION} source" and (time.sleep(0.2) or shown == [1]),
          str(shown))
    # the first copy goes away (as when the installed program stops an old copy)
    a.sock.close()
    b.on_show = lambda: shown.append("b")
    check("with nothing listening, the question simply goes unanswered", mod.SingleInstance.who() == "")
    check("after the first copy has gone, the second can take its place and answer the doorbell",
          b.take_over(wait=3) is True and mod.SingleInstance.signal_existing() is True
          and (time.sleep(0.2) or shown == [1, "b"]), str(shown))
    b.sock.close()
    # a different program owns the port: the app must still start
    squatter = socket.socket()
    squatter.bind(("127.0.0.1", 0))
    squatter.listen(1)
    mod.SINGLE_INSTANCE_PORT = squatter.getsockname()[1]
    mod.SingleInstance.MUTEX += ".b"
    c2 = mod.SingleInstance(lambda: None)
    check("port taken by another program no longer blocks startup", c2.acquire() is True)
    check("...and that program is not mistaken for a running copy",
          mod.SingleInstance.signal_existing() is False)
    squatter.close()

    # ══════════════════════ the app itself ══════════════════════
    print("-- app --")

    def _no_tray(self, app):
        self.app, self.icon, self.available = app, None, False

    mod.Tray.__init__ = _no_tray
    for f in os.listdir(OUT):
        if ".corrupt-" in f:
            os.remove(os.path.join(OUT, f))
    shutil.rmtree(mod.BACKUP_DIR, ignore_errors=True)
    write(mod.CONFIG_FILE, json.dumps({
        "password": "hunter2", "username": "u@example.com", "mattermost_enabled": False,
        "start_minimized": False, "reminders_enabled": True, "sound_enabled": False,
        "reminder_interval_minutes": 600, "window_geometry": "900x600+-99000+-99000"}))
    write(mod.CONFIG_FILE + ".bak", json.dumps({"password": "hunter2"}))
    yesterday = (datetime.now() - timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)
    write(mod.DATA_FILE, json.dumps([
        {"id": 1, "text": "plain note", "created_at": "2026-01-01T10:00:00"},
        {"id": 2, "text": "weekly report", "created_at": "2026-01-02T10:00:00", "repeat": "daily",
         "due": yesterday.isoformat(timespec="seconds"), "due_notified": True,
         "tasks": [{"id": 1, "title": "collect numbers", "percent": 100},
                   {"id": 2, "title": "send", "mode": "stages", "stage": 4}]},
        {"id": 3, "text": "overdue thing", "created_at": "2026-01-03T10:00:00",
         "due": "2026-01-05T09:00:00", "due_notified": False},
    ]))

    app = mod.StickyNotesApp()
    tk = mod.tk

    def pump(ms=300):
        end = time.time() + ms / 1000
        while time.time() < end:
            app.root.update()
            time.sleep(0.01)

    def walk(w):
        yield w
        for ch in w.winfo_children():
            yield from walk(ch)

    def shot(win, name):
        from PIL import ImageGrab
        win.attributes("-topmost", True)
        win.lift()
        pump(350)
        x, y, w, h = win.winfo_rootx(), win.winfo_rooty(), win.winfo_width(), win.winfo_height()
        ImageGrab.grab(bbox=(x, y, x + w, y + h)).save(os.path.join(OUT, name + ".png"))
        print(f"SHOT  {name}  {w}x{h}")

    def editors():
        return [w for w in app.root.winfo_children()
                if isinstance(w, tk.Toplevel) and w.winfo_exists() and w.title() == "Note"]

    pump(500)
    disk = read(mod.CONFIG_FILE) + read(mod.CONFIG_FILE + ".bak")
    check("startup encrypts a plaintext config, and its .bak", "hunter2" not in disk and "dpapi:" in disk)
    check("in memory the credential is usable", app.config["password"] == "hunter2")
    gx, gy = app.root.winfo_x(), app.root.winfo_y()
    check("an off-screen saved position is not restored", -5000 < gx < 20000 and -5000 < gy < 20000, f"{gx},{gy}")
    check("startup took the daily backup",
          os.path.exists(os.path.join(mod.BACKUP_DIR, f"notes-{datetime.now():%Y-%m-%d}.json")))
    app.root.geometry("1000x680+40+30")
    pump(200)

    # heartbeat robustness
    real = app._heartbeat
    app._heartbeat = lambda: 1 / 0
    try:
        app._tick()
        survived = True
    except Exception:
        survived = False
    app._heartbeat = real
    check("an error inside the heartbeat cannot stop the reminder loop", survived)
    app.msg_queue.put({"type": "reminder"})            # malformed: no text
    app.ui_queue.put(lambda: 1 / 0)
    try:
        app._poll_queues()
        survived = True
    except Exception:
        survived = False
    check("a bad queue message cannot stop the message loop", survived)

    # due alerts and mute
    n3 = app.get_note(3)
    check("an overdue note is announced at startup", n3["due_notified"] is True and len(app.toasts) == 1)
    for t in list(app.toasts):            # start the mute scenario from a clean slate
        t.destroy()
    app.toasts.clear()
    n3["due_notified"] = False
    app.config["muted_until"] = "forever"
    app._heartbeat()
    check("due while muted: not announced, and not marked as announced",
          n3["due_notified"] is False and len(app.toasts) == 0, str(len(app.toasts)))
    app.config["muted_until"] = ""
    app._heartbeat()
    pump(100)
    check("...announced once reminders are back on", n3["due_notified"] is True and len(app.toasts) == 1)
    app._heartbeat()
    check("...and only once", len(app.toasts) == 1)

    # toast stacking
    for t in list(app.toasts):
        t.destroy()
    app.toasts.clear()
    for i in range(3):
        app.show_toast(f"t{i}", "x", sound=False)
    first = app.toasts[0]
    app.toasts.remove(first)
    first.destroy()
    app.show_toast("new", "x", sound=False)
    slots = sorted(t._slot for t in app.toasts)
    check("a new toast takes the free slot instead of covering another", slots == [0, 1, 2], str(slots))
    for i in range(30):
        app.show_toast(f"flood{i}", "x", sound=False)
    ys = [t.winfo_y() for t in app.toasts]
    check("a flood of toasts never runs off the top of the screen",
          min(ys) >= 0 and len(set(t._slot for t in app.toasts)) == len(app.toasts), f"min y {min(ys)}")
    for t in list(app.toasts):
        t.destroy()
    app.toasts.clear()

    # periodic reminder when everything is snoozed
    for n in app.notes:
        n["snooze_until"] = (datetime.now() + timedelta(hours=5)).isoformat(timespec="seconds")
    app.next_reminder = datetime.now() - timedelta(seconds=1)
    app._heartbeat()
    pump(100)
    check("no pointless popup when every pending note is snoozed",
          app.reminder_popup is None and len(app.toasts) == 0)
    for n in app.notes:
        n["snooze_until"] = ""

    # repeating note
    n2 = app.get_note(2)
    app.mark_done(2)
    pump(150)
    due = mod.parse_dt(n2["due"])
    check("repeating note: done moves it on instead of finishing it",
          n2["done"] is False and due > datetime.now() and (due.hour, due.minute) == (9, 0)
          and due - datetime.now() <= timedelta(days=1), str(n2["due"]))
    check("...tasks start over, it sleeps until then, alert re-armed",
          [mod.task_progress(t) for t in n2["tasks"]] == [0, 0] and n2["snooze_until"] == n2["due"]
          and n2["due_notified"] is False and n2.get("last_done_at"))
    check("...and it is out of the reminder list until its date", n2 not in app._reminder_notes())

    # one window per note, and closing keeps what was typed
    app.open_note(1)
    pump(250)
    app.open_note(1)
    pump(250)
    check("opening a note twice gives one window", len(editors()) == 1, str(len(editors())))
    win = editors()[0]
    win.geometry("+140+20")
    due_entry = [w for w in walk(win) if isinstance(w, tk.Entry) and str(w.cget("width")) == "17"][0]
    due_entry.focus_force()
    pump(150)
    due_entry.insert(0, "2031-02-03 14:30")
    tag_entry = [w for w in walk(win) if isinstance(w, tk.Entry) and str(w.cget("width")) == "24"][0]
    tag_entry.insert(0, "alpha, #beta")
    shot(win, "20_note_with_repeat")
    win.focus_force()
    due_entry.focus_force()
    pump(100)
    due_entry.event_generate("<Escape>")
    pump(300)
    n1 = app.get_note(1)
    check("Esc closes the note and keeps the typed due date and tags",
          len(editors()) == 0 and n1["due"] == "2031-02-03T14:30:00" and n1["tags"] == ["alpha", "beta"],
          f"{n1['due']} {n1['tags']}")

    n1["due_notified"] = True
    app.open_note(1)
    pump(250)
    win = editors()[0]
    due_entry = [w for w in walk(win) if isinstance(w, tk.Entry) and str(w.cget("width")) == "17"][0]
    due_entry.focus_force()
    pump(100)
    due_entry.event_generate("<Return>")
    pump(100)
    check("re-confirming an unchanged due date does not re-arm its alert", n1["due_notified"] is True)
    combos = [w for w in walk(win) if isinstance(w, mod.ttk.Combobox)]
    combos[0].set("Weekly")
    combos[0].event_generate("<<ComboboxSelected>>")
    pump(100)
    check("repeat picker stores the schedule", n1["repeat"] == "weekly")
    app.archive_note(1)
    pump(250)
    check("archiving from outside closes that note's window", len(editors()) == 0 and n1["archived"])
    n1["archived"] = False

    # the X button
    quit_calls = []
    real_quit = app.quit_app
    app.quit_app = lambda: quit_calls.append(1)
    app.config["close_to_tray"] = False
    app.on_close()
    app.config["close_to_tray"] = True
    app.on_close()
    pump(150)
    app.quit_app = real_quit
    check("X quits only when 'close to tray' is off", quit_calls == [1])
    app.root.deiconify()
    pump(200)

    # connector notes inside the app
    class Fake(mod.SlackConnector):
        def __init__(self):
            super().__init__({"trigger_word": "@reminder"}, Queue())
            self.sent = []
            self.running = True

        def reply(self, note, text):
            self.sent.append((note["post_id"], note.get("thread_id"), text))
            return True

    fake = Fake()
    app.connectors["slack"] = fake
    msg = {"type": "reminder", "source": "slack", "text": "ship it", "priority": 2, "sender": "Sam",
           "channel": "launch", "channel_id": "C1", "post_id": "10.1", "thread_id": "9.0",
           "is_self": False, "timestamp": "01:00 PM"}
    app.msg_queue.put(dict(msg))
    app.msg_queue.put(dict(msg))                       # redelivered by the service
    app._poll_queues()
    pump(200)
    chat = [n for n in app.notes if n.get("source") == "slack"]
    check("a connector message becomes one note, even if delivered twice", len(chat) == 1)
    cn = chat[0]
    check("the note carries its origin", (cn["sender"], cn["channel"], cn["thread_id"], cn["color"], cn["priority"])
          == ("Sam", "launch", "9.0", mod.SlackConnector.color, 2), str(cn))
    app.filter_var.set("chat")
    check("'From chat' filter shows it", [n["id"] for n in app._visible_notes()] == [cn["id"]])
    app.filter_var.set("all")
    check("labels", mod.is_chat(cn) and not mod.is_chat(n1) and mod.source_label(cn) == "Slack")

    app.config["reply_on_done"] = False
    app.mark_done(cn["id"])
    time.sleep(0.3)
    check("no reply when replies are switched off", fake.sent == [] and cn["done"])
    app.reopen_note(cn["id"])
    app.config["reply_on_done"] = True
    app.mark_done(cn["id"])
    check("marking done replies in the original thread",
          (time.sleep(0.4) or fake.sent == [("10.1", "9.0", "✅ *Done* (completed by me): ship it")]), str(fake.sent))

    # stale status from a replaced connector is ignored
    app._set_status("slack", "connecting…", None)
    app.msg_queue.put({"type": "status", "source": "slack", "conn": object(), "text": "OLD", "ok": False})
    app.msg_queue.put({"type": "status", "source": "slack", "conn": fake, "text": "Connected to Acme", "ok": True})
    app._poll_queues()
    check("only the connector in charge can set its status", app.conn_state["slack"] == ("Connected to Acme", True))

    # import
    exported = os.path.join(OUT, "export.json")
    write(exported, json.dumps(app.notes))
    mod.filedialog.askopenfilename = lambda **k: exported
    count = len(app.notes)
    app.import_json()
    check("importing your own export adds nothing", len(app.notes) == count)
    data = json.loads(read(exported))
    data.append({"id": 1, "text": "came from a backup", "created_at": "2025-05-05T05:05:05",
                 "tasks": [{"title": "t", "percent": 40}]})
    write(exported, json.dumps(data))
    app.import_json()
    new = app.notes[-1]
    check("a note you no longer have is imported with a fresh id",
          len(app.notes) == count + 1 and new["text"] == "came from a backup"
          and len({n["id"] for n in app.notes}) == len(app.notes) and new["tasks"][0]["percent"] == 40)
    write(exported, '{"not": "notes"}')
    errors = []
    mod.messagebox.showerror = lambda *a, **k: errors.append(a)
    app.import_json()
    check("a file that is not notes is refused politely", len(errors) == 1 and len(app.notes) == count + 1)

    # save failure is surfaced
    for t in list(app.toasts):
        t.destroy()
    app.toasts.clear()
    real_write = mod._atomic_write
    mod._atomic_write = lambda *a: (_ for _ in ()).throw(PermissionError("disk says no"))
    ok = mod.save_notes(app.notes)
    app._heartbeat()
    app._heartbeat()
    check("a failed save is reported to the user, once", ok is False and len(app.toasts) == 1)
    mod._atomic_write = real_write
    mod.save_notes(app.notes)
    app._heartbeat()
    check("...and the warning re-arms after a good save", app._save_warned is False)

    # status bar + settings
    app.conn_state = {"mattermost": ("Connected as @alex", True), "slack": ("Connected to Acme", True),
                      "discord": ("Discord rejected the bot token", False)}
    app._paint_status()
    app.render_notes()
    pump(300)
    shot(app.root, "21_main_with_connectors")
    app.conn_state = {"mattermost": ("Connected as @alex", True)}
    app._paint_status()
    pump(200)

    app.open_settings("Connectors")
    pump(400)
    app.open_settings("Connectors")
    pump(200)
    settings = [w for w in app.root.winfo_children() if isinstance(w, tk.Toplevel) and w.title() == "Settings"]
    check("only one Settings window", len(settings) == 1)
    sw, book, pages = app._settings
    check("opens on the requested tab", book.tab(book.select(), "text") == "Connectors")
    sw.geometry("+160+20")
    shot(sw, "22_settings_mattermost")
    for name in ("Slack", "Discord", "Telegram"):
        chip = [w for w in walk(pages["Connectors"]) if isinstance(w, tk.Label) and w.cget("text") == name][0]
        chip.event_generate("<Button-1>")
        pump(250)
        shot(sw, f"23_settings_{name.lower()}")
    entries = [w for w in walk(pages["Connectors"]) if isinstance(w, tk.Entry)]
    entries[0].delete(0, "end")
    entries[0].insert(0, "  @todo ")
    started = []
    app.connect = lambda key: started.append(key)
    save = [w for w in walk(sw) if isinstance(w, tk.Button) and w.cget("text") == "Save"][0]
    save.invoke()
    pump(300)
    check("Save stores the trigger word and closes", app.config["trigger_word"] == "@todo" and not sw.winfo_exists())
    check("Save does not restart a disabled connector", started == [], str(started))
    check("saved config still has no plaintext secret", "hunter2" not in read(mod.CONFIG_FILE))
    app.open_settings()
    pump(300)
    sw, book, pages = app._settings
    for w in walk(pages["Do Not Disturb"]):
        if isinstance(w, tk.Entry) and w.get() == "22:00":
            w.delete(0, "end")
            w.insert(0, "25:99")
    warned = []
    mod.messagebox.showwarning = lambda *a, **k: warned.append(a)
    [w for w in walk(sw) if isinstance(w, tk.Button) and w.cget("text") == "Save"][0].invoke()
    pump(200)
    check("an impossible quiet-hours time is rejected", len(warned) == 1 and sw.winfo_exists()
          and app.config["quiet_start"] == "22:00")
    sw.destroy()
    pump(200)
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
