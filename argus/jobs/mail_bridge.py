"""Copy new Mail.app inbox messages into a directory of ``.eml`` files for jobagent."""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from argus.mail.reader import MailNotFound, MailReader, MailReaderError

log = logging.getLogger(__name__)


def export_new(reader: MailReader, after_id: int, out_dir: Path, exported: set[int]) -> int:
    """Write ``<id>.eml`` for each inbox message after ``after_id`` not yet exported; returns count written.

    A message whose file is not readable yet is skipped and retried on the next call.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    written = 0
    for summary in reader.inbox_since(after_id, limit=200):
        if summary.id in exported:
            continue
        try:
            raw = reader.read_raw(summary.id)
        except (MailNotFound, MailReaderError):
            continue
        final = out_dir / f"{summary.id}.eml"
        tmp = out_dir / f"{summary.id}.eml.tmp"
        tmp.write_bytes(raw)
        os.replace(tmp, final)
        exported.add(summary.id)
        written += 1
    return written


async def run_bridge(reader: MailReader, after_id: int, out_dir: Path, poll_s: float = 5.0) -> None:
    """Poll ``export_new`` until cancelled; unexpected errors are logged and polling continues."""
    exported: set[int] = set()
    while True:
        try:
            await asyncio.to_thread(export_new, reader, after_id, out_dir, exported)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("mail bridge poll failed")
        await asyncio.sleep(poll_s)
