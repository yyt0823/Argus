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
from datetime import datetime, timedelta
from pathlib import Path

from argus.app import App
from argus.brain import Brain
from argus.channel.imessage import IMessageChannel
from argus.config import Config, load_config
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


def build_app(cfg: Config) -> App:
    """Construct every component. Does not start Claude: sessions are created lazily on the first message.

    Raises ``FileNotFoundError`` / ``sqlite3.Error`` if chat.db cannot be read.
    """
    store = Store(cfg.db_path)
    try:
        memory = MemoryStore(cfg.memory_dir)
        channel = IMessageChannel(cfg.chat_db, cfg.self_handles, store)
        # Backups are driven by backup_loop(), not by Brain's rollover hook, because the hook
        # only fires when a message arrives after the rollover hour.
        brain = Brain(
            store,
            make_session_factory(cfg, store, memory),
            rollover_hour=cfg.rollover_hour,
            on_rollover=None,
        )
    except BaseException:
        store.close()
        raise
    return App(
        channel,
        brain,
        store,
        poll_interval=cfg.poll_interval,
        slow_notice_seconds=cfg.slow_notice_seconds,
        on_poll_failure=notify,
    )


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
    """Run the app and the backup loop until SIGTERM/SIGINT. Returns the exit code."""
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    run_task = asyncio.create_task(app.run(), name="argus-app")
    backup_task = asyncio.create_task(backup_loop(cfg), name="argus-backup")
    stop_task = asyncio.create_task(stop.wait(), name="argus-stop")
    tasks = (run_task, backup_task, stop_task)
    log.info("Argus started (root=%s)", cfg.root)
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        # App.run() closes the brain from its worker when cancelled.
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
