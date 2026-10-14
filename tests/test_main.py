import asyncio
from datetime import datetime

import pytest

import argus.main as main_mod
from argus.app import App
from argus.config import Config
from argus.main import backup_loop, build_app
from argus.scheduler import day_key


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
