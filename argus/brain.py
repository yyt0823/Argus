"""Brain: owns the daily Claude session lifecycle and archives every exchange to the store."""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime
from typing import Awaitable, Callable, Literal

from argus import turn_context
from argus.scheduler import day_key
from argus.session import Interrupted, Session, SessionError, SessionFactory
from argus.store import Store

log = logging.getLogger(__name__)

ASK_TIMEOUT = 600.0  # seconds one turn may take before the session is killed
INTERRUPT_GRACE = 10.0  # seconds a stopped turn may take to end before the session is killed
CLOSE_TIMEOUT = 10.0  # seconds to wait for a discarded session to close
TIMEOUT_MESSAGE = "回复超时"
EMPTY_REPLY_MESSAGE = "空回复"
REPLAY_NOTE = (
    "[This message was interrupted by a restart before you replied. Some tool calls may already "
    "have run — check before repeating them, and reply normally without mentioning the restart "
    "unless relevant.]"
)


class Brain:
    def __init__(
        self,
        store: Store,
        session_factory: SessionFactory,
        now: Callable[[], datetime] = datetime.now,
        rollover_hour: int = 4,
        on_rollover: Callable[[date], Awaitable[None]] | None = None,
        ask_timeout: float = ASK_TIMEOUT,
        interrupt_grace: float = INTERRUPT_GRACE,
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
        self._ask_timeout = ask_timeout
        self._interrupt_grace = interrupt_grace
        self._asking = False
        self._pending_interrupt = False
        # The timeout scope around the running session.ask; interrupt() pulls its deadline in.
        self._timeout: asyncio.Timeout | None = None
        self._forced_stop = False

    async def ask(
        self,
        text: str,
        at: datetime | None = None,
        sender: Literal["user", "system"] = "user",
        replay: bool = False,
    ) -> str:
        """Answer one incoming message. ``at`` is when it was sent (default: now).

        The session is chosen by the current time; ``at`` stamps the message for Claude
        and timestamps the archived incoming message. ``sender`` is ``"system"`` for
        Argus's own internal tasks, which are archived as such instead of as the user.
        ``replay`` marks a message already archived before a restart cut its turn short: it is
        not archived again and Claude is told its tool calls may already have run.

        Raises ``SessionError("回复超时")`` when the turn exceeds ``ask_timeout`` (the session
        is killed), ``Interrupted`` after ``interrupt()`` (the session is killed if the turn
        has not ended ``interrupt_grace`` seconds later), and ``SessionError("空回复")`` when
        Claude's reply is empty.
        """
        if at is None:
            at = self._now()
        self._pending_interrupt = False
        self._forced_stop = False
        self._asking = True
        try:
            session = await self._ensure_session()
            row_id = self._row_id
            if not replay:
                self._store.add_message(sender, text, at, session_id=row_id)
            if self._pending_interrupt:
                # Stop arrived while the session was still being created.
                self._pending_interrupt = False
                raise Interrupted()
            # The session's system prompt only knows when the session started; tell Claude the time.
            body = f"{REPLAY_NOTE} {text}" if replay else text
            stamped = f"[{at:%Y-%m-%d %H:%M}] {body}"
            try:
                with turn_context.turn(system=sender == "system", at=at):
                    async with asyncio.timeout(self._ask_timeout) as scope:
                        self._timeout = scope
                        try:
                            reply = await session.ask(stamped)
                        finally:
                            self._timeout = None
            except Interrupted:
                raise
            except TimeoutError:
                await self._discard_session(session)
                if self._forced_stop:
                    log.warning("turn did not stop within %ss of an interrupt; session killed",
                                self._interrupt_grace)
                    raise Interrupted() from None
                log.warning("turn timed out after %ss; session killed", self._ask_timeout)
                raise SessionError(TIMEOUT_MESSAGE) from None
            except Exception:
                await self._discard_session(session)
                raise
        finally:
            self._asking = False
        sdk_id = session.sdk_session_id
        if sdk_id and sdk_id != self._stored_sdk_id:
            self._store.set_session_sdk_id(row_id, sdk_id)
            self._stored_sdk_id = sdk_id
        if not isinstance(reply, str) or not reply.strip():
            log.warning("Claude returned an empty reply")
            raise SessionError(EMPTY_REPLY_MESSAGE)
        self._store.add_message("argus", reply, self._now(), session_id=row_id)
        return reply

    async def interrupt(self) -> None:
        if self._session is not None:
            scope = self._timeout
            if scope is not None:
                # If the turn ignores the interrupt, kill the session after the grace period so
                # the worker is released.
                deadline = asyncio.get_running_loop().time() + self._interrupt_grace
                current = scope.when()
                if current is None or deadline < current:
                    self._forced_stop = True
                    scope.reschedule(deadline)
            await self._session.interrupt()
        elif self._asking:
            # An ask is still creating its session; it checks this flag before asking.
            self._pending_interrupt = True

    async def _discard_session(self, session: Session) -> None:
        """Drop a failed session. The day's row (and ``_day``/``_row_id``/``_stored_sdk_id``)
        stays, so the next ask resumes it via ``factory(stored_sdk_id)``."""
        if self._session is session:
            self._session = None
        try:
            async with asyncio.timeout(CLOSE_TIMEOUT):
                await session.close()
        except Exception:
            log.exception("closing failed session failed")

    async def close(self) -> None:
        """Close the live session. The day's session row stays open so a restart can resume it."""
        session, self._session = self._session, None
        self._day = self._row_id = self._stored_sdk_id = None
        if session is not None:
            await session.close()

    async def _ensure_session(self) -> Session:
        today = day_key(self._now(), self._rollover_hour)

        # Keyed on _day, not _session: a failed session is dropped but its day stays live.
        if self._day is not None and self._day != today:
            old_session, old_row = self._session, self._row_id
            self._session = None
            self._day = self._row_id = self._stored_sdk_id = None
            if old_session is not None:
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
