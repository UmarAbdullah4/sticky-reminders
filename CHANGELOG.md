# Changelog

All notable changes to Sticky Reminders are recorded here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and
version numbers follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html):
**major** for changes that break how you use the app or its files, **minor** for
new features, **patch** for fixes.

## [Unreleased]

## [2.5.0] - 2026-11-23

### Added
- ✨ Suggest now plans what you type. Describe a piece of work in the "add a
  task" box and it comes back as a task planned in full: stages, concrete steps
  and a priority. If it is a task you already have, that task is filled in
  instead of being added twice.
- ✨ Plan on an open task plans that one task. A task that is under way keeps
  its stages.
- ✨ Structure in the Note tab: the note summarised and tidied under headings
  and bullets, with tasks proposed if it needs them. The text as it was is
  kept, and "Restore the original" brings it back.
- Context: a note can name files and folders for the assistant to read when it
  works on that note. Folders are sent as an outline plus the files that say
  most; Word documents are read as they are, PDF files with the `pypdf`
  package. Files that hold credentials are never read, values that look like
  credentials are blanked out, there is a ceiling on how much is sent, and each
  answer says what was read.

### Changed
- With the box empty, ✨ Suggest plans the whole note: tasks without notes are
  filled in and missing ones proposed, each with its steps.
- When the assistant has nothing to add, it now says why.
- While the assistant works on something you asked for, its button counts the
  seconds. Such requests no longer queue behind background ones, and a model
  that takes too long is reported as that rather than as "could not be reached".

### Fixed
- ✨ Suggest ignored what was typed in the "add a task" box and only looked at
  the note, so it often answered "nothing to add".

## [2.4.1] - 2026-11-23

### Fixed
- Taking over from a copy run with Python is now final. The old copy's program
  file is renamed to `sticky_reminders.pyw.old`, so nothing can start it again.
  Before, the hourly scheduled task that the first setup script created started
  the old copy at every login, and the installed program then handed over to it.
- If the old copy is found running again after it was replaced, the installed
  program stops it and carries on.
- An old copy that Windows will not let the app stop, because it was started as
  administrator, is now noticed and reported with what to do about it.

## [2.4.0] - 2026-11-23

### Added
- A Windows installer. `StickyReminders-Setup-2.4.0.exe` installs the app like
  any other program: Start menu and desktop shortcuts, an option to start with
  Windows, and an entry under Settings → Apps to remove it. It needs neither
  Python nor administrator rights. A portable zip of the same program is
  published next to it.
- The installed program offers to take over from a copy run with Python: it
  stops the old copy, carries its settings and sign-ins across, and takes over
  starting with Windows. Notes are shared by both, so nothing is lost.
- The app has its own icon, on its windows and on the taskbar.
- `--self-test`, which checks that a copy of the app can run without touching
  your notes or settings. The installer build runs it on the packaged program.

### Changed
- A reminder is now a note for whoever it is addressed to. In Mattermost, a
  direct message containing the trigger word reaches the other person without
  an @mention.
- A reminder you address to someone else, by direct message or by mentioning
  them, is theirs alone: it no longer also appears on your own list. Mention
  yourself as well to keep a copy. This applies to Mattermost, Slack and Discord.
- When installed, settings are kept in `%APPDATA%\StickyReminders`. Run from
  source, they stay next to the script as before.

## [2.3.1] - 2026-11-23

### Fixed
- Windows opened from the app (suggested notes, a new note, a note opened from
  the reminder popup) now come up in front of the main window instead of behind it.
- The main window keeps its maximised state when it is raised, brought back from
  the tray, or restored from minimised.
- A pinned note stays pinned when it is brought to the front.
- The cursor lands in the note's text, or in "add a task", when a note opens.

## [2.3.0] - 2026-11-23

### Added
- Optional AI assistant. Works with Claude, OpenAI, Gemini, or any
  OpenAI-compatible endpoint (Ollama, LM Studio, OpenRouter, Groq, Together),
  which is how open-source models plug in.
- Suggested notes: messages addressed to you are checked for requests without
  needing the trigger word, and wait for you to add or dismiss them.
- Thread context: notes that come from chat get a short summary of the
  conversation around them.
- Suggest tasks: break a note into tasks from its Tasks tab.
- Focus now: the reminder popup opens with the notes that matter most.
- Drafted reply: marking a chat note done opens a reply for you to review and send.
- A daily request limit as a spending guard, and an assistant indicator in the
  status bar.

### Changed
- Clicking a notification can now open what it is about.

## [2.2.0] - 2026-11-23

### Added
- Connectors for Slack, Discord and Telegram, alongside Mattermost, with a
  Test connection button and setup steps for each.
- Repeating due dates: daily, weekdays, weekly, monthly.
- Import from JSON, which also restores from a backup.
- One backup of the notes per day, the last 14 kept.
- A switch for replying in the thread when a chat note is marked done.

### Changed
- Mattermost now runs on the same connector framework as the other services,
  and signs in off the UI thread.
- The "Mattermost" filter is now "From chat"; the status bar shows one
  indicator per connector.
- With "close to tray" switched off, the window's X button quits, as its
  setting has always said.
- Saving settings restarts only the connectors whose own settings changed.

### Fixed
- A damaged notes file is set aside and the newest readable backup is loaded,
  instead of the app starting empty and overwriting it.
- An error in the reminder loop or the message loop no longer stops it for good.
- A note that fell due while reminders were muted is announced afterwards.
- Esc and the Close button keep a due date or tag that was typed but not confirmed.
- The same note can no longer be open in two windows that overwrite each other.
- Replying to a message inside a Mattermost thread; signing in again after a
  session expires; connecting to a plain `http://` server.
- The app refusing to start when another program held its port, and reopening
  off-screen after a monitor was unplugged.
- Notifications stacking on top of one another.

### Security
- TLS certificates are verified. Verification had been switched off for the
  Mattermost connection.
- Passwords and tokens are encrypted on disk with Windows DPAPI instead of
  being stored as plain text.

## [2.1.0] - 2026-11-23

### Added
- Tasks inside every note, on a Tasks tab next to the note's text.
- Progress per task, either as a percentage you drag or as named stages you
  step through, switchable per task.
- Bullet-point notes per task.
- Priority and colour per task.
- Overall progress on the note and on its card; open tasks listed on the card.
- Tasks are included in search, the reminder popup and the Markdown export.

### Changed
- The note window is taller, and sized to fit the monitor it opens on.

## [2.0.0] - 2026-09-04

A rewrite of the first version.

### Added
- Mute, snooze and daily quiet hours.
- System tray icon; closing the window hides to the tray.
- Priorities, due dates with alerts, tags, search, sort and archive.
- Rich text in notes: bold, italic, underline, headings, bullets, highlight.
- Pin a note above every other window.
- Menu bar, keyboard shortcuts, status bar.
- Export to Markdown and JSON.
- A Settings window.

### Changed
- The reminder engine counts from the last reminder rather than from the top
  of the hour.
- Only one copy of the app runs at a time.
- Notes are saved atomically with a backup of the previous save, and the app
  writes a rotating log.

## [1.0.0] - 2026-04-08

The first version, "Sticky Notes Reminder".

### Added
- Sticky notes that open and expand on click.
- Hourly reminder popups.
- Notes created from Mattermost messages, and a "Done" button that sends a
  confirmation back to the channel.
- Start with Windows.

[Unreleased]: https://github.com/UmarAbdullah4/sticky-reminders/compare/v2.5.0...HEAD
[2.5.0]: https://github.com/UmarAbdullah4/sticky-reminders/compare/v2.4.1...v2.5.0
[2.4.1]: https://github.com/UmarAbdullah4/sticky-reminders/compare/v2.4.0...v2.4.1
[2.4.0]: https://github.com/UmarAbdullah4/sticky-reminders/compare/v2.3.1...v2.4.0
[2.3.1]: https://github.com/UmarAbdullah4/sticky-reminders/compare/v2.3.0...v2.3.1
[2.3.0]: https://github.com/UmarAbdullah4/sticky-reminders/compare/v2.2.0...v2.3.0
[2.2.0]: https://github.com/UmarAbdullah4/sticky-reminders/compare/v2.1.0...v2.2.0
[2.1.0]: https://github.com/UmarAbdullah4/sticky-reminders/compare/v2.0.0...v2.1.0
[2.0.0]: https://github.com/UmarAbdullah4/sticky-reminders/compare/v1.0.0...v2.0.0
[1.0.0]: https://github.com/UmarAbdullah4/sticky-reminders/releases/tag/v1.0.0
