"""Window stacking: a window opened from the app must end up in front of the
main window, and raising must not disturb maximise state, pins or focus.

Unlike the other suites this one does NOT force windows topmost - the real
Windows z-order is the thing under test, read back with EnumWindows.
"""
import ctypes
import importlib.machinery
import importlib.util
import json
import os
import shutil
import sys
import time
import traceback
from ctypes import wintypes

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "out_win")
shutil.rmtree(OUT, ignore_errors=True)
os.makedirs(OUT)

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
except Exception:
    pass

loader = importlib.machinery.SourceFileLoader("sticky_win_test", sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(HERE), "sticky_reminders.pyw"))
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


user32 = ctypes.windll.user32


def hwnd_of(win):
    return user32.GetParent(win.winfo_id()) or win.winfo_id()


def z_order():
    """Top-level window handles, front to back, as Windows stacks them."""
    seen = []
    proto = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def each(hwnd, _):
        seen.append(hwnd)
        return True

    user32.EnumWindows(proto(each), 0)
    return seen


def in_front(a, b):
    order = z_order()
    ha, hb = hwnd_of(a), hwnd_of(b)
    return ha in order and hb in order and order.index(ha) < order.index(hb)


def _no_tray(self, app):
    self.app, self.icon, self.available = app, None, False


mod.Tray.__init__ = _no_tray
with open(mod.CONFIG_FILE, "w", encoding="utf-8") as f:
    json.dump({"mattermost_enabled": False, "start_minimized": False, "sound_enabled": False,
               "reminders_enabled": False}, f)
with open(mod.DATA_FILE, "w", encoding="utf-8") as f:
    json.dump([{"id": 1, "text": "first note"}, {"id": 2, "text": "pinned note", "pinned": True}], f)
with open(mod.SUGGEST_FILE, "w", encoding="utf-8") as f:
    json.dump([{"id": 1, "source": "slack", "sender": "Sam", "channel": "finance",
                "message": "Can you send the report?", "text": "Send Sam the report",
                "due": "", "priority": 1, "tasks": [], "why": "a request"}], f)

app = mod.StickyNotesApp()
tk = mod.tk


def pump(ms=300):
    end = time.time() + ms / 1000
    while time.time() < end:
        app.root.update()
        time.sleep(0.01)


def editors():
    return [w for w in app.root.winfo_children()
            if isinstance(w, tk.Toplevel) and w.winfo_exists() and w.title() == "Note"]


try:
    app.root.geometry("900x600+80+60")
    pump(1700)
    mod.force_foreground(app.root)
    pump(600)
    # Windows may refuse a brand-new process the foreground; that is not under test here.
    print("INFO  main window is the foreground window at start:", mod.is_foreground(app.root))

    # ── the reported case: main window maximised and in use, click ✨ ──
    app.root.state("zoomed")
    pump(500)
    mod.force_foreground(app.root)
    pump(600)
    check("still maximised after being raised", app.root.state() == "zoomed", app.root.state())
    app.suggest_btn.invoke()                          # exactly what the click does
    pump(900)                                         # well past the 250 ms flick
    sug = app._suggest_win
    check("Suggested notes opens IN FRONT of the main window", in_front(sug, app.root))
    check("...and has the keyboard", mod.is_foreground(sug))
    check("...and the main window is still maximised", app.root.state() == "zoomed", app.root.state())
    check("...and neither is left pinned on top",
          not mod.is_topmost(sug) and not mod.is_topmost(app.root))

    # main window clicked back to the front; ✨ again must bring the list forward again
    mod.force_foreground(app.root)
    pump(700)
    check("(main window back in front)", in_front(app.root, sug))
    app.suggest_btn.invoke()
    pump(900)
    check("clicking ✨ again raises the existing list above the main window", in_front(sug, app.root))
    sug.destroy()
    pump(300)

    # ── New note from the main window ──
    mod.force_foreground(app.root)
    pump(700)
    app.add_note()
    pump(900)
    ed = editors()[0]
    check("a new note opens in front of the main window", in_front(ed, app.root))
    check("...with the cursor in its text, ready to type",
          isinstance(app.root.focus_get(), tk.Text) and app.root.focus_get().winfo_toplevel() is ed,
          str(app.root.focus_get()))
    check("...and the main window still maximised", app.root.state() == "zoomed", app.root.state())
    new_id = app.notes[-1]["id"]
    ed.destroy()
    pump(300)
    app.notes[-1].update(view="tasks", tasks=[mod._normalise_task({"title": "one"}, 1)])
    app.open_note(new_id)
    pump(900)
    ed = editors()[0]
    check("a note left on its Tasks tab opens with the cursor in 'add a task'",
          isinstance(app.root.focus_get(), tk.Entry) and app.root.focus_get().winfo_toplevel() is ed,
          str(app.root.focus_get()))

    # opening a note that is already open, from behind
    mod.force_foreground(app.root)
    pump(700)
    app.open_note(new_id)
    pump(900)
    check("re-opening an open note brings that window forward",
          len(editors()) == 1 and in_front(ed, app.root))
    ed.destroy()
    pump(300)
    app.delete_note(new_id, confirm=False)

    # ── the main window is NOT in front: something else of ours is ──
    # (this is "Open" in the reminder popup, or a click on a "focus now" row)
    app.open_suggestions()
    pump(900)
    sug = app._suggest_win
    check("(suggestions in front, main window behind)", in_front(sug, app.root))
    app.show_window()
    app.open_note(1)                                  # opened in the same breath
    pump(900)
    ed = editors()[0]
    check("main window raised, and the note opened with it lands in front of it",
          in_front(ed, app.root) and in_front(app.root, sug))
    check("...main window not left topmost", not mod.is_topmost(app.root))
    ed.destroy()
    sug.destroy()
    pump(300)

    # ── pins ──
    app.open_note(2)
    pump(900)
    ed = editors()[0]
    check("a pinned note is still pinned after being raised", mod.is_topmost(ed))
    mod.force_foreground(app.root)
    pump(700)
    check("...and stays above the main window even when that is clicked", in_front(ed, app.root))
    ed.destroy()
    pump(300)
    app.open_note(1)
    pump(900)
    ed = editors()[0]
    check("an unpinned note does not become pinned", not mod.is_topmost(ed))
    ed.destroy()
    pump(300)

    # ── hide to tray and back keeps the maximised state ──
    app.tray.available = True             # so that hide_window() really hides, as with a tray
    app.hide_window()
    pump(400)
    check("(hidden to the tray)", app.root.state() == "withdrawn", app.root.state())
    app.show_window()
    pump(900)
    app.tray.available = False
    check("back from the tray: visible and still maximised",
          app.root.state() == "zoomed" and app.root.winfo_viewable(), app.root.state())
    app.root.iconify()
    pump(500)
    app.show_window()
    pump(900)
    check("back from minimised: visible and still maximised", app.root.state() == "zoomed", app.root.state())
    app.root.state("normal")
    app.root.geometry("900x600+80+60")
    pump(400)
    app.root.iconify()
    pump(500)
    app.show_window()
    pump(900)
    check("a normal-sized window comes back at its own size",
          app.root.state() == "normal" and abs(app.root.winfo_width() - 900) < 30,
          f"{app.root.state()} {app.root.winfo_width()}")
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
