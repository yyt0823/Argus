"""Context of the turn currently being handled, shared by Brain and tool handlers.

Process-wide (not contextvars): the SDK runs in-process tool handlers on a task created when the
client connected, so contextvars set around `session.ask` would not reach them. This is safe
because the App worker handles exactly one turn at a time. Only Brain enters `turn()`.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
from typing import Iterator

_system_turn = False
_message_time: datetime | None = None


def is_system_turn() -> bool:
    """True while Argus handles an internal task (briefing, alert) rather than a user message.

    Write tools refuse during system turns: their prompts carry untrusted email/web text.
    """
    return _system_turn


def message_time() -> datetime | None:
    """Send time of the message being handled, or None outside a turn."""
    return _message_time


@contextmanager
def turn(*, system: bool, at: datetime) -> Iterator[None]:
    global _system_turn, _message_time
    prev = (_system_turn, _message_time)
    _system_turn, _message_time = system, at
    try:
        yield
    finally:
        _system_turn, _message_time = prev
