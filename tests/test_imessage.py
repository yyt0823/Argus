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

# --- pending queue / restart / loop guard / circuit breaker ---

class Clock:
    def __init__(self): self.t = 1000.0
    def __call__(self): return self.t

def ok_runner(args):
    return subprocess.CompletedProcess(args, 0, "", "")

def test_poll_writes_pending_and_checkpoint(fake_chat_db, store):
    c = chan(fake_chat_db, store); c.poll()
    g = fake_chat_db.add("remember me", 0, T0)
    msgs = c.poll()
    assert store.list_pending() == msgs and msgs[0].id == g
    assert store.get_state("imessage_last_rowid") == str(fake_chat_db.n)

def test_poll_moves_checkpoint_when_everything_filtered(fake_chat_db, store):
    c = chan(fake_chat_db, store); c.poll()
    fake_chat_db.add("🤖 bot", 0, T0)
    assert c.poll() == []
    assert store.get_state("imessage_last_rowid") == str(fake_chat_db.n) and store.list_pending() == []

def test_restart_between_sent_and_echo_rows_no_duplicate(fake_chat_db, store):
    c = chan(fake_chat_db, store); c.poll()
    fake_chat_db.add("hello", 1, T0, as_blob=True)
    assert [m.text for m in c.poll()] == ["hello"]
    c2 = chan(fake_chat_db, store)  # simulated restart
    fake_chat_db.add("hello", 0, T0)
    assert c2.poll() == []

def test_restart_rebuild_keeps_already_paired_rows_paired(fake_chat_db, store):
    c = chan(fake_chat_db, store); c.poll()
    fake_chat_db.add("ok", 1, T0, as_blob=True)
    fake_chat_db.add("ok", 0, T0)
    assert [m.text for m in c.poll()] == ["ok"]
    c2 = chan(fake_chat_db, store)
    fake_chat_db.add("ok", 1, T0 + timedelta(seconds=3), as_blob=True)  # genuinely new message
    assert [m.text for m in c2.poll()] == ["ok"]

def test_restart_rebuild_ignores_old_rows(fake_chat_db, store):
    c = chan(fake_chat_db, store); c.poll()
    fake_chat_db.add("hello", 1, T0 - timedelta(seconds=120), as_blob=True)
    fake_chat_db.add("other", 1, T0, as_blob=True)
    c.poll()
    c2 = chan(fake_chat_db, store)
    fake_chat_db.add("hello", 0, T0)
    assert [m.text for m in c2.poll()] == ["hello"]

@pytest.mark.parametrize("echo", [PREFIX + "loop?", "loop?", "🤖loop?"], ids=["full", "stripped", "nospace"])
def test_recent_sent_text_echo_dropped(fake_chat_db, store, echo):
    c = chan(fake_chat_db, store, ok_runner); c.poll()
    c.send("loop?")
    fake_chat_db.add(echo, 0, T0)
    assert c.poll() == []

def test_recent_sent_text_expires(fake_chat_db, store):
    clock = Clock()
    c = IMessageChannel(fake_chat_db.path, ["+15550000000"], store, runner=ok_runner,
                        sleep=lambda s: None, clock=clock)
    c.poll()
    c.send("loop?")
    clock.t += 61
    fake_chat_db.add("loop?", 0, T0)
    assert [m.text for m in c.poll()] == ["loop?"]

def test_circuit_breaker(fake_chat_db, store):
    clock = Clock()
    calls = []
    def runner(args):
        calls.append(args); return subprocess.CompletedProcess(args, 0, "", "")
    c = IMessageChannel(fake_chat_db.path, ["+15550000000"], store, runner=runner,
                        sleep=lambda s: None, clock=clock)
    for _ in range(20):
        c.send("x")
    with pytest.raises(SendError, match="rate limit"):
        c.send("x")
    assert len(calls) == 20
    clock.t += 61
    c.send("x")
    assert len(calls) == 21

# --- contact mode (Messages signed into a dedicated bot Apple ID) ---

OWNER_PHONE = "+15550000000"
OWNER_EMAIL = "owner@example.com"

def cchan(db, store, runner=None, handles=(OWNER_PHONE,)):
    return IMessageChannel(db.path, list(handles), store, runner=runner, sleep=lambda s: None, mode="contact")

def test_invalid_mode_rejected(fake_chat_db, store):
    with pytest.raises(ValueError, match="mode"):
        IMessageChannel(fake_chat_db.path, [OWNER_PHONE], store, mode="bot")

def test_contact_inbound_accepted_once(fake_chat_db, store):
    c = cchan(fake_chat_db, store); c.poll()
    g = fake_chat_db.add("中午吃了牛肉饭", 0, T0, as_blob=True)
    msgs = c.poll()
    assert [(m.id, m.text) for m in msgs] == [(g, "中午吃了牛肉饭")]
    assert store.list_pending() == msgs
    assert c.poll() == []

def test_contact_same_text_twice_is_two_messages(fake_chat_db, store):
    c = cchan(fake_chat_db, store); c.poll()
    fake_chat_db.add("ok", 0, T0)
    fake_chat_db.add("ok", 0, T0 + timedelta(seconds=1))
    assert [m.text for m in c.poll()] == ["ok", "ok"]

def test_contact_from_me_rows_ignored(fake_chat_db, store):
    c = cchan(fake_chat_db, store); c.poll()
    fake_chat_db.add("🤖 reply", 1, T0)
    fake_chat_db.add("reply without prefix", 1, T0)
    fake_chat_db.add("someone else", 0, T0, chat_id=2)
    fake_chat_db.add("tapback", 0, T0, item_type=1)
    fake_chat_db.add("", 0, T0)
    assert c.poll() == []
    assert store.get_state("imessage_last_rowid") == str(fake_chat_db.n)

def test_contact_first_poll_ignores_history_and_restart(fake_chat_db, store):
    fake_chat_db.add("old", 0, T0)
    c = cchan(fake_chat_db, store)
    assert c.poll() == []
    fake_chat_db.add("new", 0, T0)
    assert [m.text for m in c.poll()] == ["new"]
    assert cchan(fake_chat_db, store).poll() == []

def test_contact_send_before_inbound_uses_first_handle(fake_chat_db, store):
    calls = []
    def runner(args):
        calls.append(args); return subprocess.CompletedProcess(args, 0, "", "")
    fake_chat_db.add_chat("iMessage;-;" + OWNER_EMAIL)
    c = cchan(fake_chat_db, store, runner, handles=(OWNER_EMAIL, OWNER_PHONE)); c.poll()
    c.send("hi")
    assert calls == [["osascript", "-e", SEND_SCRIPT, PREFIX + "hi", "iMessage;-;" + OWNER_EMAIL]]

def test_contact_reply_goes_to_latest_inbound_chat(fake_chat_db, store):
    calls = []
    def runner(args):
        calls.append(args[4]); return subprocess.CompletedProcess(args, 0, "", "")
    email_chat = fake_chat_db.add_chat("iMessage;-;" + OWNER_EMAIL)
    c = cchan(fake_chat_db, store, runner, handles=(OWNER_PHONE, OWNER_EMAIL)); c.poll()
    fake_chat_db.add("from email", 0, T0, chat_id=email_chat)
    assert [m.text for m in c.poll()] == ["from email"]
    c.send("a")
    fake_chat_db.add("from phone", 0, T0, chat_id=1)
    assert [m.text for m in c.poll()] == ["from phone"]
    c.send("b")
    assert calls == ["iMessage;-;" + OWNER_EMAIL, "iMessage;-;" + OWNER_PHONE]

def test_contact_reply_chat_persists_across_restart(fake_chat_db, store):
    calls = []
    def runner(args):
        calls.append(args[4]); return subprocess.CompletedProcess(args, 0, "", "")
    email_chat = fake_chat_db.add_chat("iMessage;-;" + OWNER_EMAIL)
    c = cchan(fake_chat_db, store, runner, handles=(OWNER_PHONE, OWNER_EMAIL)); c.poll()
    fake_chat_db.add("hi", 0, T0, chat_id=email_chat)
    c.poll()
    assert store.get_state("imessage_reply_chat") == "iMessage;-;" + OWNER_EMAIL
    c2 = cchan(fake_chat_db, store, runner, handles=(OWNER_PHONE, OWNER_EMAIL))
    c2.send("back")
    assert calls == ["iMessage;-;" + OWNER_EMAIL]

def test_contact_stale_reply_chat_falls_back_to_first_handle(fake_chat_db, store):
    calls = []
    def runner(args):
        calls.append(args[4]); return subprocess.CompletedProcess(args, 0, "", "")
    store.set_state("imessage_reply_chat", "iMessage;-;" + OWNER_EMAIL)  # handle since removed from config
    cchan(fake_chat_db, store, runner).send("x")
    assert calls == ["iMessage;-;" + OWNER_PHONE]

def test_contact_send_does_not_capture_guid_or_drop_inbound(fake_chat_db, store):
    def runner(args):
        fake_chat_db.add(args[3], 1, T0)
        return subprocess.CompletedProcess(args, 0, "", "")
    c = cchan(fake_chat_db, store, runner); c.poll()
    c.send("loop?")
    assert not store.has_sent_id(f"G{fake_chat_db.n}")
    fake_chat_db.add("loop?", 0, T0)   # owner may legitimately send the same text
    assert [m.text for m in c.poll()] == ["loop?"]

def test_contact_send_retries_and_rate_limit(fake_chat_db, store):
    n = []
    def bad(args):
        n.append(1); return subprocess.CompletedProcess(args, 1, "", "boom")
    with pytest.raises(SendError):
        cchan(fake_chat_db, store, bad).send("x")
    assert len(n) == 3
    clock = Clock()
    c = IMessageChannel(fake_chat_db.path, [OWNER_PHONE], store, runner=ok_runner,
                        sleep=lambda s: None, clock=clock, mode="contact")
    for _ in range(20):
        c.send("x")
    with pytest.raises(SendError, match="rate limit"):
        c.send("x")
