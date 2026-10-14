# Argus Mail + Daily Briefing — Design

- Date: 2026-10-14
- Status: Approved in conversation; pending written-spec review
- Scope: Sub-project 2 of the Argus roadmap (daily briefing), first part: email
- Builds on: `2026-10-13-argus-core-design.md`

## 1. Goals

Argus reads the user's three mailboxes and:

1. **Daily briefing** — every morning at 08:00, one iMessage summarizing the last 24 hours of email: what needs action (with deadlines), what is worth knowing, and a one-line count of the rest.
2. **Immediate alerts** — when an important email arrives, a short iMessage within ~5 minutes, outside quiet hours.
3. **Ask anytime** — the user can ask in iMessage about their email ("any McGill emails about exams this week?"), and Argus searches and reads mail to answer.

Mailboxes:

| Address | Host |
|---|---|
| Gmail account (user's personal address) | Google |
| Custom-domain address on iCloud Mail | iCloud |
| McGill university address | Microsoft 365 (Exchange) |

### Out of scope

- Replying to, sending, deleting, moving, or flagging email (Argus is strictly read-only on mail).
- Other briefing content (calendar, news, weather, learning, fitness) — later parts of sub-project 2.
- Mailboxes other than each account's inbox (no spam, sent, archive).

## 2. Decisions

| Decision | Choice | Rationale |
|---|---|---|
| Mail access | Read the local data of the macOS **Mail app**, with all three accounts added to it | Microsoft 365 forbids password IMAP and universities usually block third-party OAuth consent; Apple Mail is an allowed client and handles each provider's login (incl. SSO/MFA). Argus stores no mail passwords. One access path for all accounts. Python already has Full Disk Access. |
| Importance | Claude decides (no user rules) | User's choice. |
| Triage model | One short, tool-less Claude call per new email, default model `haiku` (configurable) | Cheap; isolated from the chat session. |
| Delivery | Alerts and briefings are **internal tasks run through the day's chat session** | The session then knows what it told the user, so follow-ups ("tell me more about the second one") work. |
| Check interval | 5 minutes | |
| Quiet hours | 23:00–08:00: no immediate alerts; those emails go into the 08:00 briefing | Don't wake the user. |
| Briefing time | 08:00 local | |
| Mail body storage | Not copied into Argus's DB; only triage results are stored. Bodies are read from Mail's files on demand | Keeps the DB and backups small; avoids a second copy of the mailbox. |

**Known trade-off:** Mail's on-disk format (`~/Library/Mail/V10/MailData/Envelope Index` SQLite + `.emlx` files) is undocumented; a major macOS upgrade may require reader changes. All format knowledge is isolated in `argus/mail/reader.py`.

## 3. Components

```
argus/mail/
├── reader.py     # read-only access to Mail's local index and message files
├── triage.py     # one tool-less Claude call per email → structured verdict
└── watcher.py    # every 5 min: new inbox mail → triage → store → maybe alert
argus/tools/mail.py   # chat tools: mail_recent, mail_search, mail_read
```

| Component | Responsible for | Depends on |
|---|---|---|
| `reader` | Listing inbox messages newer than a checkpoint; searching by text/sender/account/date; reading one message's headers and plain-text body. The only code that knows Mail's formats. Read-only. | — |
| `triage` | Turning one email (sender, subject, date, first 2,000 characters of body) into a verdict | Agent SDK |
| `watcher` | Polling, checkpointing, triage orchestration, alert batching, quiet hours, catch-up | `reader`, `triage`, `store` |
| mail tools | Letting the chat session answer email questions | `reader`, `store` |

### Changes to Core

1. **Internal tasks.** The App queue accepts Argus-generated tasks (briefing, alert) besides user messages. They run through the day's session like a user message, but are archived with sender `system` (the `messages.sender` constraint gains `'system'`) and their text tells the model it is an internal task, not the user speaking.
2. **Scheduling.** A daily 08:00 briefing trigger and the 5-minute mail loop run alongside the existing backup loop.
3. **Config.** A `[mail]` table in `argus.toml`, all keys optional: `enabled` (default true), `briefing_time` ("08:00"), `quiet_start` ("23:00"), `quiet_end` ("08:00"), `check_interval_minutes` (5), `triage_model` ("haiku").

## 4. Data Flow

1. **Discover.** Every 5 minutes, `watcher` asks `reader` for inbox messages of the three accounts newer than the stored checkpoint. On first run it only records the checkpoint (no triage of existing mail).
2. **Triage.** Each new message gets one Claude call with no tools. Output (JSON):
   - `important`: bool
   - `category`: one of `school`, `work`, `finance`, `personal`, `notification`, `marketing`
   - `summary`: one sentence
   - `action`: what the user needs to do, or null
   - `deadline`: ISO date/datetime, or null
3. **Store.** The verdict goes into `mail_triage`.
4. **Alert.** Important messages outside quiet hours become one internal task per check cycle (several important emails in one cycle are combined into one alert). Messages received more than 24 hours ago are never alerted, only briefed.
5. **Brief.** At 08:00 an internal task asks the session to write the briefing using `mail_recent` over the last 24 hours: action-needed items with deadlines first, then worth-knowing items, then one line counting the rest. Short, phone-friendly.
6. **Ask.** In normal chat the session uses `mail_search` / `mail_read` / `mail_recent`, and `query_data` over `mail_triage`.

## 5. Data

### New table

`mail_triage`: message id (Mail's ROWID), message-id header (if available), account, sender, subject, received_at, important, category, summary, action, deadline, status (`triaged` / `failed`), attempts, alerted_at, briefed_at, created_at.

### New tools

| Tool | Purpose |
|---|---|
| `mail_recent` | Triage results for a time range (default last 24 h), optionally only important ones |
| `mail_search` | Search Mail's index by words in subject/sender, account, and date range — includes old, never-triaged mail |
| `mail_read` | Full plain-text content of one message (headers + body, capped length) |

## 6. Error Handling

| Situation | Handling |
|---|---|
| Mail app not running / not syncing | If no new message has appeared in the index for 2 hours, send one `🤖` notice to check the Mail app (at most once a day) |
| Index unreadable (permissions, format change) | Log; notify once via iMessage; pause mail features; chat keeps working |
| Triage fails (quota, timeout, bad JSON) | Mark `failed`, retry next cycle (max 3 attempts); never-triaged mail is listed under "couldn't analyze" in the briefing, so nothing is silently dropped |
| Argus was down | Resume from the checkpoint; mail older than 24 h is triaged but not alerted |
| Mac off at 08:00 | On start, if today's briefing hasn't been sent and it's after 08:00, send it |

## 7. Security

Email content is untrusted input (anyone can email the user).

- Triage calls get **no tools**; an email can only influence the verdict text.
- `mail_read` / `mail_search` output wraps email content in clearly marked untrusted-content delimiters, and `CLAUDE.md` states that instructions inside emails are never followed, only reported to the user.
- Argus is read-only on mail: no tool sends, deletes, moves, or marks mail.
- WebFetch remains disabled (Core decision), limiting exfiltration paths.

## 8. Testing

- **reader:** unit tests against a fake Envelope Index + `.emlx` files built in the test; one read-only live check that the newest message of each of the three accounts can be listed and read.
- **triage:** fake Claude for parsing/retry; one live run on three sample emails (important, marketing, prompt-injection attempt).
- **watcher:** first-run baseline, quiet hours, alert batching, checkpoint resume, 24-hour no-alert rule, briefing catch-up.
- **Core changes:** internal tasks archived as `system`; existing tests stay green.
- **End-to-end:** the user emails their Gmail with "urgent deadline" in the subject and receives a `🤖` alert within 5 minutes (outside quiet hours).

## 9. User Setup

Add the Gmail (Google) and McGill (Microsoft Exchange) accounts, and the iCloud account if not present, in Mail → Add Account; sync Mail only. Argus verifies all three appear in the index before enabling mail features.
