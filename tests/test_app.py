import asyncio
import json
import logging
from datetime import datetime
import pytest
from argus.app import App, SLOW_NOTICE, STOPPED_REPLY, NOTHING_TO_STOP, NOTHING_TO_RETRY
from argus.channel.base import Channel, IncomingMessage, SendError
from argus.session import Interrupted, SessionError
from argus.store import Store

class FakeChannel(Channel):
    def __init__(self): self.inbox, self.sent, self.fail_sends = [], [], 0
    def push(self, *texts):
        for t in texts: self.inbox.append(IncomingMessage(id=t, text=t, received_at=datetime(2026, 10, 1)))
    def poll(self):
        out, self.inbox = self.inbox, []; return out
    def send(self, text):
        if self.fail_sends: self.fail_sends -= 1; raise SendError("x")
        self.sent.append(text)

class FakeBrain:
    def __init__(self): self.asked, self.ats, self.senders, self.gate, self.fail, self.interrupts = [], [], [], None, None, 0
    async def ask(self, text, at=None, sender="user"):
        self.asked.append(text); self.ats.append(at); self.senders.append(sender)
        if self.fail: e, self.fail = self.fail, None; raise e
        if self.gate:
            self._stop = asyncio.Event()
            g = asyncio.ensure_future(self.gate.wait()); s = asyncio.ensure_future(self._stop.wait())
            done, _ = await asyncio.wait({g, s}, return_when=asyncio.FIRST_COMPLETED)
            g.cancel(); s.cancel()
            if s in done: raise Interrupted()
        return f"re:{text}"
    async def interrupt(self):
        self.interrupts += 1
        if getattr(self, "_stop", None): self._stop.set()
    async def close(self): pass

@pytest.fixture
def parts(tmp_path):
    s = Store(tmp_path / "a.db"); ch, br = FakeChannel(), FakeBrain()
    app = App(ch, br, s, poll_interval=0.01, slow_notice_seconds=0.05)
    yield app, ch, br, s
    s.close()

async def run_until_idle(app):
    task = asyncio.create_task(app.run())
    await asyncio.sleep(0.05); await app.drain(); task.cancel()
    with pytest.raises(asyncio.CancelledError): await task

async def test_replies_in_order(parts):
    app, ch, br, _ = parts
    ch.push("a", "b"); await run_until_idle(app)
    assert ch.sent == ["re:a", "re:b"]

async def test_stop_when_idle(parts):
    app, ch, br, _ = parts
    ch.push("停"); await run_until_idle(app)
    assert ch.sent == [NOTHING_TO_STOP] and br.asked == []

async def test_stop_interrupts_and_queue_continues(parts):
    app, ch, br, _ = parts
    br.gate = asyncio.Event()
    task = asyncio.create_task(app.run())
    ch.push("long", "next"); await asyncio.sleep(0.1)   # long is running, slow notice sent
    ch.push("停"); await asyncio.sleep(0.05)
    br.gate.set(); await app.drain(); task.cancel()
    with pytest.raises(asyncio.CancelledError): await task
    assert ch.sent[0] == SLOW_NOTICE and STOPPED_REPLY in ch.sent and ch.sent[-1] == "re:next"

async def test_error_then_retry(parts):
    app, ch, br, s = parts
    br.fail = SessionError("额度用完")
    ch.push("hello"); await run_until_idle(app)
    assert ch.sent == ["出错了：额度用完"] and json.loads(s.get_state("last_failed")) == ["hello"]
    ch.push("重试"); await run_until_idle(app)
    assert ch.sent[-1] == "re:hello" and json.loads(s.get_state("last_failed")) == []

async def test_retry_with_nothing(parts):
    app, ch, _, _ = parts
    ch.push("重试"); await run_until_idle(app)
    assert ch.sent == [NOTHING_TO_RETRY]

async def test_send_failure_does_not_kill_worker(parts):
    app, ch, _, _ = parts
    ch.fail_sends = 1; ch.push("a", "b"); await run_until_idle(app)
    assert ch.sent == ["re:b"]

async def test_unexpected_error_reported(parts):
    app, ch, br, _ = parts
    br.fail = RuntimeError("bug")
    ch.push("x"); await run_until_idle(app)
    assert ch.sent == ["出错了：RuntimeError"]


async def test_passes_send_time_to_brain(parts):
    app, ch, br, _ = parts
    ch.push("a"); await run_until_idle(app)
    assert br.ats == [datetime(2026, 10, 1)]

async def test_restart_answers_pending_messages(parts):
    app, ch, br, s = parts
    old = [IncomingMessage(id="G1", text="first", received_at=datetime(2026, 10, 1, 9)),
           IncomingMessage(id="G2", text="second", received_at=datetime(2026, 10, 1, 10))]
    s.accept_messages(old, "imessage_last_rowid", "2")
    await run_until_idle(app)
    assert ch.sent == ["re:first", "re:second"]
    assert br.ats == [datetime(2026, 10, 1, 9), datetime(2026, 10, 1, 10)]
    assert s.list_pending() == []

async def test_pending_removed_after_error_and_control_words(parts):
    app, ch, br, s = parts
    br.fail = SessionError("x")
    msgs = [IncomingMessage(id="G1", text="boom", received_at=datetime(2026, 10, 1, 9)),
            IncomingMessage(id="G2", text="停", received_at=datetime(2026, 10, 1, 10))]
    s.accept_messages(msgs, "imessage_last_rowid", "2")
    await run_until_idle(app)
    assert s.list_pending() == []

async def test_retry_requeues_all_failures_in_order(parts):
    app, ch, br, s = parts
    br.fail = SessionError("x"); ch.push("one"); await run_until_idle(app)
    br.fail = SessionError("x"); ch.push("two"); await run_until_idle(app)
    assert json.loads(s.get_state("last_failed")) == ["one", "two"]
    ch.push("重试"); await run_until_idle(app)
    assert ch.sent[-2:] == ["re:one", "re:two"] and json.loads(s.get_state("last_failed")) == []

async def test_retry_tolerates_legacy_string(parts):
    app, ch, _, s = parts
    s.set_state("last_failed", "old")
    ch.push("重试"); await run_until_idle(app)
    assert ch.sent == ["re:old"]

async def test_failure_list_is_capped(parts):
    app, ch, br, s = parts
    s.set_state("last_failed", json.dumps([f"m{i}" for i in range(20)]))
    br.fail = SessionError("x"); ch.push("new"); await run_until_idle(app)
    failed = json.loads(s.get_state("last_failed"))
    assert len(failed) == 20 and failed[-1] == "new" and failed[0] == "m1"

async def test_worker_restarted_after_external_cancel(parts):
    app, ch, br, _ = parts
    task = asyncio.create_task(app.run())
    ch.push("a"); await asyncio.sleep(0.05); await app.drain()
    app._worker_task.cancel()
    await asyncio.sleep(0.05)
    ch.push("b"); await asyncio.sleep(0.05)
    await asyncio.wait_for(app.drain(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError): await task
    assert ch.sent == ["re:a", "re:b"]

class FlakyChannel(FakeChannel):
    def __init__(self, failures):
        super().__init__(); self.failures = failures
    def poll(self):
        if self.failures: self.failures -= 1; raise OSError("db locked")
        return super().poll()

async def test_poll_errors_rate_limited_and_notified_once(tmp_path, caplog):
    s = Store(tmp_path / "a.db"); ch, br = FlakyChannel(8), FakeBrain(); notes = []
    app = App(ch, br, s, poll_interval=0.001, slow_notice_seconds=1,
              on_poll_failure=notes.append, poll_failure_notify_after=3, poll_error_log_interval=3600)
    caplog.set_level(logging.INFO, logger="argus.app")
    try:
        ch.push("hi")
        task = asyncio.create_task(app.run())
        for _ in range(200):
            if ch.sent: break
            await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError): await task
    finally:
        s.close()
    assert ch.sent == ["re:hi"]
    errors = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(errors) == 1 and errors[0].exc_info is not None
    assert any("polling recovered" in r.getMessage() for r in caplog.records)
    assert len(notes) == 1


# --- internal tasks ---

async def test_internal_task_answered_and_sent(parts):
    app, ch, br, s = parts
    deleted = []; orig = s.delete_pending
    s.delete_pending = lambda mid: (deleted.append(mid), orig(mid))
    await app.submit_internal("brief")
    await run_until_idle(app)
    assert ch.sent == ["re:brief"] and br.asked == ["brief"] and br.senders == ["system"]
    assert deleted == [] and s.list_pending() == []

async def test_user_messages_still_ask_as_user(parts):
    app, ch, br, _ = parts
    ch.push("a"); await run_until_idle(app)
    assert br.senders == ["user"]

@pytest.mark.parametrize("err", [SessionError("quota"), RuntimeError("bug")])
async def test_internal_task_failure_sends_nothing(parts, err):
    app, ch, br, s = parts
    br.fail = err
    await app.submit_internal("alert")
    await run_until_idle(app)
    assert ch.sent == [] and s.get_state("last_failed") is None
    ch.push("重试"); await run_until_idle(app)
    assert ch.sent == [NOTHING_TO_RETRY]

async def test_internal_failure_keeps_user_failures(parts):
    app, ch, br, s = parts
    br.fail = SessionError("x"); ch.push("mine"); await run_until_idle(app)
    br.fail = SessionError("x"); await app.submit_internal("alert"); await run_until_idle(app)
    assert json.loads(s.get_state("last_failed")) == ["mine"]

async def test_internal_tasks_interleave_fifo(parts):
    app, ch, br, _ = parts
    br.gate = asyncio.Event()
    task = asyncio.create_task(app.run())
    ch.push("a"); await asyncio.sleep(0.03)
    await app.submit_internal("i"); ch.push("b"); await asyncio.sleep(0.03)
    br.gate.set(); await asyncio.wait_for(app.drain(), 2); task.cancel()
    with pytest.raises(asyncio.CancelledError): await task
    assert br.asked == ["a", "i", "b"] and br.senders == ["user", "system", "user"]
    assert [m for m in ch.sent if m != SLOW_NOTICE] == ["re:a", "re:i", "re:b"]

async def test_no_slow_notice_for_internal_task(parts):
    app, ch, br, _ = parts
    br.gate = asyncio.Event()
    task = asyncio.create_task(app.run())
    await app.submit_internal("slow"); await asyncio.sleep(0.15)   # well past slow_notice_seconds
    br.gate.set(); await asyncio.wait_for(app.drain(), 2); task.cancel()
    with pytest.raises(asyncio.CancelledError): await task
    assert ch.sent == ["re:slow"]

async def test_stop_interrupts_internal_task(parts):
    app, ch, br, _ = parts
    br.gate = asyncio.Event()
    task = asyncio.create_task(app.run())
    await app.submit_internal("long"); await asyncio.sleep(0.05)
    ch.push("停"); await asyncio.sleep(0.05)
    await asyncio.wait_for(app.drain(), 2); task.cancel()
    with pytest.raises(asyncio.CancelledError): await task
    assert br.interrupts == 1 and ch.sent == [STOPPED_REPLY]
