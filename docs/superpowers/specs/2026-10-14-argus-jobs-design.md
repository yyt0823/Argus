# Argus Internship Search + Auto-Apply — Design and Plan

- Date: 2026-10-14 (revised the same day after finding the user's existing workflow)
- Status: Requirements from the user ("every morning look for new openings and apply for me, using the Claude in Chrome extension; I'll also ask whether there are new openings, then search"); full implementation requested without further check-ins.
- Scope: Roadmap sub-project 5 (applications). Builds on Core, mail (internal tasks, briefing) and the user's local archive.

## 1. Key decision: automate the user's existing workflow

The user already runs internship search and applications by hand with Claude Code subagents, using files in `data/about_me/internships/` (git-ignored):

| File | Role |
|---|---|
| `README.md` | Search profile and conventions (Canada only, Montréal first; Winter/Summer 2027 SWE / backend / AI / AI-agent internships or co-ops; also new-grad roles) |
| `seen_companies.txt` | Companies already covered — every search reads it first, skips them, appends new ones |
| `INTERNSHIPS.md` | Cumulative tracker; each search session appends a dated section with tables (Company, Role, City, Term, Deadline, Fit 1–5, Notes, URL, Verified) |
| `raw/` | Raw per-agent findings per session |
| `APPLY_BRIEF.md` | The brief for application agents: hard rules, facts, answers, Workday tips |
| `FOLLOW_UPS.md` | Assessments, interviews, recruiter replies |
| `applications/` | Per-company files |

Argus runs the same two kinds of agents on a schedule and on request, instead of a new pipeline. This covers Workday (most large employers), which an ATS-API approach would not.

The résumé to upload is `data/about_me/yantian_yin_cv.pdf` (has the current phone number). Contact facts come from `PROFILE.md` / `APPLY_BRIEF.md`, never from older CVs.

## 2. Components

```
argus/jobs/
├── scout.py      # one isolated SDK agent run: search → update tracker files → return new postings (JSON)
├── applier.py    # one isolated SDK agent run per posting: apply in the user's Chrome via Claude in Chrome → result (JSON)
├── guard.py      # can_use_tool policy: which files each agent may read/write
└── scheduler.py  # daily 07:30 scout (+ auto-apply if enabled), queue of applications
argus/tools/jobs.py   # chat tools
```

### Scout agent (search)
- `claude_agent_sdk.query()` with tools `WebSearch`, `WebFetch`, `Read`, `Write`, `Edit`, `Glob`, `Grep`; `cwd = data/about_me/internships`; `setting_sources=[]`; `strict_mcp_config=True`; `max_turns ≈ 60`; model default.
- Prompt: follow `README.md`; read `seen_companies.txt` and `../PROFILE.md`; search for postings opened since the last session (Canada only; Montréal first); verify each posting is live; append a dated session section to `INTERNSHIPS.md` in the existing table format, save raw notes to `raw/<date>_argus.md`, append new companies to `seen_companies.txt`; finish with a JSON block listing new postings `{company, role, city, term, deadline, fit (1–5), notes, url, verified, ats}`.
- `guard.py` restricts file access: Read allowed under `data/about_me/` except `SENSITIVE_IDS.md`, `documents/`, and image/PDF files; Write/Edit allowed only under `data/about_me/internships/`. Anything else is denied.

### Applier agent (apply)
- `ClaudeSDKClient`/`query()` with `extra_args={"chrome": None}`, `cli_path` = the installed `claude` (`/opt/homebrew/bin/claude`; using the SDK's bundled CLI would repoint the user's Chrome native host into the venv), `setting_sources=[]`, `permission_mode="bypassPermissions"` restricted by `guard.py` (Chrome tools + Read of `APPLY_BRIEF.md`, `PROFILE.md`, `EDUCATION.md`, `WORK_AND_PROJECTS.md`, the résumé path for upload; Write only to `internships/applications/`), `max_turns ≈ 120`, one posting per run, at most one applier at a time.
- System prompt = `APPLY_BRIEF.md` verbatim, plus: résumé path override to `data/about_me/yantian_yin_cv.pdf`; check the Chrome connection first (`tabs_context_mcp`) and stop with `chrome_not_connected` if the extension isn't connected; never use `select_browser`/`switch_browser` prompts; finish with a JSON block `{company, role, req_id, result: submitted|needs_login|needs_attestation|skipped|error, detail, follow_up, guessed_answers}`.
- The brief's hard rules stay in force (no passwords, no account creation, no attestation ticks, no fabrication, code-writing roles only).

## 3. Data

`job_postings(id, company, role, city, term, deadline, fit, notes, url UNIQUE, verified, ats, first_seen_at, status, status_detail, applied_at, follow_up)`; status ∈ `new`, `reported`, `queued`, `applying`, `submitted`, `needs_login`, `needs_attestation`, `skipped`, `error`. `job_runs(id, kind scout|apply, started_at, finished_at, ok, summary, cost_usd)`.

## 4. Modes and limits (`[jobs]` in argus.toml)

| Key | Default | Meaning |
|---|---|---|
| `enabled` | true | |
| `scout_time` | "07:30" | Daily search |
| `auto_apply` | true | Apply automatically to postings with `fit ≥ min_fit_apply` found by the daily scout (the user asked for automatic applications; set false to only report) |
| `min_fit_apply` | 4 | On the tracker's 1–5 scale |
| `daily_apply_limit` | 8 | Max automatic applications per day |
| `apply_window` | ["08:30", "22:00"] | Only apply while the user is likely awake (they may need to tick an attestation box) |
| `internships_dir` | "data/about_me/internships" | |
| `resume_path` | "data/about_me/yantian_yin_cv.pdf" | |
| `cli_path` | "/opt/homebrew/bin/claude" | CLI used for Chrome |

## 5. User-facing behavior

- 08:00 briefing gains a jobs section: new postings found this morning (best first, with deadlines) and applications submitted / needing action (login, attestation tick) since yesterday.
- After each automatic application, no separate message unless it needs the user (needs_login / needs_attestation / error) — then a short alert with the link.
- Chat: "有没有新的 opening?" → `jobs_scout_now` (runs the scout; takes minutes — Argus first replies that it's searching, then reports via an internal task when done). "申请 3 号" → `job_apply`. "我申请了哪些?" → `jobs_list`.

## 6. Tools (chat session)

| Tool | Purpose |
|---|---|
| `jobs_scout_now` | Start a scout run in the background; result arrives as an internal task |
| `jobs_list` | Postings by status / fit / date (from SQLite) |
| `job_apply` | Queue an application for one posting id (explicit request; ignores auto_apply/fit limits but respects one-at-a-time) |
| `job_skip` | Mark skipped |
| `jobs_followups` | Read `FOLLOW_UPS.md` |

## 7. Error handling

- Chrome not connected → applications stay `queued`; one alert per day telling the user to open Chrome / sign in to the extension.
- Agent run fails or returns no parseable JSON → `job_runs.ok = false`, posting back to `queued` once, then `error` with detail.
- Scout finds nothing new → briefing says so; no alert.
- Budget: each run's `total_cost_usd` is logged; scout and applier have `max_turns` caps.

## 8. Testing

- Unit: `guard.py` allow/deny matrix (SENSITIVE_IDS denied, writes outside internships denied, résumé readable); scout/applier option builders (tools, cwd, chrome flag, cli_path); JSON result parsing (fenced, prose-wrapped, malformed); scheduler (07:30 once per day, apply window, daily limit, fit threshold, one at a time, Chrome-not-connected path); store methods; tools.
- Live (manual, cheap): applier smoke run against example.com form-less page (connection check only); scout dry run limited to `max_turns=15` printing the JSON. No real application is submitted by tests.

## 9. Implementation tasks

| Task | Files |
|---|---|
| J1 Store tables + `[jobs]` config | `argus/store.py`, `argus/config.py` |
| J2 Guard + scout + applier + result parsing | `argus/jobs/guard.py`, `argus/jobs/scout.py`, `argus/jobs/applier.py` |
| J3 Scheduler + tools + briefing + wiring + persona + docs | `argus/jobs/scheduler.py`, `argus/tools/jobs.py`, `argus/tools/server.py`, `argus/sdk_session.py`, `argus/main.py`, `argus/mail/watcher.py`, `CLAUDE.md`, `README.md`, `argus.example.toml` |
