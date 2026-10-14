import asyncio
from datetime import datetime, time, timedelta

import pytest

from argus.briefing import BriefingScheduler
from argus.config import Config, MailConfig
from argus.mail.watcher import INTERNAL_PREFIX
from argus.store import Store

DAY = datetime(2026, 10, 14)


class Clock:
    def __init__(self, at):
        self.at = at

    def __call__(self):
        return self.at


class Env:
    def __init__(self, tmp_path, at, mail=True, fitness=False, jobs=False, **kw):
        self.store = Store(tmp_path / "argus.db")
        self.cfg = Config(root=tmp_path, self_handles=("+1",), mail=MailConfig(briefing_time=time(8, 0)))
        self.clock = Clock(at)
        self.sent: list[str] = []
        self.results: list[bool] = []
        self.pending: list = []
        self.hold = False
        self.scouting = False
        self.flags = dict(mail_enabled=mail, fitness=fitness, jobs=jobs)
        self.kw = kw
        self.b = self.make()

    def make(self):
        async def submit(text, on_result=None):
            self.sent.append(text)
            if self.hold:
                self.pending.append(on_result)
            else:
                on_result(self.results.pop(0) if self.results else True)

        return BriefingScheduler(self.cfg, self.store, submit, scout_running=lambda: self.scouting,
                                 now=self.clock, **self.flags, **self.kw)

    def add_mail(self, mid, at, alerted=False):
        self.store.upsert_triage({
            "mail_id": mid, "account": "School", "sender": "a@example.com", "subject": f"S{mid}",
            "received_at": at, "important": True, "category": "school", "summary": "s",
            "action": None, "deadline": None, "status": "triaged", "attempts": 1,
        })
        if alerted:
            self.store.mark_alerted([mid], at)


async def at(env, hour, minute=0, day=DAY):
    env.clock.at = day.replace(hour=hour, minute=minute)
    await env.b.tick()


# --- scheduling ---

async def test_sends_once_at_briefing_time(tmp_path):
    env = Env(tmp_path, DAY.replace(hour=7))
    await env.b.tick()
    assert env.sent == []
    await at(env, 7, 59)
    assert env.sent == []
    await at(env, 8, 0)
    assert len(env.sent) == 1 and env.sent[0].startswith(INTERNAL_PREFIX)
    assert env.store.get_state("briefing_last_sent") == "2026-10-14"
    await at(env, 8, 1)
    await at(env, 20, 0)
    assert len(env.sent) == 1
    await at(env, 8, 0, day=DAY + timedelta(days=1))
    assert len(env.sent) == 2


async def test_first_run_after_briefing_time_skips_today(tmp_path):
    env = Env(tmp_path, DAY.replace(hour=10))
    await env.b.tick()
    assert env.sent == []
    assert env.store.get_state("briefing_last_sent") == "2026-10-14"


async def test_catch_up_after_restart(tmp_path):
    env = Env(tmp_path, DAY.replace(hour=10))
    env.store.set_state("briefing_last_sent", "2026-10-13")
    await env.b.tick()
    assert len(env.sent) == 1
    env.b = env.make()  # restarted
    await env.b.tick()
    assert len(env.sent) == 1


async def test_migrates_legacy_state_key(tmp_path):
    env = Env(tmp_path, DAY.replace(hour=10))
    env.store.set_state("mail_last_briefing", "2026-10-14")
    await env.b.tick()
    assert env.sent == []
    assert env.store.get_state("briefing_last_sent") == "2026-10-14"

    store_dir = tmp_path / "other"
    store_dir.mkdir()
    env = Env(store_dir, DAY.replace(hour=10))
    env.store.set_state("mail_last_briefing", "2026-10-13")
    await env.b.tick()
    assert len(env.sent) == 1  # yesterday's legacy state: today's briefing is still due


async def test_nothing_enabled_sends_nothing(tmp_path):
    env = Env(tmp_path, DAY.replace(hour=7), mail=False)
    await at(env, 8)
    assert env.sent == [] and env.b.pending() is False


# --- on_result ---

async def test_state_and_marks_only_after_success(tmp_path):
    env = Env(tmp_path, DAY.replace(hour=7))
    env.add_mail(1, DAY.replace(hour=6))
    env.add_mail(2, DAY.replace(hour=7), alerted=True)
    env.add_mail(3, DAY - timedelta(days=2))  # outside the 24 h window
    await env.b.tick()
    env.hold = True
    await at(env, 8)
    assert len(env.sent) == 1
    assert env.store.get_state("briefing_last_sent") is None
    assert env.store.get_triage(1)["briefed_at"] is None
    assert env.b.pending() is True
    await at(env, 8, 1)
    assert len(env.sent) == 1  # in flight: not sent again
    env.pending.pop()(True)
    assert env.store.get_state("briefing_last_sent") == "2026-10-14"
    r1, r2, r3 = (env.store.get_triage(i) for i in (1, 2, 3))
    assert r1["briefed_at"] and r1["alerted_at"]
    assert r2["briefed_at"] and r2["alerted_at"] == env.store.get_triage(2)["alerted_at"]
    assert r3["briefed_at"] is None and r3["alerted_at"] is None
    assert env.b.pending() is False


async def test_alerted_at_kept_when_already_set(tmp_path):
    env = Env(tmp_path, DAY.replace(hour=7))
    env.add_mail(2, DAY.replace(hour=7), alerted=True)
    before = env.store.get_triage(2)["alerted_at"]
    await env.b.tick()
    await at(env, 8, 30)
    assert env.store.get_triage(2)["alerted_at"] == before


async def test_failure_retries_twice_five_minutes_apart_then_gives_up(tmp_path, caplog):
    env = Env(tmp_path, DAY.replace(hour=7))
    env.add_mail(1, DAY.replace(hour=6))
    await env.b.tick()
    env.results = [False, False, False]
    await at(env, 8, 0)
    assert len(env.sent) == 1
    assert env.store.get_state("briefing_last_sent") is None
    assert env.store.get_triage(1)["briefed_at"] is None
    await at(env, 8, 4)
    assert len(env.sent) == 1
    await at(env, 8, 5)
    assert len(env.sent) == 2
    await at(env, 8, 9)
    assert len(env.sent) == 2
    await at(env, 8, 10)
    assert len(env.sent) == 3
    with caplog.at_level("ERROR"):
        for m in range(11, 60, 5):
            await at(env, 8, m)
    assert len(env.sent) == 3
    assert env.b.pending() is False
    assert env.store.get_state("briefing_last_sent") is None
    assert env.store.get_triage(1)["briefed_at"] is None
    # Next day it tries again.
    await at(env, 8, 0, day=DAY + timedelta(days=1))
    assert len(env.sent) == 4
    assert env.store.get_state("briefing_last_sent") == "2026-10-15"


async def test_retry_succeeds(tmp_path):
    env = Env(tmp_path, DAY.replace(hour=7))
    await env.b.tick()
    env.results = [False]
    await at(env, 8, 0)
    await at(env, 8, 5)
    assert len(env.sent) == 2
    assert env.store.get_state("briefing_last_sent") == "2026-10-14"


async def test_submit_raising_counts_as_failure(tmp_path):
    env = Env(tmp_path, DAY.replace(hour=7))
    await env.b.tick()
    calls = []

    async def boom(text, on_result=None):
        calls.append(text)
        raise RuntimeError("queue closed")

    env.b._submit = boom
    await at(env, 8, 0)
    await at(env, 8, 5)
    await at(env, 8, 10)
    await at(env, 8, 15)
    assert len(calls) == 3
    assert env.store.get_state("briefing_last_sent") is None


async def test_run_survives_exceptions(tmp_path, monkeypatch):
    import argus.briefing as br

    env = Env(tmp_path, DAY.replace(hour=7))
    monkeypatch.setattr(br, "TICK_SECONDS", 0)
    calls = 0

    async def flaky():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("boom")
        if calls >= 3:
            raise asyncio.CancelledError

    env.b.tick = flaky
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(env.b.run(), 5)
    assert calls == 3


# --- sections ---

async def _text(tmp_path, **kw):
    env = Env(tmp_path, DAY.replace(hour=7), **kw)
    await env.b.tick()
    await at(env, 8)
    (text,) = env.sent
    return env, text


async def test_mail_only(tmp_path):
    _, b = await _text(tmp_path)
    assert "mail_recent" in b and "untrusted" in b
    assert "nutrition_status" not in b and "weigh" not in b.lower() and "jobs_list" not in b


async def test_without_mail_has_no_email_section(tmp_path):
    _, b = await _text(tmp_path, mail=False, fitness=True)
    assert "mail_recent" not in b and "Email" not in b
    assert "nutrition_status" in b


async def test_mail_reader_unavailable_says_email_could_not_be_read(tmp_path):
    _, b = await _text(tmp_path, mail_available=False, fitness=True)
    assert "could not read" in b and "mail_recent" not in b
    assert "nutrition_status" in b


async def test_mail_read_failing_state_says_email_could_not_be_read(tmp_path):
    env = Env(tmp_path, DAY.replace(hour=7))
    env.store.set_state("mail_read_failing", "2026-10-14T07:55:00")
    await env.b.tick()
    await at(env, 8)
    assert "could not read" in env.sent[0] and "mail_recent" not in env.sent[0]


async def test_fitness_asks_for_yesterday_and_weigh_in(tmp_path):
    _, b = await _text(tmp_path, fitness=True)
    assert "nutrition_status" in b and "2026-10-13" in b
    assert "No weight has been logged" in b


async def test_fitness_no_weigh_in_reminder_when_recent_weight(tmp_path):
    env = Env(tmp_path, DAY.replace(hour=7), fitness=True)
    env.store.add_weight(DAY.date() - timedelta(days=1), DAY - timedelta(hours=12), 72.0)
    await env.b.tick()
    await at(env, 8)
    assert "nutrition_status" in env.sent[0] and "No weight has been logged" not in env.sent[0]


async def test_jobs_asks_for_postings_and_applications(tmp_path):
    _, b = await _text(tmp_path, jobs=True)
    assert "jobs_list" in b and "min_fit" in b
    for status in ("submitted", "needs_login", "needs_attestation"):
        assert status in b
    assert "still running" not in b


async def test_jobs_scout_still_running_says_results_follow(tmp_path):
    env = Env(tmp_path, DAY.replace(hour=7), jobs=True)
    env.scouting = True
    await env.b.tick()
    await at(env, 8)
    (b,) = env.sent
    assert "still running" in b and "results will follow" in b
    assert "min_fit" not in b
    assert "needs_login" in b


async def test_scout_running_check_error_is_not_fatal(tmp_path):
    env = Env(tmp_path, DAY.replace(hour=7), jobs=True)

    def broken():
        raise RuntimeError("nope")

    env.b._scout_running = broken
    await env.b.tick()
    await at(env, 8)
    assert "min_fit" in env.sent[0]


async def test_pending_window(tmp_path):
    env = Env(tmp_path, DAY.replace(hour=7))
    await env.b.tick()
    assert env.b.pending() is False
    env.clock.at = DAY.replace(hour=8)
    assert env.b.pending() is True
    await env.b.tick()
    assert env.b.pending() is False
