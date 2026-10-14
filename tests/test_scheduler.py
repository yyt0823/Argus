# tests/test_scheduler.py
import sqlite3
from datetime import date, datetime
from argus.scheduler import day_key, seconds_until_rollover, backup

def test_day_key_boundary():
    assert day_key(datetime(2026, 10, 2, 3, 59)) == date(2026, 10, 1)
    assert day_key(datetime(2026, 10, 2, 4, 0)) == date(2026, 10, 2)
    assert day_key(datetime(2026, 10, 2, 0, 30)) == date(2026, 10, 1)

def test_seconds_until_rollover():
    assert seconds_until_rollover(datetime(2026, 10, 2, 3, 0)) == 3600
    assert seconds_until_rollover(datetime(2026, 10, 2, 4, 0)) == 86400
    assert seconds_until_rollover(datetime(2026, 10, 2, 23, 0)) == 5 * 3600

def test_backup_copies_and_prunes(tmp_path):
    db = tmp_path / "data" / "argus.db"; db.parent.mkdir()
    c = sqlite3.connect(db); c.execute("CREATE TABLE t (x)"); c.execute("INSERT INTO t VALUES (1)"); c.commit()
    mem = tmp_path / "memory"; mem.mkdir(); (mem / "a.md").write_text("hi")
    bdir = tmp_path / "backups"
    for d in range(1, 4):
        backup(db, mem, bdir, date(2026, 10, d), keep=2)
    c.close()
    assert sorted(p.name for p in bdir.iterdir()) == ["2026-10-02", "2026-10-03"]
    out = bdir / "2026-10-03"
    assert (out / "memory" / "a.md").read_text() == "hi"
    assert sqlite3.connect(out / "argus.db").execute("SELECT x FROM t").fetchall() == [(1,)]

def test_backup_same_day_overwrites_and_ignores_other_dirs(tmp_path):
    bdir = tmp_path / "backups"; (bdir / "keepme").mkdir(parents=True)
    mem = tmp_path / "memory"; mem.mkdir(); (mem / "a.md").write_text("1")
    backup(tmp_path / "missing.db", mem, bdir, date(2026, 10, 1), keep=1)
    (mem / "a.md").write_text("2")
    backup(tmp_path / "missing.db", mem, bdir, date(2026, 10, 1), keep=1)
    assert (bdir / "2026-10-01" / "memory" / "a.md").read_text() == "2"
    assert (bdir / "keepme").exists()
