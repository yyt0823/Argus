import sqlite3
from datetime import date

import pytest

from argus.jobs.tracker import ApplicationsTracker

# Same CREATE TABLE statements as the user's build_db.py (schema only, invented data below).
OLD_SCHEMA = """
CREATE TABLE applications(
  id INTEGER PRIMARY KEY, applied_date TEXT, company TEXT, role TEXT, city TEXT, start TEXT,
  region TEXT,            -- Canada / China
  status TEXT DEFAULT 'Applied',  -- Applied | Confirmed | Action needed | In process | Rejected | Position closed
  last_update TEXT, notes TEXT);
CREATE TABLE email_events(
  id INTEGER PRIMARY KEY, email_date TEXT, account TEXT, sender TEXT, subject TEXT,
  company TEXT, role TEXT,
  kind TEXT,              -- confirmation | rejection | action | update
  summary TEXT, action TEXT, deadline TEXT, mail_rowid INTEGER);
"""

SEED = [
    ("2026-10-02", "Acme Corp", "Dev Intern", "Montreal", "Jan 2027", "Canada"),
    ("2026-09-25", "Globex", "Data Intern", "Toronto", "May 2027", "Canada"),
    ("before 10-17", "Initech CN", "后端实习", "Shanghai", "Internship", "China"),
    ("2026-10-17", "Umbrella CN", "算法实习", "Beijing", "Internship", "China"),
    ("2026-10-05", "Odd Region Ltd", "Intern", "Nowhere", "2027", None),
]


@pytest.fixture
def tracker(tmp_path):
    con = sqlite3.connect(tmp_path / "applications.db")
    con.executescript(OLD_SCHEMA)
    con.executemany(
        "INSERT INTO applications(applied_date,company,role,city,start,region) VALUES(?,?,?,?,?,?)", SEED
    )
    con.commit()
    con.close()
    return ApplicationsTracker(tmp_path)


def test_migrate_idempotent(tracker):
    tracker.migrate()
    tracker.migrate()
    rows = tracker.rows()
    assert len(rows) == 5
    assert {r["source"] for r in rows} == {"tracker"}
    assert all(k in rows[0] for k in ("url", "req_id", "source", "posting_id"))
    new_id = tracker.add_application(
        applied_date=date(2026, 10, 29), company="Hooli", role="SWE", city="Remote", start="Jan 2027"
    )
    tracker.migrate()
    assert [r for r in tracker.rows() if r["id"] == new_id][0]["source"] == "argus"


def test_migrate_creates_missing_db(tmp_path):
    t = ApplicationsTracker(tmp_path)
    t.migrate()
    assert t.rows() == []
    assert t.add_email_event(
        email_date="2026-10-29", account="a", sender="s", subject="x", company="c", role="r",
        kind="update", summary="m",
    ) == 1


def test_add_and_set_status_appends_note(tracker):
    tracker.migrate()
    app_id = tracker.add_application(
        applied_date=date(2026, 10, 29), company="Hooli", role="SWE", city="Remote", start="Jan 2027",
        url="https://example.com/j/1", req_id="R1", posting_id=7, notes="first",
    )
    row = [r for r in tracker.rows() if r["id"] == app_id][0]
    assert (row["status"], row["url"], row["req_id"], row["posting_id"]) == ("Applied", "https://example.com/j/1", "R1", 7)
    tracker.set_status(app_id, "Confirmed", date(2026, 10, 30), "got email")
    row = [r for r in tracker.rows() if r["id"] == app_id][0]
    assert row["status"] == "Confirmed"
    assert row["last_update"] == "2026-10-30"
    assert row["notes"] == "first ; got email"
    with pytest.raises(ValueError):
        tracker.set_status(app_id, "Bogus", date(2026, 10, 30), "x")


def test_email_event_dedup_by_rowid(tracker):
    tracker.migrate()
    kw = dict(email_date="2026-10-29", account="a", sender="s", subject="x", company="c", role="r",
              kind="rejection", summary="no", mail_rowid=42)
    first = tracker.add_email_event(**kw)
    assert tracker.add_email_event(**kw) == first
    con = sqlite3.connect(tracker.db_path)
    assert con.execute("SELECT count(*) FROM email_events").fetchone()[0] == 1
    con.close()


def test_render_keeps_manual_footer(tracker):
    tracker.migrate()
    footer = "## Filled, waiting on the user (not counted above)\n- **Canada:**\n  - Foo Inc (captcha)\n"
    tracker.md_path.write_text("# old\n\nstuff\n\n" + footer, encoding="utf-8")
    tracker.render_markdown()
    text = tracker.md_path.read_text(encoding="utf-8")
    assert text.startswith("# All applications — generated from applications.db (do not edit the tables by hand)")
    assert text.endswith(footer)
    assert "stuff" not in text
    assert not tracker.md_path.with_name("ALL_APPLICATIONS.md.tmp").exists()
    tracker.render_markdown()  # re-render is stable
    assert tracker.md_path.read_text(encoding="utf-8") == text


def test_render_without_footer(tracker):
    tracker.migrate()
    tracker.render_markdown()
    assert "Filled, waiting" not in tracker.md_path.read_text(encoding="utf-8")


def test_render_counts_by_region(tracker):
    tracker.migrate()
    tracker.render_markdown()
    text = tracker.md_path.read_text(encoding="utf-8")
    assert "## Canada / remote — 3" in text
    assert "## China — 2" in text
    assert "| # | Date | Company | Role | City | Start | Status |" in text
    assert "| 1 | 09-25 | Globex | Data Intern | Toronto | May 2027 | Applied |" in text
    assert "| 2 | 10-02 | Acme Corp |" in text
    assert "| 3 | 10-05 | Odd Region Ltd |" in text
    assert "| C1 | before 10-17 | Initech CN |" in text
    assert "| C2 | 10-17 | Umbrella CN |" in text


def test_migrate_backfills_source_after_partial_migration(tracker):
    con = sqlite3.connect(tracker.db_path)
    con.execute("ALTER TABLE applications ADD COLUMN source TEXT")  # crashed before the backfill
    con.commit()
    con.close()
    tracker.migrate()
    assert {r["source"] for r in tracker.rows()} == {"tracker"}


def test_migrate_adds_unique_mail_rowid_index(tracker):
    tracker.migrate()
    con = sqlite3.connect(tracker.db_path)
    ins = "INSERT INTO email_events(mail_rowid) VALUES(?)"
    con.execute(ins, (None,))
    con.execute(ins, (None,))  # NULLs may repeat
    con.execute(ins, (5,))
    with pytest.raises(sqlite3.IntegrityError):
        con.execute(ins, (5,))
    con.close()


def test_migrate_skips_index_when_duplicates_exist(tracker):
    con = sqlite3.connect(tracker.db_path)
    con.executemany("INSERT INTO email_events(mail_rowid) VALUES(?)", [(9,), (9,)])
    con.commit()
    con.close()
    tracker.migrate()  # must not raise
    assert len(tracker.rows()) == 5
