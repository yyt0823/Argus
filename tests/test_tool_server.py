import json
from datetime import datetime
import pytest
from argus.store import Store
from argus.tools.memory import MemoryStore
from argus.tools.server import build_tools, build_tool_server

@pytest.fixture
def env(tmp_path):
    store = Store(tmp_path / "data" / "argus.db")
    mem = MemoryStore(tmp_path / "memory")
    tools = {t.name: t for t in build_tools(store, mem, tmp_path / "data" / "argus.db")}
    yield store, mem, tools
    store.close()

def text(result):
    return result["content"][0]["text"]

async def test_allowed_names(tmp_path):
    store = Store(tmp_path / "a.db")
    _, allowed = build_tool_server(store, MemoryStore(tmp_path / "m"), tmp_path / "a.db")
    assert sorted(allowed) == sorted(f"mcp__argus__{n}" for n in
        ["memory_list", "memory_read", "memory_write", "memory_delete", "query_data", "search_history"])
    store.close()

async def test_memory_roundtrip(env):
    _, _, t = env
    assert text(await t["memory_list"].handler({})) == "(no memories)"
    await t["memory_write"].handler({"name": "knee", "content": "bad knee", "summary": "knee"})
    assert text(await t["memory_read"].handler({"name": "knee"})) == "bad knee"
    assert "knee" in text(await t["memory_list"].handler({}))
    await t["memory_delete"].handler({"name": "knee"})
    r = await t["memory_read"].handler({"name": "knee"})
    assert r.get("is_error") is True

async def test_memory_bad_name_is_error(env):
    _, _, t = env
    r = await t["memory_write"].handler({"name": "../x", "content": "c", "summary": "s"})
    assert r.get("is_error") is True

async def test_query_data(env):
    store, _, t = env
    store.add_message("user", "hi", datetime(2026, 10, 1))
    r = await t["query_data"].handler({"sql": "SELECT count(*) AS n FROM messages"})
    assert json.loads(text(r))["rows"] == [[1]]
    bad = await t["query_data"].handler({"sql": "DELETE FROM messages"})
    assert bad.get("is_error") is True

async def test_search_history(env):
    store, _, t = env
    store.add_message("user", "我膝盖疼", datetime(2026, 10, 1, 9))
    r = await t["search_history"].handler({"query": "膝盖"})
    assert "我膝盖疼" in text(r)
    r2 = await t["search_history"].handler({"query": "膝盖", "start": "2026-10-02"})
    assert text(r2) == "No matching messages."
    bad = await t["search_history"].handler({"query": "x", "start": "yesterday"})
    assert bad.get("is_error") is True

async def test_query_data_does_not_block_event_loop(env):
    import asyncio, time
    _, _, t = env
    # Calibrate a bounded CPU-heavy query to take roughly 0.5 s.
    sql_n = "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM c WHERE x < {n}) SELECT count(*) FROM c"
    n = 200_000
    t0 = time.monotonic()
    await t["query_data"].handler({"sql": sql_n.format(n=n)})
    dt = max(time.monotonic() - t0, 1e-3)
    n = int(n * 0.5 / dt)
    ticks = 0
    async def ticker():
        nonlocal ticks
        while True:
            await asyncio.sleep(0.05)
            ticks += 1
    task = asyncio.create_task(ticker())
    t0 = time.monotonic()
    r = await t["query_data"].handler({"sql": sql_n.format(n=n)})
    elapsed = time.monotonic() - t0
    task.cancel()
    assert r.get("is_error") is not True, r
    assert elapsed > 0.3
    assert ticks >= 3

async def test_mail_tools_added_only_with_reader(tmp_path):
    from argus.mail.reader import MailReader
    from tests.mail_fixtures import build_fake_mail
    store = Store(tmp_path / "a.db")
    mem = MemoryStore(tmp_path / "m")
    base = {"memory_list", "memory_read", "memory_write", "memory_delete", "query_data", "search_history"}
    assert {t.name for t in build_tools(store, mem, tmp_path / "a.db")} == base
    fm = build_fake_mail(tmp_path)
    reader = MailReader(fm.mail_dir, fm.accounts_db)
    names = {t.name for t in build_tools(store, mem, tmp_path / "a.db", mail_reader=reader)}
    assert names == base | {"mail_recent", "mail_search", "mail_read"}
    _, allowed = build_tool_server(store, mem, tmp_path / "a.db", mail_reader=reader)
    assert {"mcp__argus__mail_recent", "mcp__argus__mail_search", "mcp__argus__mail_read"} <= set(allowed)
    assert len(allowed) == 9
    store.close()

def test_query_data_describes_mail_triage(env):
    _, _, t = env
    d = t["query_data"].description
    assert "mail_triage(" in d
    for col in ("mail_id", "account", "received_at", "important", "category", "summary", "action",
                "deadline", "status", "attempts", "alerted_at", "briefed_at"):
        assert col in d


FITNESS_TOOLS = {"fitness_plan", "food_log_add", "food_log_update", "food_log_delete", "nutrition_status",
                 "weight_log_add", "workout_log_add", "fitness_summary"}


async def test_fitness_tools_added_only_when_enabled(tmp_path):
    from argus.config import Config, FitnessConfig
    store = Store(tmp_path / "a.db")
    mem = MemoryStore(tmp_path / "m")
    base = {"memory_list", "memory_read", "memory_write", "memory_delete", "query_data", "search_history"}
    on = Config(root=tmp_path, self_handles=("+1",))
    off = Config(root=tmp_path, self_handles=("+1",), fitness=FitnessConfig(enabled=False))
    assert {t.name for t in build_tools(store, mem, tmp_path / "a.db", config=on)} == base | FITNESS_TOOLS
    assert {t.name for t in build_tools(store, mem, tmp_path / "a.db", config=off)} == base
    _, allowed = build_tool_server(store, mem, tmp_path / "a.db", config=on)
    assert {f"mcp__argus__{n}" for n in FITNESS_TOOLS} <= set(allowed) and len(allowed) == 14
    store.close()


JOBS_TOOLS = {"jobs_scout_now", "jobs_list", "job_apply", "job_skip", "jobs_followups"}


async def test_jobs_tools_added_only_with_scheduler_and_enabled(tmp_path):
    from argus.config import Config, FitnessConfig, JobsConfig
    from argus.jobs.scheduler import JobScheduler
    store = Store(tmp_path / "a.db")
    mem = MemoryStore(tmp_path / "m")
    on = Config(root=tmp_path, self_handles=("+1",), fitness=FitnessConfig(enabled=False))
    off = Config(root=tmp_path, self_handles=("+1",), fitness=FitnessConfig(enabled=False),
                 jobs=JobsConfig(enabled=False))
    sched = JobScheduler(on, store, submit=None)
    names = lambda **kw: {t.name for t in build_tools(store, mem, tmp_path / "a.db", **kw)}
    assert names(config=on, job_scheduler=sched) >= JOBS_TOOLS
    assert not names(config=on) & JOBS_TOOLS
    assert not names(config=off, job_scheduler=sched) & JOBS_TOOLS
    _, allowed = build_tool_server(store, mem, tmp_path / "a.db", config=on, job_scheduler=sched)
    assert {f"mcp__argus__{n}" for n in JOBS_TOOLS} <= set(allowed)
    store.close()


def test_query_data_describes_job_tables(env):
    _, _, t = env
    d = t["query_data"].description
    assert "job_postings(" in d and "job_runs(" in d
