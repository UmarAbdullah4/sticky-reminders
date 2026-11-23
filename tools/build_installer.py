"""Build the Windows downloads.

    python tools/build_installer.py

Leaves two files in dist/:

    StickyReminders-Setup-<version>.exe      the installer
    StickyReminders-<version>-portable.zip   the same program, to unzip and run

in three steps:

  1. PyInstaller packs the app together with its own copy of Python into
     build/app/StickyReminders/, so the PC it runs on needs nothing else.
  2. The packed program is told to test itself (--self-test). A module
     missing from the bundle fails the build here, not on someone's PC.
  3. Inno Setup wraps that folder into the Setup file, and the folder is
     zipped as the portable download.

The build runs in its own virtual environment (build/venv, created on first
use), so it bundles what requirements.txt lists and nothing else that happens
to be installed on this PC. Run it from an ordinary terminal, not an
administrator one.

Needs Inno Setup 6:   winget install JRSoftware.InnoSetup

Neither file is code-signed. tests/test_installer.py installs the Setup for
real and checks the result; it needs a PC (or CI runner) where Windows will
run an unsigned installer.
"""
import functools
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys

print = functools.partial(print, flush=True)      # keep these lines in order with the tools' own

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BUILD = os.path.join(ROOT, "build")
VENV = os.path.join(BUILD, "venv")
DIST = os.path.join(ROOT, "dist")
NAME = "StickyReminders"


def run(args, **kw):
    print("  >", " ".join(os.path.relpath(a, ROOT) if os.path.isabs(str(a)) and str(a).startswith(ROOT)
                          else str(a) for a in args)[:150])
    done = subprocess.run([str(a) for a in args], cwd=ROOT, **kw)
    if done.returncode:
        sys.exit(f"\nFAILED (exit code {done.returncode})")
    return done


def app_version():
    text = open(os.path.join(ROOT, "sticky_reminders.pyw"), encoding="utf-8").read()
    return re.search(r'^APP_VERSION\s*=\s*"([^"]+)"', text, re.M).group(1)


def find_inno():
    candidates = [shutil.which("ISCC"), shutil.which("iscc")]
    for base in (os.environ.get("LOCALAPPDATA", ""), os.environ.get("ProgramFiles(x86)", ""),
                 os.environ.get("ProgramFiles", "")):
        candidates += [os.path.join(base, "Programs", "Inno Setup 6", "ISCC.exe"),
                       os.path.join(base, "Inno Setup 6", "ISCC.exe")]
    return next((c for c in candidates if c and os.path.isfile(c)), None)


def enter_venv():
    """Re-run this script inside build/venv, creating and filling it first."""
    python = os.path.join(VENV, "Scripts", "python.exe")
    if not os.path.isfile(python):
        print("Creating the build environment (first run only)...")
        run([sys.executable, "-m", "venv", VENV])
    print("Installing what the app needs, plus PyInstaller...")
    run([python, "-m", "pip", "install", "--quiet", "--disable-pip-version-check",
         "--upgrade", "-r", os.path.join(ROOT, "requirements.txt"), "pyinstaller"])
    sys.exit(subprocess.call([python, os.path.abspath(__file__), *sys.argv[1:]]))


def version_resource(version):
    """What Windows shows under the .exe's Properties → Details."""
    numbers = tuple(int(n) for n in version.split(".")) + (0,)
    path = os.path.join(BUILD, "version_info.txt")
    strings = {"CompanyName": "Umar Abdullah", "FileDescription": "Sticky Reminders",
               "FileVersion": version, "InternalName": NAME,
               "LegalCopyright": "Copyright (c) 2026 Umar Abdullah. MIT licence.",
               "OriginalFilename": NAME + ".exe", "ProductName": "Sticky Reminders",
               "ProductVersion": version}
    with open(path, "w", encoding="utf-8") as f:
        f.write(
            "VSVersionInfo(\n"
            f"  ffi=FixedFileInfo(filevers={numbers}, prodvers={numbers}, mask=0x3f, flags=0x0,\n"
            "                    OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),\n"
            "  kids=[StringFileInfo([StringTable('040904B0', [\n"
            + "".join(f"      StringStruct({k!r}, {v!r}),\n" for k, v in strings.items())
            + "    ])]),\n"
            "    VarFileInfo([VarStruct('Translation', [1033, 1200])])])\n")
    return path


def main():
    version = app_version()
    inno = find_inno()
    if not inno:
        sys.exit("Inno Setup 6 was not found. Install it with:\n"
                 "    winget install JRSoftware.InnoSetup\n"
                 "or from https://jrsoftware.org/isinfo.php, then run this again.")
    icon = os.path.join(ROOT, "assets", "icon.ico")
    app_dir = os.path.join(BUILD, "app")
    shutil.rmtree(app_dir, ignore_errors=True)
    os.makedirs(BUILD, exist_ok=True)

    print(f"\n[1/3] Packing Sticky Reminders {version} with PyInstaller...")
    run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--log-level", "WARN",
         "--windowed", "--name", NAME,
         "--icon", icon,
         "--add-data", f"{icon}{os.pathsep}assets",
         # Loaded by name at run time, where PyInstaller's import scan cannot see them.
         "--collect-submodules", "websockets",
         "--collect-submodules", "pypdf",
         "--hidden-import", "pystray._win32",
         "--version-file", version_resource(version),
         "--distpath", app_dir, "--workpath", os.path.join(BUILD, "work"), "--specpath", BUILD,
         os.path.join(ROOT, "sticky_reminders.pyw")])
    exe = os.path.join(app_dir, NAME, NAME + ".exe")

    print("\n[2/3] Asking the packed program to test itself...")
    probe = os.path.join(BUILD, "selftest")
    shutil.rmtree(probe, ignore_errors=True)
    try:
        code = subprocess.run([exe, "--self-test", probe], timeout=180).returncode
    except OSError as e:
        if getattr(e, "winerror", None) != 4551:      # "An Application Control policy has blocked this file"
            raise
        sys.exit("\nFAILED: Windows on this PC refuses to run the packed program (Smart App Control\n"
                 "or another application control policy), so it cannot be tested here. Nothing was\n"
                 "left in dist/. Build on a PC without that policy, or let CI build it.")
    try:
        report = json.load(open(os.path.join(probe, "self_test.json"), encoding="utf-8"))
    except OSError:
        sys.exit(f"\nFAILED: the packed program wrote no report (exit code {code})")
    for name, result in report["checks"].items():
        print(f"  {'ok ' if result.startswith('ok') else 'BAD'} {name}: {result}")
    if code or not report.get("passed") or not report.get("packaged") or report["version"] != version:
        sys.exit("\nFAILED: the packed program did not pass its self-test")

    print("\n[3/3] Building the Setup file with Inno Setup, and the portable zip...")
    os.makedirs(DIST, exist_ok=True)
    run([inno, "/Qp", f"/DAppVersion={version}", f"/DSourceDir={os.path.join(app_dir, NAME)}",
         f"/DOutputDir={DIST}", os.path.join(ROOT, "installer", "sticky_reminders.iss")])
    setup = os.path.join(DIST, f"{NAME}-Setup-{version}.exe")
    # The portable download is the very folder that just passed its self-test.
    shutil.copy2(os.path.join(ROOT, "LICENSE"), os.path.join(app_dir, NAME, "LICENSE.txt"))
    portable = shutil.make_archive(os.path.join(DIST, f"{NAME}-{version}-portable"), "zip",
                                   root_dir=app_dir, base_dir=NAME)
    packed = sum(os.path.getsize(os.path.join(r, f))
                 for r, _, fs in os.walk(os.path.join(app_dir, NAME)) for f in fs)
    print(f"\nBuilt, for a program that takes {packed / 1e6:.0f} MB once on disk:")
    for path in (setup, portable):
        digest = hashlib.sha256(open(path, "rb").read()).hexdigest()
        print(f"  {os.path.relpath(path, ROOT)}   {os.path.getsize(path) / 1e6:.1f} MB")
        print(f"      sha256 {digest}")


if __name__ == "__main__":
    if os.path.normcase(os.path.abspath(sys.prefix)) != os.path.normcase(VENV):
        enter_venv()
    main()
