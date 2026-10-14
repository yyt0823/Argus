"""Read-only access to the macOS Mail app's local data (``~/Library/Mail/V10``).

This is the only module that knows Mail's on-disk formats:

- ``MailData/Envelope Index``: SQLite index (``messages``, ``mailboxes``, ``subjects``,
  ``addresses``, ``summaries``, ``labels``). Opened with ``?mode=ro``; never written.
- ``<ACCOUNT-UUID>/<Name>.mbox/<STORE-UUID>/Data/[<digit>/…]/Messages/<ROWID>[.partial].emlx``:
  a byte count line, that many bytes of RFC 822 message, then an XML plist.
- Mailbox URLs: ``imap://<ACCOUNT-UUID>/INBOX``, ``ews://<ACCOUNT-UUID>/Inbox``,
  ``imap://<UUID>/%5BGmail%5D/All%20Mail`` (Gmail inbox membership is a ``labels`` row).
- Account labels come from ``~/Library/Accounts/Accounts4.sqlite`` (``ZACCOUNT``); Mail's IMAP
  account is often a child row without a description, so the parent row is consulted too.
"""

from __future__ import annotations

import email
import email.policy
import logging
import os
import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from email.message import Message
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlparse

log = logging.getLogger(__name__)

INBOX_NAMES = {"inbox"}
# Mailboxes never searched (matched against any percent-decoded path segment, lower-cased).
EXCLUDED_NAMES = {
    "junk", "junk e-mail", "junk email", "spam", "bulk mail",
    "trash", "bin", "deleted messages", "deleted items",
}
TRUNCATED_MARK = "\n[…truncated]"

_SUMMARY_SQL = (
    "SELECT m.ROWID, mb.url, a.address, a.comment, m.subject_prefix, s.subject, m.date_received, "
    "sm.summary, m.read "
    "FROM messages m JOIN mailboxes mb ON mb.ROWID = m.mailbox "
    "LEFT JOIN addresses a ON a.ROWID = m.sender "
    "LEFT JOIN subjects s ON s.ROWID = m.subject "
    "LEFT JOIN summaries sm ON sm.ROWID = m.summary "
)


@dataclass(frozen=True)
class MailSummary:
    id: int                  # Envelope Index messages.ROWID
    account: str             # human label for the account
    sender: str              # "Name <addr>" or "addr"
    subject: str             # subject_prefix + subject text
    received_at: datetime    # local naive, from date_received (unix seconds)
    snippet: str             # summaries.summary text, "" if none
    read: bool


class MailReaderError(Exception):
    """Mail's local data is missing, unreadable, or in an unexpected format."""


class MailNotFound(MailReaderError):
    """The message row or its .emlx file does not exist."""


def _escape_like(term: str) -> str:
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _url_parts(url: str) -> tuple[str, list[str]]:
    """(account uuid, percent-decoded path segments) of a mailbox URL."""
    u = urlparse(url)
    path = unquote(u.path).strip("/")
    return u.netloc, [p for p in path.split("/") if p]


def _is_inbox(url: str) -> bool:
    _, parts = _url_parts(url)
    return len(parts) == 1 and parts[0].lower() in INBOX_NAMES


def _is_excluded(url: str) -> bool:
    _, parts = _url_parts(url)
    return any(p.lower() in EXCLUDED_NAMES for p in parts)


def _day_start_ts(d: date) -> int:
    return int(datetime(d.year, d.month, d.day).timestamp())


class _HTMLText(HTMLParser):
    """Minimal HTML → text: drops script/style/head content, breaks lines at block elements."""

    _SKIP = {"script", "style", "head", "title", "noscript", "template"}
    _BLOCK = {"p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6",
              "table", "ul", "ol", "blockquote", "hr", "section", "article", "header", "footer"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._out: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._skip += 1
        elif tag in self._BLOCK:
            self._out.append("\n")

    def handle_startendtag(self, tag, attrs):
        if tag in self._BLOCK:
            self._out.append("\n")

    def handle_endtag(self, tag):
        if tag in self._SKIP:
            self._skip = max(0, self._skip - 1)
        elif tag in self._BLOCK:
            self._out.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self._out.append(data)

    def text(self) -> str:
        raw = "".join(self._out).replace("\xa0", " ")
        lines = [re.sub(r"[ \t\r\f\v]+", " ", ln).strip() for ln in raw.split("\n")]
        return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def html_to_text(html: str) -> str:
    p = _HTMLText()
    try:
        p.feed(html)
        p.close()
    except Exception:  # malformed markup: fall back to crude tag stripping
        return unescape(re.sub(r"<[^>]+>", " ", html)).strip()
    return p.text()


def _part_text(part: Message) -> str:
    try:
        content = part.get_content()
        if isinstance(content, str):
            return content
    except Exception:
        pass
    payload = part.get_payload(decode=True) or b""
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except LookupError:
        return payload.decode("utf-8", errors="replace")


def _body_text(msg: Message) -> str:
    plain = html = None
    for part in msg.walk():
        if part.is_multipart() or part.get_content_disposition() == "attachment":
            continue
        ctype = part.get_content_type()
        if ctype == "text/plain" and plain is None:
            plain = _part_text(part)
        elif ctype == "text/html" and html is None:
            html = _part_text(part)
    if plain is not None and plain.strip():
        return plain.strip()
    if html is not None:
        return html_to_text(html)
    return (plain or "").strip()


def _header(msg: Message, name: str) -> str:
    try:
        value = msg.get(name)
    except Exception:
        value = None
    return " ".join(str(value).split()) if value is not None else ""


class MailReader:
    def __init__(
        self,
        mail_dir: Path = Path.home() / "Library/Mail/V10",
        accounts_db: Path | None = Path.home() / "Library/Accounts/Accounts4.sqlite",
    ) -> None:
        self._mail_dir = Path(mail_dir)
        self._accounts_db = Path(accounts_db) if accounts_db is not None else None
        index = self._mail_dir / "MailData" / "Envelope Index"
        if not index.is_file():
            raise MailReaderError(f"Mail index not found: {index}")
        self._uri = index.resolve().as_uri() + "?mode=ro"
        self._labels: dict[str, str] = {}      # account uuid -> label (only real lookups are cached)
        self._paths: dict[int, Path] = {}      # message id -> .emlx path
        # Fail fast if the index is unreadable (permissions, not a database, format change).
        self._query("SELECT max(ROWID) FROM messages")

    # --- sqlite helpers ---

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._uri, uri=True)

    def _query(self, sql: str, params: tuple | list = ()) -> list[tuple]:
        try:
            with closing(self._connect()) as c:
                return c.execute(sql, params).fetchall()
        except sqlite3.Error as e:
            raise MailReaderError(f"cannot read Mail index: {e}") from e

    def _mailboxes(self) -> list[tuple[int, str]]:
        return self._query("SELECT ROWID, url FROM mailboxes")

    # --- accounts ---

    def _load_labels(self) -> None:
        if self._accounts_db is None or not self._accounts_db.is_file():
            return
        uri = self._accounts_db.resolve().as_uri() + "?mode=ro"
        try:
            with closing(sqlite3.connect(uri, uri=True)) as c:
                rows = c.execute(
                    "SELECT a.ZIDENTIFIER, a.ZACCOUNTDESCRIPTION, a.ZUSERNAME, "
                    "p.ZACCOUNTDESCRIPTION, p.ZUSERNAME "
                    "FROM ZACCOUNT a LEFT JOIN ZACCOUNT p ON p.Z_PK = a.ZPARENTACCOUNT"
                ).fetchall()
        except sqlite3.Error:
            log.warning("cannot read accounts DB; using account ids as labels", exc_info=True)
            return
        for ident, desc, user, pdesc, puser in rows:
            label = next((v for v in (desc, user, pdesc, puser) if v and str(v).strip()), None)
            if ident and label:
                self._labels[ident] = str(label).strip()

    def _label(self, uuid: str) -> str:
        if uuid not in self._labels:
            self._load_labels()
        return self._labels.get(uuid) or uuid[:8]

    def accounts(self) -> list[str]:
        """Labels of accounts that have an inbox mailbox."""
        uuids = {_url_parts(url)[0] for _, url in self._mailboxes() if _is_inbox(url)}
        return sorted({self._label(u) for u in uuids})

    # --- listing ---

    def _summary(self, row: tuple) -> MailSummary:
        rowid, url, address, comment, prefix, subject, received, snippet, read = row
        address = address or ""
        sender = f"{comment} <{address}>" if comment and address else (address or comment or "")
        prefix, subject = prefix or "", subject or ""
        if prefix and subject and not prefix.endswith(" "):
            prefix += " "
        return MailSummary(
            id=rowid,
            account=self._label(_url_parts(url)[0]),
            sender=sender,
            subject=prefix + subject,
            received_at=datetime.fromtimestamp(received or 0),
            snippet=(snippet or "").strip(),
            read=bool(read),
        )

    def _inbox_filter(self) -> tuple[str, list[int]]:
        ids = [rid for rid, url in self._mailboxes() if _is_inbox(url)]
        marks = ",".join("?" * len(ids)) or "NULL"
        sql = (
            f"(m.mailbox IN ({marks}) OR EXISTS (SELECT 1 FROM labels l "
            f"WHERE l.message_id = m.ROWID AND l.mailbox_id IN ({marks})))"
        )
        return sql, ids + ids

    def max_id(self) -> int:
        return self._query("SELECT max(ROWID) FROM messages")[0][0] or 0

    def inbox_since(self, after_id: int, limit: int = 200) -> list[MailSummary]:
        """Inbox messages (any account) with ROWID > after_id, oldest first."""
        cond, params = self._inbox_filter()
        rows = self._query(
            _SUMMARY_SQL + f"WHERE m.ROWID > ? AND m.deleted = 0 AND {cond} ORDER BY m.ROWID LIMIT ?",
            [after_id, *params, limit],
        )
        return [self._summary(r) for r in rows]

    def last_received_at(self) -> datetime | None:
        cond, params = self._inbox_filter()
        ts = self._query(f"SELECT max(m.date_received) FROM messages m WHERE m.deleted = 0 AND {cond}", params)[0][0]
        return datetime.fromtimestamp(ts) if ts is not None else None

    def search(
        self,
        text: str | None = None,
        sender: str | None = None,
        account: str | None = None,
        start: date | None = None,
        end: date | None = None,
        limit: int = 20,
    ) -> list[MailSummary]:
        """Newest first. Excludes deleted rows and Junk/Spam/Trash/Deleted mailboxes."""
        boxes = self._mailboxes()
        excluded = [rid for rid, url in boxes if _is_excluded(url)]
        where = ["m.deleted = 0"]
        params: list = []
        if excluded:
            marks = ",".join("?" * len(excluded))
            where.append(
                f"m.mailbox NOT IN ({marks}) AND NOT EXISTS (SELECT 1 FROM labels l "
                f"WHERE l.message_id = m.ROWID AND l.mailbox_id IN ({marks}))"
            )
            params += excluded + excluded
        if text:
            like = f"%{_escape_like(text)}%"
            where.append("(s.subject LIKE ? ESCAPE '\\' OR m.subject_prefix LIKE ? ESCAPE '\\' "
                         "OR sm.summary LIKE ? ESCAPE '\\')")
            params += [like, like, like]
        if sender:
            like = f"%{_escape_like(sender)}%"
            where.append("(a.address LIKE ? ESCAPE '\\' OR a.comment LIKE ? ESCAPE '\\')")
            params += [like, like]
        if account:
            needle = account.casefold()
            ids = [rid for rid, url in boxes if needle in self._label(_url_parts(url)[0]).casefold()]
            if not ids:
                return []
            where.append(f"m.mailbox IN ({','.join('?' * len(ids))})")
            params += ids
        if start:
            where.append("m.date_received >= ?")
            params.append(_day_start_ts(start))
        if end:
            where.append("m.date_received < ?")
            params.append(_day_start_ts(end + timedelta(days=1)))
        rows = self._query(
            _SUMMARY_SQL + "WHERE " + " AND ".join(where) + " ORDER BY m.date_received DESC, m.ROWID DESC LIMIT ?",
            [*params, limit],
        )
        return [self._summary(r) for r in rows]

    # --- message files ---

    def _scan_files(self) -> None:
        found: dict[int, Path] = {}
        for entry in self._mail_dir.iterdir() if self._mail_dir.is_dir() else ():
            if entry.name == "MailData" or not entry.is_dir():
                continue
            for dirpath, _dirs, files in os.walk(entry):
                if os.path.basename(dirpath) != "Messages":
                    continue
                for name in files:
                    m = re.fullmatch(r"(\d+)(\.partial)?\.emlx", name)
                    if not m:
                        continue
                    mid = int(m.group(1))
                    # A full .emlx wins over a .partial.emlx of the same message.
                    if mid not in found or not m.group(2):
                        found[mid] = Path(dirpath) / name
        self._paths = found

    def _find_file(self, id: int) -> Path | None:
        path = self._paths.get(id)
        if path is None or not path.is_file():
            self._scan_files()
            path = self._paths.get(id)
        return path

    @staticmethod
    def _parse_emlx(path: Path) -> Message:
        data = path.read_bytes()
        first, _, rest = data.partition(b"\n")
        try:
            n = int(first.strip())
            raw = rest[:n]
        except ValueError:
            raw = data  # not length-prefixed; treat the whole file as RFC 822
        return email.message_from_bytes(raw, policy=email.policy.default)

    def read_body(self, id: int, max_chars: int = 20000) -> str:
        """Headers + plain-text body of one message, at most ``max_chars`` characters."""
        if not self._query("SELECT 1 FROM messages WHERE ROWID = ?", (id,)):
            raise MailNotFound(f"no message {id}")
        path = self._find_file(id)
        if path is None:
            raise MailNotFound(f"no file for message {id}")
        try:
            msg = self._parse_emlx(path)
        except OSError as e:
            raise MailNotFound(f"cannot read file for message {id}: {e}") from e
        head = "\n".join(f"{h}: {_header(msg, h)}" for h in ("From", "To", "Date", "Subject"))
        out = f"{head}\n\n{_body_text(msg)}"
        if len(out) > max_chars:
            out = out[: max(0, max_chars - len(TRUNCATED_MARK))] + TRUNCATED_MARK
            out = out[:max_chars]
        return out
