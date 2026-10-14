import subprocess
from datetime import datetime, timedelta
import pytest
from argus.channel.base import SendError
from argus.channel.imessage import IMessageChannel, decode_attributed_body, PREFIX, SEND_SCRIPT
from argus.store import Store
from tests.conftest import make_attributed_body

T0 = datetime(2026, 10, 13, 9, 0, 0).astimezone()

@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "a.db"); yield s; s.close()

def chan(db, store, runner=None):
    return IMessageChannel(db.path, ["+15550000000"], store, runner=runner, sleep=lambda s: None)

@pytest.mark.parametrize("text", ["hi", "x" * 200, "x" * 70000, "引号\"\\🤖\né"])
def test_decode_roundtrip(text):
    assert decode_attributed_body(make_attributed_body(text)) == text

def test_decode_none():
    assert decode_attributed_body(None) is None and decode_attributed_body(b"garbage") is None

def test_first_poll_sets_checkpoint_and_ignores_history(fake_chat_db, store):
    fake_chat_db.add("old message", 0, T0)
    c = chan(fake_chat_db, store)
    assert c.poll() == []
    fake_chat_db.add("new", 0, T0 + timedelta(seconds=5))
    assert [m.text for m in c.poll()] == ["new"]

def test_pair_dedupe_sent_then_echo(fake_chat_db, store):
    c = chan(fake_chat_db, store); c.poll()
    fake_chat_db.add("中午吃了牛肉饭", 1, T0, as_blob=True)
    fake_chat_db.add("中午吃了牛肉饭", 0, T0 - timedelta(milliseconds=100))
    assert [m.text for m in c.poll()] == ["中午吃了牛肉饭"]

def test_pair_dedupe_across_polls(fake_chat_db, store):
    c = chan(fake_chat_db, store); c.poll()
    fake_chat_db.add("hello", 1, T0, as_blob=True)
    assert [m.text for m in c.poll()] == ["hello"]
    fake_chat_db.add("hello", 0, T0)
    assert c.poll() == []

def test_same_text_twice_is_two_messages(fake_chat_db, store):
    c = chan(fake_chat_db, store); c.poll()
    for dt in (0, 3):
        fake_chat_db.add("ok", 1, T0 + timedelta(seconds=dt), as_blob=True)
        fake_chat_db.add("ok", 0, T0 + timedelta(seconds=dt))
    assert [m.text for m in c.poll()] == ["ok", "ok"]

def test_single_row_without_echo_still_delivered(fake_chat_db, store):
    c = chan(fake_chat_db, store); c.poll()
    fake_chat_db.add("only one", 1, T0, as_blob=True)
    assert [m.text for m in c.poll()] == ["only one"]

def test_robot_prefix_and_sent_ids_skipped(fake_chat_db, store):
    c = chan(fake_chat_db, store); c.poll()
    fake_chat_db.add("🤖 reply", 1, T0, as_blob=True)
    fake_chat_db.add("🤖 reply", 0, T0)
    g = fake_chat_db.add("weird", 1, T0)
    store.add_sent_id(g, datetime.now())
    assert c.poll() == []

def test_other_chats_and_reactions_ignored(fake_chat_db, store):
    c = chan(fake_chat_db, store); c.poll()
    fake_chat_db.add("someone else", 0, T0, chat_id=2)
    fake_chat_db.add("tapback", 0, T0, item_type=1)
    assert c.poll() == []

def test_checkpoint_survives_restart(fake_chat_db, store):
    c = chan(fake_chat_db, store); c.poll()
    fake_chat_db.add("a", 0, T0)
    assert [m.text for m in c.poll()] == ["a"]
    c2 = chan(fake_chat_db, store)
    assert c2.poll() == []

def test_send_uses_argv_and_records_guid(fake_chat_db, store):
    calls = []
    def runner(args):
        calls.append(args)
        fake_chat_db.add(args[3], 1, T0, as_blob=True)
        return subprocess.CompletedProcess(args, 0, "", "")
    c = chan(fake_chat_db, store, runner); c.poll()
    c.send("好的")
    assert calls == [["osascript", "-e", SEND_SCRIPT, PREFIX + "好的", "iMessage;-;+15550000000"]]
    assert store.has_sent_id(f"G{fake_chat_db.n}")
    assert c.poll() == []   # our own message is not delivered back

def test_send_retries_then_raises(fake_chat_db, store):
    n = []
    def runner(args):
        n.append(1); return subprocess.CompletedProcess(args, 1, "", "boom")
    c = chan(fake_chat_db, store, runner)
    with pytest.raises(SendError):
        c.send("x")
    assert len(n) == 3

def test_send_timeout_counts_as_failure(fake_chat_db, store):
    def runner(args): raise subprocess.TimeoutExpired(args, 30)
    with pytest.raises(SendError):
        chan(fake_chat_db, store, runner).send("x")
