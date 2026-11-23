# Contributing

Thanks for looking. Bug reports, ideas and pull requests are all welcome.

## Getting set up

You need Windows and Python 3 with Tk (the python.org installer includes it).

```
git clone https://github.com/UmarAbdullah4/sticky-reminders.git
cd sticky-reminders
python -m pip install -r requirements.txt
pythonw sticky_reminders.pyw
```

The app keeps your notes in your home folder and your settings in `config.json`
next to the script. `config.json` is git-ignored; never commit it.

## How the code is laid out

Everything is in `sticky_reminders.pyw`, in sections marked by banner comments:
persistence, helpers, rich text, tasks, connectors, the AI assistant, the tray
icon, then the main application class.

Two parts are built to be extended without touching the UI:

- **A new chat service:** subclass `Connector` (or `SocketConnector` for a
  websocket API), describe its settings in `FIELDS` and `HELP`, add it to
  `CONNECTORS`.
- **A new AI provider:** subclass `AIProvider`, implement `ask()` and
  `models()`, add it to `AI_PROVIDERS`.

The settings pages, status bar and menus are generated from those two lists.

## Tests

```
python tests/test_connectors.py   # connectors, against simulated servers
python tests/test_tasks.py        # tasks, progress, priority, colour
python tests/test_app.py          # encryption, recovery, backups, repeat, import
python tests/test_ai.py           # the assistant: every provider and feature
python tests/test_windows.py      # window stacking, maximise state, pins, focus
```

No test touches your real notes or settings, contacts a real service, or spends
anything: they use throwaway data, servers simulated on `127.0.0.1` and a
scripted model.

All but `test_connectors.py` drive real windows. They open and close windows for
a minute or two and **take the keyboard focus while they run**, so do not type
during a run: stray keystrokes land in the test windows and can fail a check.
If one check fails oddly, run that suite once more before investigating.

CI runs the linter, a compile check, the version check and the connector suite
on every push and pull request. Run the window suites yourself before sending a
change that touches the UI.

## Style

```
python -m pip install ruff
ruff check sticky_reminders.pyw tests tools --select E9,F
```

Beyond that: match the code around you, and write comments that say *why*
(the constraint, the bug this avoids), not what the next line does.

## Sending a change

1. Create a branch from `main`.
2. Make the change, with a test for it where one is practical.
3. Add a line under **Unreleased** in `CHANGELOG.md`, in the right group
   (Added, Changed, Fixed, Security).
4. Open a pull request that says what changed and how you checked it.

## Releasing (maintainers)

Versions follow [Semantic Versioning](https://semver.org): **major** for changes
that break how the app or its files are used, **minor** for new features,
**patch** for fixes.

1. Set `APP_VERSION` in `sticky_reminders.pyw`. It is the only place the
   version is written.
2. In `CHANGELOG.md`, rename **Unreleased** to `## [X.Y.Z] - YYYY-MM-DD`, start
   a fresh empty **Unreleased** above it, and add the compare link at the bottom.
3. Run all five test suites.
4. Commit, tag and push:

   ```
   git commit -am "Release X.Y.Z"
   git tag -a vX.Y.Z -m "Sticky Reminders X.Y.Z"
   git push --follow-tags
   ```

Pushing the tag runs the Release workflow, which checks that the tag,
`APP_VERSION` and the changelog agree, then publishes a GitHub Release with
that version's changelog entry as its notes and the script attached.

`python tools/release_notes.py --check` runs the same check locally.
