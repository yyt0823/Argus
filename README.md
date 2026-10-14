# Argus

Argus is a personal AI assistant that runs continuously on a Mac (a Mac mini at home) and talks to you through iMessage. You message yourself from your iPhone; Argus reads the message from the Messages database on the Mac, answers it with Claude (via the Claude Agent SDK, on your Claude subscription), and replies in the same self-chat. Every Argus reply starts with `🤖 `.

Argus keeps:

- one Claude session per day (the day rolls over at 04:00 local time), resumed after restarts;
- long-term memory as Markdown files it maintains itself;
- a searchable archive of every message between you and Argus.

## Requirements

- macOS with Messages signed in to iMessage, using the same Apple ID as your iPhone.
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

### Permissions (important)

Argus runs under launchd as `.venv/bin/python`, which is a symlink to the real Python binary (for Homebrew, something like `/opt/homebrew/Cellar/python@3.14/<version>/Frameworks/Python.framework/Versions/3.14/bin/python3.14`). macOS permissions are attached to the real binary, so:

1. **Full Disk Access** (to read `~/Library/Messages/chat.db`). Find the real path:

   ```sh
   .venv/bin/python -c 'import os, sys; print(os.path.realpath(sys.executable))'
   # or: readlink -f .venv/bin/python
   ```

   Then open System Settings → Privacy & Security → Full Disk Access, click +, press Cmd+Shift+G, paste that path, and add it. Granting Full Disk Access to Terminal alone is not enough for the LaunchAgent. Upgrading Python changes the path, so grant it again after an upgrade (and re-run `./setup.sh`).

   If Argus cannot read chat.db at startup, it logs the error, shows a macOS notification, and exits; launchd retries every 10 seconds.

2. **Automation → Messages** (to send replies via AppleScript). macOS asks the first time Argus sends a message; click Allow. You can review it under System Settings → Privacy & Security → Automation.

### Keep the Mac running

```sh
sudo pmset -a sleep 0 displaysleep 3 disksleep 0 autorestart 1   # never sleep; screen off after 3 min; power on after a power failure
```

Also enable automatic login (System Settings → Users & Groups), so the LaunchAgent starts after a reboot. Automatic login is unavailable while FileVault is on.

## Talking to Argus

Open the conversation with yourself in Messages on your iPhone and type normally. Argus replies in the same chat, prefixed with `🤖 `.

- If a reply takes more than 15 seconds, Argus first sends `🤖 收到，正在处理…`.
- Send `停` to stop the task Argus is working on.
- Send `重试` to retry the last message that failed with an error.
- Messages sent while Argus is busy are queued and answered in order.

## Where data lives

Everything personal stays on the Mac and is git-ignored:

| Path | Contents |
|---|---|
| `argus.toml` | Your config (your handles) |
| `data/argus.db` | Message archive, daily sessions, internal state (SQLite) |
| `memory/` | Argus's long-term memory (`MEMORY.md` index + one Markdown file per topic) |
| `backups/YYYY-MM-DD/` | Daily snapshot of `data/argus.db` and `memory/`, taken at 04:00; newest 30 kept |
| `logs/argus.log` | Application log, rotated daily, 30 days kept |
| `logs/launchd.out`, `logs/launchd.err` | Raw stdout/stderr captured by launchd |

Argus's persona and rules are in `CLAUDE.md`.

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
