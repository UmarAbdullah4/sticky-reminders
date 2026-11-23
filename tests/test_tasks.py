"""Drive the real script against throwaway data and screenshot the new UI.

Nothing here touches the user's notes, config or log: every path is redirected
into this folder before the app object is created.
"""
import ctypes
import importlib.machinery
import importlib.util
import json
import os
import sys
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "out")
os.makedirs(OUT, exist_ok=True)
SCRIPT = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(HERE), "sticky_reminders.pyw")

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
except Exception:
    pass

loader = importlib.machinery.SourceFileLoader("sticky_under_test", SCRIPT)
spec = importlib.util.spec_from_loader(loader.name, loader)
mod = importlib.util.module_from_spec(spec)
loader.exec_module(mod)

# ── isolate from the real app ──
mod.log.handlers.clear()
mod.DATA_FILE = os.path.join(OUT, "notes.json")
mod.CONFIG_FILE = os.path.join(OUT, "config.json")
mod.LOG_FILE = os.path.join(OUT, "test.log")
mod.BACKUP_DIR = os.path.join(OUT, "backups")
mod.SUGGEST_FILE = os.path.join(OUT, "suggestions.json")
import shutil
shutil.rmtree(mod.BACKUP_DIR, ignore_errors=True)
for f in (mod.DATA_FILE, mod.CONFIG_FILE, mod.DATA_FILE + ".bak", mod.CONFIG_FILE + ".bak"):
    if os.path.exists(f):
        os.remove(f)
for f in os.listdir(OUT):
    if ".corrupt-" in f:
        os.remove(os.path.join(OUT, f))


def _no_tray(self, app):
    self.app, self.icon, self.available = app, None, False


mod.Tray.__init__ = _no_tray

panels = []
_orig_init = mod.TaskPanel.__init__


def _spy(self, *a, **k):
    _orig_init(self, *a, **k)
    panels.append(self)


mod.TaskPanel.__init__ = _spy

with open(mod.CONFIG_FILE, "w", encoding="utf-8") as f:
    json.dump({"mattermost_enabled": False, "start_minimized": False,
               "reminders_enabled": False, "sound_enabled": False}, f)

seed = [
    # exactly the shape of a pre-2.1 note: no `tasks`, no `view`
    {"id": 1, "text": "Plain old note written before tasks existed.", "color": 0,
     "source": "local", "done": False, "archived": False, "priority": 1, "due": "",
     "tags": [], "fmt": [], "snooze_until": "", "due_notified": False,
     "created_at": "2026-09-01T10:00:00", "done_at": "", "pinned": False},
    {"id": 2, "text": "Website relaunch", "color": 4, "priority": 2, "view": "tasks",
     "tags": ["work"], "tasks": [
         {"id": 1, "title": "Design the landing page", "mode": "percent", "percent": 60,
          "points": ["Hero needs a shorter headline",
                     "Ask Sara for the final logo files before Friday, the ones in the "
                     "shared folder are the old low resolution exports"]},
         {"id": 2, "title": "Checkout flow", "mode": "stages", "stage": 2,
          "stages": ["Spec", "Build", "QA", "Release"], "points": ["Stripe test keys in vault"]},
         {"id": 3, "title": "Write the announcement email", "mode": "percent",
          "percent": 0, "collapsed": True, "points": ["tone: short", "one CTA only"]},
         {"id": 4, "title": "Buy the domain", "mode": "percent", "percent": 100,
          "collapsed": True},
     ]},
    {"id": 3, "text": "", "color": 1, "tasks": [
        {"id": 1, "title": "Book flights", "mode": "percent", "percent": 100, "collapsed": True},
        {"id": 2, "title": "Hotel", "mode": "stages", "stage": 1, "collapsed": True},
        {"id": 3, "title": "Visa paperwork", "mode": "percent", "percent": 25, "collapsed": True},
        {"id": 4, "title": "Travel insurance", "collapsed": True},
        {"id": 5, "title": "Airport transfer", "collapsed": True},
    ]},
    {"id": 4, "text": "Quarter close", "color": 2, "tasks": [
        {"id": 1, "title": "Reconcile accounts", "percent": 100, "collapsed": True},
        {"id": 2, "title": "File the return", "mode": "stages", "stage": 4, "collapsed": True},
    ]},
    {"id": 5, "text": "Finished project", "color": 5, "done": True, "view": "tasks", "tasks": [
        {"id": 1, "title": "Kickoff", "percent": 100, "points": ["went fine"]},
        {"id": 2, "title": "Handover", "mode": "stages", "stage": 3,
         "stages": ["Draft", "Review", "Sign-off"]},
    ]},
    # deliberately broken shapes - must load, not crash
    {"id": 6, "text": "Hand-edited junk", "color": 3, "tasks": [
        "just a string", {"title": None, "percent": "abc", "stages": "nope", "stage": 99,
                          "points": "x", "id": 1},
        {"id": 1, "title": "dup id", "percent": 250}]},
]
with open(mod.DATA_FILE, "w", encoding="utf-8") as f:
    json.dump(seed, f)

results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))
    print(("PASS  " if cond else "FAIL  ") + name + (f"   [{detail}]" if detail and not cond else ""))


app = mod.StickyNotesApp()
app.root.geometry("1010x700+40+30")
app.root.attributes("-topmost", True)


def pump(ms=350):
    end = time.time() + ms / 1000
    while time.time() < end:
        app.root.update()
        time.sleep(0.01)


def shot(win, name):
    from PIL import ImageGrab
    win.attributes("-topmost", True)      # recolouring re-applies the pin state
    win.lift()
    pump(300)
    x, y = win.winfo_rootx(), win.winfo_rooty()
    w, h = win.winfo_width(), win.winfo_height()
    img = ImageGrab.grab(bbox=(x, y, x + w, y + h))
    path = os.path.join(OUT, name + ".png")
    img.save(path)
    print(f"SHOT  {name}  {w}x{h}")


def tops():
    return [w for w in app.root.winfo_children()
            if isinstance(w, mod.tk.Toplevel) and w.winfo_exists() and w.title() == "Note"]


def open_note(nid, x=120, y=30):
    before = set(tops())
    app.open_note(nid)
    pump(200)
    win = [w for w in tops() if w not in before][0]
    win.geometry(f"+{x}+{y}")
    win.attributes("-topmost", True)
    pump(300)
    return win, panels[-1]


def walk(w):
    yield w
    for c in w.winfo_children():
        yield from walk(c)


try:
    pump(500)
    print("tk scaling:", app.root.tk.call("tk", "scaling"), " dpi:", app.root.winfo_fpixels("1i"))

    # ── data layer ──
    n1, n2, n3, n4, n5, n6 = (app.get_note(i) for i in range(1, 7))
    check("old note gains empty tasks", n1["tasks"] == [] and n1["view"] == "text")
    check("percent task progress", mod.task_progress(n2["tasks"][0]) == 60)
    check("stage task progress 2/4", mod.task_progress(n2["tasks"][1]) == 50)
    check("note progress n2", mod.note_progress(n2) == (1, 4, 52), str(mod.note_progress(n2)))
    check("all done note = 100", mod.note_progress(n4) == (2, 2, 100), str(mod.note_progress(n4)))
    check("default stages applied", n3["tasks"][1]["stages"] == mod.DEFAULT_STAGES)
    j = n6["tasks"]
    check("junk tasks repaired", len(j) == 3 and j[0]["title"] == "just a string"
          and j[1]["percent"] == 0 and j[1]["stages"] == mod.DEFAULT_STAGES
          and j[1]["stage"] == 4 and j[1]["points"] == [] and j[2]["percent"] == 100,
          json.dumps(j)[:400])
    check("junk ids made unique", len({t["id"] for t in j}) == 3, str([t["id"] for t in j]))
    check("note_label falls back to tasks", mod.note_label(n3).startswith("Tasks: Book flights"))
    unfinished = {"tasks": [{"mode": "percent", "percent": 100}, {"mode": "percent", "percent": 100},
                            {"mode": "percent", "percent": 99}]}
    check("unfinished never rounds to 100", mod.note_progress(unfinished)[2] == 99)

    # search reaches into tasks
    app.filter_var.set("all")
    app.search_var.set("stripe")
    check("search finds a task note", [n["id"] for n in app._visible_notes()] == [2])
    app.search_var.set("sign-off")
    check("search finds a stage name", [n["id"] for n in app._visible_notes()] == [5])
    app.search_var.set("")
    app.render_notes()
    pump(400)
    shot(app.root, "01_main_grid")

    # ── tasks tab ──
    win, pn = open_note(2)
    check("opens on tasks tab", pn.frame.winfo_manager() == "pack")
    shot(win, "02_note_tasks")
    print("note window size:", win.winfo_width(), "x", win.winfo_height(),
          " work area:", mod.work_area(win.winfo_rootx(), win.winfo_rooty()))

    # mouse wheel hand-off
    win.geometry("560x520")
    pump(300)
    pn.frame.event_generate("<Enter>")
    pump(60)
    check("pointer over list takes the wheel", "_wheel" in app.root.bind_all("<MouseWheel>")
          and "lambda" not in app.root.bind_all("<MouseWheel>"), app.root.bind_all("<MouseWheel>"))
    y0 = pn.cv.yview()[0]
    pn.cv.event_generate("<MouseWheel>", delta=-120)
    pump(60)
    check("wheel scrolls the task list", pn.cv.yview()[0] > y0, f"{y0} -> {pn.cv.yview()[0]}")
    pn.cv.event_generate("<MouseWheel>", delta=120)
    pn.frame.event_generate("<Leave>")
    pump(60)
    check("leaving hands the wheel back", "lambda" in app.root.bind_all("<MouseWheel>"),
          app.root.bind_all("<MouseWheel>"))
    pn.frame.event_generate("<Enter>")          # left "inside" on purpose for the close test
    win.geometry("560x720")
    pump(300)

    t_pct, t_stg, t_mail, t_dom = n2["tasks"]

    # real mouse events on the percent bar
    bar = pn._refs[t_pct["id"]]["bar"]
    w = bar.winfo_width()
    bar.event_generate("<Button-1>", x=int(bar.knob + (w - 2 * bar.knob) * 0.80), y=8)
    bar.event_generate("<ButtonRelease-1>", x=int(bar.knob + (w - 2 * bar.knob) * 0.80), y=8)
    pump(150)
    check("click on bar sets 80%", t_pct["percent"] == 80, str(t_pct["percent"]))
    bar = pn._refs[t_pct["id"]]["bar"]
    bar.event_generate("<Button-1>", x=bar.knob + 5, y=8)
    bar.event_generate("<B1-Motion>", x=int(w * 0.5), y=8)
    mid_drag_saved = json.load(open(mod.DATA_FILE, encoding="utf-8"))[1]["tasks"][0]["percent"]
    bar.event_generate("<B1-Motion>", x=int(bar.knob + (w - 2 * bar.knob) * 0.35), y=8)
    bar.event_generate("<ButtonRelease-1>", x=int(bar.knob + (w - 2 * bar.knob) * 0.35), y=8)
    pump(150)
    check("drag sets 35%", t_pct["percent"] == 35, str(t_pct["percent"]))
    check("drag saves only on release", mid_drag_saved == 80, str(mid_drag_saved))
    check("saved to disk after drag",
          json.load(open(mod.DATA_FILE, encoding="utf-8"))[1]["tasks"][0]["percent"] == 35)
    pn._nudge(t_pct, 1)
    check("nudge up 35 -> 40", t_pct["percent"] == 40)
    pn._nudge(t_pct, -1)
    pn._nudge(t_pct, -1)
    check("nudge down twice -> 30", t_pct["percent"] == 30)

    # real mouse events on the stage bar
    sb = pn._refs[t_stg["id"]]["sbar"]
    seg = sb._seg() + sb.gap
    sb.event_generate("<Button-1>", x=int(seg * 2.5), y=4)      # 3rd segment
    pump(100)
    check("click stage 3 completes 3", t_stg["stage"] == 3, str(t_stg["stage"]))
    sb.event_generate("<Button-1>", x=int(seg * 2.5), y=4)      # same again -> step back
    pump(100)
    check("click last done stage steps back", t_stg["stage"] == 2, str(t_stg["stage"]))
    sb.event_generate("<Button-1>", x=int(seg * 0.5), y=4)      # first segment -> roll back to 1
    pump(100)
    check("click earlier stage rolls back", t_stg["stage"] == 1, str(t_stg["stage"]))
    sb.event_generate("<Button-1>", x=int(seg * 3.5), y=4)      # last -> complete
    pump(100)
    check("click last stage completes task", mod.task_progress(t_stg) == 100 and t_stg["done_at"])
    check("caption when complete", pn._refs[t_stg["id"]]["cap"].cget("text") == "All 4 stages complete")
    pn._set_stage(t_stg, 2)
    check("done_at cleared when reopened", t_stg["done_at"] == "")

    # tick / un-tick remembers where it was
    pn._toggle_done(t_pct)
    check("tick -> 100", t_pct["percent"] == 100)
    pn._toggle_done(t_pct)
    check("un-tick restores 30", t_pct["percent"] == 30, str(t_pct["percent"]))
    pn._toggle_done(t_dom)
    check("un-tick with no memory -> 0", t_dom["percent"] == 0)
    pn._toggle_done(t_dom)

    # switching progress style
    pn._set_mode(t_pct, "stages")
    check("30% -> 1 of 4 stages", t_pct["mode"] == "stages" and t_pct["stage"] == 1, str(t_pct["stage"]))
    pn._set_mode(t_pct, "percent")
    check("flip back is lossless (30%)", t_pct["percent"] == 30, str(t_pct["percent"]))
    pn._set_mode(t_pct, "stages")
    pn._set_stage(t_pct, 3)
    pn._set_mode(t_pct, "percent")
    check("changed in stages -> carries 75%", t_pct["percent"] == 75, str(t_pct["percent"]))
    check("last style remembered for new tasks", app.config["task_mode"] == "percent")
    t_pct["percent"] = 95
    pn._set_mode(t_pct, "stages")
    check("95% never becomes a finished stage task", t_pct["stage"] == 3, str(t_pct["stage"]))
    pn._set_mode(t_pct, "percent")
    pn._set_percent(t_pct, 60)

    # add / move / delete
    pn.add_var.set("  Send invoices  ")
    pn.add_entry.focus_force()
    pump(100)
    pn.add_entry.event_generate("<Return>")
    pump(200)
    check("task added via Enter", n2["tasks"][-1]["title"] == "Send invoices" and len(n2["tasks"]) == 5)
    check("new task id unique", len({t["id"] for t in n2["tasks"]}) == 5)
    check("later tasks start collapsed", n2["tasks"][-1]["collapsed"] is True)
    check("add field cleared", pn.add_var.get() == "")
    pn.add_var.set("   ")
    pn._add_from_entry()
    check("blank title not added", len(n2["tasks"]) == 5 and pn.add_var.get() == "")
    new = n2["tasks"][-1]
    pn._move(new, -1)
    check("move up", n2["tasks"][-2] is new)
    pn._move(new, 1)
    pn._move(new, 1)
    check("move past the end is a no-op", n2["tasks"][-1] is new)
    pn._delete(new)                                   # untouched task: no confirm dialog
    check("untouched task deleted without prompt", len(n2["tasks"]) == 4)

    # title editing through the real widget
    ent = pn._refs[t_mail["id"]]["title"]
    ent.focus_force()                     # key events go to whichever widget has focus
    pump(120)
    ent.delete(0, "end")
    ent.insert(0, "Write the launch email")
    ent.event_generate("<KeyRelease>", keysym="l")
    pump(60)
    check("title edit saved", t_mail["title"] == "Write the launch email")
    ent.delete(0, "end")
    pn._rename(t_mail, ent, leaving=True)
    check("blank title becomes Untitled task", t_mail["title"] == "Untitled task")
    ent.delete(0, "end")
    ent.insert(0, "Write the launch email")
    pn._rename(t_mail, ent)

    # bullet notes through the real Text widget
    pn._toggle_open(t_mail)
    pump(200)
    texts = [w for w in walk(pn.inner) if isinstance(w, mod.tk.Text)]
    check("three open tasks -> three note boxes", len(texts) == 3, str(len(texts)))
    box = texts[-1]
    check("points shown as bullets", box.get("1.0", "end-1c") == "• tone: short\n• one CTA only",
          repr(box.get("1.0", "end-1c")))
    box.focus_force()
    pump(100)
    box.mark_set("insert", "end-1c")
    box.event_generate("<Return>")
    pump(60)
    check("Enter starts a new bullet", box.get("1.0", "end-1c").endswith("\n• "))
    box.event_generate("<Return>")
    check("Enter on an empty bullet does nothing",
          box.get("1.0", "end-1c") == "• tone: short\n• one CTA only\n• ")
    box.insert("insert", "send Tuesday")
    box.event_generate("<KeyRelease>", keysym="y")
    pump(60)
    check("typed point saved", t_mail["points"] == ["tone: short", "one CTA only", "send Tuesday"],
          str(t_mail["points"]))
    box.mark_set("insert", "3.2")
    box.event_generate("<BackSpace>")
    pump(60)
    check("Backspace at a bullet joins lines",
          t_mail["points"] == ["tone: short", "one CTA onlysend Tuesday"], str(t_mail["points"]))
    box.delete("1.0", "end")
    box.insert("1.0", "plain pasted line\n- not ours\n•   spaced  ")
    box.event_generate("<KeyRelease>", keysym="v")
    pump(60)
    check("free-form lines parsed", t_mail["points"] == ["plain pasted line", "- not ours", "spaced"],
          str(t_mail["points"]))
    pn.add_entry.focus_force()
    pump(150)
    check("bullets tidied on focus-out",
          box.get("1.0", "end-1c") == "• plain pasted line\n• - not ours\n• spaced",
          repr(box.get("1.0", "end-1c")))
    h_before = int(box.cget("height"))
    check("box height fits its lines", h_before == 3, str(h_before))

    # empty notes box shows the hint and never saves it
    pn._toggle_open(t_dom)
    pump(200)
    texts = [w for w in walk(pn.inner) if isinstance(w, mod.tk.Text)]
    empty_box = texts[-1]
    check("empty notes show hint", empty_box.get("1.0", "end-1c") == pn.POINT_HINT)
    empty_box.focus_force()
    pump(120)
    check("hint replaced by a bullet on focus", empty_box.get("1.0", "end-1c") == "• ")
    pn.add_entry.focus_force()
    pump(120)
    check("hint never saved as a point", t_dom["points"] == [])
    pn._toggle_open(t_dom)

    pump(200)
    shot(win, "03_note_tasks_after_edits")

    # ── priority + colour per task ──
    check("tasks default to Normal priority and the note's colour",
          all(t["priority"] == 1 and t["color"] is None for t in n2["tasks"]))
    a = mod._normalise_task({"title": "x", "priority": 9, "color": 99}, 1)
    b = mod._normalise_task({"title": "x", "priority": "high", "color": True}, 1)
    c = mod._normalise_task({"title": "x", "priority": 0, "color": 5}, 1)
    check("out-of-range priority/colour repaired",
          (a["priority"], a["color"], b["priority"], b["color"], c["priority"], c["color"])
          == (3, None, 1, None, 0, 5), str((a["priority"], a["color"], b["priority"], b["color"])))

    labels = lambda text: [w for w in walk(pn.inner)
                           if isinstance(w, mod.tk.Label) and w.cget("text") == text]
    check("one priority picker per open task", len(labels("Urgent")) == 3, str(len(labels("Urgent"))))
    labels("Urgent")[2].event_generate("<Button-1>")          # 3rd open task = the email
    pump(200)
    check("click sets Urgent", t_mail["priority"] == 3, str(t_mail["priority"]))
    check("priority saved to disk",
          json.load(open(mod.DATA_FILE, encoding="utf-8"))[1]["tasks"][2]["priority"] == 3)
    # the row now carries an "Urgent" flag in its header as well as the chip
    check("priority flag shown on the task row", len(labels("Urgent")) == 4, str(len(labels("Urgent"))))
    pn._set_priority(t_stg, 2)
    pn._set_priority(t_dom, 0)

    dots = [w for w in walk(pn.inner) if isinstance(w, mod.tk.Label) and w.cget("text") == "●"]
    check("six colour dots per open task", len(dots) == 18, str(len(dots)))
    dots[2].event_generate("<Button-1>")                      # 1st open task -> pink
    pump(200)
    check("click sets the task colour", t_pct["color"] == 2, str(t_pct["color"]))
    check("task drawn in its own colour",
          pn._refs[t_pct["id"]]["pal"] == mod.task_palette(mod.COLORS[2])
          and pn._refs[t_pct["id"]]["bar"].cget("bg") == mod.task_palette(mod.COLORS[2])["surface"])
    check("other tasks keep the note colour",
          pn._refs[t_stg["id"]]["pal"] == mod.task_palette(pn.col))
    pn._set_colour(t_mail, 4)
    pn._set_colour(t_mail, None)
    check("colour can be cleared back to the note's", t_mail["color"] is None
          and pn._refs[t_mail["id"]]["pal"] == mod.task_palette(pn.col))
    pn._set_colour(t_mail, 5)
    pump(250)
    pn.cv.yview_moveto(0)
    pump(150)
    shot(win, "14_priority_colour_top")
    pn.cv.yview_moveto(1)
    pump(150)
    shot(win, "15_priority_colour_bottom")

    # the card lists open tasks most-pressing first
    app.render_notes()
    pump(250)
    order = [w.cget("text") for w in walk(app.scroll_frame) if isinstance(w, mod.tk.Label)
             and w.cget("text") in ("Design the landing page", "Checkout flow",
                                    "Write the launch email")]
    check("card orders open tasks by priority",
          order == ["Write the launch email", "Checkout flow", "Design the landing page"], str(order))
    check("task order inside the note is untouched",
          [t["id"] for t in n2["tasks"]] == [1, 2, 3, 4])

    # stage editor
    pn._edit_stages(t_stg)
    pump(300)
    dlg = [w for w in win.winfo_children() if isinstance(w, mod.tk.Toplevel)][-1]
    dlg.attributes("-topmost", True)
    shot(dlg, "04_stage_editor")
    dbox = [w for w in walk(dlg) if isinstance(w, mod.tk.Text)][0]
    dbox.delete("1.0", "end")
    dbox.insert("1.0", "Spec\n\n  Build  \nShip")
    t_stg["stage"] = 3                                   # 3 of 4 before the edit
    dlg.event_generate("<Control-Return>")
    pump(250)
    check("stages edited + blank lines dropped", t_stg["stages"] == ["Spec", "Build", "Ship"],
          str(t_stg["stages"]))
    check("shrinking stages never completes a task", t_stg["stage"] == 2, str(t_stg["stage"]))
    check("stage dialog closed", not dlg.winfo_exists())

    # card + header reflect the changes
    done_n, total, pct = mod.note_progress(n2)
    summ = [w for w in walk(win) if isinstance(w, mod.tk.Label)
            and str(w.cget("text")).endswith("% complete")]
    check("tab summary shows overall %", summ and summ[0].cget("text") == f"{pct}% complete",
          summ[0].cget("text") if summ else "missing")

    # recolour the NOTE while open (the note's own colour dots sit outside the task list)
    inside = set(walk(pn.inner))
    dots = [w for w in walk(win) if isinstance(w, mod.tk.Label) and w.cget("text") == "●"
            and w not in inside]
    check("note has its own six colour dots", len(dots) == 6, str(len(dots)))
    dots[1].event_generate("<Button-1>")
    pump(300)
    check("recolour applied", n2["color"] == 1 and pn.cv.cget("bg").upper() == mod.COLORS[1]["bg"])
    check("recolouring the note keeps a task's own colour",
          t_pct["color"] == 2 and pn._refs[t_pct["id"]]["pal"] == mod.task_palette(mod.COLORS[2])
          and pn._refs[t_stg["id"]]["pal"] == mod.task_palette(mod.COLORS[1]))
    shot(win, "05_recoloured_green")
    dots[0].event_generate("<Button-1>")
    pump(300)
    shot(win, "06_recoloured_yellow")

    # switch to the Note tab and back
    tab_note = [w for w in walk(win) if isinstance(w, mod.tk.Label) and w.cget("text") == "Note"][0]
    tab_note.event_generate("<Button-1>")
    pump(250)
    check("note tab shows text view", pn.frame.winfo_manager() == "" and n2["view"] == "text")
    shot(win, "07_note_tab")
    app.root.bind_all("<MouseWheel>", pn._wheel)     # as if closed with the pointer over the list
    win.destroy()
    pump(300)
    check("closing the note hands the wheel back", "lambda" in app.root.bind_all("<MouseWheel>"))

    # a brand-new note: text tab first, empty tasks tab
    app.add_note()
    pump(300)
    win = [w for w in tops()][-1]
    win.geometry("+120+30")
    win.attributes("-topmost", True)
    pn = panels[-1]
    check("new note opens on the text tab", pn.frame.winfo_manager() == "")
    tab_tasks = [w for w in walk(win) if isinstance(w, mod.tk.Label) and w.cget("text") == "Tasks"][0]
    tab_tasks.event_generate("<Button-1>")
    pump(300)
    shot(win, "08_empty_tasks")
    pn.add_var.set("First task")
    pn._add_from_entry()
    pn.add_var.set("Second task")
    pn._add_from_entry()
    pump(300)
    newn = pn.note
    check("first task opens, second stays compact",
          [t["collapsed"] for t in newn["tasks"]] == [False, True])
    shot(win, "09_two_new_tasks")
    win.destroy()
    pump(200)
    app.delete_note(newn["id"], confirm=False)

    # read-only (completed) note
    n5["tasks"][0]["color"] = 0
    n5["tasks"][0]["priority"] = 3
    win, pn = open_note(5)
    shot(win, "10_done_note_readonly")
    check("done note has no add field", pn.add_entry is None)
    check("done note stays grey despite a task colour",
          pn._refs[n5["tasks"][0]["id"]]["pal"] == mod.task_palette(mod.DONE_COLOR))
    check("done note has no pickers",
          not [w for w in walk(pn.inner) if isinstance(w, mod.tk.Label) and w.cget("text") == "●"])
    win.destroy()
    pump(200)

    # narrow window: nothing may overflow
    win, pn = open_note(2)
    tab_tasks = [w for w in walk(win) if isinstance(w, mod.tk.Label)
                 and str(w.cget("text")).startswith("Tasks")][0]
    tab_tasks.event_generate("<Button-1>")
    win.geometry("420x520")
    pump(400)
    shot(win, "11_narrow")
    win.destroy()
    pump(200)

    # export
    md = os.path.join(OUT, "export.md")
    mod.filedialog.asksaveasfilename = lambda **k: md
    app.export("md")
    text = open(md, encoding="utf-8").read()
    check("export lists tasks", "  - [ ] Design the landing page  _(60%)_" in text, text[:600])
    check("export lists stage + next + priority", "_(stage 2/3, next: Ship | High)_" in text)
    check("export lists priority on a percent task",
          "  - [ ] Write the launch email  _(0% | Urgent)_" in text)
    check("export lists points", "    - Hero needs a shorter headline" in text)
    check("export marks finished tasks", "  - [x] Reconcile accounts  _(100%)_" in text)

    # reminder popup + final grid
    app.show_reminder_popup()
    pump(400)
    app.reminder_popup.attributes("-topmost", True)
    app.reminder_popup.geometry("+60+60")
    shot(app.reminder_popup, "12_reminder_popup")
    app._close_reminder_popup()
    app.filter_var.set("all")
    app.render_notes()
    pump(400)
    shot(app.root, "13_main_grid_after")

    # what was written is loadable and stable
    reloaded = mod.load_notes()
    check("round-trip through disk", [t["title"] for t in reloaded[1]["tasks"]] ==
          [t["title"] for t in n2["tasks"]])
except Exception:
    traceback.print_exc()
    results.append(("harness crashed", False, ""))
finally:
    try:
        app.root.destroy()
    except Exception:
        pass

bad = [r for r in results if not r[1]]
print(f"\n{len(results) - len(bad)}/{len(results)} checks passed")
sys.exit(1 if bad else 0)
