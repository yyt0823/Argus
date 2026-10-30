"""Applications tracker: ``applications.db`` is the source of truth, ``ALL_APPLICATIONS.md`` is a view.

``ApplicationsTracker`` owns both files inside the internships directory. Argus writes the DB
(``add_application``, ``set_status``, ``add_email_event``) and ``render_markdown`` regenerates the
Markdown: a header, one table per region (``Canada`` rows numbered ``1..N``, ``China`` rows ``C1..CN``,
each ordered by applied_date then id), and the hand-written ``## Filled, waiting on the user`` section,
which is copied verbatim from the existing file to EOF.

``migrate`` is idempotent: it creates the tables when missing and adds the Argus columns
(``url``, ``req_id``, ``source``, ``posting_id``). Existing rows get ``source='tracker'``; rows added
through this module get ``source='argus'``.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import date
from pathlib import Path

STATUSES = ("Applied", "Confirmed", "Action needed", "In process", "Rejected", "Position closed")
EMAIL_KINDS = ("confirmation", "rejection", "action", "update")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS applications(
  id INTEGER PRIMARY KEY, applied_date TEXT, company TEXT, role TEXT, city TEXT, start TEXT,
  region TEXT,            -- Canada / China
  status TEXT DEFAULT 'Applied',  -- Applied | Confirmed | Action needed | In process | Rejected | Position closed
  last_update TEXT, notes TEXT);
CREATE TABLE IF NOT EXISTS email_events(
  id INTEGER PRIMARY KEY, email_date TEXT, account TEXT, sender TEXT, subject TEXT,
  company TEXT, role TEXT,
  kind TEXT,              -- confirmation | rejection | action | update
  summary TEXT, action TEXT, deadline TEXT, mail_rowid INTEGER);
"""
_NEW_COLUMNS = ("url TEXT", "req_id TEXT", "source TEXT", "posting_id INTEGER")

_HEADER = "# All applications — generated from applications.db (do not edit the tables by hand)"
_FOOTER_HEADING = "## Filled, waiting on the user"
_TABLE_HEAD = "| # | Date | Company | Role | City | Start | Status |\n|---|---|---|---|---|---|---|"
_ISO_DATE = re.compile(r"^\d{4}-(\d\d-\d\d)$")


def _cell(value: object) -> str:
    text = "" if value is None else str(value)
    return text.replace("|", "\\|").replace("\n", " ").strip()


def _display_date(applied_date: str | None) -> str:
    text = applied_date or ""
    m = _ISO_DATE.match(text)
    return m.group(1) if m else text


class ApplicationsTracker:
    def __init__(self, internships_dir: Path):
        self.db_path = internships_dir / "applications.db"
        self.md_path = internships_dir / "ALL_APPLICATIONS.md"

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def migrate(self) -> None:
        conn = self._connect()
        try:
            with conn:
                conn.executescript(_SCHEMA)
                have = {r["name"] for r in conn.execute("PRAGMA table_info(applications)")}
                for column in _NEW_COLUMNS:
                    name = column.split()[0]
                    if name in have:
                        continue
                    conn.execute(f"ALTER TABLE applications ADD COLUMN {column}")
                    if name == "source":
                        conn.execute("UPDATE applications SET source='tracker'")
        finally:
            conn.close()

    def add_application(
        self,
        *,
        applied_date: date,
        company: str,
        role: str,
        city: str,
        start: str,
        region: str = "Canada",
        url: str | None = None,
        req_id: str | None = None,
        posting_id: int | None = None,
        notes: str | None = None,
    ) -> int:
        conn = self._connect()
        try:
            with conn:
                cur = conn.execute(
                    "INSERT INTO applications(applied_date, company, role, city, start, region, status,"
                    " last_update, notes, url, req_id, source, posting_id)"
                    " VALUES(?,?,?,?,?,?,'Applied',?,?,?,?,'argus',?)",
                    (applied_date.isoformat(), company, role, city, start, region,
                     applied_date.isoformat(), notes, url, req_id, posting_id),
                )
                return int(cur.lastrowid)
        finally:
            conn.close()

    def set_status(self, app_id: int, status: str, when: date, note: str) -> None:
        if status not in STATUSES:
            raise ValueError(f"unknown status {status!r}; expected one of {STATUSES}")
        conn = self._connect()
        try:
            with conn:
                cur = conn.execute(
                    "UPDATE applications SET status=?, last_update=?,"
                    " notes=coalesce(notes||' ; ','')||? WHERE id=?",
                    (status, when.isoformat(), note, app_id),
                )
                if cur.rowcount == 0:
                    raise KeyError(f"no application with id {app_id}")
        finally:
            conn.close()

    def add_email_event(
        self,
        *,
        email_date: str,
        account: str,
        sender: str,
        subject: str,
        company: str,
        role: str,
        kind: str,
        summary: str,
        action: str = "",
        deadline: str = "",
        mail_rowid: int | None = None,
    ) -> int:
        """Insert an email event; returns its id, or the existing id when ``mail_rowid`` was seen."""
        if kind not in EMAIL_KINDS:
            raise ValueError(f"unknown kind {kind!r}; expected one of {EMAIL_KINDS}")
        conn = self._connect()
        try:
            with conn:
                if mail_rowid is not None:
                    row = conn.execute(
                        "SELECT id FROM email_events WHERE mail_rowid=?", (mail_rowid,)
                    ).fetchone()
                    if row:
                        return int(row["id"])
                cur = conn.execute(
                    "INSERT INTO email_events(email_date, account, sender, subject, company, role, kind,"
                    " summary, action, deadline, mail_rowid) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (email_date, account, sender, subject, company, role, kind, summary, action,
                     deadline, mail_rowid),
                )
                return int(cur.lastrowid)
        finally:
            conn.close()

    def rows(self) -> list[dict]:
        conn = self._connect()
        try:
            return [dict(r) for r in conn.execute("SELECT * FROM applications ORDER BY id")]
        finally:
            conn.close()

    def render_markdown(self) -> None:
        canada: list[dict] = []
        china: list[dict] = []
        for row in self.rows():
            (china if row["region"] == "China" else canada).append(row)
        parts = [_HEADER, _region_section("Canada / remote", canada, ""), _region_section("China", china, "C")]
        text = "\n\n".join(parts) + "\n"
        footer = self._footer()
        if footer:
            text += "\n" + footer
        tmp = self.md_path.with_name(self.md_path.name + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(self.md_path)

    def _footer(self) -> str:
        if not self.md_path.exists():
            return ""
        content = self.md_path.read_text(encoding="utf-8")
        for match in re.finditer(r"^" + re.escape(_FOOTER_HEADING), content, re.MULTILINE):
            return content[match.start():]
        return ""


def _region_section(title: str, rows: list[dict], prefix: str) -> str:
    # Free-text dates ("before 10-17") predate the exact ones, so they sort first.
    rows = sorted(rows, key=lambda r: (bool(_ISO_DATE.match(r["applied_date"] or "")), r["applied_date"] or "", r["id"]))
    lines = [f"## {title} — {len(rows)}", "", _TABLE_HEAD]
    for n, r in enumerate(rows, 1):
        cells = [
            f"{prefix}{n}", _display_date(r["applied_date"]), _cell(r["company"]), _cell(r["role"]),
            _cell(r["city"]), _cell(r["start"]), _cell(r["status"] or "Applied"),
        ]
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)
