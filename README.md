# Argus

Argus is a personal AI assistant that runs continuously on a Mac (a Mac mini at home) and talks to you through iMessage. You message yourself from your iPhone; Argus reads the message from the Messages database on the Mac, answers it with Claude (via the Claude Agent SDK, on your Claude subscription), and replies in the same self-chat. Every Argus reply starts with `🤖 `.

Argus keeps:

- one Claude session per day (the day rolls over at 04:00 local time), resumed after restarts;
- long-term memory as Markdown files it maintains itself;
- a searchable archive of every message between you and Argus.

## Requirements

- macOS with Messages signed in to iMessage: preferably with a dedicated Apple ID for Argus (see [Dedicated Apple ID](#dedicated-apple-id-recommended)), or with the same Apple ID as your iPhone (`mode = "self"`).
- Python 3.11+ (developed on 3.14, e.g. `brew install python@3.14`).
- Claude Code logged in to your Claude subscription. No `ANTHROPIC_API_KEY` is needed or used.

## Setup

```sh
git clone <this repo> ~/Argus && cd ~/Argus
./setup.sh            # creates .venv, installs Argus, creates argus.toml, then stops
$EDITOR argus.toml    # set self_handles to your own phone number / Apple ID email
./setup.sh            # renders the LaunchAgent, prints manual steps, asks before loading it
```

`setup.sh` is safe to re-run. It never runs `sudo`; it prints the commands you need to run yourself.

### Dedicated Apple ID (recommended)

By default (`mode = "self"`) you text yourself, and the iPhone shows every message twice. With a dedicated Apple ID, Argus becomes an ordinary contact instead:

1. Create a new Apple ID for Argus (any email address you control).
2. On the Mac, sign Messages into it: Messages → Settings → iMessage, sign out of your own Apple ID, sign in with the new one.
3. In `argus.toml`, set `mode = "contact"` and put your iPhone's handle(s) in `self_handles` (the phone number and/or email your iPhone sends iMessages from).
4. Restart Argus: `launchctl kickstart -k gui/$(id -u)/com.argus.agent`.
5. From your iPhone, send a message to the bot's Apple ID email. Argus answers in that conversation.

Note: the Mac no longer shows your own iMessages, since its Messages app now belongs to the bot.

### Permissions (important)

Argus runs under launchd as `.venv/bin/python`, which is a symlink to the real Python binary (for Homebrew, something like `/opt/homebrew/Cellar/python@3.14/<version>/Frameworks/Python.framework/Versions/3.14/bin/python3.14`). macOS permissions are attached to the real binary, so:

1. **Full Disk Access** (to read `~/Library/Messages/chat.db`). Find the real path:

   ```sh
   .venv/bin/python -c 'import os, sys; print(os.path.realpath(sys.executable))'
   # or: readlink -f .venv/bin/python
   ```

   Then open System Settings → Privacy & Security → Full Disk Access, click +, press Cmd+Shift+G, paste that path, and add it. Granting Full Disk Access to Terminal alone is not enough for the LaunchAgent. Upgrading Python changes the path, so grant it again after an upgrade (and re-run `./setup.sh`).

   If Argus cannot read chat.db at startup (or `argus.toml` is invalid), it logs the error, shows one macOS notification, and exits cleanly; launchd does not restart it (the LaunchAgent only restarts Argus after a crash). Fix the problem, then start it again with `launchctl kickstart -k gui/$(id -u)/com.argus.agent`.

2. **Automation → Messages** (to send replies via AppleScript). macOS asks the first time Argus sends a message; click Allow. You can review it under System Settings → Privacy & Security → Automation.

### Keep the Mac running

```sh
sudo pmset -a sleep 0 displaysleep 3 disksleep 0 autorestart 1   # never sleep; screen off after 3 min; power on after a power failure
```

Also enable automatic login (System Settings → Users & Groups), so the LaunchAgent starts after a reboot. Automatic login is unavailable while FileVault is on.

## Talking to Argus

Open the conversation with Argus's Apple ID (or, in `mode = "self"`, with yourself) in Messages on your iPhone and type normally. Argus replies in the same chat, prefixed with `🤖 `.

- If a reply takes more than 15 seconds, Argus first sends `🤖 收到，正在处理…`.
- Send `停` to stop the task Argus is working on.
- Send `重试` to retry the messages that failed with an error (all of them, in order; the last 20 are remembered).
- Messages sent while Argus is busy are queued and answered in order. The queue is saved, so messages that were waiting when Argus restarted are answered after it comes back.
- If Argus cannot read new messages for about a minute, it shows a macOS notification (once per outage).

## Email

Argus reads your email through the macOS **Mail** app's local data (read-only; it never sends, deletes, moves or flags mail, and stores no mail passwords):

- every new inbox email gets a short, tool-less Claude check (default model `haiku`) that decides whether it is important;
- important email triggers a short `🤖` alert within about 5 minutes, except during quiet hours (23:00–08:00);
- every day at 08:00 Argus sends one briefing on the last 24 hours (sent on start if the Mac was off at 08:00);
- you can ask about your email any time ("any emails about exams this week?").

On the very first run Argus only records where your mailbox is; existing mail is never triaged or alerted.

Setup:

1. Open Mail → Settings → Accounts (or Mail → Add Account) and add each mailbox: **Google** for Gmail, **Microsoft Exchange** for Microsoft 365 / university accounts, **iCloud** for iCloud Mail (including custom-domain addresses). Sync **Mail** only; Mail handles each provider's login (SSO, MFA).
2. Keep Mail running (add it to Login Items). Mail only downloads new messages while it is open; Argus sends one notice a day if it is not running.
3. The same Full Disk Access that lets Argus read `chat.db` covers `~/Library/Mail`.
4. Optionally tune the `[mail]` table in `argus.toml` (all keys optional, defaults shown in `argus.example.toml`): `enabled`, `briefing_time`, `quiet_start`, `quiet_end`, `check_interval_minutes`, `triage_model`. Set `enabled = false` to turn every mail feature off.

If Mail's data cannot be read at startup, Argus logs a warning and runs without mail features.

Privacy: message bodies are never copied into Argus's database; only the triage result (sender, subject, one-line summary, category, action, deadline) is stored in `data/argus.db` (table `mail_triage`). Bodies are read from Mail's files on demand. Email text sent to Claude for triage or chat stays within your Claude subscription, like your messages.

## Where data lives

Everything personal stays on the Mac and is git-ignored:

| Path | Contents |
|---|---|
| `argus.toml` | Your config (your handles) |
| `data/argus.db` | Message archive, daily sessions, email triage results, internal state (SQLite) |
| `memory/` | Argus's long-term memory (`MEMORY.md` index + one Markdown file per topic) |
| `backups/YYYY-MM-DD/` | Daily snapshot of `data/argus.db` and `memory/`, taken at 04:00; newest 30 kept |
| `logs/argus.log` | Application log, rotated daily, 30 days kept |
| `logs/launchd.out`, `logs/launchd.err` | Raw stdout/stderr captured by launchd (only output that bypasses logging, e.g. a crash before logging starts) |

Argus's persona and rules are in `CLAUDE.md`.

### Restoring a backup

1. Stop Argus: `launchctl bootout gui/$(id -u)/com.argus.agent`.
2. Copy the backup back: `cp backups/YYYY-MM-DD/argus.db data/argus.db` and replace `memory/` with `backups/YYYY-MM-DD/memory/`.
3. Delete the restored message checkpoint and queue, so Argus does not replay every iMessage sent since the backup was taken:

   ```sh
   sqlite3 data/argus.db "DELETE FROM state WHERE key='imessage_last_rowid'; DELETE FROM pending;"
   ```

   On the next start, the channel re-initializes the checkpoint to the newest message in chat.db.
4. Start Argus again: `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.argus.agent.plist`.

## Security notes

- Argus can search the web (WebSearch) but **cannot fetch arbitrary URLs: WebFetch is intentionally disabled**. A web page or search result could carry a prompt injection; without WebFetch, an injected instruction cannot make Argus send your memory or message history to an attacker-controlled URL.
- Email is untrusted input (anyone can email you). Triage calls have no tools, mail tools wrap email content in `<untrusted_email>` markers, and Argus is told to report instructions found in email, never follow them. Mail access is read-only.
- Automatic login (needed so the LaunchAgent starts after a reboot) is unavailable while FileVault is on; decide which you prefer for this machine.

## Stopping and starting

```sh
launchctl bootout gui/$(id -u)/com.argus.agent                                      # stop (and unload)
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.argus.agent.plist       # start
launchctl kickstart -k gui/$(id -u)/com.argus.agent                                 # restart
launchctl print gui/$(id -u)/com.argus.agent                                        # status
```

To run it in the foreground instead (stop the LaunchAgent first): `.venv/bin/python -m argus.main`. The terminal you run it from needs Full Disk Access.

## Development

```sh
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -m pytest -q                               # unit tests; no network, no real chat.db
ARGUS_LIVE=1 .venv/bin/python -m pytest tests/live -q -s    # live Claude test (uses subscription quota)
```

Tests use fake handles such as `+15550000000`. Never commit real handles or message content.
