"""Brain: owns the daily Claude session lifecycle and archives every exchange to the store."""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Awaitable, Callable

from argus.scheduler import day_key
from argus.session import Session, SessionError, SessionFactory
from argus.store import Store

log = logging.getLogger(__name__)


class Brain:
    def __init__(
        self,
        store: Store,
        session_factory: SessionFactory,
        now: Callable[[], datetime] = datetime.now,
        rollover_hour: int = 4,
        on_rollover: Callable[[date], Awaitable[None]] | None = None,
    ) -> None:
        self._store = store
        self._factory = session_factory
        self._now = now
        self._rollover_hour = rollover_hour
        self._on_rollover = on_rollover
        self._session: Session | None = None
        self._day: date | None = None
        self._row_id: int | None = None
        self._stored_sdk_id: str | None = None

    async def ask(self, text: str) -> str:
        session = await self._ensure_session()
        row_id = self._row_id
        self._store.add_message("user", text, self._now(), session_id=row_id)
        # The session's system prompt only knows when the session started; tell Claude the time now.
        stamped = f"[{self._now():%Y-%m-%d %H:%M}] {text}"
        reply = await session.ask(stamped)  # Interrupted / SessionError propagate
        sdk_id = session.sdk_session_id
        if sdk_id and sdk_id != self._stored_sdk_id:
            self._store.set_session_sdk_id(row_id, sdk_id)
            self._stored_sdk_id = sdk_id
        self._store.add_message("argus", reply, self._now(), session_id=row_id)
        return reply

    async def interrupt(self) -> None:
        if self._session is not None:
            await self._session.interrupt()

    async def close(self) -> None:
        """Close the live session. The day's session row stays open so a restart can resume it."""
        session, self._session = self._session, None
        self._day = self._row_id = self._stored_sdk_id = None
        if session is not None:
            await session.close()

    async def _ensure_session(self) -> Session:
        today = day_key(self._now(), self._rollover_hour)

        if self._session is not None and self._day != today:
            old_session, old_row = self._session, self._row_id
            self._session = None
            self._day = self._row_id = self._stored_sdk_id = None
            try:
                await old_session.close()
            except Exception:
                log.exception("closing previous day's session failed")
            self._store.end_session(old_row, self._now())
            if self._on_rollover is not None:
                try:
                    await self._on_rollover(today)
                except Exception:
                    log.exception("on_rollover hook failed")

        if self._session is None:
            row = self._store.get_session(today)
            if row is None:
                row_id = self._store.create_session(today, self._now())
                sdk_id = None
                session = await self._factory(None)
            else:
                row_id = row["id"]
                sdk_id = row["sdk_session_id"]
                if sdk_id:
                    try:
                        session = await self._factory(sdk_id)
                    except SessionError as e:
                        log.warning("resuming session %s failed (%s); starting fresh", sdk_id, e)
                        session = await self._factory(None)
                else:
                    session = await self._factory(None)
            self._session, self._day, self._row_id, self._stored_sdk_id = session, today, row_id, sdk_id

        return self._session
