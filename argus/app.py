"""App loop: poll the channel, route stop/retry words, feed a single worker that talks to the brain."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Protocol

from argus.channel.base import Channel, SendError
from argus.session import Interrupted, SessionError
from argus.store import Store

log = logging.getLogger(__name__)

STOP_WORD = "停"
RETRY_WORD = "重试"
SLOW_NOTICE = "收到，正在处理…"
STOPPED_REPLY = "已停止。"
NOTHING_TO_STOP = "现在没有在处理的任务。"
NOTHING_TO_RETRY = "没有需要重试的消息。"

LAST_FAILED_KEY = "last_failed"


class BrainLike(Protocol):
    async def ask(self, text: str) -> str: ...

    async def interrupt(self) -> None: ...

    async def close(self) -> None: ...


class App:
    def __init__(
        self,
        channel: Channel,
        brain: BrainLike,
        store: Store,
        poll_interval: float = 1.0,
        slow_notice_seconds: float = 15.0,
    ) -> None:
        self.channel = channel
        self.brain = brain
        self.store = store
        self.poll_interval = poll_interval
        self.slow_notice_seconds = slow_notice_seconds
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        # True while the worker is inside brain.ask(); the only time 停 should interrupt.
        self._asking = False

    # --- polling / routing (runs in the run() task) ---

    async def tick(self) -> None:
        msgs = await asyncio.to_thread(self.channel.poll)
        for msg in msgs:
            await self._route(msg.text)

    async def _route(self, text: str) -> None:
        word = text.strip()
        if word == STOP_WORD:
            if self._asking:
                log.info("stop requested; interrupting current task")
                try:
                    await self.brain.interrupt()
                except Exception:
                    log.exception("brain.interrupt failed")
            else:
                await self._send(NOTHING_TO_STOP)
        elif word == RETRY_WORD:
            last = self.store.get_state(LAST_FAILED_KEY)
            if last:
                self.store.set_state(LAST_FAILED_KEY, "")
                self._queue.put_nowait(last)
            else:
                await self._send(NOTHING_TO_RETRY)
        else:
            self._queue.put_nowait(text)

    async def run(self) -> None:
        worker = asyncio.create_task(self._worker(), name="argus-worker")
        try:
            while True:
                try:
                    await self.tick()
                except Exception:
                    log.exception("poll failed")
                await asyncio.sleep(self.poll_interval)
        finally:
            worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await worker

    async def drain(self) -> None:
        """Wait until the queue is empty and nothing is running (test helper)."""
        await self._queue.join()

    # --- worker (the only task that calls brain.ask / brain.close) ---

    async def _worker(self) -> None:
        try:
            while True:
                text = await self._queue.get()
                try:
                    await self._handle(text)
                except Exception:
                    log.exception("worker failed handling a message")
                finally:
                    self._queue.task_done()
        finally:
            try:
                await self.brain.close()
            except Exception:
                log.exception("brain.close failed")

    async def _handle(self, text: str) -> None:
        notice_started = False

        async def slow_notice() -> None:
            nonlocal notice_started
            await asyncio.sleep(self.slow_notice_seconds)
            notice_started = True
            await self._send(SLOW_NOTICE)

        timer = asyncio.create_task(slow_notice())
        self._asking = True
        try:
            reply = await self.brain.ask(text)
        except Interrupted:
            reply = STOPPED_REPLY
        except SessionError as e:
            log.warning("session error: %s", e)
            self.store.set_state(LAST_FAILED_KEY, text)
            reply = f"出错了：{e}"
        except Exception as e:
            log.exception("unexpected error while answering")
            self.store.set_state(LAST_FAILED_KEY, text)
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

    async def _send(self, text: str) -> None:
        try:
            await asyncio.to_thread(self.channel.send, text)
        except SendError:
            log.exception("send failed")
