from datetime import date, datetime
import pytest
from argus.store import Store

@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "data" / "argus.db")
    yield s
    s.close()

def test_add_and_search_english(store):
    store.add_message("user", "I did squats today", datetime(2026, 10, 1, 9, 0))
    store.add_message("argus", "Nice squats", datetime(2026, 10, 1, 9, 1))
    hits = store.search_messages("squats")
    assert [h["sender"] for h in hits] == ["argus", "user"]  # newest first

def test_search_two_char_chinese(store):
    store.add_message("user", "我膝盖疼，别安排深蹲", datetime(2026, 10, 2, 8, 0))
    hits = store.search_messages("膝盖")
    assert len(hits) == 1 and "膝盖" in hits[0]["content"]

def test_search_three_char_chinese_uses_fts(store):
    store.add_message("user", "今天吃了牛肉饭", datetime(2026, 10, 2, 12, 0))
    assert len(store.search_messages("牛肉饭")) == 1

def test_search_date_range_inclusive(store):
    store.add_message("user", "apple one", datetime(2026, 10, 1, 23, 0))
    store.add_message("user", "apple two", datetime(2026, 10, 3, 1, 0))
    hits = store.search_messages("apple", start=date(2026, 10, 1), end=date(2026, 10, 1))
    assert [h["content"] for h in hits] == ["apple one"]

def test_search_escapes_like_and_quotes(store):
    store.add_message("user", 'he said "100%"', datetime(2026, 10, 1))
    assert len(store.search_messages('"100%"')) == 1
    # "_" is a LIKE wildcard; escaped, it must match only a literal underscore.
    assert store.search_messages("_") == []
    assert len(store.search_messages("%")) == 1  # literal % is in the content
    assert store.search_messages("") == []

def test_sessions_roundtrip(store):
    assert store.get_session(date(2026, 10, 1)) is None
    sid = store.create_session(date(2026, 10, 1), datetime(2026, 10, 1, 4, 0))
    store.set_session_sdk_id(sid, "abc")
    store.end_session(sid, datetime(2026, 10, 2, 4, 0))
    row = store.get_session(date(2026, 10, 1))
    assert row["id"] == sid and row["sdk_session_id"] == "abc" and row["ended_at"].startswith("2026-10-02")

def test_sent_ids_idempotent(store):
    store.add_sent_id("g1", datetime(2026, 10, 1))
    store.add_sent_id("g1", datetime(2026, 10, 1))
    assert store.has_sent_id("g1") and not store.has_sent_id("g2")

def test_state(store):
    assert store.get_state("k", "d") == "d"
    store.set_state("k", "1"); store.set_state("k", "2")
    assert store.get_state("k") == "2"

def test_reopen_keeps_data(tmp_path):
    p = tmp_path / "x.db"
    s = Store(p); s.add_message("user", "hello world", datetime(2026, 10, 1)); s.close()
    s2 = Store(p); assert len(s2.search_messages("hello")) == 1; s2.close()

# --- pending queue ---

from argus.channel.base import IncomingMessage

def _m(guid, text, at):
    return IncomingMessage(id=guid, text=text, received_at=at)

def test_accept_messages_writes_pending_and_checkpoint(store):
    store.accept_messages([_m("g1", "hi", datetime(2026, 10, 1, 9, 0, 0, 500))], "cp", "42")
    assert store.get_state("cp") == "42"
    assert store.list_pending() == [_m("g1", "hi", datetime(2026, 10, 1, 9, 0, 0))]

def test_accept_messages_empty_still_moves_checkpoint(store):
    store.accept_messages([], "cp", "7")
    assert store.get_state("cp") == "7" and store.list_pending() == []

def test_accept_messages_is_one_transaction(store):
    store.set_state("cp", "1")
    bad = _m("g2", object(), datetime(2026, 10, 1))  # unbindable -> fails after g1 is inserted
    with pytest.raises(Exception):
        store.accept_messages([_m("g1", "ok", datetime(2026, 10, 1)), bad], "cp", "9")
    assert store.get_state("cp") == "1" and store.list_pending() == []

def test_accept_messages_ignores_duplicates(store):
    store.accept_messages([_m("g1", "a", datetime(2026, 10, 1))], "cp", "1")
    store.accept_messages([_m("g1", "changed", datetime(2026, 10, 2))], "cp", "2")
    assert [m.text for m in store.list_pending()] == ["a"]

def test_list_pending_ordered_by_at_then_rowid(store):
    store.accept_messages([
        _m("late", "c", datetime(2026, 10, 1, 10)),
        _m("e1", "a", datetime(2026, 10, 1, 9)),
        _m("e2", "b", datetime(2026, 10, 1, 9)),
    ], "cp", "1")
    assert [m.id for m in store.list_pending()] == ["e1", "e2", "late"]

def test_delete_pending(store):
    store.accept_messages([_m("g1", "a", datetime(2026, 10, 1)), _m("g2", "b", datetime(2026, 10, 1))], "cp", "1")
    store.delete_pending("g1")
    store.delete_pending("missing")  # no-op
    assert [m.id for m in store.list_pending()] == ["g2"]

def test_pending_survives_reopen(tmp_path):
    p = tmp_path / "x.db"
    s = Store(p); s.accept_messages([_m("g1", "a", datetime(2026, 10, 1))], "cp", "1"); s.close()
    s2 = Store(p); assert [m.id for m in s2.list_pending()] == ["g1"]; s2.close()

# --- system sender + migration from the old schema ---

import sqlite3

_OLD_SCHEMA = """
CREATE TABLE messages (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    sender TEXT NOT NULL CHECK (sender IN ('user','argus')),
    content TEXT NOT NULL,
    session_id INTEGER
);
CREATE VIRTUAL TABLE messages_fts USING fts5(
    content, content='messages', content_rowid='id', tokenize='trigram'
);
CREATE TRIGGER messages_ai AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, content) VALUES (new.id, new.content);
END;
CREATE TABLE sessions (
    id INTEGER PRIMARY KEY, day TEXT NOT NULL UNIQUE, sdk_session_id TEXT,
    started_at TEXT NOT NULL, ended_at TEXT
);
CREATE TABLE sent_ids (guid TEXT PRIMARY KEY, sent_at TEXT NOT NULL);
CREATE TABLE state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE pending (guid TEXT PRIMARY KEY, text TEXT NOT NULL, at TEXT NOT NULL);
"""

def test_add_system_message(store):
    store.add_message("system", "internal briefing task", datetime(2026, 10, 1, 8, 0))
    hits = store.search_messages("briefing")
    assert [h["sender"] for h in hits] == ["system"]

def test_unknown_sender_still_rejected(store):
    with pytest.raises(sqlite3.IntegrityError):
        store.add_message("bogus", "x", datetime(2026, 10, 1))

def test_migrates_old_sender_constraint(tmp_path):
    p = tmp_path / "data" / "argus.db"
    p.parent.mkdir()
    conn = sqlite3.connect(p)
    conn.executescript(_OLD_SCHEMA)
    conn.execute("INSERT INTO messages (id, ts, sender, content, session_id) VALUES (5, '2026-10-01T09:00:00', 'user', 'squats at the gym', 1)")
    conn.execute("INSERT INTO messages (id, ts, sender, content, session_id) VALUES (9, '2026-10-01T09:01:00', 'argus', '我膝盖疼了吗', NULL)")
    conn.execute("INSERT INTO state (key, value) VALUES ('cp', '42')")
    conn.commit()
    conn.close()

    s = Store(p)
    try:
        hits = s.search_messages("squats")
        assert [(h["id"], h["sender"]) for h in hits] == [(5, "user")]
        assert [h["id"] for h in s.search_messages("膝盖疼")] == [9]   # FTS path
        assert [h["id"] for h in s.search_messages("膝盖")] == [9]     # LIKE path
        assert s.get_state("cp") == "42"
        new_id = s.add_message("system", "internal task about squats", datetime(2026, 10, 2))
        assert new_id > 9
        assert {h["id"] for h in s.search_messages("squats")} == {5, new_id}
    finally:
        s.close()

    # Reopening the migrated DB is a no-op and keeps everything.
    s2 = Store(p)
    try:
        assert len(s2.search_messages("squats")) == 2
        conn = sqlite3.connect(p)
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
        conn.close()
        assert "messages_new" not in names and "messages_ai" in names
    finally:
        s2.close()

# --- mail triage ---

def _triage(mail_id, received, **kw):
    row = dict(
        mail_id=mail_id, account="Work", sender="Alice <alice@example.com>", subject="Hello",
        received_at=received, important=True, category="work", summary="sum", action="reply",
        deadline=None, status="triaged", attempts=1,
    )
    row.update(kw)
    return row

def test_upsert_and_get_triage(store):
    assert store.get_triage(1) is None
    store.upsert_triage(_triage(1, datetime(2026, 10, 14, 7, 59)))
    row = store.get_triage(1)
    assert row["mail_id"] == 1 and row["account"] == "Work" and row["important"] is True
    assert row["received_at"] == "2026-10-14T07:59:00"
    assert row["status"] == "triaged" and row["attempts"] == 1
    assert row["alerted_at"] is None and row["briefed_at"] is None and row["created_at"]

def test_upsert_triage_failed_has_null_verdict(store):
    store.upsert_triage(_triage(2, datetime(2026, 10, 14), important=None, category=None,
                                summary=None, action=None, status="failed"))
    row = store.get_triage(2)
    assert row["important"] is None and row["status"] == "failed"

def test_upsert_triage_preserves_marks_and_created_at(store):
    store.upsert_triage(_triage(1, datetime(2026, 10, 14, 9), important=False))
    created = store.get_triage(1)["created_at"]
    store.mark_alerted([1], datetime(2026, 10, 14, 9, 5))
    store.mark_briefed([1], datetime(2026, 10, 15, 8, 0))
    store.upsert_triage(_triage(1, datetime(2026, 10, 14, 9), important=True, attempts=2))
    row = store.get_triage(1)
    assert row["important"] is True and row["attempts"] == 2
    assert row["alerted_at"] == "2026-10-14T09:05:00"
    assert row["briefed_at"] == "2026-10-15T08:00:00"
    assert row["created_at"] == created

def test_triage_between(store):
    store.upsert_triage(_triage(3, datetime(2026, 10, 14, 10), important=False))
    store.upsert_triage(_triage(1, datetime(2026, 10, 14, 7, 59)))
    store.upsert_triage(_triage(2, datetime(2026, 10, 13, 23)))
    store.upsert_triage(_triage(4, datetime(2026, 10, 14, 12)))   # == end, excluded
    rows = store.triage_between(datetime(2026, 10, 13, 23), datetime(2026, 10, 14, 12))
    assert [r["mail_id"] for r in rows] == [2, 1, 3]
    rows = store.triage_between(datetime(2026, 10, 13, 23), datetime(2026, 10, 14, 12), important_only=True)
    assert [r["mail_id"] for r in rows] == [2, 1]

def test_failed_triage(store):
    store.upsert_triage(_triage(1, datetime(2026, 10, 14), status="failed", attempts=1, important=None))
    store.upsert_triage(_triage(2, datetime(2026, 10, 14), status="failed", attempts=3, important=None))
    store.upsert_triage(_triage(3, datetime(2026, 10, 14)))
    assert [r["mail_id"] for r in store.failed_triage()] == [1]
    assert [r["mail_id"] for r in store.failed_triage(max_attempts=4)] == [1, 2]

def test_mark_alerted_and_briefed(store):
    for i in (1, 2, 3):
        store.upsert_triage(_triage(i, datetime(2026, 10, 14, i)))
    store.mark_alerted([1, 3], datetime(2026, 10, 14, 9))
    store.mark_briefed([2], datetime(2026, 10, 15, 8))
    store.mark_alerted([], datetime(2026, 10, 14, 9))  # no-op
    assert [store.get_triage(i)["alerted_at"] for i in (1, 2, 3)] == ["2026-10-14T09:00:00", None, "2026-10-14T09:00:00"]
    assert store.get_triage(2)["briefed_at"] == "2026-10-15T08:00:00"
    assert store.get_triage(1)["briefed_at"] is None
