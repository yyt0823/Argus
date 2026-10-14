"""Builder for a fake macOS Mail data directory (``V10``) used by mail tests.

Only the tables/columns the reader uses are created. All data is fake.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from email.message import EmailMessage
from email.utils import format_datetime
from pathlib import Path
from urllib.parse import unquote, urlparse

STORE_UUID = "11111111-2222-3333-4444-555555555555"

_INDEX_SCHEMA = """
CREATE TABLE messages (ROWID INTEGER PRIMARY KEY AUTOINCREMENT, sender INTEGER, subject_prefix TEXT,
    subject INTEGER NOT NULL, summary INTEGER, date_received INTEGER, mailbox INTEGER NOT NULL,
    read INTEGER NOT NULL DEFAULT 0, deleted INTEGER NOT NULL DEFAULT 0);
CREATE TABLE mailboxes (ROWID INTEGER PRIMARY KEY AUTOINCREMENT, url TEXT NOT NULL UNIQUE);
CREATE TABLE subjects (ROWID INTEGER PRIMARY KEY, subject TEXT);
CREATE TABLE addresses (ROWID INTEGER PRIMARY KEY, address TEXT, comment TEXT);
CREATE TABLE summaries (ROWID INTEGER PRIMARY KEY, summary TEXT);
CREATE TABLE labels (message_id INTEGER, mailbox_id INTEGER, PRIMARY KEY(message_id, mailbox_id));
"""

_ACCOUNTS_SCHEMA = """
CREATE TABLE ZACCOUNT (Z_PK INTEGER PRIMARY KEY, ZIDENTIFIER TEXT, ZACCOUNTDESCRIPTION TEXT,
    ZUSERNAME TEXT, ZPARENTACCOUNT INTEGER);
"""


@dataclass
class FakeMail:
    mail_dir: Path
    accounts_db: Path
    paths: dict[int, Path] = field(default_factory=dict)  # message id -> .emlx path

    @property
    def index(self) -> Path:
        return self.mail_dir / "MailData" / "Envelope Index"

    def _exec(self, db: Path, sql: str, params: tuple = ()) -> int:
        c = sqlite3.connect(db)
        try:
            cur = c.execute(sql, params)
            c.commit()
            return cur.lastrowid
        finally:
            c.close()

    def add_account(self, identifier: str, description: str | None = None, username: str | None = None,
                    parent: int | None = None) -> int:
        """Add a ZACCOUNT row; returns its Z_PK (usable as ``parent`` of a child account)."""
        return self._exec(
            self.accounts_db,
            "INSERT INTO ZACCOUNT (ZIDENTIFIER, ZACCOUNTDESCRIPTION, ZUSERNAME, ZPARENTACCOUNT) VALUES (?,?,?,?)",
            (identifier, description, username, parent),
        )

    def add_mailbox(self, url: str) -> int:
        return self._exec(self.index, "INSERT INTO mailboxes (url) VALUES (?)", (url,))

    def add_message(
        self,
        mailbox: int,
        sender: str,
        name: str | None,
        subject: str,
        received: datetime,
        snippet: str = "",
        body: str = "Hello.",
        html: bool = False,
        plain_alt: str | None = None,
        read: bool = False,
        deleted: bool = False,
        labels: tuple[int, ...] = (),
        subject_prefix: str | None = None,
        partial: bool = False,
        nested: tuple[str, ...] = (),
        to: str = "me@example.com",
        write_file: bool = True,
    ) -> int:
        c = sqlite3.connect(self.index)
        try:
            addr = c.execute("INSERT INTO addresses (address, comment) VALUES (?,?)", (sender, name or "")).lastrowid
            subj = c.execute("INSERT INTO subjects (subject) VALUES (?)", (subject,)).lastrowid
            summ = c.execute("INSERT INTO summaries (summary) VALUES (?)", (snippet,)).lastrowid if snippet else None
            mid = c.execute(
                "INSERT INTO messages (sender, subject_prefix, subject, summary, date_received, mailbox, read, deleted)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (addr, subject_prefix, subj, summ, int(received.timestamp()), mailbox, int(read), int(deleted)),
            ).lastrowid
            for lb in labels:
                c.execute("INSERT INTO labels (message_id, mailbox_id) VALUES (?,?)", (mid, lb))
            url = c.execute("SELECT url FROM mailboxes WHERE ROWID = ?", (mailbox,)).fetchone()[0]
            c.commit()
        finally:
            c.close()
        if write_file:
            msg = EmailMessage()
            msg["From"] = f"{name} <{sender}>" if name else sender
            msg["To"] = to
            msg["Date"] = format_datetime(received.astimezone())
            msg["Subject"] = (subject_prefix or "") + subject
            if html and plain_alt is not None:
                msg.set_content(plain_alt)
                msg.add_alternative(body, subtype="html")
            elif html:
                # HTML only, with an attachment so the message is multipart.
                msg.set_content(body, subtype="html")
                msg.add_attachment(b"%PDF-fake", maintype="application", subtype="pdf", filename="a.pdf")
            else:
                msg.set_content(body)
            self.paths[mid] = self.write_emlx(url, mid, msg.as_bytes(), partial=partial, nested=nested)
        return mid

    def write_emlx(self, mailbox_url: str, mid: int, raw: bytes, partial: bool = False,
                   nested: tuple[str, ...] = ()) -> Path:
        u = urlparse(mailbox_url)
        box = unquote(u.path.lstrip("/")).split("/")[-1] or "INBOX"
        d = self.mail_dir / u.netloc / f"{box}.mbox" / STORE_UUID / "Data"
        for part in nested:
            d = d / part
        d = d / "Messages"
        d.mkdir(parents=True, exist_ok=True)
        path = d / (f"{mid}.partial.emlx" if partial else f"{mid}.emlx")
        plist = b'<?xml version="1.0" encoding="UTF-8"?>\n<plist version="1.0"><dict/></plist>\n'
        path.write_bytes(str(len(raw)).encode() + b"\n" + raw + plist)
        return path


def build_fake_mail(tmp_path: Path) -> FakeMail:
    mail_dir = tmp_path / "V10"
    (mail_dir / "MailData").mkdir(parents=True)
    fm = FakeMail(mail_dir=mail_dir, accounts_db=tmp_path / "Accounts4.sqlite")
    for db, schema in ((fm.index, _INDEX_SCHEMA), (fm.accounts_db, _ACCOUNTS_SCHEMA)):
        c = sqlite3.connect(db)
        c.executescript(schema)
        c.commit()
        c.close()
    return fm
