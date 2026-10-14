from datetime import datetime
import pytest
from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock, ClaudeAgentOptions
from argus.sdk_session import SDKSession, build_system_prompt
from argus.session import Interrupted, SessionError

def result(**kw):
    base = dict(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False, num_turns=1,
                session_id="sess-1", result="final answer")
    base.update(kw)
    return ResultMessage(**{k: v for k, v in base.items() if k in ResultMessage.__dataclass_fields__})

def assistant(text, error=None):
    return AssistantMessage(**{k: v for k, v in dict(content=[TextBlock(text=text)], model="m", error=error).items()
                               if k in AssistantMessage.__dataclass_fields__})

class FakeClient:
    def __init__(self, options=None, script=None):
        self.options, self.script, self.queries, self.interrupted = options, list(script or []), [], 0
    async def connect(self, prompt=None): self.connected = True
    async def query(self, prompt, session_id="default"): self.queries.append(prompt)
    async def receive_response(self):
        for m in self.script.pop(0):
            yield m
    async def interrupt(self): self.interrupted += 1
    async def disconnect(self): self.connected = False

async def make(script):
    client = FakeClient(script=script)
    s = SDKSession(ClaudeAgentOptions(), client=client)
    await s.start()
    return s, client

async def test_ask_returns_result_and_session_id():
    s, c = await make([[assistant("thinking"), result()]])
    assert await s.ask("hi") == "final answer"
    assert s.sdk_session_id == "sess-1" and c.queries == ["hi"]

async def test_falls_back_to_last_text_when_result_empty():
    s, _ = await make([[assistant("a"), assistant("b"), result(result="")]])
    assert await s.ask("hi") == "b"

async def test_error_result_raises():
    s, _ = await make([[result(is_error=True, subtype="error_during_execution", result="boom")]])
    with pytest.raises(SessionError):
        await s.ask("hi")

async def test_aborted_raises_interrupted():
    s, _ = await make([[result(terminal_reason="aborted_streaming")]])
    with pytest.raises(Interrupted):
        await s.ask("hi")

async def test_close_idempotent():
    s, c = await make([])
    await s.close(); await s.close()
    assert c.connected is False

def test_system_prompt():
    p = build_system_prompt("You are Argus.", "", datetime(2026, 10, 13, 9, 30))
    assert p.startswith("You are Argus.") and "(empty)" in p and "2026-10-13 Tuesday 09:30" in p

# --- additional tests (beyond the plan) ---
from pathlib import Path
from claude_agent_sdk import CLIConnectionError
from argus.config import Config
from argus.sdk_session import make_session_factory
from argus.store import Store
from argus.tools.memory import MemoryStore

async def test_assistant_error_without_result_text_raises():
    s, _ = await make([[assistant("", error="rate_limit"), result(result="")]])
    with pytest.raises(SessionError, match="rate_limit"):
        await s.ask("hi")

async def test_sdk_error_becomes_session_error():
    class Broken(FakeClient):
        async def query(self, prompt, session_id="default"):
            raise CLIConnectionError("pipe closed")
    s = SDKSession(ClaudeAgentOptions(), client=Broken())
    await s.start()
    with pytest.raises(SessionError, match="pipe closed"):
        await s.ask("hi")

async def test_interrupt_during_turn_raises_interrupted():
    s, c = await make([])
    async def gen():
        await s.interrupt()
        yield result()
    c.receive_response = gen
    with pytest.raises(Interrupted):
        await s.ask("hi")
    assert c.interrupted == 1
    # the flag resets for the next turn
    c.script = [[result()]]
    c.receive_response = FakeClient.receive_response.__get__(c)
    assert await s.ask("again") == "final answer"

async def test_factory_builds_options(tmp_path: Path):
    (tmp_path / "CLAUDE.md").write_text("You are Argus.")
    cfg = Config(root=tmp_path, self_handles=("+15550000000",), model="sonnet")
    cfg.data_dir.mkdir()
    store, memory = Store(cfg.db_path), MemoryStore(cfg.memory_dir)
    made = []
    def client_cls(options):
        made.append(FakeClient(options=options)); return made[-1]
    s = await make_session_factory(cfg, store, memory, client_cls=client_cls)("prev-id")
    o = made[0].options
    assert made[0].connected and s.sdk_session_id == "prev-id"
    assert o.resume == "prev-id" and o.model == "sonnet" and o.cwd == str(tmp_path)
    assert o.system_prompt.startswith("You are Argus.")
    assert o.tools == ["WebSearch", "WebFetch"] and "mcp__argus__memory_write" in o.allowed_tools
    assert o.permission_mode == "dontAsk" and o.setting_sources == [] and o.strict_mcp_config
    assert set(o.mcp_servers) == {"argus"} and o.max_turns == 30
    store.close()
