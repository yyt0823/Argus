import asyncio
import json
import os
import sys
import time
from pathlib import Path

import pytest

import argus.jobs.jobagent_applier as mod
from argus.config import load_config
from argus.jobs.applier import ApplyResult
from argus.jobs.jobagent_applier import STATUS_MAP, apply_with_jobagent, preflight

# Stands in for bin/jobagent: argv[1] is the behaviour file (JSON), the rest is jobagent's argv.
FAKE = r'''
import json, os, signal, subprocess, sys, time
spec = json.load(open(sys.argv[1]))
argv = sys.argv[2:]
out = {"argv": argv, "env": {k: os.environ.get(k) for k in ("JOBAGENT_DATA_DIR", "JOBAGENT_MAIL_DIR")},
       "pid": os.getpid(), "pgid": os.getpgid(0)}
if spec.get("grandchild"):
    out["grandchild"] = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"]).pid
if spec.get("ignore_term"):
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
json.dump(out, open(spec["probe"], "w"))
print(spec.get("log", "fake jobagent log"), flush=True)
if "result" in spec:
    rf = argv[argv.index("--result-file") + 1]
    open(rf, "w").write(spec["result"] if isinstance(spec["result"], str) else json.dumps(spec["result"]))
time.sleep(spec.get("sleep", 0))
'''

POSTING = {"id": 7, "url": "https://jobs.example.com/a/1", "company": "Acme", "role": "SWE Intern"}


@pytest.fixture
def env(tmp_path: Path):
    root = tmp_path / "argus"
    root.mkdir()
    ja = tmp_path / "ja"
    (ja / "bin").mkdir(parents=True)
    (ja / "bin" / "jobagent").write_text("#!/bin/sh\n")
    (ja / "config").mkdir()
    (ja / "config" / "profile.yaml").write_text("personal:\n  email: a@b.c\naccount:\n  email: real@x.y\n")
    (root / "argus.toml").write_text(
        f'self_handles = ["+15555550100"]\n[jobs]\njobagent_dir = "{ja}"\njobagent_model = "m-1"\n'
        'human_timeout_minutes = 3\n'
    )
    cfg = load_config(root)
    script = tmp_path / "fake_jobagent.py"
    script.write_text(FAKE)

    class Env:
        pass

    e = Env()
    e.cfg, e.tmp, e.run_dir, e.probe = cfg, tmp_path, tmp_path / "run", tmp_path / "probe.json"
    e.calls = []

    def make_spawn(**spec):
        spec["probe"] = str(e.probe)
        spec_file = tmp_path / "spec.json"
        spec_file.write_text(json.dumps(spec))

        async def spawn(*cmd, **kwargs):
            e.calls.append((cmd, kwargs))
            return await asyncio.create_subprocess_exec(sys.executable, str(script), str(spec_file), *cmd[1:], **kwargs)

        return spawn

    e.make_spawn = make_spawn
    e.read_probe = lambda: json.loads(e.probe.read_text())
    return e


def _gone(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


async def _wait_gone(pid: int, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if _gone(pid):
            return True
        await asyncio.sleep(0.05)
    return _gone(pid)


async def test_maps_submitted_with_accounts(env):
    result = {"at": "2026-10-29T10:00:00", "job_url": POSTING["url"], "mode": "auto", "status": "submitted",
              "company": "Acme Corp", "job_title": "Software Intern", "notes": "  done  ", "cost_usd": 1.25,
              "run_dir": "/x", "review": None, "accounts_created": ["acme.myworkdayjobs.com"]}
    r = await apply_with_jobagent(env.cfg, POSTING, reader=None, relay=None, run_dir=env.run_dir,
                                  spawn=env.make_spawn(result=result))
    assert r == ApplyResult(result="submitted", detail="done", cost_usd=1.25, raw=json.dumps(result),
                            accounts_created=("acme.myworkdayjobs.com",), company="Acme Corp", role="Software Intern")


@pytest.mark.parametrize("status, expected", sorted(STATUS_MAP.items()))
async def test_maps_every_status(env, status, expected):
    r = await apply_with_jobagent(env.cfg, POSTING, reader=None, relay=None, run_dir=env.run_dir,
                                  spawn=env.make_spawn(result={"status": status, "notes": "n"}))
    assert r.result == expected
    assert r.detail == ("already applied on site" if status == "already_applied" else "n")


async def test_crash_result_has_defaults_and_trimmed_notes(env):
    r = await apply_with_jobagent(env.cfg, POSTING, reader=None, relay=None, run_dir=env.run_dir,
                                  spawn=env.make_spawn(result={"status": "failed", "notes": "crash: " + "x" * 900}))
    assert r.result == "error" and len(r.detail) == 500 and r.detail.startswith("crash: ")
    assert r.accounts_created == () and r.company is None and r.role is None and r.cost_usd is None


async def test_missing_result_is_error_with_log_tail(env):
    log = "A" * 400 + "Traceback: boom"
    r = await apply_with_jobagent(env.cfg, POSTING, reader=None, relay=None, run_dir=env.run_dir,
                                  spawn=env.make_spawn(log=log))
    assert r.result == "error"
    assert r.detail.endswith("Traceback: boom") and "A" * 285 in r.detail and "A" * 286 not in r.detail
    assert (env.run_dir / "jobagent.log").read_text().strip() == log


@pytest.mark.parametrize("bad", ["not json", "[1, 2]", '{"status": "weird"}'])
async def test_unparseable_result_is_error(env, bad):
    r = await apply_with_jobagent(env.cfg, POSTING, reader=None, relay=None, run_dir=env.run_dir,
                                  spawn=env.make_spawn(result=bad, log="the log"))
    assert r.result == "error" and "the log" in r.detail


async def test_command_line(env):
    await apply_with_jobagent(env.cfg, POSTING, reader=None, relay=None, run_dir=env.run_dir,
                              spawn=env.make_spawn(result={"status": "submitted"}))
    (cmd, kwargs), = env.calls
    ja = env.tmp / "ja"
    assert list(cmd) == [
        str(ja / "bin" / "jobagent"), "--profile", str(ja / "config" / "profile.yaml"),
        "--human-dir", str(env.run_dir / "human"), "--human-timeout", "180", "--model", "m-1",
        "apply", POSTING["url"], "--mode", "auto", "--result-file", str(env.run_dir / "result.json"),
    ]
    assert kwargs["start_new_session"] is True
    probe = env.read_probe()
    assert probe["pgid"] == probe["pid"]  # its own process group
    assert "fake jobagent log" in (env.run_dir / "jobagent.log").read_text()


async def test_command_without_model(env, tmp_path):
    from dataclasses import replace
    cfg = replace(env.cfg, jobs=replace(env.cfg.jobs, jobagent_model=None))
    await apply_with_jobagent(cfg, POSTING, reader=None, relay=None, run_dir=env.run_dir,
                              spawn=env.make_spawn(result={"status": "submitted"}))
    (cmd, _), = env.calls
    assert "--model" not in cmd


async def test_env_points_to_live_dir_and_run_mail_dir(env):
    await apply_with_jobagent(env.cfg, POSTING, reader=None, relay=None, run_dir=env.run_dir,
                              spawn=env.make_spawn(result={"status": "submitted"}))
    probe = env.read_probe()
    assert probe["env"] == {"JOBAGENT_DATA_DIR": str(env.tmp / "ja" / "data" / "live"),
                            "JOBAGENT_MAIL_DIR": str(env.run_dir / "mail")}
    assert (env.run_dir / "mail").is_dir() and (env.run_dir / "human").is_dir()


async def test_default_run_dir_under_live_argus(env):
    await apply_with_jobagent(env.cfg, POSTING, reader=None, relay=None,
                              spawn=env.make_spawn(result={"status": "submitted"}))
    runs = list((env.tmp / "ja" / "data" / "live" / "argus").iterdir())
    assert len(runs) == 1 and runs[0].name.startswith("7-") and (runs[0] / "result.json").is_file()


async def test_cancel_kills_process_group(env):
    task = asyncio.create_task(apply_with_jobagent(
        env.cfg, POSTING, reader=None, relay=None, run_dir=env.run_dir,
        spawn=env.make_spawn(sleep=60, grandchild=True)))
    while not env.probe.exists():
        await asyncio.sleep(0.05)
    await asyncio.sleep(0.2)
    probe = env.read_probe()
    started = time.monotonic()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert time.monotonic() - started < 12
    assert await _wait_gone(probe["pid"], 2) and await _wait_gone(probe["grandchild"], 2)


async def test_cancel_sigkills_after_grace(env, monkeypatch):
    monkeypatch.setattr(mod, "KILL_GRACE_S", 0.5)
    task = asyncio.create_task(apply_with_jobagent(
        env.cfg, POSTING, reader=None, relay=None, run_dir=env.run_dir,
        spawn=env.make_spawn(sleep=60, ignore_term=True)))
    while not env.probe.exists():
        await asyncio.sleep(0.05)
    await asyncio.sleep(0.2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await _wait_gone(env.read_probe()["pid"], 2)


class FakeReader:
    def __init__(self):
        self.max_id_calls = 0

    def max_id(self):
        self.max_id_calls += 1
        return 41


class FakeRelay:
    def __init__(self):
        self.watched = None
        self.cancelled = False

    async def watch(self, human_dir, posting):
        self.watched = (human_dir, posting)
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise


async def test_bridge_and_relay_run_and_are_cancelled(env, monkeypatch):
    bridge = {}

    async def fake_bridge(reader, after_id, out_dir, poll_s=5.0):
        bridge.update(reader=reader, after_id=after_id, out_dir=out_dir)
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            bridge["cancelled"] = True
            raise

    monkeypatch.setattr(mod, "run_bridge", fake_bridge)
    reader, relay = FakeReader(), FakeRelay()
    r = await apply_with_jobagent(env.cfg, POSTING, reader=reader, relay=relay, run_dir=env.run_dir,
                                  spawn=env.make_spawn(result={"status": "submitted"}, sleep=0.3))
    assert r.result == "submitted"
    assert bridge == {"reader": reader, "after_id": 41, "out_dir": env.run_dir / "mail", "cancelled": True}
    assert relay.watched == (env.run_dir / "human", POSTING) and relay.cancelled


async def test_bridge_and_relay_cancelled_on_cancel(env, monkeypatch):
    cancelled = []

    async def fake_bridge(reader, after_id, out_dir, poll_s=5.0):
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.append("bridge")
            raise

    monkeypatch.setattr(mod, "run_bridge", fake_bridge)
    relay = FakeRelay()
    task = asyncio.create_task(apply_with_jobagent(
        env.cfg, POSTING, reader=FakeReader(), relay=relay, run_dir=env.run_dir, spawn=env.make_spawn(sleep=60)))
    while not env.probe.exists():
        await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled == ["bridge"] and relay.cancelled
    assert await _wait_gone(env.read_probe()["pid"], 2)


# --- preflight ---

def test_preflight_ok(env):
    assert preflight(env.cfg) is None


def test_preflight_refuses_test_profile_and_test_email(env, tmp_path):
    from dataclasses import replace
    ja = tmp_path / "ja"
    (ja / "config" / "test-profile.yaml").write_text("account:\n  email: real@x.y\n")
    cfg = replace(env.cfg, jobs=replace(env.cfg.jobs, jobagent_profile="config/test-profile.yaml"))
    assert "test-profile" in preflight(cfg)

    (ja / "config" / "profile.yaml").write_text(
        "personal:\n  email: real@x.y\naccount:\n  # the login\n  email: \"me@yyt.email\"\n  password: ''\n")
    assert "me@yyt.email" in preflight(env.cfg)


def test_preflight_test_email_outside_account_block_is_fine(env, tmp_path):
    (tmp_path / "ja" / "config" / "profile.yaml").write_text(
        "personal:\n  email: me@yyt.email\naccount:\n  email: real@x.y\n")
    assert preflight(env.cfg) is None


def test_preflight_missing_bin_and_profile(env, tmp_path):
    (tmp_path / "ja" / "config" / "profile.yaml").unlink()
    assert "profile" in preflight(env.cfg)
    (tmp_path / "ja" / "bin" / "jobagent").unlink()
    assert "bin/jobagent" in preflight(env.cfg)
