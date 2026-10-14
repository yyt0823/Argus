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
