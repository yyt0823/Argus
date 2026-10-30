# Argus Jobs v2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Argus runs the user's entry-level job hunt end to end: one memory on the Desktop, a wider daily search, applications through jobagent (accounts, login, email verification, cover letters), and one iMessage summary per run.

**Architecture:** Argus stays the orchestrator (`argus/jobs/scheduler.py`). The Claude-in-Chrome applier is replaced by a subprocess call to jobagent. Two file bridges connect them: one for human questions (relayed over iMessage) and one for mail (Mail.app → `.eml` files). The applications tracker becomes `~/Desktop/about_me/internships/applications.db`, which Argus writes and renders to `ALL_APPLICATIONS.md`.

**Tech Stack:** Python 3.14, claude-agent-sdk, SQLite, pytest (both projects); jobagent also uses Playwright.

**Spec:** `docs/superpowers/specs/2026-10-25-argus-jobs-v2-design.md` (read §8 builder decisions first)

## Global Constraints

- Never apply twice to the same job. If a duplicate is uncertain, skip it with detail `possible duplicate of <row>`.
- Never auto-apply to a notify-only company. Default list: Google, Amazon, Meta, Apple, Microsoft, Nvidia, OpenAI.
- The account password lives only in jobagent's `config/profile.yaml` (gitignored, chmod 600). It never goes in the Argus repo, logs, prompts, memory files or iMessages.
- Never submit with test data. Refuse a profile path containing `test-profile` or an account email of `<test account email>`. Live runs use `JOBAGENT_DATA_DIR=<jobagent>/data/live`.
- No invented facts. Cover letters and free-text answers come only from `PROFILE.md`, `WORK_AND_PROJECTS.md`, `EDUCATION.md` and `APPLY_BRIEF.md`.
- `SENSITIVE_IDS.md` and `documents/` stay unreadable to every agent (existing `guard.py` rules).
- Limits kept from v1: fit ≥ 4, ≤ 8 automatic submissions/day, window 08:30–22:00, one application at a time.
- Tests never submit a real application and never call a real model (except jobagent's existing opt-in `-m e2e`).
- Argus repo is public. Commit nothing personal. Code, comments and commits are in English. Commit and push after each task (Argus). jobagent is local-only git (no remote).
- Develop Argus in a git worktree, not in `~/Argus-build` itself: launchd runs from there.

## Review Focus

1. **Same job, different URL** (LinkedIn link vs Workday link, `?source=` params). Dedup must match on req id or company + role, not only on URL. Test: `test_dedup_same_req_id_different_url` (Task 6).
2. **Argus restarts while jobagent runs.** The posting must become `error` "interrupted", the jobagent process group must be killed, and the posting must never be re-queued. Test: `test_cancel_kills_process_group` (Task 8).
3. **User replies to an old or unknown question id**, or answers after the 30-minute timeout. `job_answer` must say it expired, not write a stale answer file. Test: `test_answer_after_timeout_is_rejected` (Task 9).
4. **Mail.app message without a file on disk yet** (`.partial.emlx`, or not downloaded). The export must skip it and retry on the next poll, never crash the run. Test: `test_export_skips_missing_then_picks_up` (Task 7).
5. **Rejection email whose company matches several rows** (Company A ×4). The status update must apply only where the role matches, or to all rows when the email names no role. It must never touch a different company that shares a word. Test: `test_email_event_role_disambiguation` (Task 11).

---

## Part A — jobagent (`~/Desktop/side_project/job_finding_agent`)

Run tests with `.venv/bin/python -m pytest -m "not e2e" -q`.

### Task 1: Local git baseline

**Files:** none changed.

- [ ] **Step 1:** Run `git init && git add -A && git commit -m "Baseline before Argus integration"` (`.gitignore` already excludes `data/`, `config/profile.yaml`, `config/.env`, `documents/`). Check first with `git status --ignored` that no file under `data/` or `config/profile.yaml` is staged.
- [ ] **Step 2:** Run the fast tests. Expected: all pass. Record the count.

### Task 2: File-based human bridge (`--human-dir`)

**Files:**
- Modify: `jobagent/human.py`, `jobagent/tools.py` (`wait_for_human_action`), `jobagent/cli.py`, `jobagent/agent.py` (`run_application` gets `human: Human | None = None`)
- Test: `tests/test_human_bridge.py`

**Interfaces (produces; Argus Task 9 depends on these file formats):**
- `class FileHuman(Human)` in `human.py`: `__init__(self, dir: Path, timeout_s: int, job_url: str)`.
- Request file `<dir>/q-<n>.json`, with n = 1, 2, … per run: `{"id": n, "kind": "question" | "action", "text": str, "job_url": str, "at": iso, "timeout_s": int}`. Write it atomically (tmp + rename).
- Answer file `<dir>/a-<n>.json`: `{"answer": str}`. FileHuman polls every 2 s and deletes nothing.
- `async ask(question) -> str | None`: writes a `question` request and returns the answer, or None on timeout. On timeout it also writes `<dir>/q-<n>.expired`.
- `async wait_action(instructions, cleared: Callable[[], Awaitable[bool]]) -> str`: writes an `action` request. Returns `"done"` when the answer is done/ok/好了/完成, `"skip"` when it is skip/跳过, `"cleared"` when `cleared()` is true on 3 polls in a row, and `"timeout"` otherwise.
- `wait_for_human_action` calls `ctx.human.wait_action(...)` when `isinstance(ctx.human, FileHuman)`; otherwise the old stdin path is unchanged. On "skip" or "timeout" the tool tells the model to stop with `report_result('blocked')`.
- CLI: global `--human-dir DIR`. It implies `interactive=True` and `human_timeout_s=1800` unless `--human-timeout` is given.

- [ ] **Step 1: Write failing tests** `test_ask_writes_request_and_reads_answer` (a background task writes `a-1.json` with `{"answer":"Yes"}` → `ask` returns `"Yes"`, and `q-1.json` has `kind == "question"`), `test_ask_timeout_returns_none_and_marks_expired` (timeout_s=1 → None, and `q-1.expired` exists), `test_wait_action_done_and_skip` (answers "好了" → "done", "skip" → "skip"), `test_wait_action_cleared` (`cleared` always True → "cleared" with no answer file).
- [ ] **Step 2:** Run them. Expected: FAIL (`FileHuman` not defined).
- [ ] **Step 3:** Implement `FileHuman`, the `wait_for_human_action` branch and the CLI flags.
- [ ] **Step 4:** Run the new tests and the full fast suite. Expected: PASS.
- [ ] **Step 5:** Commit `Add file-based human bridge (--human-dir)`.

### Task 3: `--result-file` and accounts created

**Files:** Modify `jobagent/cli.py`, `jobagent/agent.py`, `jobagent/vault.py`. Test `tests/test_units.py`.

**Interfaces (produces):**
- `Vault.created_since(ts: str) -> list[str]`: site keys whose `created_at >= ts` and whose `status` is `registered` or `verified`.
- The `run_application` entry gains `"accounts_created": list[str]`.
- `apply --result-file PATH`: after the run, write the entry dict as JSON to PATH (atomic). When the run crashes before an entry exists, write `{"status": "failed", "notes": "crash: <repr>"}`. The exit code behaviour is unchanged.

- [ ] **Step 1:** Write `test_vault_created_since` (an old entry is excluded; a new `registered` entry is included; a new entry with status `new` is excluded) and `test_result_file_written_on_crash` (monkeypatch `run_application` to raise → the file has `status == "failed"`).
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run the fast suite. Expected: PASS.
- [ ] **Step 5:** Commit `Add --result-file and accounts_created`.

### Task 4: Cover letters and human-voice answers (G5)

**Files:**
- Create: `jobagent/cover_letter.py`
- Modify: `jobagent/tools.py` (two tools), `jobagent/prompt.py` (style rules), `jobagent/config.py` (`Profile.cover_letters` property), `config/profile.example.yaml` (new section)
- Test: `tests/test_cover_letter.py`

**Interfaces (produces):**
- Profile section:
  ```yaml
  cover_letters:
    library_dir: ~/Desktop/about_me/internships/applications   # reusable letters: <company>/ and general/
    output_dir: ~/Desktop/about_me/internships/applications    # generated: <company-slug>/cover_letter_<YYYY-MM-DD>.{md,docx}
    facts_files: [~/Desktop/about_me/PROFILE.md, ~/Desktop/about_me/WORK_AND_PROJECTS.md, ~/Desktop/about_me/EDUCATION.md, ~/Desktop/about_me/internships/APPLY_BRIEF.md]
  ```
- `BANNED_PHRASES: tuple[str, ...]` in `cover_letter.py`, at least: "thrilled", "leverage", "passionate", "synerg", "excited to apply", "i am writing to express", "fast-paced", "dynamic team", "cutting-edge", "delve", "tapestry", "testament", "—" (em dash). Matching is case-insensitive substring.
- `def slug(company: str) -> str` (lowercase, non-alnum → `_`).
- `def library(lib_dir: Path, company: str) -> list[Path]`: `.pdf`/`.docx`/`.md` files in `lib_dir/<slug or exact dir name, case-insensitive>/` first, then `lib_dir/general/`.
- `def violations(text: str) -> list[str]`: the banned phrases found.
- `async def write_letter(company, job_title, job_description, facts: str, query_fn=None) -> str`: one tool-less SDK call. The system prompt holds the style rules (≤ 250 words; plain short sentences; one or two concrete projects with real numbers from the facts; no claims beyond the facts; no greeting clichés; sign as the applicant). If `violations()` is non-empty, retry once with the violations listed. If they are still non-empty, raise `CoverLetterError`.
- `def save_letter(text, out_dir, company) -> tuple[Path, Path]`: writes the `.md`, then the `.docx` via `textutil -convert docx`.
- Tool `cover_letter_library(company)`: returns the file names (relative to `library_dir`).
- Tool `cover_letter(company, job_title, job_description, use_existing: str | null)`: registers the chosen or generated file as document `cover_letter` for `browser_upload` (add it to `ctx.extra_documents`, which `browser_upload` checks next to `profile.documents()`). Returns the letter text when it was generated, so the agent can paste it into text boxes.
- `prompt.py` gets a "Free-text answers" section: the same style rules and banned phrases, and facts from `profile_get` only. When a cover letter field exists, call `cover_letter_library` first and reuse a letter whose file name fits the role (e.g. `General`, `NewGrad`); otherwise call `cover_letter`.

- [ ] **Step 1:** Write `test_violations_found_case_insensitive`, `test_library_company_dir_before_general` (tmp dirs), `test_write_letter_retries_once_then_raises` (a fake `query_fn` returns "I am thrilled…" twice → `CoverLetterError`, and it was called twice), `test_write_letter_ok` (clean text returned unchanged), `test_save_letter_writes_md_and_docx` (skip if `textutil` is missing).
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement the module, the tools and the prompt section.
- [ ] **Step 4:** Run the fast suite. Expected: PASS.
- [ ] **Step 5:** Commit `Add cover letter tools and free-text style rules`.

---

## Part B — Argus (worktree of `~/Argus-build`)

Run tests with `.venv/bin/python -m pytest -q`. Baseline: 941 passed, 5 skipped. First commit the pending contact-mode send fix (`argus/channel/imessage.py`, `tests/test_imessage.py`) on main as `Contact mode: send via the iMessage account, not chat id`. Its tests pass already.

### Task 5: Applications tracker module

**Files:**
- Create: `argus/jobs/tracker.py`
- Test: `tests/test_jobs_tracker.py`

**Interfaces (produces):**
- `class ApplicationsTracker`: `__init__(self, internships_dir: Path)`. Its DB is `internships_dir / "applications.db"` and its Markdown is `internships_dir / "ALL_APPLICATIONS.md"`.
- `migrate() -> None`: idempotent `ALTER TABLE applications ADD COLUMN` for `url TEXT`, `req_id TEXT`, `source TEXT` (`'tracker'` for existing rows, `'argus'` for new ones), `posting_id INTEGER`. Creates both tables with the same schema as `build_db.py` when the DB is missing.
- `add_application(*, applied_date: date, company, role, city, start, region="Canada", url=None, req_id=None, posting_id=None, notes=None) -> int`: status `Applied`.
- `set_status(app_id: int, status: str, when: date, note: str) -> None`. Status is one of `Applied, Confirmed, Action needed, In process, Rejected, Position closed`. The note is appended with `" ; "`, like build_db.
- `add_email_event(*, email_date, account, sender, subject, company, role, kind, summary, action="", deadline="", mail_rowid=None) -> int`. Kind is one of `confirmation, rejection, action, update`. Skip the insert if `mail_rowid` already exists (idempotent).
- `rows() -> list[dict]`.
- `render_markdown() -> None`: rewrites `ALL_APPLICATIONS.md` as: a header line `# All applications — generated from applications.db (do not edit the tables by hand)`, then `## Canada / remote — N` and `## China — N` tables with columns `| # | Date | Company | Role | City | Start | Status |`, then everything from the existing file's `## Filled, waiting on the user` heading to EOF, copied verbatim (omitted if the heading is absent). Write to a tmp file, then rename.

- [ ] **Step 1:** Write tests against a copy of a small fixture DB built with build_db's schema: `test_migrate_idempotent`, `test_add_and_set_status_appends_note`, `test_email_event_dedup_by_rowid`, `test_render_keeps_manual_footer`, `test_render_counts_by_region`.
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement with `sqlite3`, one connection per call, `with conn:` transactions.
- [ ] **Step 4:** Run the full suite. Expected: PASS.
- [ ] **Step 5:** Commit and push `Jobs: applications tracker (applications.db is the source)`.

### Task 6: Duplicate check

**Files:**
- Create: `argus/jobs/dedup.py`
- Test: `tests/test_jobs_dedup.py`

**Interfaces:**
- Consumes: `ApplicationsTracker.rows()` (Task 5), `Store.list_postings(status="submitted")`, and the jobagent tracker at `<jobagent live data>/applications.jsonl` (entries with `status` submitted or already_applied).
- Produces: `@dataclass(frozen=True) class DupVerdict: kind: Literal["none","duplicate","possible"]; detail: str`.
- `def check_duplicate(posting: dict, tracker_rows: list[dict], submitted_postings: list[dict], jobagent_entries: list[dict]) -> DupVerdict` (pure).
- Helpers: `norm_company(s)` (casefold; drop `inc, ltd, corp, canada, group, technologies` and punctuation; text in parentheses dropped; "RBC" == "Royal Bank of Canada" is NOT handled, so exact normalized match only), `req_id(text)` (regex for `R\d{5,}`, `JR\d+`, `REQ[-_ ]?\d+`, `\b\d{5,7}\b` inside parentheses or `_` URL tails), `norm_url(url)` (lowercase host, drop the fragment and the query except `company`, `jobId`, `gh_jid`; strip trailing `/`).
- Rules, in order. Same `norm_url` → duplicate. Same company and same req id → duplicate. Same company, both req ids known and different → none. Same company and role token Jaccard ≥ 0.6 (tokens: lowercase words ≥ 2 chars, minus `intern, internship, co, op, coop, 2026, 2027, winter, summer, fall, new, grad`) → `possible` if either req id is missing, otherwise none. Same company and identical normalized role → duplicate. The detail names the matched row: `"tracker #12 Company C — Backend Developer Intern"` or `"posting #31 …"` or `"jobagent 2026-10-23 Company D — …"`.

- [ ] **Step 1:** Write tests: `test_dedup_same_url_with_tracking_params`, `test_dedup_same_req_id_different_url`, `test_dedup_different_req_ids_same_role_is_none` (three rows of one company with req ids R482913/4/5), `test_dedup_similar_role_without_req_is_possible`, `test_dedup_other_company_sharing_word_is_none` ("Vandelay Bank" vs "Bank Inc").
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run the full suite. Expected: PASS.
- [ ] **Step 5:** Commit and push `Jobs: duplicate check across tracker, postings and jobagent`.

### Task 7: Mail bridge (Mail.app → `.eml`)

**Files:**
- Modify: `argus/mail/reader.py` (add `read_raw`)
- Create: `argus/jobs/mail_bridge.py`
- Test: `tests/test_mail_reader.py` (extend), `tests/test_jobs_mail_bridge.py`

**Interfaces (produces):**
- `MailReader.read_raw(id: int) -> bytes`: the RFC 822 bytes of the `.emlx` (the byte-count line, then exactly that many bytes). Raises `MailNotFound` when there is no file or only a `.partial.emlx`.
- `def export_new(reader, after_id: int, out_dir: Path, exported: set[int]) -> int`: for each `reader.inbox_since(after_id, limit=200)` message not in `exported`, write `out_dir/<id>.eml` (tmp + rename) and add the id. `MailNotFound`/`MailReaderError` → skip, retry next call. Returns the count written.
- `async def run_bridge(reader, after_id, out_dir, poll_s=5.0) -> None`: loops `export_new` in `asyncio.to_thread` until cancelled.

- [ ] **Step 1:** Write `test_read_raw_returns_message_bytes` (use `mail_fixtures.py`) and `test_export_skips_missing_then_picks_up` (first call: the file is missing → 0 written; create the file; second call → 1 written, and no duplicate on a third call).
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement. Reuse `_find_file`.
- [ ] **Step 4:** Run the full suite. Expected: PASS.
- [ ] **Step 5:** Commit and push `Jobs: export new Mail.app messages for jobagent`.

### Task 8: Config, statuses, and the jobagent applier

**Files:**
- Modify: `argus/config.py` (`JobsConfig` + `_load_jobs` keys), `argus/store.py` (`JOB_STATUSES` += `"notify_only"`), `argus.example.toml`
- Create: `argus/jobs/jobagent_applier.py`
- Test: `tests/test_config.py`, `tests/test_jobs_jobagent_applier.py`

**Interfaces (produces):**
- New `JobsConfig` fields: `jobagent_dir: str = "~/Desktop/side_project/job_finding_agent"`, `jobagent_profile: str = "config/profile.yaml"` (relative to jobagent_dir), `jobagent_model: str | None = None`, `notify_only_companies: tuple[str, ...] = ("Google", "Amazon", "Meta", "Apple", "Microsoft", "Nvidia", "OpenAI")`, `human_timeout_minutes: int = 30`, `summary_mail_wait_minutes: int = 20`. Properties on `Config`: `jobagent_dir: Path`, `jobagent_live_dir: Path` (= `jobagent_dir / "data" / "live"`).
- `ApplyResult` (in `applier.py`) gains `accounts_created: tuple[str, ...] = ()`, `company: str | None = None`, `role: str | None = None`.
- `STATUS_MAP = {"submitted": "submitted", "already_applied": "skipped", "needs_input": "needs_user", "blocked": "needs_user", "ready_to_submit": "needs_user", "failed": "error"}`. `already_applied` keeps the detail `"already applied on site"`.
- `def preflight(cfg) -> str | None`: returns an error string if `bin/jobagent` is missing, if the profile is missing, if the profile path contains `test-profile`, or if the profile's `account.email` is `<test account email>` (found with a regex on the `email:` line inside the `account:` block, so Argus does not need PyYAML).
- `async def apply_with_jobagent(cfg, posting, *, reader, relay, run_dir: Path | None = None, spawn=asyncio.create_subprocess_exec) -> ApplyResult`:
  - `run_dir` defaults to `cfg.jobagent_live_dir / "argus" / <posting id>-<timestamp>`, with `human/` and `mail/` subdirs.
  - Command: `[<jobagent_dir>/bin/jobagent, "--profile", <profile>, "--human-dir", <run_dir/human>, "--human-timeout", <minutes*60>, *(["--model", m] if m), "apply", url, "--mode", "auto", "--result-file", <run_dir/result.json>]`, with env `JOBAGENT_DATA_DIR=<live dir>` and `JOBAGENT_MAIL_DIR=<run_dir/mail>`. Use `start_new_session=True` and stdout/stderr redirected to `run_dir/jobagent.log`.
  - While it runs: `mail_bridge.run_bridge(reader, reader.max_id(), run_dir/mail)` and `relay.watch(run_dir/"human", posting)` (Task 9) as tasks. Both are cancelled at the end.
  - On `CancelledError`: `os.killpg(proc.pid, SIGTERM)`, wait 10 s, then SIGKILL, and re-raise.
  - Map `result.json` via `STATUS_MAP`. A missing or unparseable file → `error` with the last 300 chars of `jobagent.log`.
- `APPLY_TIMEOUT` in the scheduler becomes `(30 + human_timeout_minutes + 15) * 60`.

- [ ] **Step 1:** Write tests with a fake `spawn` that runs a tiny Python script writing a given `result.json`: `test_maps_submitted_with_accounts`, `test_missing_result_is_error_with_log_tail`, `test_preflight_refuses_test_profile_and_test_email`, `test_cancel_kills_process_group` (the fake script sleeps 60 s; cancel → the process is gone within 12 s), `test_env_points_to_live_dir_and_run_mail_dir`. Config tests: the new keys parse, a bad type raises a `ValueError` naming `jobs.<key>`, and the defaults apply.
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run the full suite. Expected: PASS.
- [ ] **Step 5:** Commit and push `Jobs: jobagent applier, notify_only status, config`.

### Task 9: Human relay over iMessage

**Files:**
- Create: `argus/jobs/human_relay.py`
- Modify: `argus/tools/jobs.py` (tool `job_answer`), `argus/main.py` (create one `HumanRelay`; share it between the scheduler and the tools)
- Test: `tests/test_jobs_human_relay.py`, `tests/test_jobs_tools.py`

**Interfaces:**
- Consumes: the request/answer file formats from Task 2.
- Produces: `class HumanRelay`: `__init__(self, submit: Submit | None, now=datetime.now)`; `async watch(self, human_dir: Path, posting: dict, poll_s=2.0)` (until cancelled; for each new `q-<n>.json` it registers question id `"<posting id>-<n>"` and submits an internal task); `answer(self, qid: str, text: str) -> str` (writes `a-<n>.json` and returns `"sent"`; returns `"expired"` if `q-<n>.expired` exists or the question is older than its `timeout_s`; `"unknown"` for an unregistered id); `pending() -> list[dict]`.
- Internal task text (`INTERNAL_PREFIX` + untrusted block). Question: "jobagent, applying to {company} — {role}, needs an answer. Ask the user this question, with question id {qid}. When they answer, call job_answer." Action: "…needs the user to act in the jobagent Chrome window on the Mac mini (e.g. a captcha): {text}. Tell them to do it and reply 好了 / done, or skip; then call job_answer with id {qid}." Both say the request expires in N minutes.
- Tool `job_answer(question_id: str, answer: str)`: `_require_user_turn` first, then `relay.answer`. Replies: sent → "Passed to jobagent."; expired → "That question expired; the application was marked needs_user."; unknown → "No such pending question."

- [ ] **Step 1:** Write `test_watch_submits_internal_task_once_per_question`, `test_answer_writes_file`, `test_answer_after_timeout_is_rejected`, `test_job_answer_refused_in_system_turn`.
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement, and wire it in `main.py` and `build_jobs_tools`.
- [ ] **Step 4:** Run the full suite. Expected: PASS.
- [ ] **Step 5:** Commit and push `Jobs: relay jobagent questions over iMessage (job_answer)`.

### Task 10: Scheduler v2 (jobagent, notify-only, dedup, tracker writes, quiet alerts)

**Files:**
- Modify: `argus/jobs/scheduler.py`, `argus/main.py`
- Test: `tests/test_jobs_scheduler.py`

**Interfaces:**
- Consumes: Tasks 5, 6, 8, 9.
- `JobScheduler.__init__` gains `tracker: ApplicationsTracker | None`, `reader`, `relay: HumanRelay | None`. The default `apply_fn` becomes `functools.partial(apply_with_jobagent, reader=reader, relay=relay)`.
- `_store_postings`: if `norm_company(company)` equals a normalized `notify_only_companies` entry, set status `notify_only` and send right away: "Top-tier opening (Argus never applies here): {company} — {role}, {url}". This happens once per posting, because upsert keeps its status.
- `_maybe_apply`: before marking a posting `applying`, run `check_duplicate`. `duplicate` → `skipped` "duplicate of …". `possible` → `skipped` "possible duplicate of …". Neither is counted as a run. Then `preflight(cfg)`: an error string → leave the posting `queued`, pause until tomorrow, and send one alert per day (same pattern as `KEY_CHROME_NOTIFIED`; new key `jobs_preflight_notified`).
- `_resolve`: on `submitted`, also `tracker.add_application(...)` (applied_date today, company/role from the result or the posting, city, start = term, url, `req_id(url or role)`, `posting_id`) and `render_markdown()`. Tracker errors are logged, never raised.
- Alerts: requested postings keep their immediate alerts (`APPLY_ALERT_TEXT`; rewrite `needs_login` to "jobagent could not log in or register"). Automatic postings send nothing per application; their outcome is reported by Task 11's summary. Delete the v1 Chrome path: `applier.apply_to` and its prompt/options builders, `CHROME_TEXT`, the `CHROME_*` constants, `chrome_not_connected` handling, and their tests. Keep `ApplyResult` and `PRIOR_ATTEMPT` in `applier.py`.

- [ ] **Step 1:** Update or add tests: `test_notify_only_company_never_queued_and_alerts_once`, `test_duplicate_skipped_before_apply`, `test_possible_duplicate_skipped_with_detail`, `test_preflight_error_pauses_and_alerts_once_per_day`, `test_submitted_writes_tracker_row`, `test_auto_application_sends_no_alert`, `test_requested_application_still_alerts`. Remove the Chrome tests.
- [ ] **Step 2:** Run. Expected: the new tests FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run the full suite. Expected: PASS.
- [ ] **Step 5:** Commit and push `Jobs: scheduler uses jobagent, notify-only, dedup, tracker`.

### Task 11: Run summary (G6)

**Files:**
- Create: `argus/jobs/summary.py`
- Modify: `argus/jobs/scheduler.py` (run state + trigger)
- Test: `tests/test_jobs_summary.py`, `tests/test_jobs_scheduler.py`

**Interfaces:**
- State key `jobs_run`: JSON `{"day", "started_at", "mail_after_id", "scout_done": bool, "work_done_at": iso | null, "summary_sent": bool}`. It is created when the daily scout starts (`mail_after_id = reader.max_id()`).
- Work is done when the scout has finished and no automatic posting is `queued` or `applying`, or when the apply window has closed, or when the daily auto limit is reached. Set `work_done_at` once. When `now >= work_done_at + summary_mail_wait_minutes`, run the summary once (`summary_sent` is set before sending; one attempt).
- `@dataclass class JobEmail: mail_id: int; account: str; sender: str; subject: str; received_at: datetime; kind: str; company: str | None; role: str | None; summary: str; action: str; deadline: str`.
- `async def classify_job_emails(mails: list[tuple[MailSummary, str]], model: str, runner=triage.sdk_runner) -> list[JobEmail]`: one tool-less call per batch of ≤ 15. Bodies are cut to 2000 chars and wrapped in `<untrusted_email>`. The model returns a JSON array. Mails that are not job-related are dropped. Kinds: `confirmation, rejection, action, update`.
- `def apply_email_events(tracker, events) -> list[str]`: insert `email_events`. Match rows by `norm_company`. If `role` is given, match only rows with role Jaccard ≥ 0.5 (Task 6 tokens); without a role, match all rows of that company. Map rejection → `Rejected` ("filled/closed" in the summary → `Position closed`), action → `Action needed`, confirmation → `Confirmed` (only from `Applied`), update → `In process` (only from `Applied`/`Confirmed`). Returns notes for unmatched events. Then `render_markdown()`.
- `def build_summary_task(run, postings_today, events, unmatched, accounts_created) -> str`: an internal task listing: submitted (company — role), accounts created (site keys), needs you (needs_user/error with detail and link), skipped duplicates, top-tier openings, and notable emails (rejections, OAs/actions with deadlines, confirmations as a count). Web/email text goes in untrusted blocks. It ends with "Send ONE concise message."
- `accounts_created` is collected from the `ApplyResult`s of that day. Store them in `job_runs.summary` or in the `jobs_run` state, appended per run.

- [ ] **Step 1:** Write tests: `test_classify_parses_and_drops_non_job` (fake runner), `test_email_event_role_disambiguation` (Company A ×4: an email with role "Pickle" updates one row; an email without a role updates all four; a company sharing one of its words untouched), `test_confirmation_does_not_downgrade_rejected`, `test_summary_runs_once_after_wait` (fake clock: scout done, queue empty → nothing before 20 min, one submit after), `test_summary_after_window_close_with_queue_left`.
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement. Read mail with `reader.inbox_since(mail_after_id)` and `read_body`, prefiltering out messages already in `email_events.mail_rowid`.
- [ ] **Step 4:** Run the full suite. Expected: PASS.
- [ ] **Step 5:** Commit and push `Jobs: end-of-run summary with email tracker updates`.

### Task 12: Wider scout (G3)

**Files:** Modify `argus/jobs/scout.py`, `argus/jobs/guard.py` (scout may read `ALL_APPLICATIONS.md` and `CN_TRACKER.md`; it already may read text files under about_me, so only check). Test `tests/test_jobs_agents.py`.

**Interfaces:**
- `SCOUT_PROMPT` rewrite. Roles: SDE/software engineer, MLE, AI agent developer, project/product manager, or very close. Level: internships/co-ops Winter or Summer 2027 and new-grad. Location: Montréal, Toronto, or remote workable from Canada. Rank big/mid-size tech and finance higher (fit +1, max 5). Sources: Reddit, Xiaohongshu, LinkedIn, then career pages and job boards. Follow shared posts to the employer's real application URL. The same job seen in several places is one posting. Read `ALL_APPLICATIONS.md` and do not list jobs already applied to. `seen_companies.txt` is a record only, not a skip list (keep appending). When a source cannot be read, say so in the summary.
- `POSTING_FIELDS` += `"source"` (where it was found) and `"level"` (`intern` | `new_grad`). Store them in `notes` (prefix `[source: …]`) to avoid a schema change.

- [ ] **Step 1:** Write `test_scout_prompt_scope` (asserts the prompt contains "Reddit", "Xiaohongshu", "LinkedIn", "remote", "product manager", "ALL_APPLICATIONS.md", and no longer contains "Do NOT re-list any company already in seen_companies.txt") and `test_parse_postings_folds_source_into_notes`.
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run the full suite. Expected: PASS.
- [ ] **Step 5:** Commit and push `Jobs: wider scout scope and sources`.

---

## Part C — Data, setup and go-live (done by the controller, with the user)

These steps change the user's files, not code. Do them in order. Check every step's output.

### Task 13: One memory (G2)

- [ ] **Step 1: Back up.** `cp -a ~/Desktop/about_me ~/Desktop/about_me.bak-2026-10-29` and `cp -a ~/Argus-build/data/about_me ~/Argus-build/data/about_me.pre-v2`.
- [ ] **Step 2: Merge Argus's copy into the Desktop.** The Desktop is newer for every profile file (10-21 vs 09/10-13), so keep the Desktop versions of `PROFILE.md`, `EDUCATION.md`, `WORK_AND_PROJECTS.md`, `OPEN_QUESTIONS.md`, `TIMELINE.md` and `APPLY_BRIEF.md`. Show the user the few lines the Argus copy had that the Desktop lacks (`diff`), and add them only if the user agrees. Copy the files that exist only in Argus's copy: `internships/raw/*_argus.md` and `internships/applications/*_argus_*.md`. In `INTERNSHIPS.md`, append the `## Argus session — 2026-10-14` … `2026-10-29` sections from Argus's copy after the Desktop content. `seen_companies.txt`: the union, keeping order (Desktop first).
- [ ] **Step 3: Tracker.** Rename `internships/db/build_db.py` → `build_db_2026-10-23_legacy.py`. Then run `ApplicationsTracker.migrate()`. Add Argus's 4 submitted postings, skipping any that `check_duplicate` already finds (two are probably in the tracker). Run `render_markdown()`. Check: row count = the old count + newly added, and the status counts are unchanged for old rows.
- [ ] **Step 4: Point Argus at the Desktop.** In local `argus.toml` `[jobs]`: `internships_dir = "~/Desktop/about_me/internships"`, `resume_path = "<résumé path>"`. The repo default stays `data/about_me/...`.
- [ ] **Step 5: APPLY_BRIEF.md.** Update the hard rules to match the new policy: account creation and login are allowed through jobagent (with the password never typed by a model); PM roles are in scope; top-tier companies are notify-only. Show the user the diff before saving.

### Task 14: jobagent live profile

- [ ] **Step 1:** Generate `config/profile.yaml` from `PROFILE.md`, `EDUCATION.md`, `WORK_AND_PROJECTS.md` and `APPLY_BRIEF.md` (facts only; leave a field empty when the files do not say). Use `account.email: <account email>`, `account.password: ""`, `documents.resume: <résumé path>`, and the `cover_letters` section from Task 4. `chmod 600`.
- [ ] **Step 2:** The user reviews the profile and types the password into it themselves (`! open -e config/profile.yaml`). Claude never sees or echoes it. Check only with `grep -c 'password: ""'` → 0.
- [ ] **Step 3:** `JOBAGENT_DATA_DIR=data/live bin/jobagent doctor` → profile ✓, resume ✓, chrome ✓.
- [ ] **Step 4: Dry run on one real posting** (a Workday posting from `job_postings` with status `new`): `JOBAGENT_DATA_DIR=data/live bin/jobagent --human-dir /tmp/… apply <url> --mode dry-run --result-file …`. The user watches; answer one bridge question by hand. Expected: `ready_to_submit`, and the verification mail is read through `JOBAGENT_MAIL_DIR` if the site sends one.

### Task 15: Deploy

- [ ] **Step 1:** Merge the worktree branch into main in `~/Argus-build`. Run the full suite there. Push.
- [ ] **Step 2:** Check that Argus is idle (last message more than 5 min ago, `pending` empty). Then `launchctl kickstart -k gui/$(id -u)/com.argus.agent`. Expected: `Argus started` in `logs/argus.log`, and no `Operation not permitted` on `~/Desktop/about_me` (launchd's Python must be able to read the Desktop; if not, the user grants Full Disk Access to `.venv/bin/python`'s real binary).
- [ ] **Step 3:** Ask Argus over iMessage to apply to one specific posting ("申请 #id"). Expected: an immediate result message; on `submitted`, a new row in `applications.db` and `ALL_APPLICATIONS.md`.
- [ ] **Step 4:** The next morning, check the 07:30 run end to end: scout → applications → one summary iMessage.
- [ ] **Step 5:** Update the memory file `argus-project.md` with the v2 status.
