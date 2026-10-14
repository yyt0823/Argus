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

# --- fitness: food / weight / workout logs ---

D1, D2, D3 = date(2026, 10, 1), date(2026, 10, 2), date(2026, 10, 3)

def _food(store, day=D1, at=None, **kw):
    args = dict(description="oats", kcal=300, protein_g=10, fat_g=5, carbs_g=50)
    args.update(kw)
    return store.add_food(day, at or datetime.combine(day, datetime.min.time()).replace(hour=8), **args)

def test_add_food_roundtrip(store):
    fid = _food(store, added_sugar_g=3, sodium="low", meal="breakfast", note="n")
    [row] = store.foods_for_day(D1)
    assert row["id"] == fid and row["day"] == "2026-10-01" and row["logged_at"] == "2026-10-01T08:00:00"
    assert (row["description"], row["kcal"], row["protein_g"], row["fat_g"], row["carbs_g"]) == ("oats", 300, 10, 5, 50)
    assert (row["added_sugar_g"], row["sodium"], row["meal"], row["note"], row["deleted"]) == (3, "low", "breakfast", "n", 0)

def test_add_food_optional_fields_default_none(store):
    _food(store)
    [row] = store.foods_for_day(D1)
    assert row["added_sugar_g"] is None and row["sodium"] is None and row["meal"] is None and row["note"] is None

def test_foods_for_day_ordered_and_scoped(store):
    b = _food(store, at=datetime(2026, 10, 1, 12), description="b")
    a = _food(store, at=datetime(2026, 10, 1, 8), description="a")
    a2 = _food(store, at=datetime(2026, 10, 1, 8), description="a2")
    _food(store, day=D2, description="other day")
    assert [r["id"] for r in store.foods_for_day(D1)] == [a, a2, b]

def test_food_day_is_explicit_not_from_logged_at(store):
    # a 01:00 snack belongs to the previous Argus day; the caller passes that day
    _food(store, day=D1, at=datetime(2026, 10, 2, 1, 0), description="late snack")
    assert [r["description"] for r in store.foods_for_day(D1)] == ["late snack"]
    assert store.foods_for_day(D2) == []

def test_foods_between_inclusive(store):
    for d in (D1, D2, D3):
        _food(store, day=d, description=d.isoformat())
    assert [r["description"] for r in store.foods_between(D1, D2)] == ["2026-10-01", "2026-10-02"]

@pytest.mark.parametrize("kw", [
    {"kcal": -1}, {"protein_g": -0.1}, {"fat_g": -1}, {"carbs_g": -1}, {"added_sugar_g": -1},
    {"kcal": 5001}, {"protein_g": 301}, {"kcal": float("nan")}, {"kcal": float("inf")}, {"kcal": "100"},
    {"kcal": True}, {"meal": "brunch"}, {"sodium": "extreme"}, {"description": "  "},
])
def test_add_food_validation(store, kw):
    with pytest.raises(ValueError):
        _food(store, **kw)
    assert store.foods_for_day(D1) == []

def test_add_food_accepts_all_meals_and_sodium(store):
    for meal in ("breakfast", "lunch", "dinner", "snack", "pre_workout", "other", None):
        _food(store, meal=meal)
    for sodium in ("low", "medium", "high", None):
        _food(store, sodium=sodium)
    assert len(store.foods_for_day(D1)) == 11

def test_update_food(store):
    fid = _food(store)
    row = store.update_food(fid, kcal=250, protein_g=12.5, meal="snack", note="fixed", added_sugar_g=None)
    assert row["kcal"] == 250 and row["protein_g"] == 12.5 and row["meal"] == "snack" and row["note"] == "fixed"
    assert store.foods_for_day(D1)[0] == row

def test_update_food_can_move_day(store):
    fid = _food(store)
    row = store.update_food(fid, day=D2)
    assert row["day"] == "2026-10-02" and store.foods_for_day(D1) == []

def test_update_food_rejects_bad_fields(store):
    fid = _food(store)
    with pytest.raises(ValueError):
        store.update_food(fid, logged_at=datetime(2026, 10, 1))
    with pytest.raises(ValueError):
        store.update_food(fid, deleted=1)
    with pytest.raises(ValueError):
        store.update_food(fid, kcal=-5)
    with pytest.raises(ValueError):
        store.update_food(fid, sodium="salty")
    with pytest.raises(ValueError):
        store.update_food(fid)
    assert store.foods_for_day(D1)[0]["kcal"] == 300

def test_update_food_missing_or_deleted(store):
    with pytest.raises(KeyError):
        store.update_food(999, kcal=1)
    fid = _food(store)
    store.delete_food(fid)
    with pytest.raises(KeyError):
        store.update_food(fid, kcal=1)

def test_delete_food_is_soft(store):
    fid = _food(store)
    keep = _food(store, description="keep")
    row = store.delete_food(fid)
    assert row["id"] == fid and row["deleted"] == 1
    assert [r["id"] for r in store.foods_for_day(D1)] == [keep]
    assert [r["id"] for r in store.foods_between(D1, D1)] == [keep]
    with pytest.raises(KeyError):
        store.delete_food(fid)
    with pytest.raises(KeyError):
        store.delete_food(12345)
    # the row is still in the database
    assert store._read("SELECT deleted FROM food_log WHERE id = ?", (fid,))[0]["deleted"] == 1

def test_weights(store):
    store.add_weight(D2, datetime(2026, 10, 2, 7), 70.5, waist_cm=80, note="am")
    wid = store.add_weight(D1, datetime(2026, 10, 1, 7), 71.0)
    store.add_weight(D3, datetime(2026, 10, 3, 7), 70.0)
    rows = store.weights_between(D1, D2)
    assert [r["weight_kg"] for r in rows] == [71.0, 70.5]
    assert rows[0]["id"] == wid and rows[0]["waist_cm"] is None and rows[1]["waist_cm"] == 80
    assert rows[1]["note"] == "am" and rows[1]["day"] == "2026-10-02"

@pytest.mark.parametrize("kw", [{"weight_kg": 0}, {"weight_kg": -70}, {"weight_kg": 700}, {"waist_cm": -1}])
def test_add_weight_validation(store, kw):
    args = {"weight_kg": 70.0}
    args.update(kw)
    with pytest.raises(ValueError):
        store.add_weight(D1, datetime(2026, 10, 1, 7), **args)

def test_workouts(store):
    store.add_workout(D1, datetime(2026, 10, 1, 18), "A", description="squat", note="good")
    store.add_workout(D2, datetime(2026, 10, 2, 18), "cardio")
    rows = store.workouts_between(D1, D1)
    assert len(rows) == 1 and rows[0]["kind"] == "A" and rows[0]["description"] == "squat"
    assert [r["kind"] for r in store.workouts_between(D1, D3)] == ["A", "cardio"]

@pytest.mark.parametrize("kind", ["C", "a", "", "run"])
def test_add_workout_rejects_kind(store, kind):
    with pytest.raises(ValueError):
        store.add_workout(D1, datetime(2026, 10, 1, 18), kind)

def test_imported_days(store):
    _food(store, day=D1, note="[imported] x")
    fid = _food(store, day=D2, note="[imported] y")
    store.delete_food(fid)  # a deleted imported row still counts as imported
    _food(store, day=D3, note="manual")
    store.add_weight(D1, datetime(2026, 10, 1, 7), 70, note="[imported]")
    assert store.imported_days("food") == {D1, D2}
    assert store.imported_days("weight") == {D1}
    with pytest.raises(ValueError):
        store.imported_days("workout")

def test_fitness_tables_survive_reopen(tmp_path):
    p = tmp_path / "x.db"
    s = Store(p); _food(s); s.close()
    s2 = Store(p); assert len(s2.foods_for_day(D1)) == 1; s2.close()


# --- jobs: postings and runs ---

from datetime import timedelta

def _posting(**kw):
    p = {"company": "Acme", "role": "SWE Intern", "city": "Montréal", "term": "Winter 2027",
         "deadline": "2026-11-01", "fit": 4, "notes": "Python", "url": "https://acme.example/jobs/1",
         "verified": True, "ats": "workday"}
    p.update(kw)
    return p

def test_upsert_posting_insert_and_get(store):
    pid = store.upsert_posting(_posting())
    p = store.get_posting(pid)
    assert p["company"] == "Acme" and p["fit"] == 4 and p["status"] == "new"
    assert p["verified"] is True and p["first_seen_at"]
    assert store.get_posting(9999) is None

def test_upsert_posting_by_url_keeps_status(store):
    pid = store.upsert_posting(_posting())
    store.set_posting_status(pid, "queued")
    first = store.get_posting(pid)["first_seen_at"]
    pid2 = store.upsert_posting(_posting(fit=5, notes="updated"))
    assert pid2 == pid
    p = store.get_posting(pid)
    assert p["status"] == "queued" and p["fit"] == 5 and p["notes"] == "updated"
    assert p["first_seen_at"] == first
    store.upsert_posting(_posting(status="skipped"))
    assert store.get_posting(pid)["status"] == "skipped"

def test_upsert_posting_validation(store):
    with pytest.raises(ValueError):
        store.upsert_posting(_posting(url=""))
    with pytest.raises(ValueError):
        store.upsert_posting(_posting(status="bogus"))
    with pytest.raises(ValueError):
        store.upsert_posting(_posting(fit=9))
    pid = store.upsert_posting({"url": "https://x.example/2"})
    p = store.get_posting(pid)
    assert p["company"] is None and p["fit"] is None and p["verified"] is None

def test_set_posting_status(store):
    pid = store.upsert_posting(_posting())
    at = datetime(2026, 10, 14, 10, 0)
    store.set_posting_status(pid, "submitted", detail="ok", applied_at=at, follow_up="watch email")
    p = store.get_posting(pid)
    assert p["status"] == "submitted" and p["status_detail"] == "ok"
    assert p["applied_at"] == "2026-10-14T10:00:00" and p["follow_up"] == "watch email"
    with pytest.raises(ValueError):
        store.set_posting_status(pid, "nope")
    with pytest.raises(KeyError):
        store.set_posting_status(4242, "new")

def test_list_postings_filters(store):
    a = store.upsert_posting(_posting(url="u1", fit=5))
    b = store.upsert_posting(_posting(url="u2", fit=3))
    c = store.upsert_posting(_posting(url="u3", fit=None))
    store.set_posting_status(b, "submitted")
    assert {p["id"] for p in store.list_postings()} == {a, b, c}
    assert [p["id"] for p in store.list_postings(status="submitted")] == [b]
    assert [p["id"] for p in store.list_postings(min_fit=4)] == [a]
    assert store.list_postings(since=datetime.now() + timedelta(days=1)) == []
    assert len(store.list_postings(since=datetime.now() - timedelta(days=1))) == 3
    assert len(store.list_postings(limit=2)) == 2
    with pytest.raises(ValueError):
        store.list_postings(status="bad")

def test_count_applied_on(store):
    a = store.upsert_posting(_posting(url="u1"))
    b = store.upsert_posting(_posting(url="u2"))
    c = store.upsert_posting(_posting(url="u3"))
    store.set_posting_status(a, "submitted", applied_at=datetime(2026, 10, 14, 9, 0))
    store.set_posting_status(b, "needs_attestation", applied_at=datetime(2026, 10, 14, 23, 0))
    store.set_posting_status(c, "submitted", applied_at=datetime(2026, 10, 15, 0, 30))
    assert store.count_applied_on(date(2026, 10, 14)) == 2
    assert store.count_applied_on(date(2026, 10, 15)) == 1
    assert store.count_applied_on(date(2026, 10, 13)) == 0

def test_job_runs(store):
    rid = store.add_job_run("scout", datetime(2026, 10, 14, 7, 30))
    store.finish_job_run(rid, datetime(2026, 10, 14, 7, 45), True, "3 new", 0.42)
    row = store.get_job_run(rid)
    assert row["kind"] == "scout" and row["ok"] is True and row["summary"] == "3 new"
    assert row["cost_usd"] == 0.42 and row["finished_at"] == "2026-10-14T07:45:00"
    with pytest.raises(ValueError):
        store.add_job_run("other", datetime(2026, 10, 14))


# --- WAL + busy timeout (M3) ---
import threading as _threading


def test_wal_and_busy_timeout(tmp_path):
    s = Store(tmp_path / "w.db")
    try:
        assert s._conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert s._conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    finally:
        s.close()
    # reopen keeps WAL and data
    s = Store(tmp_path / "w.db")
    try:
        s.set_state("k", "v")
        assert s._conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    finally:
        s.close()
    s = Store(tmp_path / "w.db")
    try:
        assert s.get_state("k") == "v"
    finally:
        s.close()


def test_read_during_open_write_transaction(tmp_path):
    from argus.tools.data import query_readonly
    path = tmp_path / "w.db"
    s = Store(path)
    try:
        s.set_state("a", "1")
        writer = sqlite3.connect(path)
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("INSERT INTO state (key, value) VALUES ('b', '2')")
        # a separate read-only reader and the Store both read while the write is uncommitted
        result = query_readonly(path, "SELECT key FROM state ORDER BY key")
        assert [r[0] for r in result["rows"]] == ["a"]
        assert s.get_state("b") is None
        done = []
        t = _threading.Thread(target=lambda: (s.set_state("c", "3"), done.append(1)))
        t.start()
        writer.commit(); writer.close()  # the Store's write waits (busy_timeout), then succeeds
        t.join(5)
        assert done == [1] and s.get_state("b") == "2" and s.get_state("c") == "3"
    finally:
        s.close()


def test_has_message(store):
    at = datetime(2026, 10, 1, 9, 30)
    store.add_message("user", "hello", at)
    assert store.has_message("user", "hello", at)
    assert not store.has_message("user", "hello", datetime(2026, 10, 1, 9, 31))
    assert not store.has_message("argus", "hello", at)
    assert not store.has_message("user", "hell", at)
