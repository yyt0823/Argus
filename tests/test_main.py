import asyncio
from datetime import datetime

import pytest

import argus.main as main_mod
from argus.app import App
from argus.config import Config
from argus.config import MailConfig
from argus.mail.reader import MailReader, MailReaderError
from argus.mail.watcher import MailWatcher
from argus.main import backup_loop, build_app
from argus.scheduler import day_key
from tests.mail_fixtures import build_fake_mail


@pytest.fixture(autouse=True)
def no_real_mail(monkeypatch):
    """Never open the real ~/Library/Mail from tests: the default reader always fails here."""
    def refuse(*args, **kwargs):
        raise MailReaderError("real Mail data is off-limits in tests")

    monkeypatch.setattr(main_mod, "MailReader", refuse)


def test_build_app_wires_everything(tmp_path, fake_chat_db):
    cfg = Config(root=tmp_path, self_handles=("+15550000000",), chat_db=fake_chat_db.path)
    app = build_app(cfg)
    try:
        assert isinstance(app, App)
        assert cfg.db_path.exists() and cfg.memory_dir.exists()
    finally:
        app.store.close()


def test_build_app_raises_when_chat_db_missing(tmp_path):
    cfg = Config(root=tmp_path, self_handles=("+15550000000",), chat_db=tmp_path / "nope.db")
    with pytest.raises(FileNotFoundError):
        build_app(cfg)


async def test_backup_loop_takes_a_backup_at_rollover(tmp_path, monkeypatch):
    cfg = Config(root=tmp_path, self_handles=("+15550000000",), chat_db=tmp_path / "chat.db")
    cfg.memory_dir.mkdir()
    (cfg.memory_dir / "MEMORY.md").write_text("- [x](x.md) — test\n")

    delays = iter([0.0])
    monkeypatch.setattr(main_mod, "seconds_until_rollover", lambda now, hour: next(delays, 3600.0))

    task = asyncio.create_task(backup_loop(cfg))
    target = cfg.backups_dir / day_key(datetime.now(), cfg.rollover_hour).isoformat()
    try:
        for _ in range(200):
            if (target / "memory" / "MEMORY.md").exists():
                break
            await asyncio.sleep(0.01)
        assert (target / "memory" / "MEMORY.md").exists()
        assert not task.done()  # keeps waiting for the next rollover
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_backup_loop_survives_backup_errors(tmp_path, monkeypatch):
    cfg = Config(root=tmp_path, self_handles=("+15550000000",), chat_db=tmp_path / "chat.db")
    calls = []

    def failing_backup(*args):
        calls.append(args)
        raise OSError("disk full")

    delays = iter([0.0, 0.0])
    monkeypatch.setattr(main_mod, "seconds_until_rollover", lambda now, hour: next(delays, 3600.0))
    monkeypatch.setattr(main_mod, "backup", failing_backup)

    task = asyncio.create_task(backup_loop(cfg))
    try:
        for _ in range(200):
            if len(calls) >= 2:
                break
            await asyncio.sleep(0.01)
        assert len(calls) == 2
        assert not task.done()
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


def _stream_handlers():
    import logging
    return [h for h in logging.getLogger().handlers
            if type(h) is logging.StreamHandler]


def test_setup_logging_skips_stderr_when_not_a_tty(tmp_path, monkeypatch):
    import io
    import logging
    fake = io.StringIO()
    monkeypatch.setattr(main_mod.sys, "stderr", fake)
    saved = list(logging.getLogger().handlers)
    try:
        main_mod.setup_logging(tmp_path / "logs")
        assert _stream_handlers() == []
        assert (tmp_path / "logs" / "argus.log").exists()
    finally:
        for h in list(logging.getLogger().handlers):
            logging.getLogger().removeHandler(h); h.close()
        for h in saved:
            logging.getLogger().addHandler(h)


def test_setup_logging_adds_stderr_on_a_tty(tmp_path, monkeypatch):
    import io
    import logging

    class Tty(io.StringIO):
        def isatty(self): return True

    monkeypatch.setattr(main_mod.sys, "stderr", Tty())
    saved = list(logging.getLogger().handlers)
    try:
        main_mod.setup_logging(tmp_path / "logs")
        assert len(_stream_handlers()) == 1
    finally:
        for h in list(logging.getLogger().handlers):
            logging.getLogger().removeHandler(h); h.close()
        for h in saved:
            logging.getLogger().addHandler(h)


def test_main_config_error_notifies_and_exits_zero(monkeypatch):
    notes = []
    monkeypatch.setattr(main_mod, "load_config", lambda root: (_ for _ in ()).throw(ValueError("bad")))
    monkeypatch.setattr(main_mod, "notify", notes.append)
    assert main_mod.main() == 0
    assert len(notes) == 1


def test_main_chat_db_error_notifies_and_exits_zero(tmp_path, monkeypatch):
    notes = []
    cfg = Config(root=tmp_path, self_handles=("+15550000000",), chat_db=tmp_path / "nope.db")
    monkeypatch.setattr(main_mod, "load_config", lambda root: cfg)
    monkeypatch.setattr(main_mod, "setup_logging", lambda d: None)
    monkeypatch.setattr(main_mod, "notify", notes.append)
    assert main_mod.main() == 0
    assert len(notes) == 1


def test_main_unexpected_crash_exits_one(tmp_path, monkeypatch, fake_chat_db):
    cfg = Config(root=tmp_path, self_handles=("+15550000000",), chat_db=fake_chat_db.path)
    monkeypatch.setattr(main_mod, "load_config", lambda root: cfg)
    monkeypatch.setattr(main_mod, "setup_logging", lambda d: None)
    monkeypatch.setattr(main_mod, "notify", lambda m: None)

    async def boom(cfg, app):
        return 1

    monkeypatch.setattr(main_mod, "_serve", boom)
    assert main_mod.main() == 1


def test_build_app_wires_poll_failure_notification(tmp_path, fake_chat_db):
    cfg = Config(root=tmp_path, self_handles=("+15550000000",), chat_db=fake_chat_db.path)
    app = build_app(cfg)
    try:
        assert app.on_poll_failure is main_mod.notify
    finally:
        app.store.close()


@pytest.mark.parametrize("mode", ["self", "contact"])
def test_build_app_passes_mode_to_channel(tmp_path, fake_chat_db, mode):
    cfg = Config(root=tmp_path, self_handles=("+15550000000",), chat_db=fake_chat_db.path, mode=mode)
    app = build_app(cfg)
    try:
        assert app.channel.mode == mode
    finally:
        app.store.close()


# --- mail wiring ---


def _record_session_factory(monkeypatch):
    calls = []
    real = main_mod.make_session_factory

    def recording(cfg, store, memory, **kwargs):
        calls.append(kwargs)
        return real(cfg, store, memory, **kwargs)

    monkeypatch.setattr(main_mod, "make_session_factory", recording)
    return calls


def test_build_app_wires_mail_reader_and_watcher(tmp_path, fake_chat_db, monkeypatch):
    fm = build_fake_mail(tmp_path)
    calls = _record_session_factory(monkeypatch)
    cfg = Config(root=tmp_path, self_handles=("+15550000000",), chat_db=fake_chat_db.path)
    app = build_app(cfg, mail_reader_factory=lambda: MailReader(fm.mail_dir, fm.accounts_db))
    try:
        reader = calls[0]["mail_reader"]
        assert isinstance(reader, MailReader)
        assert isinstance(app.mail_watcher, MailWatcher)
        assert app.mail_watcher._reader is reader
        assert app.mail_watcher._store is app.store
        assert app.mail_watcher._cfg is cfg.mail
        assert app.mail_watcher._submit == app.submit_internal
    finally:
        app.store.close()


def test_build_app_without_mail_when_disabled(tmp_path, fake_chat_db, monkeypatch):
    calls = _record_session_factory(monkeypatch)
    built = []
    cfg = Config(root=tmp_path, self_handles=("+15550000000",), chat_db=fake_chat_db.path,
                 mail=MailConfig(enabled=False))
    app = build_app(cfg, mail_reader_factory=lambda: built.append(1))
    try:
        assert built == []
        assert app.mail_watcher is None
        assert calls[0]["mail_reader"] is None
    finally:
        app.store.close()


def test_build_app_survives_mail_reader_failure(tmp_path, fake_chat_db, monkeypatch, caplog):
    calls = _record_session_factory(monkeypatch)
    cfg = Config(root=tmp_path, self_handles=("+15550000000",), chat_db=fake_chat_db.path)
    # Default factory: the autouse fixture makes MailReader raise MailReaderError.
    app = build_app(cfg)
    try:
        assert isinstance(app, App)
        assert app.mail_watcher is None
        assert calls[0]["mail_reader"] is None
        assert "mail" in caplog.text.lower()
    finally:
        app.store.close()


def test_build_app_survives_unexpected_mail_reader_error(tmp_path, fake_chat_db):
    cfg = Config(root=tmp_path, self_handles=("+15550000000",), chat_db=fake_chat_db.path)

    def boom():
        raise PermissionError("no access")

    app = build_app(cfg, mail_reader_factory=boom)
    try:
        assert app.mail_watcher is None
    finally:
        app.store.close()


class _FakeWatcher:
    def __init__(self):
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.cancelled = False

    async def run(self):
        self.started.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise


class _IdleApp:
    def __init__(self, watcher):
        self.mail_watcher = watcher

    async def run(self):
        await asyncio.Event().wait()


async def test_serve_runs_and_cancels_mail_watcher(tmp_path, monkeypatch):
    import os
    import signal

    async def idle_backup(cfg):
        await asyncio.Event().wait()

    monkeypatch.setattr(main_mod, "backup_loop", idle_backup)
    cfg = Config(root=tmp_path, self_handles=("+15550000000",), chat_db=tmp_path / "chat.db")
    watcher = _FakeWatcher()
    serve = asyncio.create_task(main_mod._serve(cfg, _IdleApp(watcher)))
    await asyncio.wait_for(watcher.started.wait(), 5)
    os.kill(os.getpid(), signal.SIGTERM)
    assert await asyncio.wait_for(serve, 5) == 0
    assert watcher.cancelled


async def test_serve_without_mail_watcher(tmp_path, monkeypatch):
    import os
    import signal

    async def idle_backup(cfg):
        await asyncio.Event().wait()

    monkeypatch.setattr(main_mod, "backup_loop", idle_backup)
    cfg = Config(root=tmp_path, self_handles=("+15550000000",), chat_db=tmp_path / "chat.db")
    serve = asyncio.create_task(main_mod._serve(cfg, _IdleApp(None)))
    await asyncio.sleep(0.05)
    os.kill(os.getpid(), signal.SIGTERM)
    assert await asyncio.wait_for(serve, 5) == 0


# --- fitness ---

FOOD_LOG_MD = """# 饮食记录

| 日期 | 训练 | 早餐 | 晚餐 | 全天热量 | 蛋白质 | 备注 |
|---|---|---|---|---|---|---|
| 2026-10-10 | A | 燕麦 | 虾 | 约 1500 千卡 | 150 克 | |
| 2026-10-11 | | 鸡蛋 | 牛肉 | 1400 | 140 | 休息 |
"""


def _fitness_root(tmp_path):
    d = tmp_path / "data" / "fitness"
    d.mkdir(parents=True)
    (d / "food-log.md").write_text(FOOD_LOG_MD, encoding="utf-8")
    return d


def test_build_app_imports_fitness_logs_once(tmp_path, fake_chat_db, caplog):
    from datetime import date
    _fitness_root(tmp_path)
    cfg = Config(root=tmp_path, self_handles=("+15550000000",), chat_db=fake_chat_db.path)
    caplog.set_level("INFO", logger="argus.main")
    app = build_app(cfg)
    try:
        rows = app.store.foods_between(date(2026, 10, 1), date(2026, 10, 31))
        assert [(r["day"], r["kcal"], r["protein_g"]) for r in rows] == [
            ("2026-10-10", 1500, 150), ("2026-10-11", 1400, 140)]
        assert "food_imported" in caplog.text
    finally:
        app.store.close()
    app = build_app(cfg)  # restart: idempotent
    try:
        assert len(app.store.foods_between(date(2026, 10, 1), date(2026, 10, 31))) == 2
    finally:
        app.store.close()


def test_build_app_skips_fitness_import_when_disabled(tmp_path, fake_chat_db):
    from datetime import date
    from argus.config import FitnessConfig
    _fitness_root(tmp_path)
    cfg = Config(root=tmp_path, self_handles=("+15550000000",), chat_db=fake_chat_db.path,
                 fitness=FitnessConfig(enabled=False))
    app = build_app(cfg)
    try:
        assert app.store.foods_between(date(2026, 10, 1), date(2026, 10, 31)) == []
    finally:
        app.store.close()


def test_build_app_survives_fitness_import_error(tmp_path, fake_chat_db, monkeypatch, caplog):
    _fitness_root(tmp_path)
    def boom(store, d):
        raise RuntimeError("bad markdown")
    monkeypatch.setattr(main_mod, "import_markdown_logs", boom)
    cfg = Config(root=tmp_path, self_handles=("+15550000000",), chat_db=fake_chat_db.path)
    app = build_app(cfg)
    try:
        assert "fitness log import failed" in caplog.text
    finally:
        app.store.close()


def test_build_app_without_fitness_dir_does_not_import(tmp_path, fake_chat_db, monkeypatch):
    called = []
    monkeypatch.setattr(main_mod, "import_markdown_logs", lambda *a: called.append(a))
    cfg = Config(root=tmp_path, self_handles=("+15550000000",), chat_db=fake_chat_db.path)
    app = build_app(cfg)
    app.store.close()
    assert called == []


def test_build_app_passes_fitness_flag_to_watcher(tmp_path, fake_chat_db):
    from argus.config import FitnessConfig
    fm = build_fake_mail(tmp_path)
    for enabled in (True, False):
        cfg = Config(root=tmp_path, self_handles=("+15550000000",), chat_db=fake_chat_db.path,
                     fitness=FitnessConfig(enabled=enabled))
        app = build_app(cfg, mail_reader_factory=lambda: MailReader(fm.mail_dir, fm.accounts_db))
        try:
            assert app.mail_watcher._fitness is enabled
        finally:
            app.store.close()


# --- jobs wiring ---


def test_build_app_wires_job_scheduler(tmp_path, fake_chat_db, monkeypatch):
    from argus.jobs.scheduler import JobScheduler
    calls = _record_session_factory(monkeypatch)
    cfg = Config(root=tmp_path, self_handles=("+15550000000",), chat_db=fake_chat_db.path)
    app = build_app(cfg)
    try:
        sched = app.job_scheduler
        assert isinstance(sched, JobScheduler)
        assert calls[0]["job_scheduler"] is sched
        assert sched.submit == app.submit_internal
        assert sched._store is app.store and sched._cfg is cfg
    finally:
        app.store.close()


def test_build_app_without_jobs_when_disabled(tmp_path, fake_chat_db, monkeypatch):
    from argus.config import JobsConfig
    calls = _record_session_factory(monkeypatch)
    cfg = Config(root=tmp_path, self_handles=("+15550000000",), chat_db=fake_chat_db.path,
                 jobs=JobsConfig(enabled=False))
    app = build_app(cfg)
    try:
        assert app.job_scheduler is None
        assert calls[0]["job_scheduler"] is None
    finally:
        app.store.close()


def test_build_app_briefing_includes_jobs_when_enabled(tmp_path, fake_chat_db):
    from argus.config import JobsConfig
    fm = build_fake_mail(tmp_path)
    for enabled in (True, False):
        cfg = Config(root=tmp_path, self_handles=("+15550000000",), chat_db=fake_chat_db.path,
                     jobs=JobsConfig(enabled=enabled))
        app = build_app(cfg, mail_reader_factory=lambda: MailReader(fm.mail_dir, fm.accounts_db))
        try:
            assert app.mail_watcher._jobs is enabled
        finally:
            app.store.close()


class _JobsApp(_IdleApp):
    def __init__(self, scheduler):
        super().__init__(None)
        self.job_scheduler = scheduler


async def test_serve_runs_and_cancels_job_scheduler(tmp_path, monkeypatch):
    import os
    import signal

    async def idle_backup(cfg):
        await asyncio.Event().wait()

    monkeypatch.setattr(main_mod, "backup_loop", idle_backup)
    cfg = Config(root=tmp_path, self_handles=("+15550000000",), chat_db=tmp_path / "chat.db")
    sched = _FakeWatcher()  # same run()/cancel contract
    serve = asyncio.create_task(main_mod._serve(cfg, _JobsApp(sched)))
    await asyncio.wait_for(sched.started.wait(), 5)
    os.kill(os.getpid(), signal.SIGTERM)
    assert await asyncio.wait_for(serve, 5) == 0
    assert sched.cancelled
