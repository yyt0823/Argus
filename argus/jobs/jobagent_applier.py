"""Apply to one posting by running jobagent (a separate project) as a subprocess.

jobagent registers accounts, logs in, verifies email, fills and submits the application. While it
runs, Argus copies new Mail.app messages into the run's ``mail/`` directory (``JOBAGENT_MAIL_DIR``)
and relays jobagent's questions from ``human/`` to the user. The outcome comes back as the
``--result-file`` JSON and is mapped onto Argus's ``ApplyResult``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import signal
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from argus.config import Config
from argus.jobs.agent import opt_str
from argus.jobs.applier import ApplyResult
from argus.jobs.mail_bridge import run_bridge
from argus.mail.reader import MailReader

log = logging.getLogger(__name__)

# jobagent status -> Argus apply result.
STATUS_MAP = {
    "submitted": "submitted",
    "already_applied": "skipped",
    "needs_input": "needs_user",
    "blocked": "needs_user",
    "ready_to_submit": "needs_user",
    "failed": "error",
}
ALREADY_APPLIED_DETAIL = "already applied on site"
TEST_EMAIL = "me@yyt.email"  # jobagent's test account; a live run must never use it
TEST_PROFILE_MARKER = "test-profile"
KILL_GRACE_S = 10.0  # after SIGTERM, before SIGKILL
DETAIL_MAX = 500
LOG_TAIL = 300

# The indented lines after a top-level ``<name>:`` key, and the ``email:`` value among them.
_BLOCK = r"^{name}:[ \t]*(?:#.*)?\n((?:(?:[ \t]+.*|[ \t]*)(?:\n|$))*)"
_EMAIL_LINE = re.compile(r"""^[ \t]+email:[ \t]*["']?([^"'\s#]*)""", re.MULTILINE)


class HumanRelay(Protocol):
    async def watch(self, human_dir: Path, posting: dict) -> None: ...


def jobagent_bin(cfg: Config) -> Path:
    return cfg.jobagent_dir / "bin" / "jobagent"


def jobagent_profile(cfg: Config) -> Path:
    """``jobs.jobagent_profile`` resolved against the jobagent directory (absolute paths are kept)."""
    return cfg.jobagent_dir / Path(cfg.jobs.jobagent_profile).expanduser()


def _block_email(profile_text: str, name: str) -> str | None:
    block = re.search(_BLOCK.format(name=name), profile_text, re.MULTILINE)
    m = _EMAIL_LINE.search(block[1]) if block else None
    return m[1] if m and m[1] else None


def _login_email(profile_text: str) -> str | None:
    """The email jobagent logs in with: ``account.email``, else ``personal.email``."""
    return _block_email(profile_text, "account") or _block_email(profile_text, "personal")


def preflight(cfg: Config) -> str | None:
    """Why jobagent cannot be used for a live application, or None if it can."""
    binary, profile = jobagent_bin(cfg), jobagent_profile(cfg)
    if not binary.is_file():
        return f"jobagent not found: {binary} (bin/jobagent is missing)"
    if TEST_PROFILE_MARKER in str(profile):
        return f"jobagent profile {profile} is a test profile"
    try:
        text = profile.read_text(encoding="utf-8")
    except OSError:
        return f"jobagent profile not found: {profile}"
    email = _login_email(text)
    if email is not None and email.casefold() == TEST_EMAIL.casefold():
        return f"jobagent profile {profile} uses the test account {TEST_EMAIL}"
    return None


def _log_tail(log_path: Path) -> str:
    try:
        data = log_path.read_bytes()
    except OSError:
        return ""
    return data.decode("utf-8", errors="replace").strip()[-LOG_TAIL:]


def _error(reason: str, log_path: Path, raw: str = "") -> ApplyResult:
    tail = _log_tail(log_path)
    return ApplyResult(result="error", detail=f"{reason}; log: {tail}" if tail else reason, raw=raw)


def _cost(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def read_result(result_file: Path, log_path: Path) -> ApplyResult:
    """Map jobagent's result file onto an ``ApplyResult``; read every field with defaults."""
    try:
        raw = result_file.read_text(encoding="utf-8")
    except OSError:
        return _error("jobagent wrote no result", log_path)
    try:
        data = json.loads(raw)
    except ValueError:
        return _error("jobagent result is not JSON", log_path, raw)
    if not isinstance(data, dict):
        return _error("jobagent result is not a JSON object", log_path, raw)
    status = data.get("status")
    if status not in STATUS_MAP:
        return _error(f"unknown jobagent status {status!r}", log_path, raw)
    notes = opt_str(data.get("notes"))
    if status == "already_applied":
        detail = ALREADY_APPLIED_DETAIL
    else:
        detail = (notes or f"jobagent: {status}")[:DETAIL_MAX]
    accounts = data.get("accounts_created")
    accounts = tuple(s for s in (opt_str(a) for a in accounts) if s) if isinstance(accounts, list) else ()
    return ApplyResult(
        result=STATUS_MAP[status],
        detail=detail,
        cost_usd=_cost(data.get("cost_usd")),
        raw=raw,
        accounts_created=accounts,
        company=opt_str(data.get("company")),
        role=opt_str(data.get("job_title")),
    )


async def _stop(proc: asyncio.subprocess.Process) -> None:
    """SIGTERM jobagent's process group, then SIGKILL it after ``KILL_GRACE_S``."""
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        await asyncio.wait_for(proc.wait(), KILL_GRACE_S)
    except TimeoutError:
        pass
    finally:  # even if cancelled again during the grace wait
        _kill_group(proc.pid)
    await proc.wait()


def _kill_group(pgid: int) -> None:
    """SIGKILL what is left of jobagent's process group (e.g. a browser that outlived it)."""
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        pass


async def _cancel_all(tasks: list[asyncio.Task]) -> None:
    for t in tasks:
        t.cancel()
    for t, outcome in zip(tasks, await asyncio.gather(*tasks, return_exceptions=True)):
        if isinstance(outcome, Exception):
            log.error("jobagent helper %s failed", t.get_name(), exc_info=outcome)


async def apply_with_jobagent(
    cfg: Config,
    posting: dict,
    *,
    reader: MailReader | None,
    relay: HumanRelay | None,
    run_dir: Path | None = None,
    spawn=asyncio.create_subprocess_exec,
) -> ApplyResult:
    """Run one jobagent application for ``posting`` and map its result.

    Cancellation stops jobagent's whole process group and re-raises. If ``preflight`` fails,
    jobagent is not started and the result is ``error`` with the reason.
    """
    refused = preflight(cfg)
    if refused is not None:
        return ApplyResult(result="error", detail=refused)
    if run_dir is None:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        run_dir = cfg.jobagent_live_dir / "argus" / f"{posting['id']}-{stamp}"
    human_dir, mail_dir = run_dir / "human", run_dir / "mail"
    human_dir.mkdir(parents=True, exist_ok=True)
    mail_dir.mkdir(parents=True, exist_ok=True)
    result_file, log_path = run_dir / "result.json", run_dir / "jobagent.log"

    cmd = [
        str(jobagent_bin(cfg)),
        "--profile", str(jobagent_profile(cfg)),
        "--human-dir", str(human_dir),
        "--human-timeout", str(cfg.jobs.human_timeout_minutes * 60),
        *(["--model", cfg.jobs.jobagent_model] if cfg.jobs.jobagent_model else []),
        "apply", posting["url"], "--mode", "auto", "--result-file", str(result_file),
    ]
    env = {**os.environ, "JOBAGENT_DATA_DIR": str(cfg.jobagent_live_dir), "JOBAGENT_MAIL_DIR": str(mail_dir)}
    after_id = await asyncio.to_thread(reader.max_id) if reader is not None else 0

    with open(log_path, "wb") as log_file:
        proc = await spawn(
            *cmd, stdin=subprocess.DEVNULL, stdout=log_file, stderr=subprocess.STDOUT, env=env,
            start_new_session=True,
        )
    tasks: list[asyncio.Task] = []
    if reader is not None:
        tasks.append(asyncio.create_task(run_bridge(reader, after_id, mail_dir), name="mail bridge"))
    if relay is not None:
        tasks.append(asyncio.create_task(relay.watch(human_dir, posting), name="human relay"))
    try:
        try:
            await proc.wait()
        except asyncio.CancelledError:
            await _stop(proc)
            raise
        _kill_group(proc.pid)
    finally:
        await _cancel_all(tasks)
    return read_result(result_file, log_path)
