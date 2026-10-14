"""App loop: poll the channel, route stop/retry words, feed a single worker that talks to the brain.

Besides user messages, the worker runs Argus's own internal tasks (``submit_internal``), e.g. mail
alerts and briefings. They share the day's session, are archived with sender ``system``, and their
reply goes to the user like any other; but they never touch the pending queue or the retry list,
get no slow notice, and send nothing when they fail. User messages and internal tasks wait in two
queues: the worker always takes a waiting user message first, so the user's own message never sits
behind a briefing. An internal task that fails with ``SessionError`` is retried once (re-queued
behind any waiting user message); ``on_result`` reports whether its reply was delivered.

On shutdown (``run`` cancelled) the in-flight item may finish for up to ``shutdown_grace`` seconds;
user messages not yet started stay in the store's pending queue and are replayed at the next start.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections import deque
from dataclasses import dataclass, field, replace
from datetime import datetime
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


SHUTDOWN_GRACE = 45.0  # seconds the in-flight reply may take to finish on shutdown
INTERNAL_ATTEMPTS = 2  # an internal task is retried once after a SessionError


class BrainLike(Protocol):
    async def ask(
        self,
        text: str,
        at: datetime | None = None,
        sender: Literal["user", "system"] = "user",
        replay: bool = False,
    ) -> str: ...

    async def interrupt(self) -> None: ...

    async def close(self) -> None: ...


@dataclass
class _Job:
    msg: IncomingMessage
    internal: bool = False
    # A pending message already archived before a restart cut its turn short.
    replay: bool = False
    # False for 重试 re-runs, which live only in memory (not in the store's pending queue).
    persisted: bool = True
    on_result: Callable[[bool], None] | None = None
    attempt: int = 1
    reported: bool = field(default=False, compare=False)


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
        shutdown_grace: float = SHUTDOWN_GRACE,
    ) -> None:
        self.channel = channel
        self.brain = brain
        self.store = store
        self.poll_interval = poll_interval
        self.slow_notice_seconds = slow_notice_seconds
        self.on_poll_failure = on_poll_failure
        self.poll_failure_notify_after = poll_failure_notify_after
        self.poll_error_log_interval = poll_error_log_interval
        self.shutdown_grace = shutdown_grace
        self._user_jobs: deque[_Job] = deque()
        self._internal_jobs: deque[_Job] = deque()
        self._wakeup = asyncio.Event()
        self._unfinished = 0  # queued + running jobs
        self._idle = asyncio.Event()
        self._idle.set()
        self._stopping = False
        # True while the worker handles a job (shutdown waits for it).
        self._busy = False
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

    async def _route(self, msg: IncomingMessage, replay: bool = False) -> None:
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
                    self._put(
                        _Job(
                            IncomingMessage(
                                id=f"retry-{self._retry_seq}-{now.timestamp():.6f}",
                                text=text,
                                received_at=now,
                            ),
                            persisted=False,
                        )
                    )
            else:
                await self._send(NOTHING_TO_RETRY)
            self._forget(msg)
        else:
            self._put(_Job(msg, replay=replay))

    async def submit_internal(
        self, text: str, on_result: Callable[[bool], None] | None = None
    ) -> None:
        """Queue an Argus-generated task (e.g. a mail alert); its reply is sent to the user.

        ``on_result(True)`` is called once the reply was sent; ``on_result(False)`` when it was not
        (the session failed twice, an unexpected error, the send failed, the user said 停, or Argus
        shut down first). It is called exactly once, from the event loop; its errors are logged.
        """
        self._internal_seq += 1
        now = datetime.now()
        msg = IncomingMessage(
            id=f"internal-{self._internal_seq}-{now.timestamp():.6f}", text=text, received_at=now
        )
        self._put(_Job(msg, internal=True, on_result=on_result))

    # --- the two queues ---

    def _put(self, job: _Job) -> None:
        (self._internal_jobs if job.internal else self._user_jobs).append(job)
        self._unfinished += 1
        self._idle.clear()
        self._wakeup.set()

    async def _next_job(self) -> _Job:
        """The oldest waiting user message, else the oldest internal task."""
        while not (self._user_jobs or self._internal_jobs):
            self._wakeup.clear()
            await self._wakeup.wait()
        return self._user_jobs.popleft() if self._user_jobs else self._internal_jobs.popleft()

    def _job_done(self) -> None:
        self._unfinished -= 1
        if self._unfinished <= 0:
            self._unfinished = 0
            self._idle.set()

    def _report(self, job: _Job, ok: bool) -> None:
        if job.reported:
            return
        job.reported = True
        if job.on_result is None:
            return
        try:
            job.on_result(ok)
        except Exception:
            log.exception("internal task result callback failed")

    def _was_archived(self, msg: IncomingMessage) -> bool:
        """True if a pending message was already archived, i.e. its turn began before a restart."""
        try:
            return self.store.has_message("user", msg.text, msg.received_at)
        except Exception:
            log.exception("could not check whether pending message %s was archived", msg.id)
            return False

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
                await self._route(msg, replay=self._was_archived(msg))
        except Exception:
            log.exception("could not restore pending messages")
        self._stopping = False
        self._worker_task = asyncio.create_task(self._worker(), name="argus-worker")
        try:
            while True:
                self._ensure_worker()
                await self._poll_once()
                await asyncio.sleep(self.poll_interval)
        finally:
            await self._shutdown()

    async def _shutdown(self) -> None:
        """Let the in-flight item finish (up to ``shutdown_grace``), then stop the worker.

        Unstarted user messages stay in the store's pending queue for replay; unstarted 重试
        re-runs go back to the retry list; unstarted internal tasks are reported as not delivered.
        """
        self._stopping = True
        worker = self._worker_task
        try:
            if worker is not None and not worker.done() and self._busy:
                log.info("shutting down: waiting up to %ss for the current reply", self.shutdown_grace)
                await asyncio.wait({worker}, timeout=self.shutdown_grace)
                if not worker.done():
                    log.warning("current reply did not finish within %ss; abandoning it",
                                self.shutdown_grace)
        finally:
            if worker is not None:
                if not worker.done():
                    worker.cancel()
                # return_exceptions: a worker that already died must not mask the shutdown.
                await asyncio.gather(worker, return_exceptions=True)
            self._drop_queued()

    def _drop_queued(self) -> None:
        while self._user_jobs:
            job = self._user_jobs.popleft()
            if not job.persisted:
                self._record_failure(job.msg.text)
            self._job_done()
        while self._internal_jobs:
            job = self._internal_jobs.popleft()
            self._report(job, False)
            self._job_done()

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
        self._busy = False
        self._worker_task = asyncio.create_task(self._worker(), name="argus-worker")

    async def drain(self) -> None:
        """Wait until both queues are empty and nothing is running (test helper)."""
        await self._idle.wait()

    # --- worker (the only task that calls brain.ask / brain.close) ---

    async def _worker(self) -> None:
        try:
            while not self._stopping:
                job = await self._next_job()
                self._busy = True
                try:
                    await self._run_job(job)
                finally:
                    self._busy = False
                    self._job_done()
        finally:
            try:
                await self.brain.close()
            except Exception:
                log.exception("brain.close failed")

    async def _run_job(self, job: _Job) -> None:
        try:
            if job.internal:
                await self._handle_internal(job)
            else:
                await self._handle(job.msg, replay=job.replay)
        except asyncio.CancelledError:
            # Shutdown gave up on this item: a user message stays pending for replay.
            if job.internal:
                self._report(job, False)
            elif not job.persisted:
                self._record_failure(job.msg.text)
            raise
        except Exception:
            log.exception("worker failed handling a message")
            if job.internal:
                self._report(job, False)
            else:
                self._record_failure(job.msg.text)
                self._forget(job.msg)

    async def _handle(self, msg: IncomingMessage, replay: bool = False) -> None:
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
            reply = await self.brain.ask(text, msg.received_at, replay=replay)
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

    async def _handle_internal(self, job: _Job) -> None:
        msg = job.msg
        self._asking = True
        try:
            reply = await self.brain.ask(msg.text, msg.received_at, sender="system")
        except Interrupted:
            await self._send(STOPPED_REPLY)
            self._report(job, False)
            return
        except SessionError as e:
            if job.attempt < INTERNAL_ATTEMPTS:
                log.warning("internal task failed (session error: %s); retrying once", e)
                # Behind any waiting user message, which runs first.
                self._put(replace(job, attempt=job.attempt + 1, reported=False))
                return
            log.warning("internal task failed again (session error: %s); giving up", e)
            self._report(job, False)
            return
        finally:
            self._asking = False
        self._report(job, await self._send(reply))

    async def _send(self, text: str) -> bool:
        """Send ``text``; False (logged) if it could not be sent."""
        try:
            await asyncio.to_thread(self.channel.send, text)
        except SendError:
            log.exception("send failed")
            return False
        return True
