# Argus Core + iMessage Channel — Design

- Date: 2026-10-13
- Status: Approved
- Scope: Sub-project 1 of the Argus roadmap

## 1. Background and Goals

Argus is a personal AI assistant built on Claude Code. It runs continuously on a Mac mini at home, and the user talks to it through iMessage on their iPhone.

### Roadmap

Each sub-project goes through its own design → spec → implementation plan → implementation cycle.

| Order | Sub-project | Notes |
|---|---|---|
| **1** | **Core + iMessage channel** (this document) | Foundation: messaging, session management, memory, data storage |
| 2 | Daily briefing | Scheduled summary pushed to the user |
| 3 | Learning assistant | Tracks what the user is learning and their progress |
| 4 | Fitness plan | Training plans, workout logs, **diet logs**, body metrics |
| 5 | Applications / account registration | Browser automation with configurable autonomy (see Section 9) |

### Core goals

1. The user texts themselves on iMessage and gets a reply from Argus within a few seconds.
2. Conversation keeps context within a day; a new session starts automatically at 4:00 AM each day.
3. Argus can read and write Markdown memory, search past conversations, and run read-only queries over structured data.
4. After a Mac reboot or a crash, Argus recovers on its own, without missing or duplicating replies.

### Out of scope

- Business logic and data tables for the briefing, learning, fitness, and application modules
- Vector semantic search (can be added later with `sqlite-vec` without changing the architecture)
- Channels other than iMessage (the interface is designed for them but not implemented)

## 2. Decisions

| Decision | Choice | Rationale |
|---|---|---|
| Host | The user's existing Mac mini (M4, 16 GB), always on | Low idle power; model inference happens in the cloud, so local load is minimal |
| Channel | iMessage, **user texts themselves** | No new Apple ID needed; the user's own iMessage keeps working on the Mac |
| Argus message format | `🤖 ` prefix followed by the body, in a single message | Distinguishes Argus from the user; one notification per reply |
| iMessage integration | Poll `~/Library/Messages/chat.db` + send via AppleScript | No third-party dependencies; the terminal already has Full Disk Access |
| Claude integration | Claude Agent SDK (Python), `ClaudeSDKClient` | Long-lived session, tools as in-process Python functions, supports interrupts |
| Billing | The user's existing Claude subscription | User's choice; quota is shared with the user's own Claude Code usage |
| Session granularity | One session per day, rolling over at 4:00 AM | Coherent within a day; cross-day continuity comes from memory |
| Storage | Text memory → Markdown; structured data and conversation archive → SQLite | Markdown is easy for the user to read and edit; SQLite supports aggregation and date-range queries |
| Retrieval | SQL for structured data; SQLite FTS5 full-text search for past conversations | Date-based questions need exact results, which SQL gives more reliably than vector retrieval |

## 3. Architecture

```
launchd (start at boot, restart on crash)
  └─ argus/main.py
        ├─ channel/imessage.py   polls chat.db every second, sends via AppleScript
        ├─ brain.py              one ClaudeSDKClient per day, message queue
        │     └─ tools/          in-process MCP tools
        ├─ store.py              the only module that touches SQLite
        └─ scheduler.py          scheduled jobs (this phase: daily rollover and backup only)
```

### Directory layout

```
Argus/
├── argus/
│   ├── main.py
│   ├── channel/
│   │   ├── base.py        # Channel interface: receive() / send()
│   │   └── imessage.py
│   ├── brain.py
│   ├── tools/
│   │   ├── memory.py      # read/write Markdown memory
│   │   ├── data.py        # query_data (read-only SQL)
│   │   └── history.py     # search_history (FTS5)
│   ├── store.py
│   └── scheduler.py
├── memory/                # Markdown memory
├── data/argus.db          # SQLite
├── backups/               # daily backups, 30 days retained
├── logs/                  # daily-rotated logs
├── .claude/skills/        # one skill per module, added in later sub-projects
├── CLAUDE.md              # Argus persona and rules
└── setup.sh               # one-time system setup
```

### Component responsibilities

| Component | Responsible for | Not responsible for | Depends on |
|---|---|---|---|
| `channel` | Sending/receiving, filtering echoes and duplicates | Any AI logic | macOS (chat.db, AppleScript) |
| `brain` | Daily sessions, queueing, interrupts, calling Claude | iMessage details | Agent SDK, `tools` |
| `tools` | Data capabilities exposed to Claude | Session management | `store` |
| `store` | SQLite connection, schema, reads/writes | Business logic | — |
| `scheduler` | Firing jobs on schedule | What the jobs do | `brain`, `store` |

`Channel` is an abstract interface. Adding Telegram or a web UI later means adding one new implementation; nothing else changes.

## 4. Message Flow

1. The user texts themselves on their iPhone.
2. `channel` queries chat.db every second, looking only at the user's self-chat, reading from the checkpoint stored in the `state` table. It filters out:
   - messages whose IDs are in `sent_ids` (sent by Argus)
   - messages starting with `🤖`
   - duplicate rows for the same message (a self-sent message may appear as both "sent" and "received")
3. `brain` checks the current day's session:
   - None exists (it is past 4:00 AM) → close the old session, create a new `ClaudeSDKClient`, and load `memory/MEMORY.md` into context.
   - One exists → use it.
4. Claude handles the message, calling tools as needed.
5. `channel` sends `🤖 <reply>` via AppleScript and records its message ID in `sent_ids`.
6. `store` writes the exchange (user message + Argus reply) to `messages` and syncs it to `messages_fts`.

### Edge cases

| Situation | Behavior |
|---|---|
| New message arrives while busy | Queue it and process in order |
| User sends `停` ("stop") | Interrupt the current task |
| Processing takes longer than 15 s | Send `🤖 收到，正在处理…` ("Got it, working on it…") first |
| First start | Process only messages that arrive after startup; never reply to history |
| Restart | Resume from the checkpoint in `state` |

## 5. Data Storage

### SQLite tables (Core creates only these)

| Table | Fields (summary) | Purpose |
|---|---|---|
| `messages` | id, timestamp, sender (user/argus), content, session_id | Conversation archive |
| `messages_fts` | FTS5 virtual table over `messages.content` | `search_history` |
| `sessions` | id, date, SDK session ID, start time, end time | Daily session management |
| `sent_ids` | iMessage guid, sent time | Filtering out Argus's own messages |
| `state` | key, value | Checkpoints (last processed chat.db ROWID, etc.) |

Later modules (e.g. fitness diet and workout tables) define their own tables in their own sub-projects.

### Markdown memory

Same format as Claude Code memory: one fact per file with frontmatter; `memory/MEMORY.md` is the index.

- Only `MEMORY.md` is loaded automatically when a new daily session starts; individual files are read on demand.
- The user can view, edit, and delete files directly.

### Tools

| Tool | Purpose | Safety constraint |
|---|---|---|
| `memory_read` / `memory_write` / `memory_delete` | Read/write files under `memory/` | Restricted to the `memory/` directory |
| `query_data` | Run SQL queries | Database opened in **read-only mode**; only `SELECT` is possible |
| `search_history` | FTS5 search over past conversations, with optional date range | Read-only |

Structured data can be written only through each module's dedicated tools (e.g. a future `log_meal`), and each tool writes only to its own table.

### Backups

At the 4:00 AM rollover, `data/` and `memory/` are copied to `backups/<date>/`; the most recent 30 days are kept.

## 6. Error Handling

| Situation | Handling |
|---|---|
| Claude call fails / subscription quota exhausted | Reply `🤖 出错了：<reason>` ("Error: <reason>"); mark the message unprocessed and reprocess it when the user says "重试" ("retry") |
| iMessage send fails | Retry 3 times, then log |
| Process crash | launchd restarts it; resume from checkpoint |
| Mac reboot / power restored | Auto power-on + auto login + launchd start at boot |
| chat.db unreadable | Log, and post a macOS notification to alert the user |

## 7. Operations and Deployment

- **launchd**: start at boot, restart on crash.
- **Logs**: `logs/argus.log`, rotated daily.
- **`setup.sh`** performs one-time setup, with clear prompts for steps the user must do by hand:
  - Full Disk Access (to read chat.db)
  - Automation permission (AppleScript controlling Messages)
  - Disable automatic sleep (currently: sleep after 1 minute idle)
  - Enable restart after power failure (currently `autorestart` = 0)
  - Automatic login for the macOS user
  - Install and load the launchd configuration

## 8. Testing

### Step one: spike (before writing production code)

The user sends themselves a few test messages; inspect how the real chat.db records self-sent messages, and confirm the deduplication approach in Section 4 is reliable.

**If it is not reliable:** fall back to signing the Messages app into a separate Apple ID, which the user's iPhone then texts.

### Unit tests

- `channel`: filtering logic against a fake chat.db, with emphasis on duplicate rows and `🤖` echoes.
- `tools`: read-only enforcement, FTS search, and memory directory restriction, against a temporary database.
- `brain`: daily rollover (including the 4:00 AM boundary), queueing, `停` interrupt, and the slow-task notice, using a fake Claude client.
- `store`: schema creation, checkpoint reads/writes.

### End-to-end acceptance

1. Send `你好` from the iPhone; receive a reply starting with `🤖` within a few seconds.
2. A follow-up on the same day can refer back to earlier context.
3. Ask Argus to remember something; a matching file appears under `memory/`.
4. Ask "what did I say before about X"; past conversations are found.
5. Start a long task, then send `停`; the task is interrupted.
6. Reboot the Mac without touching anything; Argus resumes with no duplicate replies.

## 9. Known Requirements for Later Sub-projects (recorded, not implemented now)

- **Applications / registration**: Must be capable of **fully automatic** operation (filling forms, submitting, reading email verification codes/links via the Gmail API), while also supporting a per-task or per-site "confirm before submit" mode. When a step cannot be completed automatically, such as an image CAPTCHA, send a screenshot to the user and continue once they have handled it. No CAPTCHA-bypass mechanisms will be designed. Credentials are stored in the macOS Keychain or a password manager CLI, never in plaintext config. SMS verification requires a separate SMS-forwarding integration.
- **Fitness plan**: Includes diet logging; data goes into SQLite to support date-range questions such as "how did I eat and train last week".
- **Subscription quota**: Argus shares the subscription quota with the user's own Claude Code usage; if that proves insufficient, switch to an API key (supported by the Agent SDK; only the auth method changes).
