# Security

## Reporting a problem

Please report security problems privately, not in a public issue: on this
repository's **Security** tab, choose **Report a vulnerability**. Say what you
found and how to reproduce it. You will get a reply, and credit in the release
notes if you would like it.

Fixes are made on the latest release only.

## What the app handles

Sticky Reminders stores credentials for the services you connect it to
(a chat password or tokens, and AI provider keys), so these are the areas that
matter most:

- **Credentials at rest.** They are encrypted with Windows DPAPI before being
  written to `config.json`, so the file is useless to another Windows account
  or on another machine. They are decrypted only in memory.
- **Credentials in transit.** TLS certificates are verified. The one opt-out is
  a per-server setting for self-signed Mattermost servers. Keys are sent in
  request headers, never in a URL, and are kept out of the log. The custom AI
  provider refuses plain `http://` to anything outside your own PC or network.
- **Untrusted text.** Chat messages are untrusted input. When the assistant is
  on they are fenced off in the prompt as material rather than instructions, and
  nothing a model returns takes effect by itself: a suggested note waits for you
  to accept it, a drafted reply waits for you to send it.
- **Who can create notes.** The Telegram bot only turns messages from your own
  user ID into notes; anyone else is told the bot is private.
- **Lock-outs.** A connector that is refused (wrong password, revoked token)
  stops and reports it instead of retrying.

## If you fork or publish it

`config.json` holds your server addresses and account names as well as the
encrypted credentials. It is git-ignored; keep it that way.
