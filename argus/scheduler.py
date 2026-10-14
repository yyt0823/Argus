"""Day-boundary math and daily backups.

An "Argus day" runs from the rollover hour (04:00 local) to the next rollover,
so a message sent at 01:00 still belongs to the previous day's session.
"""

import re
import shutil
import sqlite3
from contextlib import closing
from datetime import date, datetime, timedelta
from pathlib import Path

_DAY_DIR = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def day_key(now: datetime, rollover_hour: int = 4) -> date:
    """Return the Argus day that `now` belongs to."""
    return (now - timedelta(hours=rollover_hour)).date()


def seconds_until_rollover(now: datetime, rollover_hour: int = 4) -> float:
    """Seconds from `now` to the next rollover; in (0, 86400]."""
    next_rollover = datetime.combine(
        day_key(now, rollover_hour) + timedelta(days=1), datetime.min.time()
    ) + timedelta(hours=rollover_hour)
    return (next_rollover - now).total_seconds()


def backup(
    db_path: Path, memory_dir: Path, backups_dir: Path, day: date, keep: int = 30
) -> Path:
    """Snapshot the database and memory folder into backups_dir/<day>/, then prune.

    The snapshot is built in a temporary sibling folder and swapped in, so a
    failure never leaves a half-written dated folder behind.
    """
    backups_dir.mkdir(parents=True, exist_ok=True)
    target = backups_dir / day.isoformat()
    staging = backups_dir / f".{day.isoformat()}.tmp"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir()

    if db_path.exists():
        # The sqlite backup API yields a consistent copy even while the db is open.
        src_uri = db_path.resolve().as_uri() + "?mode=ro"
        with closing(sqlite3.connect(src_uri, uri=True)) as src, closing(
            sqlite3.connect(staging / "argus.db")
        ) as dst:
            src.backup(dst)
    if memory_dir.exists():
        shutil.copytree(memory_dir, staging / "memory")

    if target.exists():
        shutil.rmtree(target)
    staging.rename(target)

    _prune(backups_dir, keep)
    return target


def _prune(backups_dir: Path, keep: int) -> None:
    """Delete the oldest YYYY-MM-DD folders so at most `keep` remain."""
    dated = sorted(
        p for p in backups_dir.iterdir() if p.is_dir() and _DAY_DIR.match(p.name)
    )
    for old in dated[: max(len(dated) - keep, 0)]:
        shutil.rmtree(old)
