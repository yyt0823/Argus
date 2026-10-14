"""App loop: poll the channel, route stop/retry words, feed a single worker that talks to the brain.

Besides user messages, the worker runs Argus's own internal tasks (``submit_internal``), e.g. mail
alerts and briefings. They share the FIFO queue and the day's session, are archived with sender
``system``, and their reply goes to the user like any other; but they never touch the pending queue
or the retry list, get no slow notice, and send nothing when they fail.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from datetime import datetime
from dataclasses import dataclass
from typing import Callable, Literal, Protocol

from argus.channel.base import Channel, IncomingMessage, SendError
from argus.session import Interrupted, SessionError
from argus.store import Store

log = logging.getLogger(__name__)

STOP_WORD = "停"
RETRY_WORD = "重试"
SLOW_NOTICE = "收到，正在处理…"
STOPPED_REPLY = "已停止。"
NOTHING_TO_STOP = "现在没有在处理的任务。"
NOTHING_TO_RETRY = "没有需要重试的消息。"

LAST_FAILED_KEY = "last_failed"  # JSON list of failed message texts, oldest first
MAX_FAILED = 20


class BrainLike(Protocol):
    async def ask(
        self, text: str, at: datetime | None = None, sender: Literal["user", "system"] = "user"
    ) -> str: ...

    async def interrupt(self) -> None: ...

    async def close(self) -> None: ...


@dataclass(frozen=True)
class _Job:
    msg: IncomingMessage
    internal: bool = False


class App:
    def __init__(
        self,
        channel: Channel,
        brain: BrainLike,
        store: Store,
        poll_interval: float = 1.0,
        slow_notice_seconds: float = 15.0,
        on_poll_failure: Callable[[str], None] | None = None,
        poll_failure_notify_after: int = 60,
        poll_error_log_interval: float = 300.0,
    ) -> None:
        self.channel = channel
        self.brain = brain
        self.store = store
        self.poll_interval = poll_interval
        self.slow_notice_seconds = slow_notice_seconds
        self.on_poll_failure = on_poll_failure
        self.poll_failure_notify_after = poll_failure_notify_after
        self.poll_error_log_interval = poll_error_log_interval
        self._queue: asyncio.Queue[_Job] = asyncio.Queue()
        # True while the worker is inside brain.ask(); the only time 停 should interrupt.
        self._asking = False
        self._worker_task: asyncio.Task | None = None
        self._retry_seq = 0
        self._internal_seq = 0
        # Poll-failure bookkeeping (rate-limited logging, one notification per outage).
        self._poll_failures = 0
        self._last_poll_log = 0.0
        self._poll_notified = False

    # --- polling / routing (runs in the run() task) ---

    async def tick(self) -> None:
        msgs = await asyncio.to_thread(self.channel.poll)
        for msg in msgs:
            await self._route(msg)

    async def _route(self, msg: IncomingMessage) -> None:
        word = msg.text.strip()
        if word == STOP_WORD:
            if self._asking:
                log.info("stop requested; interrupting current task")
                try:
                    await self.brain.interrupt()
                except Exception:
                    log.exception("brain.interrupt failed")
            else:
                await self._send(NOTHING_TO_STOP)
            self._forget(msg)
        elif word == RETRY_WORD:
            failed = self._load_failed()
            if failed:
                self._save_failed([])
                now = datetime.now()
                for text in failed:
                    self._retry_seq += 1
                    self._queue.put_nowait(
                        _Job(
                            IncomingMessage(
                                id=f"retry-{self._retry_seq}-{now.timestamp():.6f}",
                                text=text,
                                received_at=now,
                            )
                        )
                    )
            else:
                await self._send(NOTHING_TO_RETRY)
            self._forget(msg)
        else:
            self._queue.put_nowait(_Job(msg))

    async def submit_internal(self, text: str) -> None:
        """Queue an Argus-generated task (e.g. a mail alert); its reply is sent to the user."""
        self._internal_seq += 1
        now = datetime.now()
        msg = IncomingMessage(
            id=f"internal-{self._internal_seq}-{now.timestamp():.6f}", text=text, received_at=now
        )
        self._queue.put_nowait(_Job(msg, internal=True))

    def _forget(self, msg: IncomingMessage) -> None:
        """Drop a fully handled message from the persistent pending queue."""
        try:
            self.store.delete_pending(msg.id)
        except Exception:
            log.exception("could not delete pending message %s", msg.id)

    def _load_failed(self) -> list[str]:
        raw = self.store.get_state(LAST_FAILED_KEY)
        if not raw:
            return []
        try:
            value = json.loads(raw)
        except ValueError:
            return [raw]  # legacy: a single plain-text message
        if isinstance(value, list):
            return [str(v) for v in value]
        return [raw]

    def _save_failed(self, texts: list[str]) -> None:
        self.store.set_state(LAST_FAILED_KEY, json.dumps(texts[-MAX_FAILED:], ensure_ascii=False))

    def _record_failure(self, text: str) -> None:
        try:
            self._save_failed(self._load_failed() + [text])
        except Exception:
            log.exception("could not record failed message")

    async def _poll_once(self) -> None:
        try:
            await self.tick()
        except Exception as e:
            note = self._poll_failed(e)
            if note is not None and self.on_poll_failure is not None:
                try:
                    await asyncio.to_thread(self.on_poll_failure, note)
                except Exception:
                    log.exception("poll-failure callback failed")
            return
        if self._poll_failures:
            log.info("polling recovered after %d failed attempts", self._poll_failures)
            self._poll_failures = 0
            self._poll_notified = False

    def _poll_failed(self, e: Exception) -> str | None:
        """Count and (rate-limited) log a poll failure; return a notification text once per outage."""
        self._poll_failures += 1
        now = time.monotonic()
        if self._poll_failures == 1:
            log.exception("poll failed")
            self._last_poll_log = now
        elif now - self._last_poll_log >= self.poll_error_log_interval:
            log.error("poll still failing (%d attempts): %s: %s", self._poll_failures, type(e).__name__, e)
            self._last_poll_log = now
        if (
            not self._poll_notified
            and self._poll_failures >= self.poll_failure_notify_after
            and self.on_poll_failure is not None
        ):
            self._poll_notified = True
            return f"Argus cannot read new messages: {type(e).__name__}: {e}"
        return None

    async def run(self) -> None:
        # Messages accepted before a restart but never fully handled come first.
        try:
            for msg in self.store.list_pending():
                await self._route(msg)
        except Exception:
            log.exception("could not restore pending messages")
        self._worker_task = asyncio.create_task(self._worker(), name="argus-worker")
        try:
            while True:
                self._ensure_worker()
                await self._poll_once()
                await asyncio.sleep(self.poll_interval)
        finally:
            worker = self._worker_task
            worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await worker

    def _ensure_worker(self) -> None:
        worker = self._worker_task
        if worker is None or not worker.done():
            return
        if worker.cancelled():
            log.error("worker was cancelled unexpectedly; restarting it")
        elif worker.exception() is not None:
            log.error("worker died; restarting it", exc_info=worker.exception())
        else:
            log.error("worker exited unexpectedly; restarting it")
        self._asking = False
        self._worker_task = asyncio.create_task(self._worker(), name="argus-worker")

    async def drain(self) -> None:
        """Wait until the queue is empty and nothing is running (test helper)."""
        await self._queue.join()

    # --- worker (the only task that calls brain.ask / brain.close) ---

    async def _worker(self) -> None:
        try:
            while True:
                job = await self._queue.get()
                try:
                    if job.internal:
                        await self._handle_internal(job.msg)
                    else:
                        await self._handle(job.msg)
                except Exception:
                    log.exception("worker failed handling a message")
                    if not job.internal:
                        self._record_failure(job.msg.text)
                        self._forget(job.msg)
                finally:
                    self._queue.task_done()
        finally:
            try:
                await self.brain.close()
            except Exception:
                log.exception("brain.close failed")

    async def _handle(self, msg: IncomingMessage) -> None:
        text = msg.text
        notice_started = False

        async def slow_notice() -> None:
            nonlocal notice_started
            await asyncio.sleep(self.slow_notice_seconds)
            notice_started = True
            await self._send(SLOW_NOTICE)

        timer = asyncio.create_task(slow_notice())
        self._asking = True
        try:
            reply = await self.brain.ask(text, msg.received_at)
        except Interrupted:
            reply = STOPPED_REPLY
        except SessionError as e:
            log.warning("session error: %s", e)
            self._record_failure(text)
            reply = f"出错了：{e}"
        except Exception as e:
            log.exception("unexpected error while answering")
            self._record_failure(text)
            reply = f"出错了：{type(e).__name__}"
        finally:
            self._asking = False
            if notice_started:
                # Let an in-flight notice finish so it lands before the reply.
                with contextlib.suppress(Exception):
                    await timer
            else:
                timer.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await timer
        await self._send(reply)
        self._forget(msg)

    async def _handle_internal(self, msg: IncomingMessage) -> None:
        self._asking = True
        try:
            reply = await self.brain.ask(msg.text, msg.received_at, sender="system")
        except Interrupted:
            reply = STOPPED_REPLY
        except SessionError as e:
            log.warning("internal task failed (session error): %s", e)
            return
        except Exception:
            log.exception("internal task failed")
            return
        finally:
            self._asking = False
        await self._send(reply)

    async def _send(self, text: str) -> None:
        try:
            await asyncio.to_thread(self.channel.send, text)
        except SendError:
            log.exception("send failed")
