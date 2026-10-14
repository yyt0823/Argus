"""SQLite persistence for ``data/argus.db``: message archive, sessions, sent ids, key/value state,
mail triage verdicts, fitness logs (food, weight, workouts), and job postings/runs.

This module is the only writer of the database.
"""

from __future__ import annotations

import math
import sqlite3
import threading
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Literal

from argus.channel.base import IncomingMessage

# ``{name}`` is ``messages`` normally and ``messages_new`` while migrating an older schema.
_MESSAGES_TABLE = """
CREATE TABLE IF NOT EXISTS {name} (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    sender TEXT NOT NULL CHECK (sender IN ('user','argus','system')),
    content TEXT NOT NULL,
    session_id INTEGER
);
"""
_MESSAGES_TRIGGER = """
CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, content) VALUES (new.id, new.content);
END;
"""

_SCHEMA = _MESSAGES_TABLE.format(name="messages") + """
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    content, content='messages', content_rowid='id', tokenize='trigram'
);
""" + _MESSAGES_TRIGGER + """
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY,
    day TEXT NOT NULL UNIQUE,
    sdk_session_id TEXT,
    started_at TEXT NOT NULL,
    ended_at TEXT
);
CREATE TABLE IF NOT EXISTS sent_ids (guid TEXT PRIMARY KEY, sent_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS pending (guid TEXT PRIMARY KEY, text TEXT NOT NULL, at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS mail_triage (
    mail_id INTEGER PRIMARY KEY,
    account TEXT,
    sender TEXT,
    subject TEXT,
    received_at TEXT NOT NULL,
    important INTEGER,
    category TEXT,
    summary TEXT,
    action TEXT,
    deadline TEXT,
    status TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    alerted_at TEXT,
    briefed_at TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS mail_triage_received ON mail_triage (received_at);
CREATE TABLE IF NOT EXISTS food_log (
    id INTEGER PRIMARY KEY,
    day TEXT NOT NULL,
    logged_at TEXT NOT NULL,
    meal TEXT,
    description TEXT NOT NULL,
    kcal REAL NOT NULL,
    protein_g REAL NOT NULL,
    fat_g REAL NOT NULL,
    carbs_g REAL NOT NULL,
    added_sugar_g REAL,
    sodium TEXT,
    note TEXT,
    deleted INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS food_log_day ON food_log (day);
CREATE TABLE IF NOT EXISTS weight_log (
    id INTEGER PRIMARY KEY,
    day TEXT NOT NULL,
    logged_at TEXT NOT NULL,
    weight_kg REAL NOT NULL,
    waist_cm REAL,
    note TEXT
);
CREATE INDEX IF NOT EXISTS weight_log_day ON weight_log (day);
CREATE TABLE IF NOT EXISTS workout_log (
    id INTEGER PRIMARY KEY,
    day TEXT NOT NULL,
    logged_at TEXT NOT NULL,
    kind TEXT NOT NULL,
    description TEXT,
    note TEXT
);
CREATE INDEX IF NOT EXISTS workout_log_day ON workout_log (day);
CREATE TABLE IF NOT EXISTS job_postings (
    id INTEGER PRIMARY KEY,
    company TEXT,
    role TEXT,
    city TEXT,
    term TEXT,
    deadline TEXT,
    fit INTEGER,
    notes TEXT,
    url TEXT NOT NULL UNIQUE,
    verified INTEGER,
    ats TEXT,
    first_seen_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'new',
    status_detail TEXT,
    applied_at TEXT,
    follow_up TEXT
);
CREATE INDEX IF NOT EXISTS job_postings_status ON job_postings (status);
CREATE TABLE IF NOT EXISTS job_runs (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('scout','apply')),
    started_at TEXT NOT NULL,
    finished_at TEXT,
    ok INTEGER,
    summary TEXT,
    cost_usd REAL
);
"""

# Rebuilds ``messages`` (keeping ids) with the current sender CHECK; run inside one transaction.
_MIGRATE_MESSAGES = (
    _MESSAGES_TABLE.format(name="messages_new")
    + """
INSERT INTO messages_new (id, ts, sender, content, session_id)
    SELECT id, ts, sender, content, session_id FROM messages;
DROP TABLE messages;
ALTER TABLE messages_new RENAME TO messages;
"""
    + _MESSAGES_TRIGGER
    + """
INSERT INTO messages_fts(messages_fts) VALUES ('rebuild');
"""
)

_TRIAGE_FIELDS = (
    "mail_id", "account", "sender", "subject", "received_at", "important", "category",
    "summary", "action", "deadline", "status", "attempts",
)

MEALS = ("breakfast", "lunch", "dinner", "snack", "pre_workout", "other")
SODIUM_LEVELS = ("low", "medium", "high")
WORKOUT_KINDS = ("A", "B", "cardio", "other")
IMPORTED_PREFIX = "[imported]"

# Upper bounds for a single food entry; larger values are almost certainly estimation mistakes.
_FOOD_MAX = {"kcal": 5000, "protein_g": 300, "fat_g": 500, "carbs_g": 1000, "added_sugar_g": 500}
_FOOD_UPDATABLE = (
    "day", "meal", "description", "kcal", "protein_g", "fat_g", "carbs_g", "added_sugar_g", "sodium", "note",
)
_FOOD_COLUMNS = (
    "id, day, logged_at, meal, description, kcal, protein_g, fat_g, carbs_g, added_sugar_g, sodium, note, deleted"
)

JOB_STATUSES = (
    "new", "reported", "queued", "applying", "submitted", "needs_login", "needs_attestation", "needs_user", "skipped", "error",
)
JOB_RUN_KINDS = ("scout", "apply")
_POSTING_FIELDS = ("company", "role", "city", "term", "deadline", "fit", "notes", "verified", "ats")

# The trigram tokenizer cannot match terms shorter than 3 characters.
_MIN_FTS_TERM = 3


def _ts(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _check_amount(name: str, value: object, maximum: float | None = None, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number, got {value!r}")
    if value < 0 or (positive and value == 0):
        raise ValueError(f"{name} must be {'> 0' if positive else '>= 0'}, got {value!r}")
    if maximum is not None and value > maximum:
        raise ValueError(f"{name} must be <= {maximum:g}, got {value!r}")
    return float(value)


def _check_choice(name: str, value: object, choices: tuple[str, ...], nullable: bool = True) -> str | None:
    if value is None and nullable:
        return None
    if value not in choices:
        raise ValueError(f"{name} must be one of {', '.join(choices)}, got {value!r}")
    return value


def _check_text(name: str, value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string, got {value!r}")
    return value.strip()


def _check_note(value: object) -> str | None:
    if value is not None and not isinstance(value, str):
        raise ValueError(f"note must be a string, got {value!r}")
    return value


def _check_food_field(name: str, value: object) -> object:
    if name == "day":
        if not isinstance(value, date) or isinstance(value, datetime):
            raise ValueError(f"day must be a date, got {value!r}")
        return value.isoformat()
    if name == "meal":
        return _check_choice("meal", value, MEALS)
    if name == "sodium":
        return _check_choice("sodium", value, SODIUM_LEVELS)
    if name == "description":
        return _check_text("description", value)
    if name == "note":
        return _check_note(value)
    if name == "added_sugar_g" and value is None:
        return None
    return _check_amount(name, value, _FOOD_MAX[name])


def _escape_like(term: str) -> str:
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class Store:
    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        # WAL lets readers (query_data, backups) run alongside writes; busy_timeout makes a write
        # that meets another connection's lock wait instead of failing with "database is locked".
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute("PRAGMA journal_mode=WAL")
        with self._lock, self._conn:
            self._conn.executescript(_SCHEMA)
        self._migrate_messages()

    def _migrate_messages(self) -> None:
        """Allow sender 'system' in a database created before it existed."""
        with self._lock:
            row = self._conn.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'messages'"
            ).fetchone()
            if "'system'" in row["sql"]:
                return
            # executescript() does not manage transactions, so wrap the rebuild explicitly.
            try:
                self._conn.executescript("BEGIN IMMEDIATE;\n" + _MIGRATE_MESSAGES + "\nCOMMIT;")
            except BaseException:
                if self._conn.in_transaction:
                    self._conn.rollback()
                raise

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _write(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock, self._conn:
            return self._conn.execute(sql, params)

    def _read(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    # --- messages ---

    def add_message(
        self,
        sender: Literal["user", "argus", "system"],
        content: str,
        ts: datetime,
        session_id: int | None = None,
    ) -> int:
        cur = self._write(
            "INSERT INTO messages (ts, sender, content, session_id) VALUES (?, ?, ?, ?)",
            (_ts(ts), sender, content, session_id),
        )
        return cur.lastrowid

    def has_message(self, sender: str, content: str, ts: datetime) -> bool:
        """True if a message with exactly this sender, content and timestamp is archived."""
        rows = self._read(
            "SELECT 1 FROM messages WHERE sender = ? AND ts = ? AND content = ? LIMIT 1",
            (sender, _ts(ts), content),
        )
        return bool(rows)

    def search_messages(
        self,
        query: str,
        start: date | None = None,
        end: date | None = None,
        limit: int = 20,
    ) -> list[dict]:
        """Find messages containing every whitespace-separated term; newest first.

        ``start``/``end`` are inclusive calendar dates of the message timestamp.
        """
        terms = query.split()
        if not terms:
            return []

        where: list[str] = []
        params: list = []
        if all(len(t) >= _MIN_FTS_TERM for t in terms):
            source = "messages_fts JOIN messages m ON m.id = messages_fts.rowid"
            where.append("messages_fts MATCH ?")
            params.append(" ".join('"' + t.replace('"', '""') + '"' for t in terms))
        else:
            source = "messages m"
            for t in terms:
                where.append("m.content LIKE ? ESCAPE '\\'")
                params.append(f"%{_escape_like(t)}%")
        if start is not None:
            where.append("m.ts >= ?")
            params.append(start.isoformat())
        if end is not None:
            where.append("m.ts < ?")
            params.append((end + timedelta(days=1)).isoformat())

        sql = (
            f"SELECT m.id, m.ts, m.sender, m.content FROM {source} "
            f"WHERE {' AND '.join(where)} ORDER BY m.ts DESC, m.id DESC LIMIT ?"
        )
        params.append(limit)
        return [dict(r) for r in self._read(sql, tuple(params))]

    # --- sessions ---

    def get_session(self, day: date) -> dict | None:
        rows = self._read(
            "SELECT id, day, sdk_session_id, started_at, ended_at FROM sessions WHERE day = ?",
            (day.isoformat(),),
        )
        return dict(rows[0]) if rows else None

    def create_session(self, day: date, started_at: datetime) -> int:
        cur = self._write(
            "INSERT INTO sessions (day, started_at) VALUES (?, ?)",
            (day.isoformat(), _ts(started_at)),
        )
        return cur.lastrowid

    def set_session_sdk_id(self, session_id: int, sdk_session_id: str) -> None:
        self._write("UPDATE sessions SET sdk_session_id = ? WHERE id = ?", (sdk_session_id, session_id))

    def end_session(self, session_id: int, ended_at: datetime) -> None:
        self._write("UPDATE sessions SET ended_at = ? WHERE id = ?", (_ts(ended_at), session_id))

    # --- sent ids (guids of messages Argus sent, to avoid answering its own echoes) ---

    def add_sent_id(self, guid: str, sent_at: datetime) -> None:
        self._write("INSERT OR IGNORE INTO sent_ids (guid, sent_at) VALUES (?, ?)", (guid, _ts(sent_at)))

    def has_sent_id(self, guid: str) -> bool:
        return bool(self._read("SELECT 1 FROM sent_ids WHERE guid = ?", (guid,)))

    # --- key/value state ---

    def get_state(self, key: str, default: str | None = None) -> str | None:
        rows = self._read("SELECT value FROM state WHERE key = ?", (key,))
        return rows[0]["value"] if rows else default

    def set_state(self, key: str, value: str) -> None:
        self._write(
            "INSERT INTO state (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    # --- pending queue (accepted incoming messages not yet fully handled) ---

    def accept_messages(
        self, messages: list[IncomingMessage], checkpoint_key: str, checkpoint_value: str
    ) -> None:
        """Atomically queue ``messages`` and advance the channel checkpoint."""
        with self._lock, self._conn:
            self._conn.executemany(
                "INSERT OR IGNORE INTO pending (guid, text, at) VALUES (?, ?, ?)",
                [(m.id, m.text, _ts(m.received_at)) for m in messages],
            )
            self._conn.execute(
                "INSERT INTO state (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (checkpoint_key, checkpoint_value),
            )

    def list_pending(self) -> list[IncomingMessage]:
        rows = self._read("SELECT guid, text, at FROM pending ORDER BY at, rowid")
        return [
            IncomingMessage(id=r["guid"], text=r["text"], received_at=datetime.fromisoformat(r["at"]))
            for r in rows
        ]

    def delete_pending(self, guid: str) -> None:
        self._write("DELETE FROM pending WHERE guid = ?", (guid,))

    # --- mail triage (one row per Mail message id) ---

    def upsert_triage(self, row: dict) -> None:
        """Insert or replace the verdict for ``row["mail_id"]``.

        ``alerted_at``, ``briefed_at`` and ``created_at`` of an existing row are kept.
        """
        values = {k: row[k] for k in _TRIAGE_FIELDS}
        values["received_at"] = _ts(values["received_at"])
        if values["important"] is not None:
            values["important"] = int(bool(values["important"]))
        values["created_at"] = _ts(datetime.now())
        cols = ", ".join(values)
        updates = ", ".join(f"{k} = excluded.{k}" for k in _TRIAGE_FIELDS if k != "mail_id")
        self._write(
            f"INSERT INTO mail_triage ({cols}) VALUES ({', '.join('?' * len(values))}) "
            f"ON CONFLICT(mail_id) DO UPDATE SET {updates}",
            tuple(values.values()),
        )

    def get_triage(self, mail_id: int) -> dict | None:
        rows = self._read("SELECT * FROM mail_triage WHERE mail_id = ?", (mail_id,))
        return _triage_dict(rows[0]) if rows else None

    def triage_between(self, start: datetime, end: datetime, important_only: bool = False) -> list[dict]:
        """Rows with ``start <= received_at < end``, oldest first."""
        sql = "SELECT * FROM mail_triage WHERE received_at >= ? AND received_at < ?"
        if important_only:
            sql += " AND important = 1"
        rows = self._read(sql + " ORDER BY received_at, mail_id", (_ts(start), _ts(end)))
        return [_triage_dict(r) for r in rows]

    def failed_triage(self, max_attempts: int = 3) -> list[dict]:
        """Failed rows that may be retried, oldest first."""
        rows = self._read(
            "SELECT * FROM mail_triage WHERE status = 'failed' AND attempts < ? "
            "ORDER BY received_at, mail_id",
            (max_attempts,),
        )
        return [_triage_dict(r) for r in rows]

    def mark_alerted(self, mail_ids: list[int], at: datetime) -> None:
        self._mark("alerted_at", mail_ids, at)

    def mark_briefed(self, mail_ids: list[int], at: datetime) -> None:
        self._mark("briefed_at", mail_ids, at)

    def _mark(self, column: str, mail_ids: list[int], at: datetime) -> None:
        with self._lock, self._conn:
            self._conn.executemany(
                f"UPDATE mail_triage SET {column} = ? WHERE mail_id = ?",
                [(_ts(at), i) for i in mail_ids],
            )

    # --- fitness: food log (soft delete), weights, workouts ---

    def add_food(
        self,
        day: date,
        logged_at: datetime,
        description: str,
        kcal: float,
        protein_g: float,
        fat_g: float,
        carbs_g: float,
        added_sugar_g: float | None = None,
        sodium: str | None = None,
        meal: str | None = None,
        note: str | None = None,
    ) -> int:
        """Record one food entry for Argus day ``day``; raise ``ValueError`` on invalid values."""
        values = {
            name: _check_food_field(name, value)
            for name, value in (
                ("day", day), ("meal", meal), ("description", description), ("kcal", kcal),
                ("protein_g", protein_g), ("fat_g", fat_g), ("carbs_g", carbs_g),
                ("added_sugar_g", added_sugar_g), ("sodium", sodium), ("note", note),
            )
        }
        values["logged_at"] = _ts(logged_at)
        cur = self._write(
            f"INSERT INTO food_log ({', '.join(values)}) VALUES ({', '.join('?' * len(values))})",
            tuple(values.values()),
        )
        return cur.lastrowid

    def update_food(self, id: int, **fields) -> dict:
        """Change fields of a live entry and return the updated row.

        Raise ``ValueError`` for unknown/invalid fields, ``KeyError`` if the entry is missing or deleted.
        """
        if not fields:
            raise ValueError("no fields to update")
        unknown = sorted(set(fields) - set(_FOOD_UPDATABLE))
        if unknown:
            raise ValueError(f"cannot update field(s): {', '.join(unknown)}")
        values = {name: _check_food_field(name, value) for name, value in fields.items()}
        assignments = ", ".join(f"{name} = ?" for name in values)
        with self._lock, self._conn:
            cur = self._conn.execute(
                f"UPDATE food_log SET {assignments} WHERE id = ? AND deleted = 0",
                (*values.values(), id),
            )
            if cur.rowcount == 0:
                raise KeyError(f"food entry {id} not found")
            row = self._conn.execute(f"SELECT {_FOOD_COLUMNS} FROM food_log WHERE id = ?", (id,)).fetchone()
        return dict(row)

    def delete_food(self, id: int) -> dict:
        """Soft-delete a live entry and return it; ``KeyError`` if missing or already deleted."""
        with self._lock, self._conn:
            cur = self._conn.execute("UPDATE food_log SET deleted = 1 WHERE id = ? AND deleted = 0", (id,))
            if cur.rowcount == 0:
                raise KeyError(f"food entry {id} not found")
            row = self._conn.execute(f"SELECT {_FOOD_COLUMNS} FROM food_log WHERE id = ?", (id,)).fetchone()
        return dict(row)

    def foods_for_day(self, day: date) -> list[dict]:
        return self.foods_between(day, day)

    def foods_between(self, start: date, end: date) -> list[dict]:
        """Live entries with ``start <= day <= end``, ordered by day, logged_at, id."""
        rows = self._read(
            f"SELECT {_FOOD_COLUMNS} FROM food_log WHERE deleted = 0 AND day >= ? AND day <= ? "
            "ORDER BY day, logged_at, id",
            (start.isoformat(), end.isoformat()),
        )
        return [dict(r) for r in rows]

    def add_weight(
        self,
        day: date,
        logged_at: datetime,
        weight_kg: float,
        waist_cm: float | None = None,
        note: str | None = None,
    ) -> int:
        weight = _check_amount("weight_kg", weight_kg, 500, positive=True)
        waist = None if waist_cm is None else _check_amount("waist_cm", waist_cm, 300, positive=True)
        cur = self._write(
            "INSERT INTO weight_log (day, logged_at, weight_kg, waist_cm, note) VALUES (?, ?, ?, ?, ?)",
            (_check_food_field("day", day), _ts(logged_at), weight, waist, _check_note(note)),
        )
        return cur.lastrowid

    def weights_between(self, start: date, end: date) -> list[dict]:
        """Entries with ``start <= day <= end``, ordered by day, logged_at, id."""
        rows = self._read(
            "SELECT id, day, logged_at, weight_kg, waist_cm, note FROM weight_log "
            "WHERE day >= ? AND day <= ? ORDER BY day, logged_at, id",
            (start.isoformat(), end.isoformat()),
        )
        return [dict(r) for r in rows]

    def add_workout(
        self,
        day: date,
        logged_at: datetime,
        kind: str,
        description: str | None = None,
        note: str | None = None,
    ) -> int:
        _check_choice("kind", kind, WORKOUT_KINDS, nullable=False)
        cur = self._write(
            "INSERT INTO workout_log (day, logged_at, kind, description, note) VALUES (?, ?, ?, ?, ?)",
            (_check_food_field("day", day), _ts(logged_at), kind, _check_note(description), _check_note(note)),
        )
        return cur.lastrowid

    def workouts_between(self, start: date, end: date) -> list[dict]:
        """Entries with ``start <= day <= end``, ordered by day, logged_at, id."""
        rows = self._read(
            "SELECT id, day, logged_at, kind, description, note FROM workout_log "
            "WHERE day >= ? AND day <= ? ORDER BY day, logged_at, id",
            (start.isoformat(), end.isoformat()),
        )
        return [dict(r) for r in rows]

    def imported_days(self, kind: Literal["food", "weight"]) -> set[date]:
        """Days that already have a row imported from the Markdown logs (deleted food rows included)."""
        table = {"food": "food_log", "weight": "weight_log"}.get(kind)
        if table is None:
            raise ValueError(f"kind must be 'food' or 'weight', got {kind!r}")
        rows = self._read(
            f"SELECT DISTINCT day FROM {table} WHERE substr(note, 1, ?) = ?",
            (len(IMPORTED_PREFIX), IMPORTED_PREFIX),
        )
        return {date.fromisoformat(r["day"]) for r in rows}

    # --- jobs: postings (one row per URL) and scout/apply runs ---

    def upsert_posting(self, posting: dict) -> int:
        """Insert or update the posting with ``posting["url"]``; return its id.

        Missing fields are stored as NULL. On update, ``first_seen_at`` is kept and ``status``
        is kept unless ``posting`` has a ``status``. Raise ``ValueError`` on invalid values.
        """
        url = _check_text("url", posting.get("url"))
        values: dict = {}
        for name in _POSTING_FIELDS:
            value = posting.get(name)
            if name == "fit" and value is not None:
                if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 5:
                    raise ValueError(f"fit must be an integer 1-5, got {value!r}")
            elif name == "verified" and value is not None:
                value = int(bool(value))
            elif value is not None and name != "fit":
                value = str(value)
            values[name] = value
        status = posting.get("status")
        if status is not None:
            _check_choice("status", status, JOB_STATUSES, nullable=False)
        updates = [f"{k} = excluded.{k}" for k in values]
        if status is not None:
            updates.append("status = excluded.status")
        cols = {"url": url, **values, "first_seen_at": _ts(datetime.now()), "status": status or "new"}
        with self._lock, self._conn:
            self._conn.execute(
                f"INSERT INTO job_postings ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))}) "
                f"ON CONFLICT(url) DO UPDATE SET {', '.join(updates)}",
                tuple(cols.values()),
            )
            return self._conn.execute("SELECT id FROM job_postings WHERE url = ?", (url,)).fetchone()["id"]

    def get_posting(self, id: int) -> dict | None:
        rows = self._read("SELECT * FROM job_postings WHERE id = ?", (id,))
        return _posting_dict(rows[0]) if rows else None

    def list_postings(
        self,
        status: str | None = None,
        min_fit: int | None = None,
        since: datetime | None = None,
        limit: int = 50,
    ) -> list[dict]:
        """Postings, best fit first then newest; ``since`` filters on ``first_seen_at``."""
        where, params = ["1 = 1"], []
        if status is not None:
            _check_choice("status", status, JOB_STATUSES, nullable=False)
            where.append("status = ?")
            params.append(status)
        if min_fit is not None:
            where.append("fit >= ?")
            params.append(min_fit)
        if since is not None:
            where.append("first_seen_at >= ?")
            params.append(_ts(since))
        rows = self._read(
            f"SELECT * FROM job_postings WHERE {' AND '.join(where)} "
            "ORDER BY fit IS NULL, fit DESC, first_seen_at DESC, id DESC LIMIT ?",
            (*params, limit),
        )
        return [_posting_dict(r) for r in rows]

    def set_posting_status(
        self,
        id: int,
        status: str,
        detail: str | None = None,
        applied_at: datetime | None = None,
        follow_up: str | None = None,
    ) -> None:
        """Set ``status`` and ``status_detail``; ``applied_at``/``follow_up`` are only changed when given.

        Raise ``ValueError`` for an unknown status, ``KeyError`` if the posting is missing.
        """
        _check_choice("status", status, JOB_STATUSES, nullable=False)
        sets, params = ["status = ?", "status_detail = ?"], [status, detail]
        if applied_at is not None:
            sets.append("applied_at = ?")
            params.append(_ts(applied_at))
        if follow_up is not None:
            sets.append("follow_up = ?")
            params.append(follow_up)
        cur = self._write(f"UPDATE job_postings SET {', '.join(sets)} WHERE id = ?", (*params, id))
        if cur.rowcount == 0:
            raise KeyError(f"job posting {id} not found")

    def count_applied_on(self, day: date) -> int:
        """Postings whose ``applied_at`` falls on calendar day ``day`` (any resulting status)."""
        rows = self._read(
            "SELECT COUNT(*) AS n FROM job_postings WHERE applied_at >= ? AND applied_at < ?",
            (day.isoformat(), (day + timedelta(days=1)).isoformat()),
        )
        return rows[0]["n"]

    def add_job_run(self, kind: str, started_at: datetime) -> int:
        _check_choice("kind", kind, JOB_RUN_KINDS, nullable=False)
        cur = self._write("INSERT INTO job_runs (kind, started_at) VALUES (?, ?)", (kind, _ts(started_at)))
        return cur.lastrowid

    def finish_job_run(
        self, id: int, finished_at: datetime, ok: bool, summary: str | None, cost_usd: float | None
    ) -> None:
        self._write(
            "UPDATE job_runs SET finished_at = ?, ok = ?, summary = ?, cost_usd = ? WHERE id = ?",
            (_ts(finished_at), int(bool(ok)), summary, cost_usd, id),
        )

    def get_job_run(self, id: int) -> dict | None:
        rows = self._read("SELECT * FROM job_runs WHERE id = ?", (id,))
        if not rows:
            return None
        d = dict(rows[0])
        if d["ok"] is not None:
            d["ok"] = bool(d["ok"])
        return d


def _posting_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    if d["verified"] is not None:
        d["verified"] = bool(d["verified"])
    return d


def _triage_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    if d["important"] is not None:
        d["important"] = bool(d["important"])
    return d
