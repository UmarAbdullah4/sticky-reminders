# Sticky Reminders

[![CI](https://github.com/UmarAbdullah4/sticky-reminders/actions/workflows/ci.yml/badge.svg)](https://github.com/UmarAbdullah4/sticky-reminders/actions/workflows/ci.yml)
[![Latest release](https://img.shields.io/github/v/release/UmarAbdullah4/sticky-reminders)](https://github.com/UmarAbdullah4/sticky-reminders/releases/latest)
[![Licence: MIT](https://img.shields.io/badge/licence-MIT-green.svg)](LICENSE)

A desktop sticky-notes and reminder app for Windows, in a single Python file.

Notes can hold tasks with their own progress, due dates repeat, and messages
from Mattermost, Slack, Discord and Telegram can turn into notes on their own.
When you mark such a note done, the app replies in the thread it came from.

![The main window: coloured notes with tasks, progress and due dates](docs/screenshots/main.png)

| Tasks inside a note | Requests spotted in chat | What to focus on |
|---|---|---|
| ![A note's Tasks tab](docs/screenshots/tasks.png) | ![Suggested notes waiting for a decision](docs/screenshots/suggestions.png) | ![The reminder popup with a focus pick](docs/screenshots/reminder.png) |

The screenshots show made-up demo data.

## Features

**Notes**
- Coloured sticky notes with rich text (bold, headings, bullets, highlight)
- Priority, due date, tags, search, sort, archive, undo
- Pin a note so it stays above every other window
- Due dates that repeat: daily, weekdays, weekly or monthly

**Tasks inside a note**
- Any number of tasks per note, each with its own priority and colour
- Progress per task, two ways: a percentage you drag, or named stages you step through
- Bullet-point notes per task
- Overall progress on the note and on its card

**Reminders**
- A periodic nudge listing what is pending, plus an alert when something falls due
- Snooze, mute, and daily quiet hours
- Lives in the system tray; one copy per login session

**Connectors**
- Mattermost, Slack, Discord, Telegram
- A message containing the trigger word (default `@reminder`) becomes a note when
  you wrote it, when it mentions you, or when it was sent to you directly
- Add `!urgent`, `!high` or `!low` to the message to set the note's priority
- Marking the note done posts a reply in the original thread (can be switched off)

**Assistant (optional)**
- Works with Claude, OpenAI, Gemini, or any OpenAI-compatible endpoint, which is
  how open-source models plug in (Ollama, LM Studio, OpenRouter, Groq, Together)
- Suggested notes: requests addressed to you are spotted without a trigger word
  and wait for you to accept or dismiss them
- A line of background from the thread on notes that come from chat
- Break a note into tasks
- A "focus now" pick at the top of the reminder popup
- A drafted reply when you mark a chat note done, which you review before it is sent

**Looking after your data**
- Every save is atomic and keeps the previous version
- One backup per day, the last 14 kept
- A damaged notes file is set aside and the newest readable backup is loaded
- Export to Markdown or JSON, import from JSON
- Tokens and passwords are encrypted on disk with Windows DPAPI

## Requirements

- Windows 10 or 11
- Python 3 with Tk (included in the python.org installer). Developed and tested
  on Python 3.14; earlier 3.x versions are untested.

The app runs on the standard library alone. Optional packages add features:

```
python -m pip install -r requirements.txt
```

| Package | What it adds |
|---|---|
| `websockets` | Mattermost, Slack and Discord connectors |
| `pystray` + `pillow` | The system tray icon |
| `anthropic` | The Claude option of the assistant |

Telegram needs no extra package, and neither do the OpenAI, Gemini and
open-source options of the assistant.

## Running it

```
pythonw sticky_reminders.pyw
```

`pythonw` runs it without a console window. To start it with Windows, tick
**Settings → System → Start automatically when I log in**.

Command-line switches: `--show` opens the main window, `--reminder` shows the
reminder list straight away.

## Connectors

Open **Settings → Connectors**, pick a service, fill in its fields and press
**Test connection**. Each service's setup steps are shown on that page too.

### Mattermost

Sign in with your username and password, or better, a personal access token
(Profile → Security → Personal Access Tokens). A message becomes a note when
you sent it or it mentions you.

If your server uses a self-signed certificate that you trust, untick
*Verify the server's TLS certificate*.

### Slack

Slack is reached through Socket Mode, so no public URL is needed.

1. Create an app at <https://api.slack.com/apps> (From scratch).
2. **Socket Mode**: switch it on and create an app-level token with the
   `connections:write` scope. That is the `xapp-` token.
3. **OAuth & Permissions → Bot Token Scopes**: `app_mentions:read`,
   `channels:history`, `groups:history`, `im:history`, `mpim:history`,
   `channels:read`, `groups:read`, `users:read`, `chat:write`.
4. **Event Subscriptions → Subscribe to bot events**: `app_mention`,
   `message.channels`, `message.groups`, `message.im`, `message.mpim`.
5. Install the app to your workspace and copy the Bot User OAuth Token. That is
   the `xoxb-` token.
6. Invite the app into each channel it should watch: `/invite @YourApp`.

Your member ID (profile → ⋮ → Copy member ID) is optional. With it, your own
messages and messages that mention you become notes. Without it, only messages
that mention the app, or are sent to it directly, do.

### Discord

Discord needs a bot; automating a personal account is against its terms.

1. <https://discord.com/developers/applications> → New Application → Bot →
   Reset Token, and copy the token.
2. On the same Bot page, switch on **Message Content Intent**.
3. **OAuth2 → URL Generator**: scope `bot`; permissions View Channels, Send
   Messages and Read Message History. Open the generated link to add the bot
   to your server.

Your user ID (User Settings → Advanced → Developer Mode, then right-click your
name → Copy User ID) is optional and works like the Slack member ID above.

### Telegram

1. In Telegram, message `@BotFather`, send `/newbot` and copy the token.
2. Paste the token into Settings and save. Send your new bot any message: it
   answers with your user ID. Paste that into Settings and save again.

After that, anything you send the bot becomes a note, which is a quick way to
capture one from your phone. The bot ignores everyone else. In a group, add the
bot and use the trigger word; so that it can see group messages, send
`/setprivacy` to `@BotFather` and choose Disable.

### Adding another service

Subclass `Connector` (or `SocketConnector` for a websocket API), describe its
settings in `FIELDS` and `HELP`, and add the class to `CONNECTORS`. The settings
page, status bar and menus are generated from that list.

## The assistant

Off until you choose a provider in **Settings → AI**. Enter a key, pick a model
(**Load models** lists what your key can use), press **Test**, then switch on
the things you want it to do.

| Provider | What you need |
|---|---|
| Claude | A key from console.anthropic.com, and `pip install anthropic` |
| OpenAI | A key from platform.openai.com |
| Gemini | A key from aistudio.google.com |
| Open-source / custom | The address of any OpenAI-compatible server, plus its key if it has one |

Common addresses for the last one:

| Server | Address | Key |
|---|---|---|
| Ollama, on this PC | `http://localhost:11434/v1` | none |
| LM Studio, on this PC | `http://localhost:1234/v1` | none |
| OpenRouter | `https://openrouter.ai/api/v1` | yes |
| Groq | `https://api.groq.com/openai/v1` | yes |
| Together | `https://api.together.xyz/v1` | yes |

What it can do, each with its own switch:

- **Suggest notes.** With this on, connectors also pass along messages that
  mention you or are sent to you directly, even without the trigger word. The
  model decides whether each one is a request. Those that are wait under the ✨
  button with a proposed note, due date and tasks; you add or dismiss them.
- **Thread context.** When a note comes from chat, the conversation around it
  is fetched and summarised in a line or two on the note. Not available for
  Telegram, whose bots cannot read history.
- **Focus now.** The reminder popup opens with up to three notes that matter
  most right now, and why.
- **Draft the reply.** Marking a chat note done opens the reply for you to read
  and edit, drafted from the note's tasks. Nothing is posted until you press Send.
- **Suggest tasks.** Always available once a provider is set: ✨ Suggest in a
  note's Tasks tab proposes tasks, and you choose which to keep.

### What is sent, and what it costs

- The text the model works on (the note or message involved, and for context
  the surrounding thread) goes to the provider you chose. Use a model running
  on your own PC and it stays there.
- Keys are sent in headers, never in a URL, and are stored encrypted.
- Text from chat is treated as untrusted: it is fenced off in the prompt and the
  model is told it is material, not instructions. Nothing a model returns takes
  effect on its own. A suggestion waits for you; a draft waits for you.
- Suggesting notes makes one request per message addressed to you, so on a busy
  account the model you choose matters for cost. A daily request limit
  (default 300) is a hard stop, whichever feature is asking.
- Problems with the provider show as a red dot in the status bar rather than
  interrupting you.

With Claude, structured output is used so answers are always valid JSON, and
the API's server-side refusal fallback is enabled. Models that do not support
those options are detected and asked the plain way.

## Where things are stored

| What | Where |
|---|---|
| Notes | `~/.sticky_reminders.json` (previous save in `.bak`) |
| Suggested notes awaiting a decision | `~/.sticky_reminders_suggestions.json` |
| Daily backups | `~/.sticky_reminders_backups/` |
| Settings | `config.json`, next to the script |
| Log | `~/.sticky_reminders.log` |

`config.json` is listed in `.gitignore`. Credentials inside it are encrypted for
your Windows account, so the file is no use on another PC or to another user,
but it still names your servers and accounts: keep it out of version control.
`config.example.json` shows every setting with its default.

To restore notes, use **File → Import from JSON** and pick a file from the
backups folder. Notes you still have are skipped.

## Security notes

- TLS certificates are verified. The one opt-out is the Mattermost setting for
  self-signed servers.
- A connector that is refused (wrong password, revoked token) stops and says so
  rather than retrying, so it cannot lock your account.
- The Telegram bot only turns messages from your own user ID into notes.
- Prefer tokens to passwords: a token can be revoked without changing your login.

## Tests

The suites drive the real app against throwaway data and simulated chat
servers on `127.0.0.1`. They open windows and take screenshots, so run them on
a desktop session. They never read your notes or settings.

```
python tests/test_tasks.py        # tasks, progress, priority, colour
python tests/test_connectors.py   # the four connectors, against simulated servers
python tests/test_app.py          # encryption, recovery, backups, repeat, import
python tests/test_ai.py           # the assistant: every provider and every feature
python tests/test_windows.py      # window stacking, maximise state, pins, focus
```

`test_connectors.py` needs `websockets`, `test_ai.py` needs `anthropic`, and the
screenshots need `pillow`.

The connector and assistant tests check the app against each service's
documented wire format, using simulated servers and a scripted model, so they
cost nothing to run. They cannot check the live services, or how well a real
model judges your messages.

## Versions

Releases follow [Semantic Versioning](https://semver.org) and are listed on the
[Releases page](https://github.com/UmarAbdullah4/sticky-reminders/releases), each
with the script attached as a single download. What changed in every version is
in [CHANGELOG.md](CHANGELOG.md). The running version is shown under
**Help → About**.

## Contributing

Bug reports, ideas and pull requests are welcome. See
[CONTRIBUTING.md](CONTRIBUTING.md) for how the code is laid out, how to run the
tests and how releases are made, and [SECURITY.md](SECURITY.md) for reporting a
security problem privately.

## Licence

[MIT](LICENSE) © Umar Abdullah
