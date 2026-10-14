# tests/test_brain.py
from datetime import date, datetime
import pytest
from argus.brain import Brain
from argus.session import SessionError
from argus.store import Store

class FakeSession:
    def __init__(self, resume, log):
        self.sdk_session_id, self.resume, self.log, self.closed = resume, resume, log, False
    async def ask(self, text):
        self.log.append(("ask", self.resume, text))
        if self.sdk_session_id is None:
            self.sdk_session_id = f"sdk-{len(self.log)}"
        return f"re:{text}"
    async def interrupt(self): self.log.append(("interrupt",))
    async def close(self): self.closed = True; self.log.append(("close", self.sdk_session_id))

class Clock:
    def __init__(self, t): self.t = t
    def __call__(self): return self.t

@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "a.db"); yield s; s.close()

def factory(log, fail_resume=False):
    async def f(resume):
        if resume and fail_resume:
            raise SessionError("cannot resume")
        log.append(("new", resume))
        return FakeSession(resume, log)
    return f

async def test_ask_archives_and_saves_sdk_id(store):
    log = []; clock = Clock(datetime(2026, 10, 1, 9))
    b = Brain(store, factory(log), now=clock)
    assert await b.ask("hi") == "re:[2026-10-01 09:00] hi"
    row = store.get_session(date(2026, 10, 1))
    assert row["sdk_session_id"].startswith("sdk-")
    assert [h["sender"] for h in store.search_messages("hi")] == ["argus", "user"]

async def test_same_day_reuses_session(store):
    log = []; clock = Clock(datetime(2026, 10, 1, 9))
    b = Brain(store, factory(log), now=clock)
    await b.ask("a"); clock.t = datetime(2026, 10, 2, 3, 59); await b.ask("b")
    assert [e for e in log if e[0] == "new"] == [("new", None)]

async def test_rollover_at_4am(store):
    log, rolled = [], []
    async def on_rollover(d): rolled.append(d)
    clock = Clock(datetime(2026, 10, 1, 9))
    b = Brain(store, factory(log), now=clock, on_rollover=on_rollover)
    await b.ask("a"); clock.t = datetime(2026, 10, 2, 4, 0); await b.ask("b")
    assert rolled == [date(2026, 10, 2)]
    assert store.get_session(date(2026, 10, 1))["ended_at"] is not None
    assert [e for e in log if e[0] == "new"] == [("new", None), ("new", None)]
    assert any(e[0] == "close" for e in log)

async def test_restart_resumes_today(store):
    log = []; clock = Clock(datetime(2026, 10, 1, 9))
    b1 = Brain(store, factory(log), now=clock); await b1.ask("a"); await b1.close()
    sdk_id = store.get_session(date(2026, 10, 1))["sdk_session_id"]
    b2 = Brain(store, factory(log), now=clock); await b2.ask("b")
    assert ("new", sdk_id) in log

async def test_resume_failure_falls_back_to_fresh(store):
    log = []; clock = Clock(datetime(2026, 10, 1, 9))
    b1 = Brain(store, factory(log), now=clock); await b1.ask("a"); await b1.close()
    b2 = Brain(store, factory(log, fail_resume=True), now=clock)
    assert await b2.ask("b") == "re:[2026-10-01 09:00] b"

async def test_user_message_archived_even_if_session_fails(store):
    class Boom(FakeSession):
        async def ask(self, text): raise SessionError("quota")
    async def f(resume): return Boom(resume, [])
    b = Brain(store, f, now=Clock(datetime(2026, 10, 1, 9)))
    with pytest.raises(SessionError):
        await b.ask("lost?")
    assert [h["sender"] for h in store.search_messages("lost?")] == ["user"]

async def test_interrupt_without_session_is_noop(store):
    b = Brain(store, factory([]), now=Clock(datetime(2026, 10, 1, 9)))
    await b.interrupt()


async def test_session_sees_current_time_but_archive_does_not(store):
    log = []; clock = Clock(datetime(2026, 10, 1, 9, 5))
    b = Brain(store, factory(log), now=clock)
    await b.ask("几点了")
    assert ("ask", None, "[2026-10-01 09:05] 几点了") in log
    assert store.search_messages("几点了")[-1]["content"] == "几点了"


# --- review fixes ---
import asyncio
from argus.session import Interrupted


async def test_failed_session_is_replaced_by_resumed_one(store):
    log = []; clock = Clock(datetime(2026, 10, 1, 9))
    sessions = []
    fail = {"next": False}
    class Flaky(FakeSession):
        async def ask(self, text):
            if fail["next"]:
                fail["next"] = False
                raise SessionError("process died")
            return await super().ask(text)
    async def f(resume):
        log.append(("new", resume))
        sessions.append(Flaky(resume, log)); return sessions[-1]
    b = Brain(store, f, now=clock)
    await b.ask("a")
    sdk_id = store.get_session(date(2026, 10, 1))["sdk_session_id"]
    fail["next"] = True
    with pytest.raises(SessionError, match="process died"):
        await b.ask("b")
    assert sessions[0].closed
    assert await b.ask("c") == "re:[2026-10-01 09:00] c"
    assert [e for e in log if e[0] == "new"] == [("new", None), ("new", sdk_id)]
    assert len(sessions) == 2
    # same day row kept
    assert store.get_session(date(2026, 10, 1))["ended_at"] is None


async def test_failed_session_close_error_is_swallowed(store):
    class Boom(FakeSession):
        async def ask(self, text): raise RuntimeError("weird")
        async def close(self): raise RuntimeError("close failed")
    made = []
    async def f(resume):
        made.append(resume); return Boom(resume, [])
    b = Brain(store, f, now=Clock(datetime(2026, 10, 1, 9)))
    with pytest.raises(RuntimeError, match="weird"):
        await b.ask("x")
    with pytest.raises(RuntimeError, match="weird"):
        await b.ask("y")
    assert len(made) == 2


async def test_interrupted_keeps_session(store):
    made = []
    class Stopped(FakeSession):
        async def ask(self, text): raise Interrupted()
    async def f(resume):
        made.append(resume); return Stopped(resume, [])
    b = Brain(store, f, now=Clock(datetime(2026, 10, 1, 9)))
    for _ in range(2):
        with pytest.raises(Interrupted):
            await b.ask("x")
    assert len(made) == 1


async def test_ask_uses_send_time_for_stamp_and_user_archive(store):
    log = []; clock = Clock(datetime(2026, 10, 1, 9, 30))
    b = Brain(store, factory(log), now=clock)
    await b.ask("delayed msg", at=datetime(2026, 10, 1, 9, 12))
    assert ("ask", None, "[2026-10-01 09:12] delayed msg") in log
    hits = {h["sender"]: h["ts"] for h in store.search_messages("delayed msg")}
    assert hits["user"] == "2026-10-01T09:12:00"
    assert hits["argus"] == "2026-10-01T09:30:00"


async def test_ask_day_selection_uses_now_not_send_time(store):
    log = []; clock = Clock(datetime(2026, 10, 2, 4, 5))
    b = Brain(store, factory(log), now=clock)
    await b.ask("late", at=datetime(2026, 10, 2, 3, 55))
    assert store.get_session(date(2026, 10, 2)) is not None
    assert store.get_session(date(2026, 10, 1)) is None


async def test_interrupt_during_session_creation_is_honored(store):
    log = []; gate = asyncio.Event(); entered = asyncio.Event()
    async def slow_factory(resume):
        entered.set(); await gate.wait()
        return FakeSession(resume, log)
    b = Brain(store, slow_factory, now=Clock(datetime(2026, 10, 1, 9)))
    task = asyncio.create_task(b.ask("stop me"))
    await entered.wait()
    await b.interrupt()
    gate.set()
    with pytest.raises(Interrupted):
        await task
    assert not any(e[0] == "ask" for e in log)
    # user message still archived
    assert [h["sender"] for h in store.search_messages("stop me")] == ["user"]
    # flag cleared: next ask works on the same session
    assert await b.ask("again") == "re:[2026-10-01 09:00] again"


async def test_idle_interrupt_does_not_affect_next_ask(store):
    b = Brain(store, factory([]), now=Clock(datetime(2026, 10, 1, 9)))
    await b.interrupt()
    assert await b.ask("ok") == "re:[2026-10-01 09:00] ok"


async def test_rollover_after_failed_session_still_ends_old_day(store):
    log, rolled = [], []
    async def on_rollover(d): rolled.append(d)
    fail = {"next": False}
    class Flaky(FakeSession):
        async def ask(self, text):
            if fail["next"]:
                fail["next"] = False; raise SessionError("died")
            return await super().ask(text)
    async def f(resume):
        log.append(("new", resume)); return Flaky(resume, log)
    clock = Clock(datetime(2026, 10, 1, 9))
    b = Brain(store, f, now=clock, on_rollover=on_rollover)
    await b.ask("a"); fail["next"] = True
    with pytest.raises(SessionError):
        await b.ask("b")
    clock.t = datetime(2026, 10, 2, 9)
    await b.ask("c")
    assert rolled == [date(2026, 10, 2)]
    assert store.get_session(date(2026, 10, 1))["ended_at"] is not None
    assert [e for e in log if e[0] == "new"][-1] == ("new", None)
