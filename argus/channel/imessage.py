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
# Contact mode: address the owner's handle on the bot's iMessage account. Sending to a chat id can
# silently pick SMS (Text Message Forwarding) and send as the owner instead of the bot.
CONTACT_SEND_SCRIPT = (
    'on run argv\n'
    '  tell application "Messages"\n'
    '    set svc to 1st account whose service type = iMessage\n'
    '    send (item 1 of argv) to participant (item 2 of argv) of svc\n'
    '  end tell\n'
    'end run'
)

CHECKPOINT_KEY = "imessage_last_rowid"
REPLY_CHAT_KEY = "imessage_reply_chat"  # contact mode: chat guid of the owner handle that last wrote
MODES = ("self", "contact")
SEND_ATTEMPTS = 3
CAPTURE_TRIES = 12
CAPTURE_INTERVAL = 0.25
REMEMBER_NS = 60 * 1_000_000_000  # how long accepted rows stay eligible for pairing
SENT_TEXT_WINDOW = 60.0  # seconds a sent text is remembered for the self-reply loop guard
RESYNC_TOLERANCE_NS = 100_000_000  # re-synced copies keep the original date to within ~1 ms
RATE_WINDOW = 60.0  # seconds
RATE_LIMIT = 20  # max sends per RATE_WINDOW

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
    """Two modes, depending on which Apple ID the Mac's Messages app is signed into.

    - ``self``: the user's own Apple ID; the user texts themselves. Every message shows up twice
      (a sent row and an echo row), which are paired and delivered once.
    - ``contact``: a dedicated bot Apple ID; the owner texts the bot. ``self_handles`` are the
      owner's handles. The owner's messages are ``is_from_me = 0`` rows (one each); Argus's own
      replies are ``is_from_me = 1`` rows and are ignored.
    """

    def __init__(
        self,
        chat_db: Path,
        self_handles: Sequence[str],
        store: Store,
        *,
        runner: Callable[[list[str]], subprocess.CompletedProcess] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        pair_window_seconds: float = 10.0,
        clock: Callable[[], float] = time.monotonic,
        mode: str = "self",
    ) -> None:
        if mode not in MODES:
            raise ValueError(f"mode must be one of {MODES}, got {mode!r}")
        self.mode = mode
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
        self._clock = clock
        # Recently accepted rows, candidates for pairing with their sent/echo twin.
        self._recent: list[dict] = []
        # (monotonic time, full prefixed text) of recent sends: loop guard + circuit breaker.
        self._sent: list[tuple[float, str]] = []
        # Fail fast if chat.db is unreadable (e.g. missing Full Disk Access).
        with closing(self._connect()) as c:
            c.execute("SELECT max(ROWID) FROM message").fetchone()
        raw = self._store.get_state(CHECKPOINT_KEY)
        if raw is not None and self.mode == "self":
            self._rebuild_recent(int(raw))

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._uri, uri=True)

    def _max_rowid(self) -> int:
        with closing(self._connect()) as c:
            row = c.execute("SELECT max(ROWID) FROM message").fetchone()
        return row[0] or 0

    def _fetch_after(
        self, rowid: int, *, from_me_only: bool = False, upto: int | None = None, since_date: int | None = None
    ) -> list[tuple]:
        placeholders = ",".join("?" for _ in self.chat_guids)
        sql = (
            "SELECT m.ROWID, m.guid, m.text, m.attributedBody, m.is_from_me, m.date "
            "FROM message m JOIN chat_message_join j ON j.message_id = m.ROWID "
            "JOIN chat c ON c.ROWID = j.chat_id "
            f"WHERE c.guid IN ({placeholders}) AND m.ROWID > ? "
            "AND m.item_type = 0 AND m.associated_message_type = 0 "
        )
        params: list = [*self.chat_guids, rowid]
        if from_me_only:
            sql += "AND m.is_from_me = 1 "
        if upto is not None:
            sql += "AND m.ROWID <= ? "
            params.append(upto)
        if since_date is not None:
            sql += "AND m.date >= ? "
            params.append(since_date)
        sql += "ORDER BY m.ROWID"
        with closing(self._connect()) as c:
            return c.execute(sql, params).fetchall()

    def _newest_date_upto(self, rowid: int) -> int | None:
        placeholders = ",".join("?" for _ in self.chat_guids)
        sql = (
            "SELECT max(m.date) FROM message m JOIN chat_message_join j ON j.message_id = m.ROWID "
            "JOIN chat c ON c.ROWID = j.chat_id "
            f"WHERE c.guid IN ({placeholders}) AND m.ROWID <= ? "
            "AND m.item_type = 0 AND m.associated_message_type = 0"
        )
        with closing(self._connect()) as c:
            return c.execute(sql, (*self.chat_guids, rowid)).fetchone()[0]

    def _is_resync_copy(self, rowid: int, body: str, date: int | None) -> bool:
        """True if an earlier row in the same chats has this exact date and text.

        iCloud Messages sometimes re-downloads old messages as new rows (new ROWID and guid,
        and a ``date`` within a millisecond of the original). Nobody sends the same text twice
        within 100 ms, so a match with an earlier row means we already handled it.
        """
        if not date:
            return False
        placeholders = ",".join("?" for _ in self.chat_guids)
        sql = (
            "SELECT m.text, m.attributedBody FROM message m "
            "JOIN chat_message_join j ON j.message_id = m.ROWID JOIN chat c ON c.ROWID = j.chat_id "
            f"WHERE c.guid IN ({placeholders}) AND m.ROWID < ? AND m.date BETWEEN ? AND ? "
            "AND m.item_type = 0 AND m.associated_message_type = 0"
        )
        with closing(self._connect()) as c:
            rows = c.execute(
                sql, (*self.chat_guids, rowid, date - RESYNC_TOLERANCE_NS, date + RESYNC_TOLERANCE_NS)
            ).fetchall()
        return any(self._body(text, blob) == body for text, blob in rows)

    def _rebuild_recent(self, checkpoint: int) -> None:
        """After a restart, restore pairing memory from rows already consumed before the checkpoint.

        Without this, a restart between a message's sent row and its echo row would deliver
        the echo as a new message.
        """
        newest = self._newest_date_upto(checkpoint)
        if newest is None:
            return
        for _rowid, guid, text, blob, is_from_me, date in self._fetch_after(
            0, upto=checkpoint, since_date=newest - REMEMBER_NS
        ):
            body = self._body(text, blob)
            if self._is_filtered(guid, body):
                continue
            self._pair_or_remember(body, bool(is_from_me), date or 0)

    @staticmethod
    def _body(text: str | None, blob: bytes | None) -> str:
        body = text if text else decode_attributed_body(blob)
        return (body or "").rstrip()

    # --- polling ---

    def _is_filtered(self, guid: str, body: str) -> bool:
        return not body or body.startswith("🤖") or self._store.has_sent_id(guid)

    def _pair_or_remember(self, body: str, from_me: bool, date: int) -> bool:
        """Return True if this row is the twin of a remembered row; otherwise remember it."""
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
            return True
        self._recent.append({"body": body, "from_me": from_me, "date": date, "paired": False})
        return False

    def _prune_sent(self) -> None:
        cutoff = self._clock() - max(SENT_TEXT_WINDOW, RATE_WINDOW)
        self._sent = [(t, s) for t, s in self._sent if t > cutoff]

    def _recent_sent_texts(self) -> set[str]:
        self._prune_sent()
        cutoff = self._clock() - SENT_TEXT_WINDOW
        texts: set[str] = set()
        for t, full in self._sent:
            if t <= cutoff:
                continue
            for variant in (full, full.removeprefix("🤖"), full.removeprefix("🤖 ")):
                texts.add(variant.rstrip())
        return texts

    def poll(self) -> list[IncomingMessage]:
        raw = self._store.get_state(CHECKPOINT_KEY)
        if raw is None:
            # First run: start from the present, never answer history.
            self._store.set_state(CHECKPOINT_KEY, str(self._max_rowid()))
            return []
        checkpoint = int(raw)
        if self.mode == "contact":
            return self._poll_contact(checkpoint)
        rows = self._fetch_after(checkpoint)
        accepted: list[IncomingMessage] = []
        max_rowid = checkpoint
        sent_texts = self._recent_sent_texts() if rows else set()
        for rowid, guid, text, blob, is_from_me, date in rows:
            max_rowid = max(max_rowid, rowid)
            body = self._body(text, blob)
            if self._is_filtered(guid, body):
                continue
            if body in sent_texts:
                log.info("dropping row %s: matches a text Argus sent recently", guid)
                continue
            date = date or 0
            if self._pair_or_remember(body, bool(is_from_me), date):
                continue
            if self._is_resync_copy(rowid, body, date):
                log.info("dropping row %s: re-synced copy of an earlier message", guid)
                continue
            accepted.append(IncomingMessage(id=guid, text=body, received_at=apple_ns_to_local(date)))
        if max_rowid != checkpoint:
            # Queue + checkpoint in one transaction so a crash cannot lose accepted messages.
            self._store.accept_messages(accepted, CHECKPOINT_KEY, str(max_rowid))
        return accepted

    def _fetch_contact_rows(self, rowid: int) -> list[tuple]:
        """Contact mode: (ROWID, guid, text, attributedBody, is_from_me, date, chat guid) of every
        row after ``rowid`` in the owner chats. Our own sends are included so the checkpoint can
        move past them; the caller skips them."""
        placeholders = ",".join("?" for _ in self.chat_guids)
        sql = (
            "SELECT m.ROWID, m.guid, m.text, m.attributedBody, m.is_from_me, m.date, c.guid "
            "FROM message m JOIN chat_message_join j ON j.message_id = m.ROWID "
            "JOIN chat c ON c.ROWID = j.chat_id "
            f"WHERE c.guid IN ({placeholders}) AND m.ROWID > ? "
            "AND m.item_type = 0 AND m.associated_message_type = 0 "
            "ORDER BY m.ROWID"
        )
        with closing(self._connect()) as c:
            return c.execute(sql, (*self.chat_guids, rowid)).fetchall()

    def _poll_contact(self, checkpoint: int) -> list[IncomingMessage]:
        rows = self._fetch_contact_rows(checkpoint)
        accepted: list[IncomingMessage] = []
        max_rowid = checkpoint
        reply_chat: str | None = None
        for rowid, guid, text, blob, is_from_me, date, chat_guid in rows:
            max_rowid = max(max_rowid, rowid)
            if is_from_me:
                continue  # Argus's own reply
            body = self._body(text, blob)
            if not body or body.startswith("🤖"):
                continue
            if self._is_resync_copy(rowid, body, date):
                log.info("dropping row %s: re-synced copy of an earlier message", guid)
                continue
            accepted.append(IncomingMessage(id=guid, text=body, received_at=apple_ns_to_local(date or 0)))
            reply_chat = chat_guid
        if max_rowid != checkpoint:
            self._store.accept_messages(accepted, CHECKPOINT_KEY, str(max_rowid))
        if reply_chat is not None:
            self._store.set_state(REPLY_CHAT_KEY, reply_chat)
        return accepted

    def _reply_chat(self) -> str:
        """The chat to send to: in contact mode, the owner chat that most recently wrote."""
        if self.mode == "contact":
            saved = self._store.get_state(REPLY_CHAT_KEY)
            if saved in self.chat_guids:
                return saved
        return self.chat_guids[0]

    # --- sending ---

    def send(self, text: str) -> None:
        full = PREFIX + text
        self._prune_sent()
        now = self._clock()
        if sum(1 for t, _ in self._sent if t > now - RATE_WINDOW) >= RATE_LIMIT:
            log.error("circuit breaker: %d+ replies in %ds; refusing to send", RATE_LIMIT, int(RATE_WINDOW))
            raise SendError("rate limit: too many replies")
        self._sent.append((now, full))
        before: int | None = None
        if self.mode == "self":  # contact mode ignores is_from_me rows, so no capture is needed
            try:
                before = self._max_rowid()
            except sqlite3.Error:
                log.warning("could not read chat.db before send; skipping guid capture", exc_info=True)
        if self.mode == "contact":
            handle = self._reply_chat().split(";-;", 1)[-1]
            args = ["osascript", "-e", CONTACT_SEND_SCRIPT, full, handle]
        else:
            args = ["osascript", "-e", SEND_SCRIPT, full, self._reply_chat()]
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
