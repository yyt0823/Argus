"""Entry point: wire config, store, channel, brain and app together and run until SIGTERM.

Run with ``python -m argus.main`` from the project root (launchd does this).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import logging.handlers
import os
import signal
import sqlite3
import subprocess
import sys
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path

from argus.app import App
from argus.brain import Brain
from argus.channel.imessage import IMessageChannel
from argus.config import Config, load_config
from argus.fitness.importer import import_markdown_logs
from argus.jobs.scheduler import JobScheduler
from argus.mail.reader import MailReader
from argus.mail.watcher import MailWatcher
from argus.scheduler import backup, day_key, seconds_until_rollover
from argus.sdk_session import make_session_factory
from argus.store import Store
from argus.tools.memory import MemoryStore

log = logging.getLogger("argus.main")

_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def setup_logging(logs_dir: Path) -> None:
    """Log INFO+ to ``logs/argus.log`` (rotated at midnight, 30 kept), and to stderr when it is a
    terminal. Under launchd stderr is the unrotated ``logs/launchd.err``, so it is skipped there."""
    logs_dir.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter(_LOG_FORMAT)
    file_handler = logging.handlers.TimedRotatingFileHandler(
        logs_dir / "argus.log", when="midnight", backupCount=30, encoding="utf-8"
    )
    handlers: list[logging.Handler] = [file_handler]
    if sys.stderr is not None and sys.stderr.isatty():
        handlers.append(logging.StreamHandler(sys.stderr))
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
        h.close()
    for h in handlers:
        h.setFormatter(formatter)
        root.addHandler(h)
    root.setLevel(logging.INFO)


def open_mail_reader(cfg: Config, factory: Callable[[], MailReader] | None = None) -> MailReader | None:
    """The shared MailReader, or None when mail is disabled or Mail's data cannot be read.

    Built once at startup and shared by the watcher and the chat tools. It is safe to call from
    worker threads: every query opens its own read-only SQLite connection and its caches are
    only ever replaced or extended. A failure never stops Argus; chat works without mail.
    """
    if not cfg.mail.enabled:
        return None
    try:
        return (factory or MailReader)()
    except Exception as e:
        log.warning("mail features disabled: cannot read the Mail app's data: %s", e)
        return None


def import_fitness_logs(cfg: Config, store: Store) -> None:
    """Import ``data/fitness/*.md`` logs into the Store once (idempotent); never raises."""
    fitness_dir = cfg.data_dir / "fitness"
    if not cfg.fitness.enabled or not fitness_dir.is_dir():
        return
    try:
        result = import_markdown_logs(store, fitness_dir)
    except Exception:
        log.exception("fitness log import failed; continuing without it")
        return
    log.info("fitness log import: %s", result)


def make_job_scheduler(cfg: Config, store: Store) -> JobScheduler | None:
    """The internship scheduler, or None when ``[jobs]`` is disabled. ``submit`` is set by
    ``build_app`` once the App exists."""
    if not cfg.jobs.enabled:
        return None
    if not cfg.jobs_internships_dir.is_dir():
        log.warning("jobs: internships directory %s not found; searches will fail until it exists",
                    cfg.jobs_internships_dir)
    return JobScheduler(cfg, store, submit=None)


def build_app(cfg: Config, mail_reader_factory: Callable[[], MailReader] | None = None) -> App:
    """Construct every component. Does not start Claude: sessions are created lazily on the first message.

    ``mail_reader_factory`` defaults to ``MailReader`` (the real ``~/Library/Mail``); the
    resulting watcher (or None) is stored as ``app.mail_watcher``.

    Raises ``FileNotFoundError`` / ``sqlite3.Error`` if chat.db cannot be read.
    """
    store = Store(cfg.db_path)
    try:
        import_fitness_logs(cfg, store)
        memory = MemoryStore(cfg.memory_dir)
        channel = IMessageChannel(cfg.chat_db, cfg.self_handles, store, mode=cfg.mode)
        reader = open_mail_reader(cfg, mail_reader_factory)
        scheduler = make_job_scheduler(cfg, store)
        # Backups are driven by backup_loop(), not by Brain's rollover hook, because the hook
        # only fires when a message arrives after the rollover hour.
        brain = Brain(
            store,
            make_session_factory(cfg, store, memory, mail_reader=reader, job_scheduler=scheduler),
            rollover_hour=cfg.rollover_hour,
            on_rollover=None,
        )
    except BaseException:
        store.close()
        raise
    app = App(
        channel,
        brain,
        store,
        poll_interval=cfg.poll_interval,
        slow_notice_seconds=cfg.slow_notice_seconds,
        on_poll_failure=notify,
    )
    if scheduler is not None:
        scheduler.submit = app.submit_internal  # the App exists only now
    app.job_scheduler = scheduler
    app.mail_watcher = (
        MailWatcher(reader, store, cfg.mail, submit=app.submit_internal, fitness=cfg.fitness.enabled,
                    jobs=scheduler is not None)
        if reader is not None
        else None
    )
    return app


async def backup_loop(cfg: Config) -> None:
    """Back up ``data/argus.db`` and ``memory/`` at every rollover hour, forever."""
    while True:
        delay = seconds_until_rollover(datetime.now(), cfg.rollover_hour)
        wake_at = datetime.now() + timedelta(seconds=delay)
        await asyncio.sleep(delay)
        # Label by the intended wake time so an early wakeup cannot pick the previous day.
        day = day_key(max(datetime.now(), wake_at), cfg.rollover_hour)
        try:
            target = await asyncio.to_thread(
                backup, cfg.db_path, cfg.memory_dir, cfg.backups_dir, day, cfg.backup_keep
            )
            log.info("backup written to %s", target)
        except Exception:
            log.exception("daily backup failed")


def notify(message: str) -> None:
    """Show a macOS notification; never raises."""
    escaped = message.replace("\\", "\\\\").replace('"', '\\"')
    try:
        subprocess.run(
            ["osascript", "-e", f'display notification "{escaped}" with title "Argus"'],
            capture_output=True,
            timeout=10,
        )
    except Exception:
        log.exception("could not post notification")


async def _serve(cfg: Config, app: App) -> int:
    """Run the app, the backup loop and (if enabled) the mail watcher and the job scheduler until
    SIGTERM/SIGINT.
    Returns the exit code."""
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    run_task = asyncio.create_task(app.run(), name="argus-app")
    backup_task = asyncio.create_task(backup_loop(cfg), name="argus-backup")
    stop_task = asyncio.create_task(stop.wait(), name="argus-stop")
    tasks = [run_task, backup_task, stop_task]
    watcher = getattr(app, "mail_watcher", None)
    if watcher is not None:
        # MailWatcher.run() logs and survives failed checks, so it only ends when cancelled.
        tasks.append(asyncio.create_task(watcher.run(), name="argus-mail"))
    scheduler = getattr(app, "job_scheduler", None)
    if scheduler is not None:
        # JobScheduler.run() logs and survives failed ticks; cancelling it also cancels a running
        # search or application.
        tasks.append(asyncio.create_task(scheduler.run(), name="argus-jobs"))
    log.info("Argus started (root=%s)", cfg.root)
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        # Cancelling App.run() starts its graceful shutdown: the reply in progress may finish (up to
        # app.SHUTDOWN_GRACE seconds; launchd's ExitTimeOut is 60) and unstarted messages stay
        # pending; its worker then closes the brain. gather() waits for all of that.
        for t in tasks:
            t.cancel()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.remove_signal_handler(sig)

    if stop.is_set():
        log.info("Argus stopped")
        return 0
    for t, result in zip(tasks, results):
        if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError):
            log.error("task %s crashed", t.get_name(), exc_info=result)
    log.error("Argus exited unexpectedly")
    return 1


def main() -> int:
    """Exit codes: 0 for a clean stop and for setup problems a restart cannot fix (bad config,
    unreadable chat.db), so launchd's ``KeepAlive {SuccessfulExit: false}`` does not loop on
    them; 1 for unexpected crashes, which launchd restarts."""
    root = Path(__file__).resolve().parent.parent
    try:
        cfg = load_config(root)
    except Exception as e:
        print(f"Argus: invalid configuration: {e}", file=sys.stderr)
        notify(f"Argus cannot start: {e}")
        return 0
    setup_logging(cfg.logs_dir)

    try:
        app = build_app(cfg)
    except (FileNotFoundError, PermissionError, sqlite3.Error) as e:
        real_python = os.path.realpath(sys.executable)
        log.error(
            "cannot read chat.db at %s: %s. Grant Full Disk Access to %s in System Settings "
            "-> Privacy & Security -> Full Disk Access (see README).",
            cfg.chat_db, e, real_python,
        )
        notify("Cannot read chat.db. Grant Full Disk Access to Python (see logs/argus.log).")
        return 0

    try:
        return asyncio.run(_serve(cfg, app))
    finally:
        with contextlib.suppress(Exception):
            app.store.close()


if __name__ == "__main__":
    sys.exit(main())
