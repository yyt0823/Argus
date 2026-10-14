import asyncio
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
    def __init__(self): self.asked, self.gate, self.fail, self.interrupts = [], None, None, 0
    async def ask(self, text):
        self.asked.append(text)
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
    assert ch.sent == ["出错了：额度用完"] and s.get_state("last_failed") == "hello"
    ch.push("重试"); await run_until_idle(app)
    assert ch.sent[-1] == "re:hello" and not s.get_state("last_failed")

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
