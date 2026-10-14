"""Shared plumbing for the jobs agents: one isolated SDK run, and JSON extraction from its reply."""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    HookMatcher,
    ResultMessage,
    TextBlock,
    query,
)

from argus.jobs.guard import GuardPolicy


@dataclass(frozen=True)
class AgentOutcome:
    text: str  # final result text (or the joined assistant text if there is no result text)
    cost_usd: float | None
    is_error: bool = False
    error: str | None = None  # error subtype / message when ``is_error``


Runner = Callable[[str, ClaudeAgentOptions], Awaitable[AgentOutcome]]  # (prompt, options) -> outcome


def guard_hooks(policy: GuardPolicy) -> dict:
    """A PreToolUse hook for every tool, so the policy also covers calls the CLI would auto-approve."""
    return {"PreToolUse": [HookMatcher(matcher=None, hooks=[policy.pre_tool_use_hook])]}


async def sdk_runner(prompt: str, options: ClaudeAgentOptions) -> AgentOutcome:
    """Run one ``query()`` to completion; SDK exceptions propagate to the caller."""
    texts: list[str] = []
    final: ResultMessage | None = None
    async for msg in query(prompt=prompt, options=options):
        if isinstance(msg, AssistantMessage):
            texts.extend(b.text for b in msg.content if isinstance(b, TextBlock) and b.text)
        elif isinstance(msg, ResultMessage):
            final = msg
    if final is None:
        return AgentOutcome(text="\n".join(texts), cost_usd=None, is_error=True, error="no result message")
    text = final.result if isinstance(final.result, str) and final.result.strip() else "\n".join(texts)
    error = None
    if final.is_error:
        error = (final.errors or [None])[0] or final.subtype or "error"
    return AgentOutcome(text=text, cost_usd=final.total_cost_usd, is_error=final.is_error, error=error)


def _top_level_values(text: str) -> list[tuple[int, int, Any]]:
    """Every JSON array/object that decodes at a ``[``/``{`` and is not inside an earlier one."""
    decoder = json.JSONDecoder()
    found: list[tuple[int, int, Any]] = []
    i, n = 0, len(text)
    while i < n:
        if text[i] in "[{":
            try:
                value, end = decoder.raw_decode(text, i)
            except json.JSONDecodeError:
                i += 1
                continue
            found.append((i, end, value))
            i = end
        else:
            i += 1
    return found


def last_json_array(text: str) -> list[dict] | None:
    """The last top-level JSON array of objects in ``text`` (fences/prose tolerated), else None."""
    for _, _, value in reversed(_top_level_values(text or "")):
        if isinstance(value, list) and all(isinstance(v, dict) for v in value):
            return value
    return None


def last_json_object(text: str) -> dict | None:
    """The last top-level JSON object in ``text`` (fences/prose tolerated), else None."""
    for _, _, value in reversed(_top_level_values(text or "")):
        if isinstance(value, dict):
            return value
    return None


def strip_json(text: str) -> str:
    """``text`` without its decodable JSON values and code fences: the prose summary."""
    out, last = [], 0
    for start, end, _ in _top_level_values(text or ""):
        out.append(text[last:start])
        last = end
    out.append((text or "")[last:])
    prose = "".join(out)
    lines = [ln for ln in prose.splitlines() if not ln.strip().startswith("```")]
    return "\n".join(lines).strip()


def opt_str(value: Any) -> str | None:
    if value is None or isinstance(value, (dict, list)):
        return None
    s = str(value).strip()
    return s or None


UNTRUSTED_TAG = "untrusted_web"
_TAG_RE = re.compile(r"<(/?)untrusted_web", re.IGNORECASE)


def defang(text: str) -> str:
    """Neutralise ``<untrusted_web`` / ``</untrusted_web`` inside web-derived text."""
    return _TAG_RE.sub(r"[\1untrusted_web", text)


def untrusted_block(text: str) -> str:
    """``text`` (web-derived) inside an ``<untrusted_web>`` block it cannot close early."""
    return f"<{UNTRUSTED_TAG}>\n{defang(text)}\n</{UNTRUSTED_TAG}>"
