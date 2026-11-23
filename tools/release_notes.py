"""Release helper.

    python tools/release_notes.py --check     the script's version has a changelog entry
    python tools/release_notes.py v2.3.1      print that version's changelog section
                                              (fails if the tag, the script and the
                                              changelog do not all name the same version)
    python tools/release_notes.py v2.1.0 --any-version
                                              print an older version's section without
                                              comparing it to the script

The version lives in one place, APP_VERSION in sticky_reminders.pyw. This is
what keeps a release tag, the changelog and the code from drifting apart.
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def read(name):
    with open(os.path.join(ROOT, name), encoding="utf-8") as f:
        return f.read()


def app_version():
    found = re.search(r'^APP_VERSION\s*=\s*"([^"]+)"', read("sticky_reminders.pyw"), re.M)
    if not found:
        sys.exit("APP_VERSION not found in sticky_reminders.pyw")
    return found.group(1)


def section(version):
    """The body of the '## [version]' entry in CHANGELOG.md, or None."""
    lines = read("CHANGELOG.md").splitlines()
    start = next((i for i, ln in enumerate(lines)
                  if ln.startswith(f"## [{version}]")), None)
    if start is None:
        return None
    end = next((i for i in range(start + 1, len(lines))
                if lines[i].startswith("## [") or re.match(r"^\[[^\]]+\]: ", lines[i])),
               len(lines))
    return "\n".join(lines[start + 1:end]).strip()


def main(argv):
    version = app_version()
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        sys.exit(f"APP_VERSION is {version!r}; it should look like 2.3.1")
    if argv == ["--check"]:
        if not section(version):
            sys.exit(f"CHANGELOG.md has no entry for {version}. Add '## [{version}] - "
                     f"<date>' with what changed.")
        print(f"OK: version {version} has a changelog entry")
        return
    if not argv or argv[0].startswith("--"):
        sys.exit(__doc__)
    wanted = argv[0].removeprefix("v")
    if "--any-version" not in argv and wanted != version:
        sys.exit(f"The tag says {wanted} but APP_VERSION is {version}. Bump APP_VERSION, "
                 f"commit, then tag that commit.")
    notes = section(wanted)
    if not notes:
        sys.exit(f"CHANGELOG.md has no entry for {wanted}")
    sys.stdout.reconfigure(encoding="utf-8")
    print(notes)


if __name__ == "__main__":
    main(sys.argv[1:])
