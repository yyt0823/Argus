"""SQLite persistence for ``data/argus.db``: message archive, sessions, sent ids, and key/value state.

This module is the only writer of the database.
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Literal

from argus.channel.base import IncomingMessage

_SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    sender TEXT NOT NULL CHECK (sender IN ('user','argus')),
    content TEXT NOT NULL,
    session_id INTEGER
);
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    content, content='messages', content_rowid='id', tokenize='trigram'
);
CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, content) VALUES (new.id, new.content);
END;
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
"""

# The trigram tokenizer cannot match terms shorter than 3 characters.
_MIN_FTS_TERM = 3


def _ts(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _escape_like(term: str) -> str:
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class Store:
    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock, self._conn:
            self._conn.executescript(_SCHEMA)

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
        sender: Literal["user", "argus"],
        content: str,
        ts: datetime,
        session_id: int | None = None,
    ) -> int:
        cur = self._write(
            "INSERT INTO messages (ts, sender, content, session_id) VALUES (?, ?, ?, ?)",
            (_ts(ts), sender, content, session_id),
        )
        return cur.lastrowid

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
