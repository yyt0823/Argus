"""Live applier smoke test in the user's Chrome. Run with ARGUS_LIVE=1.

ARGUS_JOBS_SMOKE=1 makes the agent only check the Chrome connection, open https://example.com in a
new tab, close it and report "skipped" — nothing is filled or submitted. One call.
"""

import os
from pathlib import Path

import pytest
from claude_agent_sdk import AssistantMessage, ResultMessage, SystemMessage, ToolUseBlock, query

from argus.config import load_config
from argus.jobs.agent import AgentOutcome
from argus.jobs.applier import apply_to

pytestmark = pytest.mark.skipif(os.environ.get("ARGUS_LIVE") != "1", reason="set ARGUS_LIVE=1")

ROOT = Path(__file__).resolve().parents[2]


async def logging_runner(prompt, options):
    """Like ``sdk_runner`` but prints the loaded tools and every tool call."""
    final, texts = None, []
    async for msg in query(prompt=prompt, options=options):
        if isinstance(msg, SystemMessage) and msg.subtype == "init":
            tools = msg.data.get("tools", [])
            print("tools:", len(tools), sorted(t for t in tools if not t.startswith("mcp__claude-in-chrome__")),
                  "chrome:", sum(t.startswith("mcp__claude-in-chrome__") for t in tools))
        elif isinstance(msg, AssistantMessage):
            for b in msg.content:
                if isinstance(b, ToolUseBlock):
                    print("tool_use:", b.name, b.input)
                elif getattr(b, "text", None):
                    texts.append(b.text)
        elif isinstance(msg, ResultMessage):
            final = msg
    print("denials:", final.permission_denials)
    return AgentOutcome(text=final.result or "\n".join(texts), cost_usd=final.total_cost_usd,
                        is_error=final.is_error, error=(final.errors or [final.subtype])[0] if final.is_error else None)


async def test_live_apply_smoke(monkeypatch):
    monkeypatch.setenv("ARGUS_JOBS_SMOKE", "1")
    cfg = load_config(ROOT)
    res = await apply_to(cfg, {"company": "Example", "role": "Test", "url": "https://example.com"},
                         runner=logging_runner)
    print("result:", res.result, "|", res.detail, "| cost:", res.cost_usd)
    print("raw:", res.raw[-600:])
    assert res.result in ("skipped", "chrome_not_connected")
