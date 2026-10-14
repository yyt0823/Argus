# Argus Internship Search + Auto-Apply — Design and Plan

- Date: 2026-10-14
- Status: Requirements from the user ("every morning look for new openings and apply for me; I'll also ask whether there are new openings, then search"); the user asked for full implementation without further check-ins.
- Scope: Roadmap sub-project 5 (applications), internships only. Builds on Core, mail (internal tasks, briefing) and the user's local profile archive.

## 1. Goals

1. Every morning, find new internship postings that fit the user (target from the profile: Summer/Winter 2027 SWE / backend / AI-agent internships; Canadian PR, so Canada roles need no sponsorship, US roles do).
2. Score each new posting for fit, report the best ones in the 08:00 briefing.
3. Apply automatically to high-fit postings on standard ATS forms (Greenhouse, Lever, Ashby), with a per-day cap; anything that can't be completed automatically is sent to the user as a link with the reason.
4. On request ("有没有新的 opening？"), run a search immediately and answer.

### Non-goals

- LinkedIn/Indeed automation (their terms forbid it; account-ban risk). Workday and custom portals (too varied; reported as links). Cover-letter-required postings are applied to only if the form accepts an optional/free-text cover letter generated from the profile.
- Bypassing CAPTCHAs or bot checks: if one appears, stop and send the link.

## 2. Inputs

- **Profile archive** `data/about_me/` (git-ignored): `PROFILE.md` (source of truth for contact data — phone/email/links), `EDUCATION.md`, `WORK_AND_PROJECTS.md`, `OPEN_QUESTIONS.md`, `SENSITIVE_IDS.md` (never used by the job pipeline), `extracted_text/` (CV text).
- **Applicant data** `data/about_me/applicant.toml` (generated once by Argus from `PROFILE.md`, then user-editable; git-ignored): name, email, phone, location, LinkedIn, GitHub, website, school, degree, graduation date, work authorization answers (Canada: authorized, no sponsorship; US: requires sponsorship), EEO answers default "decline to self-identify", résumé PDF path.
- **Résumé PDF**: path in `applicant.toml`. Before any auto-apply, Argus checks the PDF's text contains the phone number from `applicant.toml`; if not (e.g. outdated CV with the old number), auto-apply is blocked and the user is told why.

## 3. Sources

| Source | How |
|---|---|
| Community internship lists | GitHub repos maintained for the season (e.g. `SimplifyJobs/Summer2027-Internships`, and the off-season/Winter list if present); parse the README table (company, role, location, link, date). Repo names are config. |
| Greenhouse boards | `GET https://boards-api.greenhouse.io/v1/boards/<token>/jobs?content=true` |
| Lever | `GET https://api.lever.co/v0/postings/<company>?mode=json` |
| Ashby | `GET https://api.ashbyhq.com/posting-api/job-board/<org>?includeCompensation=false` |

A company watchlist (`[jobs] greenhouse = [...]`, `lever = [...]`, `ashby = [...]`) ships with a starter list of tech companies whose board tokens were verified to respond. Filters before scoring: title matches intern/co-op/internship; season text or posting mentions 2027 (or no season and posted within 60 days); location in Canada, remote, or US (US flagged "needs sponsorship"). Postings are de-duplicated by canonical URL.

## 4. Scoring

One tool-less Claude call per new candidate (model `haiku`, like mail triage): input = posting title/company/location/description excerpt (untrusted, wrapped) + a short profile summary built from the archive (skills, experience, graduation date, authorization). Output JSON: `fit` 0–100, `reasons` (one sentence, Chinese), `season`, `needs_sponsorship` bool, `red_flags`. Postings with fit ≥ `min_fit_report` (default 60) appear in the briefing.

## 5. Applying

- Browser automation with **Playwright (Chromium)** using a persistent profile at `data/browser-profile/` (git-ignored), headless by default.
- Supported forms: Greenhouse (`boards.greenhouse.io`, `job-boards.greenhouse.io`), Lever (`jobs.lever.co`), Ashby (`jobs.ashbyhq.com`). Each has an adapter that maps known fields (name, email, phone, résumé upload, LinkedIn, GitHub, website, school, graduation, work authorization, sponsorship, EEO).
- Unknown required questions: a tool-less Claude call answers from the profile summary (no sensitive IDs); questions that can't be answered truthfully from the profile (e.g. "Are you available full-time in Jan 2027?" when unknown) → stop, mark `needs_user`, send the link and the question.
- Before submit: screenshot to `data/jobs/<id>/before_submit.png`; after submit: verify the confirmation page/text, screenshot `after_submit.png`. Any doubt → status `failed` with reason, never retried automatically more than once.
- **Modes** (config `[jobs] auto_apply`):
  - `false` (default until the user flips it): Argus prepares and reports "I would apply to N postings"; the user can say "申请 <id>" to apply to one (explicit request).
  - `true`: Argus applies to postings with fit ≥ `min_fit_apply` (default 80), at most `daily_apply_limit` (default 10) per day.
- Rate limiting and politeness: ≤ 1 request/second per host for API fetches; one application at a time.

## 6. Data (SQLite)

`job_postings(id, source, company, title, location, url UNIQUE, ats, posted_at, first_seen_at, description_excerpt, fit, reasons, season, needs_sponsorship, status, status_reason, applied_at, screenshot_dir)`; status ∈ `new`, `reported`, `skipped`, `applying`, `applied`, `needs_user`, `failed`.

## 7. Tools (chat session)

| Tool | Purpose |
|---|---|
| `jobs_scan_now` | Run the fetch + filter + score pipeline now; return new candidates (for "有没有新的 opening") |
| `jobs_list` | List postings by status / min fit / date |
| `job_apply` | Apply to one posting by id (explicit user request; works in either mode) |
| `job_skip` | Mark a posting skipped |
| `jobs_profile_check` | Report applicant data, résumé PDF phone check, and any OPEN_QUESTIONS that affect forms |

## 8. Scheduling

07:30 daily scan (and auto-apply if enabled) via an internal loop like the mail watcher; results feed the 08:00 briefing (the briefing text asks the model to call `jobs_list` for postings first seen in the last 24 h with fit ≥ `min_fit_report`, and to report applications made/failed).

## 9. Testing

- Unit: each source parser against saved fixture payloads (synthetic, no personal data); filters; de-dup; scoring parse; applicant data generation from a synthetic PROFILE; résumé phone check; Playwright adapters against **local fixture HTML forms** served from a temp dir (Greenhouse/Lever/Ashby-like), including required unknown question → `needs_user`, CAPTCHA element present → stop, submit verification.
- Live (read-only): fetch real postings from the configured sources and print counts per source; one scoring call.
- No live submission in tests. The first real application happens only when the user explicitly asks or enables `auto_apply`.

## 10. Implementation tasks

| Task | Files |
|---|---|
| J1 Sources + filters + store table + config `[jobs]` | `argus/jobs/sources.py`, `argus/jobs/filters.py`, `argus/store.py`, `argus/config.py` |
| J2 Scoring + applicant data + résumé check | `argus/jobs/scoring.py`, `argus/jobs/applicant.py` |
| J3 Browser applier (Playwright adapters) | `argus/jobs/applier.py`, `argus/jobs/adapters/*.py` |
| J4 Scanner loop, tools, briefing, wiring, persona, docs | `argus/jobs/scanner.py`, `argus/tools/jobs.py`, `argus/tools/server.py`, `argus/sdk_session.py`, `argus/main.py`, `argus/mail/watcher.py`, `CLAUDE.md`, `README.md` |
