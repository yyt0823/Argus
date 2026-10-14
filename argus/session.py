"""Session protocol: one conversation with Claude. The real one is sdk_session.SDKSession."""

from __future__ import annotations

from typing import Awaitable, Callable, Protocol


class SessionError(Exception):
    """Claude failed to answer. str(e) is a short human-readable reason."""


class Interrupted(Exception):
    """The turn was interrupted by the user."""


class Session(Protocol):
    sdk_session_id: str | None  # known after the first answered turn

    async def ask(self, text: str) -> str:
        """Send one user message, return Claude's final reply text.

        Raises Interrupted if interrupt() was called during the turn, SessionError on failure.
        """

    async def interrupt(self) -> None: ...

    async def close(self) -> None: ...


# Called with an SDK session id to resume, or None for a fresh session.
SessionFactory = Callable[[str | None], Awaitable[Session]]
