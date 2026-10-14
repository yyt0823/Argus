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
