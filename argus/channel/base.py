"""Channel interface: the pipe between the user and Argus."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class IncomingMessage:
    id: str  # channel-specific unique id (iMessage guid)
    text: str
    received_at: datetime  # local, timezone-naive


class Channel(ABC):
    @abstractmethod
    def poll(self) -> list[IncomingMessage]:
        """Return new user messages since the last call, oldest first.

        Never returns messages Argus sent itself. Blocking; callers run it in a thread.
        """

    @abstractmethod
    def send(self, text: str) -> None:
        """Send text to the user. The channel adds the Argus prefix.

        Raises SendError after retries are exhausted. Blocking; callers run it in a thread.
        """


class SendError(Exception):
    pass
