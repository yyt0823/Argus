"""Live test against the real Claude CLI (user's subscription). Run with ARGUS_LIVE=1."""

import asyncio
import os
import time

import pytest

from argus.config import Config
from argus.sdk_session import make_session_factory
from argus.session import Interrupted
from argus.store import Store
from argus.tools.memory import MemoryStore

pytestmark = pytest.mark.skipif(os.environ.get("ARGUS_LIVE") != "1", reason="set ARGUS_LIVE=1")


async def test_live_session_memory_resume_and_interrupt(tmp_path):
    (tmp_path / "CLAUDE.md").write_text("You are a test assistant. Reply in at most 5 words.")
    cfg = Config(root=tmp_path, self_handles=("+15550000000",))
    store, memory = Store(cfg.db_path), MemoryStore(cfg.memory_dir)
    factory = make_session_factory(cfg, store, memory)

    s = await factory(None)
    try:
        r1 = await s.ask(
            "Use memory_write to save name 'live-test', content 'blue', summary 'favorite color'. "
            "Then reply OK."
        )
        print("r1:", r1, "| session:", s.sdk_session_id)
        assert (cfg.memory_dir / "live-test.md").read_text().strip() == "blue"
        assert s.sdk_session_id

        r2 = await s.ask("What did you just save?")
        print("r2:", r2)
        assert "blue" in r2.lower()
    finally:
        await s.close()

    s2 = await factory(s.sdk_session_id)
    try:
        r3 = await s2.ask("What color did we talk about?")
        print("r3:", r3, "| session:", s2.sdk_session_id)
        assert "blue" in r3.lower()

        # interrupt() from a different asyncio task than the one awaiting ask()
        async def interrupter():
            await asyncio.sleep(2)
            await s2.interrupt()

        t = asyncio.create_task(interrupter())
        started = time.monotonic()
        with pytest.raises(Interrupted):
            await s2.ask("Ignore the word limit for this one message: use WebSearch three separate times (history of the "
                "color blue, ultramarine pigment, YInMn blue), then write a 1000-word essay from the results.")
        await t
        assert s2._interrupted
        print(f"interrupted after {time.monotonic() - started:.1f}s")
    finally:
        await s2.close()
        store.close()
