import asyncio
from datetime import datetime, time, timedelta

import pytest

from argus.config import Config, JobsConfig
from argus.jobs.applier import ApplyResult
from argus.jobs.scheduler import JobScheduler
from argus.jobs.scout import ScoutResult
from argus.mail.watcher import INTERNAL_PREFIX
from argus.store import Store

DAY = datetime(2026, 10, 14)


class Clock:
    def __init__(self, at):
        self.at = at

    def __call__(self):
        return self.at


class FakeScout:
    def __init__(self):
        self.calls = []
        self.results: list[ScoutResult] = []
        self.gate: asyncio.Event | None = None
        self.raise_exc: Exception | None = None

    async def __call__(self, cfg, today=None):
        self.calls.append(today)
        if self.gate is not None:
            await self.gate.wait()
        if self.raise_exc:
            raise self.raise_exc
        return self.results.pop(0) if self.results else ScoutResult(ok=True, summary="nothing")


class FakeApply:
    def __init__(self):
        self.calls: list[dict] = []
        self.results: dict[str, list[ApplyResult]] = {}  # url -> results in order
        self.default = ApplyResult(result="submitted", detail="done", cost_usd=0.5)
        self.gate: asyncio.Event | None = None
        self.started = asyncio.Event()

    async def __call__(self, cfg, posting):
        self.calls.append(posting)
        self.started.set()
        if self.gate is not None:
            await self.gate.wait()
        queue = self.results.get(posting["url"])
        return queue.pop(0) if queue else self.default


class Env:
    def __init__(self, tmp_path, at=DAY.replace(hour=9), **jobs_kw):
        self.cfg = Config(root=tmp_path, self_handles=("+1",), jobs=JobsConfig(**jobs_kw))
        self.store = Store(tmp_path / "argus.db")
        self.clock = Clock(at)
        self.sent: list[str] = []
        self.scout = FakeScout()
        self.apply = FakeApply()

        async def submit(text):
            self.sent.append(text)

        self.sched = JobScheduler(self.cfg, self.store, submit, scout_fn=self.scout,
                                  apply_fn=self.apply, now=self.clock)

    def add(self, url, fit=4, status=None, seen=None, company="Acme", role="SWE Intern", detail=None):
        pid = self.store.upsert_posting({"url": url, "fit": fit, "company": company, "role": role,
                                         "city": "Montréal", "term": "Summer 2027"})
        seen = seen or self.clock()
        self.store._write("UPDATE job_postings SET first_seen_at = ? WHERE id = ?",
                          (seen.isoformat(timespec="seconds"), pid))
        if status:
            self.store.set_posting_status(pid, status, detail=detail)
        return pid

    def status(self, pid):
        return self.store.get_posting(pid)["status"]

    def runs(self, kind):
        rows = self.store._read("SELECT * FROM job_runs WHERE kind = ? ORDER BY id", (kind,))
        return [dict(r) for r in rows]

    async def step(self):
        await self.sched.tick()
        await self.sched.wait_idle()


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path)
    yield e
    e.store.close()


def make_env(tmp_path, **kw):
    return Env(tmp_path, **kw)


# --- daily scout ---

async def test_scout_runs_once_per_day_at_scout_time(tmp_path):
    env = make_env(tmp_path, at=DAY.replace(hour=7, minute=29), auto_apply=False)
    await env.step()
    assert env.scout.calls == []
    env.clock.at = DAY.replace(hour=7, minute=30)
    await env.step()
    assert env.scout.calls == [DAY.date()]
    env.clock.at = DAY.replace(hour=12)
    await env.step()
    assert len(env.scout.calls) == 1
    assert env.store.get_state("jobs_last_scout") == "2026-10-14"
    env.clock.at = DAY.replace(hour=7, minute=31) + timedelta(days=1)
    await env.step()
    assert len(env.scout.calls) == 2
    env.store.close()


async def test_scout_catches_up_when_started_later_in_the_day(tmp_path):
    env = make_env(tmp_path, at=DAY.replace(hour=15), auto_apply=False)
    await env.step()
    assert env.scout.calls == [DAY.date()]
    await env.step()
    assert len(env.scout.calls) == 1
    env.store.close()


async def test_scout_stores_postings_and_records_run(env):
    env.clock.at = DAY.replace(hour=7, minute=30)
    env.scout.results.append(ScoutResult(
        ok=True, summary="found 2", cost_usd=1.25,
        postings=[{"company": "A", "role": "SWE", "url": "https://a/1", "fit": 5},
                  {"company": "B", "role": "ML", "url": "https://b/2", "fit": 2}]))
    await env.step()
    rows = env.store.list_postings()
    assert {r["url"] for r in rows} == {"https://a/1", "https://b/2"}
    assert all(r["status"] == "new" for r in rows)
    (run,) = env.runs("scout")
    assert run["ok"] == 1 and run["cost_usd"] == 1.25 and run["finished_at"] and "found 2" in run["summary"]
    assert env.sent == []  # the daily scout reports through the briefing, not an alert


async def test_scout_does_not_reset_existing_posting_status(env):
    pid = env.add("https://a/1", status="submitted")
    env.clock.at = DAY.replace(hour=7, minute=30)
    env.scout.results.append(ScoutResult(ok=True, postings=[{"company": "A", "url": "https://a/1", "fit": 5}]))
    await env.step()
    assert env.status(pid) == "submitted"


async def test_failed_scout_records_failed_run(env):
    env.clock.at = DAY.replace(hour=7, minute=30)
    env.scout.results.append(ScoutResult(ok=False, error="no JSON", cost_usd=0.3))
    await env.step()
    (run,) = env.runs("scout")
    assert run["ok"] == 0 and "no JSON" in run["summary"]


async def test_scout_exception_is_logged_not_fatal(env):
    env.clock.at = DAY.replace(hour=7, minute=30)
    env.scout.raise_exc = RuntimeError("boom")
    await env.step()
    (run,) = env.runs("scout")
    assert run["ok"] == 0
    assert not env.sched.scouting


# --- request_scout ---

async def test_request_scout_reports_new_postings(env):
    env.scout.results.append(ScoutResult(ok=True, summary="s", postings=[
        {"company": "Acme", "role": "Backend Intern", "city": "Montréal", "term": "Summer 2027",
         "deadline": "2026-11-01", "fit": 5, "url": "https://acme/1"}]))
    env.store.set_state("jobs_last_scout", DAY.date().isoformat())
    assert env.sched.request_scout() is True
    assert env.sched.scouting
    assert env.sched.request_scout() is False  # one at a time
    await env.sched.wait_idle()
    assert len(env.scout.calls) == 1
    (msg,) = env.sent
    assert msg.startswith(INTERNAL_PREFIX)
    for part in ("Acme", "Backend Intern", "Montréal", "Summer 2027", "2026-11-01", "fit 5", "https://acme/1"):
        assert part in msg
    pid = env.store.list_postings()[0]["id"]
    assert f"#{pid}" in msg


async def test_request_scout_reports_nothing_new_and_failures(env):
    env.scout.results.append(ScoutResult(ok=True, summary="s", postings=[]))
    env.sched.request_scout()
    await env.sched.wait_idle()
    assert "no new" in env.sent[-1].lower()
    env.scout.results.append(ScoutResult(ok=False, error="timeout"))
    env.sched.request_scout()
    await env.sched.wait_idle()
    assert "failed" in env.sent[-1].lower() and "timeout" in env.sent[-1]


async def test_request_during_daily_scout_reports_when_done(env):
    env.clock.at = DAY.replace(hour=7, minute=30)
    env.scout.gate = asyncio.Event()
    await env.sched.tick()
    assert env.sched.scouting
    assert env.sched.request_scout() is False
    env.scout.gate.set()
    await env.sched.wait_idle()
    assert len(env.scout.calls) == 1 and len(env.sent) == 1


# --- auto queueing ---

async def test_auto_apply_queues_by_fit_and_age(tmp_path):
    env = make_env(tmp_path, at=DAY.replace(hour=7), daily_apply_limit=0)
    env.store.set_state("jobs_last_scout", DAY.date().isoformat())
    good = env.add("https://g", fit=4)
    great = env.add("https://gg", fit=5)
    low = env.add("https://l", fit=3)
    nofit = env.add("https://n", fit=None)
    old = env.add("https://o", fit=5, seen=DAY - timedelta(days=8))
    skipped = env.add("https://s", fit=5, status="skipped")
    await env.step()
    assert env.status(good) == env.status(great) == "queued"
    assert env.status(low) == env.status(nofit) == env.status(old) == "new"
    assert env.status(skipped) == "skipped"
    env.store.close()


async def test_no_auto_queue_when_auto_apply_off(tmp_path):
    env = make_env(tmp_path, auto_apply=False)
    env.store.set_state("jobs_last_scout", DAY.date().isoformat())
    pid = env.add("https://g", fit=5)
    await env.step()
    assert env.status(pid) == "new" and env.apply.calls == []
    env.store.close()


# --- applying ---

def _scouted(env):
    env.store.set_state("jobs_last_scout", env.clock().date().isoformat())


async def test_apply_only_inside_window(tmp_path):
    env = make_env(tmp_path, at=DAY.replace(hour=8, minute=29))
    _scouted(env)
    pid = env.add("https://g", fit=5)
    await env.step()
    assert env.status(pid) == "queued" and env.apply.calls == []
    env.clock.at = DAY.replace(hour=22)
    await env.step()
    assert env.apply.calls == []
    env.clock.at = DAY.replace(hour=8, minute=30)
    await env.step()
    assert [p["url"] for p in env.apply.calls] == ["https://g"]
    env.store.close()


async def test_submitted_sets_applied_at_and_records_run(env):
    _scouted(env)
    pid = env.add("https://g", fit=5)
    env.apply.results["https://g"] = [ApplyResult(result="submitted", detail="ok", follow_up="OA soon",
                                                 guessed_answers=["salary: 0"], cost_usd=2.0)]
    await env.step()
    p = env.store.get_posting(pid)
    assert p["status"] == "submitted" and p["applied_at"].startswith("2026-10-14T09:00")
    assert p["follow_up"] == "OA soon" and "salary: 0" in p["status_detail"]
    (run,) = env.runs("apply")
    assert run["ok"] == 1 and run["cost_usd"] == 2.0 and run["finished_at"]
    assert env.sent == []


async def test_daily_limit(tmp_path):
    env = make_env(tmp_path, daily_apply_limit=2)
    _scouted(env)
    ids = [env.add(f"https://p{i}", fit=5) for i in range(3)]
    for _ in range(4):
        await env.step()
    assert len(env.apply.calls) == 2
    assert sorted(env.status(i) for i in ids) == ["queued", "submitted", "submitted"]
    env.clock.at = DAY.replace(hour=9) + timedelta(days=1)
    _scouted(env)
    await env.step()
    assert len(env.apply.calls) == 3
    env.store.close()


async def test_one_application_at_a_time(env):
    _scouted(env)
    a = env.add("https://a", fit=5, seen=DAY.replace(hour=1))
    b = env.add("https://b", fit=5, seen=DAY.replace(hour=2))
    env.apply.gate = asyncio.Event()
    await env.sched.tick()
    await asyncio.wait_for(env.apply.started.wait(), 2)
    assert env.sched.applying and env.status(a) == "applying"
    await env.sched.tick()
    await env.sched.tick()
    assert len(env.apply.calls) == 1 and env.status(b) == "queued"
    env.apply.gate.set()
    await env.sched.wait_idle()
    assert not env.sched.applying and env.status(a) == "submitted"
    await env.step()
    assert [p["url"] for p in env.apply.calls] == ["https://a", "https://b"]


async def test_oldest_queued_first_and_requested_has_priority(env):
    _scouted(env)
    old = env.add("https://old", fit=4, seen=DAY - timedelta(days=2))
    new = env.add("https://new", fit=5, seen=DAY - timedelta(days=1))
    req = env.add("https://req", fit=1, seen=DAY - timedelta(days=1))
    env.sched.request_apply(req)
    assert env.store.get_posting(req)["status"] == "queued"
    for _ in range(3):
        await env.step()
    assert [p["url"] for p in env.apply.calls] == ["https://req", "https://old", "https://new"]
    assert env.status(old) == env.status(new) == "submitted"


async def test_requested_applies_even_with_auto_apply_off_and_limit_reached(tmp_path):
    env = make_env(tmp_path, auto_apply=False, daily_apply_limit=0)
    _scouted(env)
    pid = env.add("https://r", fit=2)
    env.sched.request_apply(pid)
    await env.step()
    assert env.status(pid) == "submitted"
    env.store.close()


@pytest.mark.parametrize("result", ["needs_login", "needs_attestation"])
async def test_needs_user_results_alert(env, result):
    _scouted(env)
    pid = env.add("https://x", fit=5, company="Shopify", role="Backend Intern")
    env.apply.results["https://x"] = [ApplyResult(result=result, detail="please do X")]
    await env.step()
    p = env.store.get_posting(pid)
    assert p["status"] == result and p["status_detail"] == "please do X"
    (msg,) = env.sent
    assert msg.startswith(INTERNAL_PREFIX)
    for part in ("Shopify", "Backend Intern", "https://x", "please do X"):
        assert part in msg


async def test_skipped_result_sets_status_without_alert(env):
    _scouted(env)
    pid = env.add("https://x", fit=5)
    env.apply.results["https://x"] = [ApplyResult(result="skipped", detail="closed")]
    await env.step()
    assert env.status(pid) == "skipped" and env.sent == []


async def test_error_is_retried_once_then_alerts(env):
    _scouted(env)
    pid = env.add("https://x", fit=5, company="Shopify")
    env.apply.results["https://x"] = [ApplyResult(result="error", detail="crash 1", cost_usd=0.1),
                                      ApplyResult(result="error", detail="crash 2", cost_usd=0.1)]
    await env.step()
    assert env.status(pid) == "queued" and env.sent == []
    await env.step()
    p = env.store.get_posting(pid)
    assert p["status"] == "error" and "crash 2" in p["status_detail"]
    (msg,) = env.sent
    assert "Shopify" in msg and "crash 2" in msg and "https://x" in msg
    assert [r["ok"] for r in env.runs("apply")] == [0, 0]
    await env.step()
    assert len(env.apply.calls) == 2


async def test_error_then_success(env):
    _scouted(env)
    pid = env.add("https://x", fit=5)
    env.apply.results["https://x"] = [ApplyResult(result="error", detail="flaky")]
    await env.step()
    await env.step()
    assert env.status(pid) == "submitted" and env.sent == []


async def test_apply_fn_exception_counts_as_error(env):
    _scouted(env)
    pid = env.add("https://x", fit=5)

    async def boom(cfg, posting):
        raise RuntimeError("kaput")

    env.sched._apply_fn = boom
    await env.step()
    assert env.status(pid) == "queued"
    await env.step()
    assert env.status(pid) == "error" and "kaput" in env.store.get_posting(pid)["status_detail"]


async def test_chrome_not_connected_requeues_and_alerts_once_per_day(env):
    _scouted(env)
    pid = env.add("https://x", fit=5)
    env.apply.default = ApplyResult(result="chrome_not_connected", detail="no extension")
    await env.step()
    assert env.status(pid) == "queued"
    assert len(env.sent) == 1 and "Chrome" in env.sent[0]
    # Paused for a while: no hot loop of runs.
    env.clock.at = DAY.replace(hour=9, minute=5)
    await env.step()
    assert len(env.apply.calls) == 1
    env.clock.at = DAY.replace(hour=10)
    await env.step()
    assert len(env.apply.calls) == 2 and len(env.sent) == 1  # one alert per day
    assert env.status(pid) == "queued"
    env.clock.at = DAY.replace(hour=10) + timedelta(days=1)
    _scouted(env)
    await env.step()
    assert len(env.sent) == 2
    env.apply.default = ApplyResult(result="submitted", detail="ok")
    env.clock.at += timedelta(hours=1)
    await env.step()
    assert env.status(pid) == "submitted"


async def test_chrome_not_connected_keeps_request_priority(env):
    _scouted(env)
    other = env.add("https://o", fit=5, seen=DAY - timedelta(days=3))
    req = env.add("https://r", fit=2)
    env.sched.request_apply(req)
    env.apply.results["https://r"] = [ApplyResult(result="chrome_not_connected", detail="x")]
    await env.step()
    env.clock.at = DAY.replace(hour=11)
    await env.step()
    assert [p["url"] for p in env.apply.calls] == ["https://r", "https://r"]
    assert env.status(other) == "queued"


async def test_stale_applying_is_requeued_on_start(env):
    _scouted(env)
    pid = env.add("https://x", fit=5, status="applying")
    await env.step()
    assert env.status(pid) == "submitted"


async def test_run_survives_tick_errors(env, monkeypatch):
    import argus.jobs.scheduler as mod
    monkeypatch.setattr(mod, "TICK_SECONDS", 0.01)
    calls = []

    async def bad_tick():
        calls.append(1)
        raise RuntimeError("tick failed")

    env.sched.tick = bad_tick
    task = asyncio.create_task(env.sched.run())
    for _ in range(200):
        if len(calls) >= 3:
            break
        await asyncio.sleep(0.01)
    assert len(calls) >= 3 and not task.done()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_run_cancel_cancels_background_work(env, monkeypatch):
    _scouted(env)
    env.add("https://x", fit=5)
    env.apply.gate = asyncio.Event()
    task = asyncio.create_task(env.sched.run())
    await asyncio.wait_for(env.apply.started.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not env.sched.applying
