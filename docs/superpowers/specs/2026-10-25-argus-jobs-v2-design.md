# Argus Jobs v2 — jobagent applier, single memory, wider search, run summary

- Date: 2026-10-25
- Status: Approved by the user 2026-10-29 (builder decisions in §8)
- Builds on: `2026-10-14-argus-jobs-design.md` (v1, implemented and running)
- Related project: `~/Desktop/side_project/job_finding_agent` ("jobagent")

## 1. Intent

The user wants Argus to run their entry-level job hunt end to end, in this order:

1. **Read memory**: what has already been applied to, what is new, which sites need an account.
2. **Find new openings** on the web, mainly where people share them (Reddit, Xiaohongshu, LinkedIn).
3. **Apply** by handing each posting to jobagent, which creates the account if needed, logs in, fills the form and submits.
4. **When all work is done**, read the resulting emails, update memory, and **send one iMessage** saying what was done.

v1 already covers search, a tracker, auto-apply and mail reading. v2 changes *how* applications happen and closes the gaps listed below. Argus stays the orchestrator; jobagent is the worker.

### What v1 does today (keep unless changed below)
- Daily scout at 07:30 (`argus/jobs/scout.py`), postings in `job_postings`, auto-apply with fit ≥ 4, daily limit 8, apply window 08:30–22:00, one applier at a time (`argus/jobs/scheduler.py`).
- Applier (`argus/jobs/applier.py`) drives the user's Chrome through Claude in Chrome with `APPLY_BRIEF.md` as the prompt; it may not create accounts or type passwords, so many postings end as `needs_login`.
- Mail reader/watcher and the 08:00 briefing already exist (`argus/mail/`, `argus/briefing.py`).
- Memory for jobs is `data/about_me/` — a **copy** taken 2026-10-14 that has since diverged from `~/Desktop/about_me`.

## 2. Goals

1. **G1 – Applier = jobagent.** Replace the Claude-in-Chrome applier with a call to jobagent (`bin/jobagent apply <url> --mode auto --unattended`), so account creation, login, password reset and email verification are handled. Argus maps jobagent's result into `job_postings.status`.
2. **G2 – One memory.** `~/Desktop/about_me` is the single source of truth (user decision). Argus reads and writes it directly; the `data/about_me` copy is merged into it once and then removed (or replaced by a symlink). Specifically `internships/ALL_APPLICATIONS.md`, `INTERNSHIPS.md`, `FOLLOW_UPS.md`, `seen_companies.txt`, `applications.db`, `applications/`.
3. **G3 – Search scope** (user's words):
   - Roles: SDE / software engineer, MLE, AI agent developer, project manager, or very close paths. (This widens v1's "code-writing roles only" rule to include PM.)
   - Level: entry — internships/co-ops (Winter 2027, Summer 2027) and new-grad roles. *(Inferred from the existing tracker; user did not object.)*
   - Location: Montréal, Toronto, or remote (remote = workable from Canada).
   - Preference: big and mid-size tech, and finance — rank these higher.
   - Sources: Reddit, Xiaohongshu (小红书), LinkedIn, plus anything else where people share openings; company career pages and job boards remain fine. A shared post is followed to the employer's real application URL; the same job seen in several places is one posting.
4. **G4 – Top-tier companies: notify only.** For a configurable list (default: Google, Amazon, Meta, Apple, Microsoft, Nvidia, OpenAI — *my guess at "Google/Amazon level"; user can edit*), Argus never applies. When a matching role opens it sends an iMessage with the company, role and link, and marks the posting `notify_only`.
5. **G5 – Cover letters and open questions.** When a form asks for a cover letter or free-text answers ("why us", "describe a project"):
   - Use an existing letter from `~/Desktop/about_me/internships/applications/` if one fits the role (e.g. `general/…NewGrad.pdf`, `general/…General.pdf`, or a company's own letter).
   - Otherwise write one from the user's real experience only (`PROFILE.md`, `WORK_AND_PROJECTS.md`, `APPLY_BRIEF.md` facts), in a plain, human voice — short sentences, specific details, no stock AI phrasing ("thrilled", "leverage", "passionate about synergies"), no invented facts. Generated letters are saved under `applications/<company>/` so they can be reused and checked.
6. **G6 – Run summary.** After a run's work is finished (scout + all applications it queued), Argus waits for confirmation mail to arrive, reads the job-related emails since the run started, updates `applications.db` (`applications`, `email_events`) and `ALL_APPLICATIONS.md`, and sends **one** iMessage: submitted (company – role), accounts created, needs you (captcha, missing answer, verification), top-tier openings, and notable emails (confirmations, OAs, rejections).

## 3. Non-goals
- Solving captchas automatically.
- Applying outside Canada/remote, or to senior roles.
- Changing the fitness, mail-alert or briefing features except to add the jobs summary.
- Sending email on the user's behalf.

## 4. Anti-goals (failure even if it "works")
- **Applying twice to the same job.** Permanent user rule ("never ever"). Before each application: check `ALL_APPLICATIONS.md`, `INTERNSHIPS.md`, `FOLLOW_UPS.md`, `applications.db`, jobagent's own `data/applications.jsonl`, and the site's "already applied" state.
- **Auto-applying to a top-tier company.**
- **Invented facts** in forms, cover letters or answers.
- **Cover letters that read as AI-written.**
- **Submitting with test data** (jobagent's `data/test-profile*.yaml`, test account `<test account email>`).
- **Leaking sensitive data**: `SENSITIVE_IDS.md` stays off-limits to agents, as in v1's guard.

## 5. Decisions so far

| Decision | Choice | By |
|---|---|---|
| Orchestrator / worker | Argus orchestrates; jobagent applies | user |
| Memory | `~/Desktop/about_me` is Argus's main memory for jobs | user |
| Submission | Fully automatic (no per-job approval), except top-tier = notify only | user |
| Cover letters | Reuse local letter if suitable, else generate from real experience, human voice | user |
| Search sources | Reddit, Xiaohongshu, LinkedIn, and similar | user |
| Summary channel | iMessage via Argus's existing channel | user |
| jobagent invocation | Subprocess `bin/jobagent`, one posting at a time, JSON result read back | my call |
| jobagent profile | Generate `job_finding_agent/config/profile.yaml` from `APPLY_BRIEF.md` + `PROFILE.md`; résumé = `<résumé path>` (brief's "only file guaranteed uploadable") | my call |
| Keep v1 limits | fit ≥ 4, ≤ 8/day, 08:30–22:00, one at a time | my call |
| `seen_companies.txt` | No longer a company-wide skip list (the wider role scope would find almost nothing). Duplicates are checked per job instead (see §4); the file stays as a record of companies covered | my call |
| Tracker source of truth | **Changed 2026-10-29 (see §8):** `applications.db` becomes the source; `ALL_APPLICATIONS.md` is rendered from it | my call |
| jobagent result | **Changed 2026-10-29:** jobagent gets a `--result-file` flag and writes its tracker entry there; Argus reads that file plus the exit code | my call |
| `APPLY_BRIEF.md` rules | Update the user's brief to match the new policy (account creation allowed via jobagent; PM roles in scope), since the user changed these rules | my call |
| Message timing | Immediate iMessage only for top-tier openings (G4) and anything needing the user within the run; everything else goes in the one end-of-run summary, which replaces v1's per-application alerts | my call |

## 6. User answers (2026-10-25)
1. **Uncertain duplicate → skip.** If Argus cannot be sure a posting is a different job from a past application (fuzzy company + role match, no URL/req id to tell them apart), it does not apply; the posting is marked `skipped` (`possible duplicate of <row>`) and listed in the summary so the user can apply by hand.
2. **Account password:** one fixed password chosen by the user. It is stored only locally — in jobagent's gitignored config (`config/profile.yaml` `account.password` or `config/.env`, chmod 600) — and injected by jobagent's `account_fill` so the model never sees it. It must never appear in this repo, specs, logs, prompts, memory files or iMessages. Account email: `<account email>` (from the brief; *my call, user did not object*).
3. **Captcha → text the user and wait.** jobagent runs attended-by-iMessage: when it hits a captcha (or other `wait_for_human_action`), Argus texts the user the company, site and what to do in Chrome, and jobagent waits (with a timeout, e.g. 30 min) for the user's reply ("done" / "skip"). On timeout the posting is marked `needs_user` and listed in the summary. This needs jobagent to run in a Chrome the user can see (likely `--cdp` into their own Chrome, or jobagent's visible persistent profile) and a bridge from jobagent's `ask_human` / `wait_for_human_action` to Argus's iMessage channel. Applications already run only inside the 08:30–22:00 window.
4. **Verification mail:** Mail.app has an Exchange account Argus already reads (the user's mail, with the account email's address in the index) — *assumed to be the account email's inbox; builder verifies*. jobagent reads verification codes through a bridge to Argus's Mail.app reader (jobagent's local-directory mailbox), so no IMAP credentials are needed.

## 7. Left to the builder
- How to bridge jobagent's mailbox to Argus's mail reader (jobagent already supports a local-directory mailbox).
- Whether jobagent runs in its own Chrome profile or `--cdp` into the user's Chrome.
- How cover-letter generation is wired (a jobagent tool vs. Argus pre-generating per posting).
- How LinkedIn/Xiaohongshu are read (WebSearch/WebFetch where possible; logged-in browser if they block bots). If a source can't be read reliably, report it rather than silently skipping.
- Merge procedure for the two `about_me` copies (no rows lost; duplicates by company + req id/role).
- Tests: no real application is submitted by tests; jobagent integration tested with its mock ATS.

## 8. Builder decisions (2026-10-29)

- **Tracker source of truth = `applications.db`.** `build_db.py` hard-codes every status change (rejections, confirmations) as Python lists and drops the DB on each run, so "write Markdown, then rebuild" would lose every status Argus records. Argus writes the DB (`argus/jobs/tracker.py`) and re-renders `ALL_APPLICATIONS.md` from it; the hand-written "Filled, waiting on the user" section is kept verbatim. `build_db.py` is renamed `build_db_2026-10-23_legacy.py` so it is not re-run by accident.
- **jobagent stays a subprocess** (crash isolation, its own venv), extended with two flags: `--human-dir DIR` (file bridge for `ask_human` / `wait_for_human_action`) and `--result-file PATH`.
- **Human bridge:** jobagent writes `q-<n>.json`; Argus texts the user through an internal task; the user's reply reaches jobagent through a new chat tool `job_answer(question_id, answer)` (user turns only), which writes `a-<n>.json`. Timeout 30 min, then jobagent stops with `needs_input`/`blocked` → `needs_user`.
- **Mail bridge:** while jobagent runs, Argus copies every new inbox message (all Mail.app accounts) as raw `.eml` into the run's `JOBAGENT_MAIL_DIR`; jobagent's existing `DirMailbox` reads them.
- **Chrome:** jobagent's own visible persistent profile under its live data dir (not `--cdp`, not the user's Chrome).
- **Test/live separation:** live runs use `JOBAGENT_DATA_DIR=<jobagent>/data/live` (own vault, log, Chrome profile). Argus refuses to start jobagent when the profile path contains `test-profile` or the account email is `<test account email>`.
- **Cover letters:** two new jobagent tools, `cover_letter_library` (list reusable letters) and `cover_letter` (reuse one, or write one from the facts files under fixed style rules with a code-enforced banned-phrase check; saved as `.md` + `.docx` under `applications/<company>/`).
- **Requested applications** ("申请 #id") still get an immediate result message; automatic ones only appear in the run summary.
