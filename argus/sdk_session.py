"""The real ``Session``: one Claude conversation backed by ``ClaudeSDKClient``."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ClaudeSDKError,
    ResultMessage,
    TextBlock,
)

from argus.config import Config
from argus.session import Interrupted, SessionError, SessionFactory
from argus.store import Store
from argus.tools.memory import MemoryStore
from argus.tools.server import build_tool_server

if TYPE_CHECKING:
    from argus.mail.reader import MailReader

log = logging.getLogger(__name__)

# WebFetch is deliberately excluded: it can send arbitrary data (e.g. memory, chat history) to any URL.
BUILTIN_TOOLS = ["WebSearch"]
MAX_TURNS = 30
INTERRUPT_TIMEOUT = 5  # seconds; the SDK's own control-request timeout is 60s
_ABORTED = ("aborted_streaming", "aborted_tools")


def build_system_prompt(persona: str, memory_index: str, now: datetime) -> str:
    """Persona, then the long-term memory index, then the current local time."""
    return (
        persona
        + "\n\n# Long-term memory index\n"
        + (memory_index.strip() or "(empty)")
        + "\n\n# Now\n"
        + now.strftime("%Y-%m-%d %A %H:%M")
        + " (local time)"
    )


class SDKSession:
    """A ``Session`` over one ``ClaudeSDKClient`` subprocess.

    ``start``/``ask``/``close`` must all run in the same asyncio task (the SDK's
    anyio task group is bound to the task that connected). ``interrupt`` may be
    called from another task: it only writes a control request.
    """

    def __init__(self, options: ClaudeAgentOptions, client: Any = None) -> None:
        self.options = options
        self.sdk_session_id: str | None = options.resume
        self._client = client if client is not None else ClaudeSDKClient(options=options)
        self._connected = False
        self._closed = False
        self._interrupted = False

    async def start(self) -> None:
        try:
            await self._client.connect()
        except Exception as e:  # the SDK raises plain Exception on e.g. initialize timeout
            raise SessionError(f"could not start Claude: {e or type(e).__name__}") from e
        self._connected = True

    async def ask(self, text: str) -> str:
        if not self._connected or self._closed:
            raise SessionError("session is not connected")
        self._interrupted = False
        texts: list[str] = []
        assistant_error: str | None = None
        final: ResultMessage | None = None
        try:
            await self._client.query(text)
            async for msg in self._client.receive_response():
                if isinstance(msg, AssistantMessage):
                    if msg.error is not None:
                        assistant_error = str(msg.error)
                    texts.extend(b.text for b in msg.content if isinstance(b, TextBlock) and b.text)
                elif isinstance(msg, ResultMessage):
                    final = msg
                    if msg.session_id:
                        self.sdk_session_id = msg.session_id
        except ClaudeSDKError as e:
            if self._interrupted:
                raise Interrupted() from e
            raise SessionError(str(e) or type(e).__name__) from e

        if self._interrupted or (final is not None and final.terminal_reason in _ABORTED):
            raise Interrupted()
        if final is None:
            raise SessionError(assistant_error or "no result from Claude")
        if final.is_error:
            reason = (final.errors or [None])[0] or final.result or final.subtype
            raise SessionError(str(reason))
        if isinstance(final.result, str) and final.result.strip():
            return final.result
        if assistant_error is not None:
            raise SessionError(assistant_error)
        return texts[-1] if texts else ""

    async def interrupt(self) -> None:
        self._interrupted = True
        if not self._connected or self._closed:
            return
        try:
            await asyncio.wait_for(self._client.interrupt(), timeout=INTERRUPT_TIMEOUT)
        except asyncio.TimeoutError:
            log.warning("interrupt timed out after %ss", INTERRUPT_TIMEOUT)
        except Exception as e:  # noqa: BLE001 - the flag is set; ask() will still report Interrupted
            log.warning("interrupt failed: %s", e)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if not self._connected:
            return
        try:
            await self._client.disconnect()
        except Exception as e:  # noqa: BLE001 - closing must not raise
            log.warning("disconnect failed: %s", e)


def build_options(
    cfg: Config,
    store: Store,
    memory: MemoryStore,
    resume_id: str | None,
    mail_reader: MailReader | None = None,
) -> ClaudeAgentOptions:
    # Fitness tools are registered when cfg.fitness.enabled (build_tools checks it).
    server, allowed = build_tool_server(store, memory, cfg.db_path, mail_reader, config=cfg)
    persona_path = cfg.root / "CLAUDE.md"
    persona = persona_path.read_text(encoding="utf-8") if persona_path.is_file() else ""
    return ClaudeAgentOptions(
        system_prompt=build_system_prompt(persona, memory.index(), datetime.now()),
        mcp_servers={"argus": server},
        tools=list(BUILTIN_TOOLS),
        allowed_tools=allowed + BUILTIN_TOOLS,
        permission_mode="dontAsk",
        setting_sources=[],
        strict_mcp_config=True,
        cwd=str(cfg.root),
        model=cfg.model,
        resume=resume_id,
        max_turns=MAX_TURNS,
    )


def make_session_factory(
    cfg: Config,
    store: Store,
    memory: MemoryStore,
    mail_reader: MailReader | None = None,
    client_cls: Any = ClaudeSDKClient,
) -> SessionFactory:
    """Return ``factory(resume_id)`` that builds options and returns a started ``SDKSession``."""

    async def factory(resume_id: str | None) -> SDKSession:
        options = build_options(cfg, store, memory, resume_id, mail_reader)
        session = SDKSession(options, client=client_cls(options=options))
        await session.start()
        return session

    return factory
