import asyncio
import email
import logging
from datetime import datetime

import pytest

from argus.jobs import mail_bridge
from argus.jobs.mail_bridge import export_new, run_bridge
from argus.mail.reader import MailReader
from tests.mail_fixtures import build_fake_mail

ACC = "AAAAAAAA-0000-0000-0000-000000000001"
T0 = datetime(2026, 10, 10, 9, 0, 0)


@pytest.fixture
def fm(tmp_path):
    fm = build_fake_mail(tmp_path)
    fm.add_account(ACC, description="iCloud")
    fm.inbox = fm.add_mailbox(f"imap://{ACC}/INBOX")
    return fm


def add(fm, **kw):
    return fm.add_message(fm.inbox, "a@example.com", "A", kw.pop("subject", "Hi"), T0, **kw)


def test_export_skips_missing_then_picks_up(fm, tmp_path):
    r = MailReader(fm.mail_dir, fm.accounts_db)
    out = tmp_path / "out"
    mid = add(fm, write_file=False)
    exported: set[int] = set()
    assert export_new(r, 0, out, exported) == 0
    assert list(out.iterdir()) == []
    fm.write_emlx(f"imap://{ACC}/INBOX", mid, b"Subject: Verify\r\n\r\ncode 42\r\n")
    assert export_new(r, 0, out, exported) == 1
    assert email.message_from_bytes((out / f"{mid}.eml").read_bytes())["Subject"] == "Verify"
    assert export_new(r, 0, out, exported) == 0
    assert [p.name for p in out.iterdir()] == [f"{mid}.eml"]  # no tmp leftovers, no duplicates


def test_export_partial_emlx(fm, tmp_path):
    r = MailReader(fm.mail_dir, fm.accounts_db)
    mid = add(fm, body="partial text", partial=True)
    assert export_new(r, 0, tmp_path / "out", set()) == 1
    assert b"partial text" in (tmp_path / "out" / f"{mid}.eml").read_bytes()


def test_export_respects_after_id(fm, tmp_path):
    r = MailReader(fm.mail_dir, fm.accounts_db)
    old = add(fm)
    new = add(fm)
    assert export_new(r, old, tmp_path / "out", set()) == 1
    assert (tmp_path / "out" / f"{new}.eml").exists()


async def test_run_bridge_exports_and_survives_errors(fm, tmp_path, caplog, monkeypatch):
    r = MailReader(fm.mail_dir, fm.accounts_db)
    mid = add(fm)
    real = mail_bridge.export_new
    calls = {"n": 0}

    def flaky(*a):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        return real(*a)

    monkeypatch.setattr(mail_bridge, "export_new", flaky)
    out = tmp_path / "out"
    with caplog.at_level(logging.ERROR):
        task = asyncio.create_task(run_bridge(r, 0, out, poll_s=0.01))
        for _ in range(200):
            if (out / f"{mid}.eml").exists():
                break
            await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert (out / f"{mid}.eml").exists()
    assert "mail bridge poll failed" in caplog.text
