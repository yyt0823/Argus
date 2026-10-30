import sqlite3
from datetime import date, datetime, timedelta

import pytest

from argus.mail.reader import MailNotFound, MailReader, MailReaderError, MailSummary
from tests.mail_fixtures import build_fake_mail

ICLOUD = "AAAAAAAA-0000-0000-0000-000000000001"
GMAIL = "BBBBBBBB-0000-0000-0000-000000000002"
EXCH = "CCCCCCCC-0000-0000-0000-000000000003"
T0 = datetime(2026, 10, 10, 9, 0, 0)


@pytest.fixture
def fm(tmp_path):
    fm = build_fake_mail(tmp_path)
    parent = fm.add_account("PARENT-ID", description="iCloud", username="alice@example.com")
    fm.add_account(ICLOUD, parent=parent)  # child IMAP account with no label of its own
    fm.add_account(GMAIL, description="Google", username="alice@example.com")
    fm.add_account(EXCH, username="alice@school.example.edu")
    return fm


@pytest.fixture
def boxes(fm):
    return {
        "inbox": fm.add_mailbox(f"imap://{ICLOUD}/INBOX"),
        "junk": fm.add_mailbox(f"imap://{ICLOUD}/Junk"),
        "sent": fm.add_mailbox(f"imap://{ICLOUD}/Sent%20Messages"),
        "trash": fm.add_mailbox(f"imap://{ICLOUD}/Deleted%20Messages"),
        "g_all": fm.add_mailbox(f"imap://{GMAIL}/%5BGmail%5D/All%20Mail"),
        "g_inbox": fm.add_mailbox(f"imap://{GMAIL}/INBOX"),
        "g_spam": fm.add_mailbox(f"imap://{GMAIL}/%5BGmail%5D/Spam"),
        "x_inbox": fm.add_mailbox(f"ews://{EXCH}/Inbox"),
        "x_trash": fm.add_mailbox(f"ews://{EXCH}/Deleted%20Items"),
    }


def reader(fm):
    return MailReader(fm.mail_dir, fm.accounts_db)


def add(fm, box, subject="Hi", sender="alice@example.com", name="Alice", at=T0, **kw):
    return fm.add_message(box, sender, name, subject, at, **kw)


# --- construction ---


def test_missing_index_raises(tmp_path):
    with pytest.raises(MailReaderError):
        MailReader(tmp_path / "nope", None)


def test_unreadable_index_raises(tmp_path):
    d = tmp_path / "V10" / "MailData"
    d.mkdir(parents=True)
    (d / "Envelope Index").write_bytes(b"not a database at all" * 100)
    with pytest.raises(MailReaderError):
        MailReader(tmp_path / "V10", None)


def test_index_opened_read_only(fm, boxes):
    r = reader(fm)
    add(fm, boxes["inbox"])
    r.inbox_since(0)
    with pytest.raises(sqlite3.OperationalError):
        with r._connect() as c:
            c.execute("DELETE FROM messages")


# --- inbox_since ---


def test_inbox_since_order_after_id_limit_and_filters(fm, boxes):
    a = add(fm, boxes["inbox"], "one")
    add(fm, boxes["junk"], "junk")
    add(fm, boxes["sent"], "sent")
    add(fm, boxes["inbox"], "deleted", deleted=True)
    b = add(fm, boxes["inbox"], "two", at=T0 + timedelta(minutes=1))
    c = add(fm, boxes["x_inbox"], "three")
    r = reader(fm)
    assert [m.id for m in r.inbox_since(0)] == [a, b, c]
    assert [m.id for m in r.inbox_since(a)] == [b, c]
    assert [m.id for m in r.inbox_since(0, limit=2)] == [a, b]
    assert r.inbox_since(c) == []


def test_summary_fields(fm, boxes):
    mid = add(fm, boxes["inbox"], "Lunch", sender="bob@example.com", name="Bob", snippet="See you at noon",
              subject_prefix="Re: ", read=True)
    nameless = add(fm, boxes["inbox"], "Ping", sender="carol@example.com", name=None)
    m, n = reader(fm).inbox_since(0)
    assert m == MailSummary(id=mid, account="iCloud", sender="Bob <bob@example.com>", subject="Re: Lunch",
                            received_at=T0, snippet="See you at noon", read=True)
    assert n.sender == "carol@example.com" and n.snippet == "" and n.read is False and n.id == nameless


def test_exchange_inbox_url(fm, boxes):
    mid = add(fm, boxes["x_inbox"])
    (m,) = reader(fm).inbox_since(0)
    assert m.id == mid and m.account == "alice@school.example.edu"


def test_gmail_all_mail_with_inbox_label_found_once(fm, boxes):
    g = add(fm, boxes["g_all"], "gmail inbox", labels=(boxes["g_inbox"], boxes["g_all"]))
    add(fm, boxes["g_all"], "archived only")  # no INBOX label -> not inbox
    both = add(fm, boxes["g_inbox"], "direct", labels=(boxes["g_inbox"],))
    got = reader(fm).inbox_since(0)
    assert [m.id for m in got] == [g, both]
    assert got[0].account == "Google"


# --- accounts / max_id / last_received_at ---


def test_accounts_labels_and_uuid_fallback(fm, boxes):
    unknown = "DDDDDDDD-0000-0000-0000-000000000004"
    fm.add_mailbox(f"imap://{unknown}/INBOX")
    fm.add_mailbox("local://EEEEEEEE-0000-0000-0000-000000000005/Outbox")
    assert reader(fm).accounts() == sorted(["iCloud", "Google", "alice@school.example.edu", "DDDDDDDD"])


def test_accounts_db_unreadable_falls_back(fm, boxes):
    add(fm, boxes["inbox"])
    fm.accounts_db.write_bytes(b"garbage" * 100)
    r = MailReader(fm.mail_dir, fm.accounts_db)
    assert r.inbox_since(0)[0].account == ICLOUD[:8]
    assert MailReader(fm.mail_dir, None).accounts()[0] in {ICLOUD[:8], GMAIL[:8], EXCH[:8]}


def test_max_id_and_last_received_at(fm, boxes):
    r = reader(fm)
    assert r.max_id() == 0
    assert r.last_received_at() is None
    add(fm, boxes["inbox"], at=T0)
    add(fm, boxes["g_all"], at=T0 + timedelta(hours=1), labels=(boxes["g_inbox"],))
    junk = add(fm, boxes["junk"], at=T0 + timedelta(hours=5))
    assert r.max_id() == junk
    assert r.last_received_at() == T0 + timedelta(hours=1)


# --- search ---


def test_search_text_case_insensitive_newest_first(fm, boxes):
    a = add(fm, boxes["inbox"], "Exam schedule", at=T0)
    b = add(fm, boxes["x_inbox"], "other", snippet="the EXAM is moved", at=T0 + timedelta(days=1))
    add(fm, boxes["inbox"], "nothing", at=T0)
    assert [m.id for m in reader(fm).search(text="exam")] == [b, a]


def test_search_escapes_like_wildcards(fm, boxes):
    pct = add(fm, boxes["inbox"], "50% off")
    add(fm, boxes["inbox"], "500 off")
    under = add(fm, boxes["inbox"], "a_b")
    add(fm, boxes["inbox"], "axb")
    r = reader(fm)
    assert [m.id for m in r.search(text="50%")] == [pct]
    assert [m.id for m in r.search(text="a_b")] == [under]


def test_search_sender_by_address_or_name(fm, boxes):
    a = add(fm, boxes["inbox"], sender="prof@school.example.edu", name="Dr. Smith")
    add(fm, boxes["inbox"], sender="shop@example.com", name="Shop")
    r = reader(fm)
    assert [m.id for m in r.search(sender="school.example")] == [a]
    assert [m.id for m in r.search(sender="smith")] == [a]


def test_search_account_and_dates(fm, boxes):
    a = add(fm, boxes["inbox"], at=datetime(2026, 10, 1, 23, 59))
    b = add(fm, boxes["x_inbox"], at=datetime(2026, 10, 2, 0, 0))
    c = add(fm, boxes["x_inbox"], at=datetime(2026, 10, 3, 12, 0))
    r = reader(fm)
    assert [m.id for m in r.search(account="school")] == [c, b]
    assert [m.id for m in r.search(account="icloud")] == [a]
    assert [m.id for m in r.search(start=date(2026, 10, 2), end=date(2026, 10, 2))] == [b]
    assert [m.id for m in r.search(start=date(2026, 10, 2))] == [c, b]
    assert [m.id for m in r.search(end=date(2026, 10, 1))] == [a]


def test_search_excludes_junk_trash_deleted_and_limit(fm, boxes):
    keep = add(fm, boxes["inbox"], "match")
    sent = add(fm, boxes["sent"], "match")
    add(fm, boxes["junk"], "match")
    add(fm, boxes["trash"], "match")
    add(fm, boxes["x_trash"], "match")
    add(fm, boxes["g_spam"], "match")
    add(fm, boxes["g_all"], "match", labels=(boxes["g_spam"],))  # Gmail spam via label
    add(fm, boxes["inbox"], "match", deleted=True)
    r = reader(fm)
    assert sorted(m.id for m in r.search(text="match")) == sorted([keep, sent])
    assert len(r.search(text="match", limit=1)) == 1


# --- read_body ---


def test_read_body_plain(fm, boxes):
    mid = add(fm, boxes["inbox"], "Lunch", sender="bob@example.com", name="Bob", body="Meet at noon.\nBring snacks.")
    out = reader(fm).read_body(mid)
    head, _, body = out.partition("\n\n")
    lines = head.splitlines()
    assert lines[0] == "From: Bob <bob@example.com>"
    assert lines[1] == "To: me@example.com"
    assert lines[2].startswith("Date: ")
    assert lines[3] == "Subject: Lunch"
    assert body.strip() == "Meet at noon.\nBring snacks."


def test_read_body_html_to_text(fm, boxes):
    html = ("<html><head><style>p{color:red}</style><title>T</title></head><body>"
            "<script>alert('pwn')</script><p>Hello &amp; welcome</p><p>Line two<br>three</p></body></html>")
    mid = add(fm, boxes["inbox"], body=html, html=True)
    body = reader(fm).read_body(mid).partition("\n\n")[2]
    assert "Hello & welcome" in body
    assert "Line two\nthree" in body
    assert "alert" not in body and "color:red" not in body and "<p>" not in body
    assert "PDF" not in body


def test_read_body_prefers_plain_alternative(fm, boxes):
    mid = add(fm, boxes["inbox"], body="<p>html version</p>", html=True, plain_alt="plain version")
    body = reader(fm).read_body(mid)
    assert "plain version" in body and "html version" not in body


def test_read_body_partial_and_nested(fm, boxes):
    p = add(fm, boxes["inbox"], body="partial body", partial=True)
    n = add(fm, boxes["g_all"], body="nested body", nested=("3", "1"), labels=(boxes["g_inbox"],))
    r = reader(fm)
    assert "partial body" in r.read_body(p)
    assert "nested body" in r.read_body(n)


def test_read_body_truncates(fm, boxes):
    mid = add(fm, boxes["inbox"], body="x" * 5000)
    out = reader(fm).read_body(mid, max_chars=300)
    assert len(out) <= 300


def test_read_body_missing(fm, boxes):
    r = reader(fm)
    with pytest.raises(MailNotFound):
        r.read_body(999)
    nofile = add(fm, boxes["inbox"], write_file=False)
    with pytest.raises(MailNotFound):
        r.read_body(nofile)


def test_read_body_finds_file_written_after_first_scan(fm, boxes):
    r = reader(fm)
    a = add(fm, boxes["inbox"], body="first")
    assert "first" in r.read_body(a)
    b = add(fm, boxes["inbox"], body="second")
    assert "second" in r.read_body(b)


def test_mail_not_found_is_reader_error():
    assert issubclass(MailNotFound, MailReaderError)


# --- robustness: one bad email must never raise beyond MailReaderError ---


def _raw_mail(ctype: str, body: bytes, extra: str = "") -> bytes:
    return (
        b"From: Eve <eve@example.com>\r\nTo: me@example.com\r\nSubject: Poison\r\n"
        b"Date: Sat, 10 Oct 2026 09:00:00 +0000\r\nMIME-Version: 1.0\r\n"
        + extra.encode()
        + f"Content-Type: {ctype}\r\nContent-Transfer-Encoding: 8bit\r\n\r\n".encode()
        + body
    )


def _poisoned(fm, boxes, raw: bytes) -> int:
    mid = add(fm, boxes["inbox"], "Poison", write_file=False)
    fm.write_emlx(f"imap://{ICLOUD}/INBOX", mid, raw)
    return mid


@pytest.mark.parametrize("charset", ["x-no-such-charset", "utf-8\x00junk", "iso-2022-jp-ext", "éé"])
def test_read_body_unknown_or_invalid_charset_decodes_as_utf8(fm, boxes, charset):
    raw = _raw_mail(f'text/plain; charset="{charset}"', "Pay by Friday — thanks \xff".encode("utf-8") + b"\xfe")
    mid = _poisoned(fm, boxes, raw)
    out = reader(fm).read_body(mid)
    assert "Pay by Friday" in out
    assert "Subject: Poison" in out


def test_read_body_invalid_charset_in_html_part(fm, boxes):
    raw = _raw_mail('text/html; charset="bogus-cs"', b"<p>Hello <b>there</b></p>\xff")
    out = reader(fm).read_body(_poisoned(fm, boxes, raw))
    assert "Hello there" in out


def test_read_body_garbage_mime_never_raises_unexpected(fm, boxes):
    raw = _raw_mail('multipart/mixed; boundary="', b"--\r\n\xff\xfe garbage \x00\r\n--")
    mid = _poisoned(fm, boxes, raw)
    try:
        out = reader(fm).read_body(mid)
    except MailReaderError:
        return
    assert isinstance(out, str)


def test_read_body_decoder_crash_becomes_reader_error(fm, boxes, monkeypatch):
    import argus.mail.reader as rd

    mid = add(fm, boxes["inbox"], body="fine")

    def boom(msg):
        raise RuntimeError("decoder exploded")

    monkeypatch.setattr(rd, "_body_text", boom)
    with pytest.raises(MailReaderError):
        reader(fm).read_body(mid)


# --- read_raw ---


def test_read_raw_returns_message_bytes(fm, boxes):
    r = reader(fm)
    a = add(fm, boxes["inbox"], subject="Verify", body="code 123456")
    raw = r.read_raw(a)
    assert raw.startswith(b"From: ")
    assert b"Subject: Verify" in raw and b"code 123456" in raw
    assert b"<plist" not in raw  # trailing plist excluded


def test_read_raw_partial_emlx_is_returned(fm, boxes):
    r = reader(fm)
    a = add(fm, boxes["inbox"], body="partial body", partial=True)
    assert b"partial body" in r.read_raw(a)


def test_read_raw_missing(fm, boxes):
    r = reader(fm)
    with pytest.raises(MailNotFound):
        r.read_raw(999)
    nofile = add(fm, boxes["inbox"], write_file=False)
    with pytest.raises(MailNotFound):
        r.read_raw(nofile)
