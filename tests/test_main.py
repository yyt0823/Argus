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
