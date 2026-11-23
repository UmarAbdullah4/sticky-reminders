"""Install the Setup file for real and check what it does to the PC.

    python tools/build_installer.py        (first: builds dist/...Setup....exe)
    python tests/test_installer.py

It installs silently into a scratch folder, checks the entry in Windows'
installed apps and all three shortcuts, installs again over the top (the
upgrade path), runs the installed program's self-test, then uninstalls and
checks that everything is gone - and that the shortcuts of a copy of the app
run from source were left alone.

The Setup is not code-signed. Where Windows refuses to run unsigned
installers (Smart App Control), this test cannot run and says so; CI runs it
on every build.
"""
import glob
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import winreg

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VERSION = re.search(r'^APP_VERSION\s*=\s*"([^"]+)"',
                    open(os.path.join(ROOT, "sticky_reminders.pyw"), encoding="utf-8").read(),
                    re.M).group(1)
SETUP = (sys.argv[1] if len(sys.argv) > 1
         else os.path.join(ROOT, "dist", f"StickyReminders-Setup-{VERSION}.exe"))
APPID = "{6F2B9C1E-3A47-4D8B-9E5C-7A1D2F4B8C63}_is1"          # AppId in the .iss, plus Inno's suffix
KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall" + "\\" + APPID
BLOCKED_BY_POLICY = 4551                                       # "An Application Control policy has blocked this file"
results = []


def check(name, cond, detail=""):
    results.append(bool(cond))
    print(("PASS  " if cond else "FAIL  ") + name + (f"   [{detail}]" if detail and not cond else ""))


def shell_folder(name):
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                        r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders") as k:
        return os.path.expandvars(winreg.QueryValueEx(k, name)[0])


def uninstall_entry():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, KEY) as k:
            return {n: winreg.QueryValueEx(k, n)[0]
                    for n in ("DisplayName", "DisplayVersion", "Publisher", "UninstallString")}
    except OSError:
        return None


def shortcut(path):
    """(target, arguments) of a .lnk, read through the Windows shell."""
    ps = (f"$s=(New-Object -COM WScript.Shell).CreateShortcut('{path}');"
          "Write-Output ($s.TargetPath + '|' + $s.Arguments)")
    out = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True)
    return tuple(out.stdout.strip().split("|", 1))


if not os.path.isfile(SETUP):
    sys.exit(f"{SETUP} does not exist - run tools/build_installer.py first")
if uninstall_entry() is not None:
    sys.exit("Sticky Reminders is installed on this PC. This test installs and then REMOVES it, "
             "so it only runs where it is not installed.")

target = os.path.join(tempfile.mkdtemp(prefix="sticky_install_test_"), "Sticky Reminders")
desktop, startup, programs = (shell_folder(n) for n in ("Desktop", "Startup", "Programs"))
lnk_menu = os.path.join(programs, "Sticky Reminders.lnk")
lnk_desk = os.path.join(desktop, "Sticky Reminders.lnk")
lnk_start = os.path.join(startup, "Sticky Reminders.lnk")
exe = os.path.join(target, "StickyReminders.exe")
# What must survive untouched: the shortcuts of a copy that runs from source.
theirs = [p for p in (os.path.join(startup, "StickyReminders.lnk"),
                      os.path.join(desktop, "Sticky Notes.lnk")) if os.path.exists(p)]
stamps = {p: os.path.getmtime(p) for p in theirs}

for step in ("install", "install again over it (upgrade)"):
    started = time.time()
    try:
        done = subprocess.run([SETUP, "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART",
                               f"/DIR={target}"], timeout=300)
    except OSError as e:
        if getattr(e, "winerror", None) == BLOCKED_BY_POLICY:
            print("NOT RUN: Windows on this PC refuses to run the unsigned Setup file\n"
                  "(Smart App Control or another application control policy). Nothing was\n"
                  "installed. Run this test on a PC without that policy, or let CI run it.")
            sys.exit(2)
        raise
    check(f"{step}: Setup ran silently without administrator rights ({time.time() - started:.0f}s)",
          done.returncode == 0, str(done.returncode))

entry = uninstall_entry()
check("listed in Windows' installed apps with its version and publisher",
      entry and entry["DisplayName"] == "Sticky Reminders" and entry["DisplayVersion"] == VERSION
      and entry["Publisher"] == "Umar Abdullah", str(entry))
check("the program and its bundled Python are where it was told to install",
      os.path.isfile(exe) and os.path.isfile(os.path.join(target, "LICENSE.txt"))
      and glob.glob(os.path.join(target, "_internal", "python3*.dll")))
check("Start menu shortcut opens the main window",
      os.path.isfile(lnk_menu) and shortcut(lnk_menu) == (exe, "--show"),
      str(shortcut(lnk_menu)) if os.path.isfile(lnk_menu) else "missing")
check("desktop shortcut opens the main window",
      os.path.isfile(lnk_desk) and shortcut(lnk_desk) == (exe, "--show"))
check("sign-in shortcut starts it quietly, in the tray",
      os.path.isfile(lnk_start) and shortcut(lnk_start) == (exe, ""))

probe = target + " self-test"
code = subprocess.run([exe, "--self-test", probe], timeout=180).returncode
try:
    report = json.load(open(os.path.join(probe, "self_test.json"), encoding="utf-8"))
except OSError:
    report = {"checks": {}, "passed": False, "packaged": False, "version": "", "program": ""}
check("the INSTALLED program passes its self-test",
      code == 0 and report["passed"] and report["packaged"] and report["version"] == VERSION
      and os.path.normcase(report["program"]) == os.path.normcase(exe), json.dumps(report["checks"]))
shown = subprocess.run(["powershell", "-NoProfile", "-Command",
                        f"$v=(Get-Item '{exe}').VersionInfo; "
                        "$v.ProductName + '|' + $v.ProductVersion + '|' + $v.CompanyName"],
                       capture_output=True, text=True).stdout.strip()
check("the .exe carries its name, version and publisher",
      shown == f"Sticky Reminders|{VERSION}|Umar Abdullah", shown)

done = subprocess.run([os.path.join(target, "unins000.exe"), "/VERYSILENT", "/SUPPRESSMSGBOXES",
                       "/NORESTART"], timeout=300)
for _ in range(60):                                   # the uninstaller finishes in a child process
    if not os.path.exists(exe) and uninstall_entry() is None:
        break
    time.sleep(0.5)
check("uninstall removes the program and its entry in installed apps",
      done.returncode == 0 and not os.path.exists(exe) and uninstall_entry() is None
      and not os.path.isdir(os.path.join(target, "_internal")))
check("uninstall removes all three shortcuts",
      not any(os.path.exists(p) for p in (lnk_menu, lnk_desk, lnk_start)))
check("shortcuts of a copy run from source were not touched",
      all(os.path.exists(p) and os.path.getmtime(p) == stamps[p] for p in theirs))

print(f"\n{sum(results)}/{len(results)} checks passed")
sys.exit(0 if all(results) else 1)
