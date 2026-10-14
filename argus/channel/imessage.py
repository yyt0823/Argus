"""iMessage channel: polls the Messages chat.db (read-only) and sends via AppleScript."""

from __future__ import annotations

import logging
import sqlite3
import subprocess
import time
from collections.abc import Callable, Sequence
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

from argus.channel.base import Channel, IncomingMessage, SendError
from argus.store import Store

log = logging.getLogger(__name__)

PREFIX = "🤖 "
SEND_SCRIPT = (
    'on run argv\n'
    '  tell application "Messages" to send (item 1 of argv) to chat id (item 2 of argv)\n'
    'end run'
)

CHECKPOINT_KEY = "imessage_last_rowid"
SEND_ATTEMPTS = 3
CAPTURE_TRIES = 12
CAPTURE_INTERVAL = 0.25
REMEMBER_NS = 60 * 1_000_000_000  # how long accepted rows stay eligible for pairing

_APPLE_EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)


def decode_attributed_body(blob: bytes | None) -> str | None:
    """Extract the plain string from a typedstream NSAttributedString blob."""
    if not blob:
        return None
    b = bytes(blob)
    i = b.find(b"NSString")
    if i < 0:
        i = b.find(b"NSMutableString")
        if i < 0:
            return None
    j = b.find(b"\x01\x2b", i)
    if j < 0 or j + 2 >= len(b):
        return None
    p = j + 2
    tag = b[p]
    if tag == 0x81:
        n = int.from_bytes(b[p + 1:p + 3], "little"); p += 3
    elif tag == 0x82:
        n = int.from_bytes(b[p + 1:p + 5], "little"); p += 5
    else:
        n = tag; p += 1
    return b[p:p + n].decode("utf-8", errors="replace")


def apple_ns_to_local(ns: int) -> datetime:
    """Convert chat.db `message.date` (ns since 2001-01-01 UTC) to a local, naive datetime."""
    dt = _APPLE_EPOCH + timedelta(microseconds=ns // 1000)
    return dt.astimezone().replace(tzinfo=None)


def _default_runner(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, timeout=30)


class IMessageChannel(Channel):
    def __init__(
        self,
        chat_db: Path,
        self_handles: Sequence[str],
        store: Store,
        *,
        runner: Callable[[list[str]], subprocess.CompletedProcess] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        pair_window_seconds: float = 10.0,
    ) -> None:
        if not self_handles:
            raise ValueError("self_handles must not be empty")
        self._chat_db = Path(chat_db)
        if not self._chat_db.is_file():
            raise FileNotFoundError(f"chat.db not found: {self._chat_db}")
        self._uri = self._chat_db.resolve().as_uri() + "?mode=ro"
        self.chat_guids = ["iMessage;-;" + h for h in self_handles]
        self._store = store
        self._runner = runner or _default_runner
        self._sleep = sleep
        self._pair_window_ns = int(pair_window_seconds * 1_000_000_000)
        # Recently accepted rows, candidates for pairing with their sent/echo twin.
        self._recent: list[dict] = []
        # Fail fast if chat.db is unreadable (e.g. missing Full Disk Access).
        with closing(self._connect()) as c:
            c.execute("SELECT max(ROWID) FROM message").fetchone()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._uri, uri=True)

    def _max_rowid(self) -> int:
        with closing(self._connect()) as c:
            row = c.execute("SELECT max(ROWID) FROM message").fetchone()
        return row[0] or 0

    def _fetch_after(self, rowid: int, *, from_me_only: bool = False) -> list[tuple]:
        placeholders = ",".join("?" for _ in self.chat_guids)
        sql = (
            "SELECT m.ROWID, m.guid, m.text, m.attributedBody, m.is_from_me, m.date "
            "FROM message m JOIN chat_message_join j ON j.message_id = m.ROWID "
            "JOIN chat c ON c.ROWID = j.chat_id "
            f"WHERE c.guid IN ({placeholders}) AND m.ROWID > ? "
            "AND m.item_type = 0 AND m.associated_message_type = 0 "
        )
        if from_me_only:
            sql += "AND m.is_from_me = 1 "
        sql += "ORDER BY m.ROWID"
        with closing(self._connect()) as c:
            return c.execute(sql, (*self.chat_guids, rowid)).fetchall()

    @staticmethod
    def _body(text: str | None, blob: bytes | None) -> str:
        body = text if text else decode_attributed_body(blob)
        return (body or "").rstrip()

    # --- polling ---

    def poll(self) -> list[IncomingMessage]:
        raw = self._store.get_state(CHECKPOINT_KEY)
        if raw is None:
            # First run: start from the present, never answer history.
            self._store.set_state(CHECKPOINT_KEY, str(self._max_rowid()))
            return []
        checkpoint = int(raw)
        rows = self._fetch_after(checkpoint)
        accepted: list[IncomingMessage] = []
        max_rowid = checkpoint
        for rowid, guid, text, blob, is_from_me, date in rows:
            max_rowid = max(max_rowid, rowid)
            body = self._body(text, blob)
            if not body or body.startswith("🤖") or self._store.has_sent_id(guid):
                continue
            date = date or 0
            from_me = bool(is_from_me)
            self._recent = [r for r in self._recent if date - r["date"] <= REMEMBER_NS]
            twin = next(
                (
                    r for r in self._recent
                    if not r["paired"]
                    and r["body"] == body
                    and r["from_me"] != from_me
                    and abs(r["date"] - date) <= self._pair_window_ns
                ),
                None,
            )
            if twin is not None:
                twin["paired"] = True
                continue
            accepted.append(IncomingMessage(id=guid, text=body, received_at=apple_ns_to_local(date)))
            self._recent.append({"body": body, "from_me": from_me, "date": date, "paired": False})
        if max_rowid != checkpoint:
            self._store.set_state(CHECKPOINT_KEY, str(max_rowid))
        return accepted

    # --- sending ---

    def send(self, text: str) -> None:
        full = PREFIX + text
        try:
            before: int | None = self._max_rowid()
        except sqlite3.Error:
            log.warning("could not read chat.db before send; skipping guid capture", exc_info=True)
            before = None
        args = ["osascript", "-e", SEND_SCRIPT, full, self.chat_guids[0]]
        reason = "unknown error"
        for attempt in range(1, SEND_ATTEMPTS + 1):
            try:
                result = self._runner(args)
            except (subprocess.TimeoutExpired, OSError) as e:
                reason = f"{type(e).__name__}: {e}"
            else:
                if result.returncode == 0:
                    break
                reason = (result.stderr or "").strip() or f"osascript exited {result.returncode}"
            log.warning("iMessage send attempt %d/%d failed: %s", attempt, SEND_ATTEMPTS, reason)
            if attempt < SEND_ATTEMPTS:
                self._sleep(1)
        else:
            raise SendError(reason)
        if before is not None:
            self._capture_sent_guid(before, full)

    def _capture_sent_guid(self, before: int, full: str) -> None:
        """Best effort: record the guid of the row our send produced."""
        target = full.rstrip()
        try:
            for attempt in range(CAPTURE_TRIES):
                for _rowid, guid, text, blob, _from_me, _date in self._fetch_after(before, from_me_only=True):
                    if self._body(text, blob) == target:
                        self._store.add_sent_id(guid, datetime.now())
                        return
                if attempt < CAPTURE_TRIES - 1:
                    self._sleep(CAPTURE_INTERVAL)
        except sqlite3.Error:
            log.warning("sent-guid capture failed", exc_info=True)
            return
        log.info("sent-guid capture found no matching row")
