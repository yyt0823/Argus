import sqlite3
from datetime import datetime, timezone
import pytest

APPLE_EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)

class FakeChatDB:
    def __init__(self, path):
        self.path = path
        c = sqlite3.connect(path)
        c.executescript("""
            CREATE TABLE message (ROWID INTEGER PRIMARY KEY AUTOINCREMENT, guid TEXT, text TEXT, attributedBody BLOB,
                                  is_from_me INTEGER, date INTEGER, item_type INTEGER DEFAULT 0,
                                  associated_message_type INTEGER DEFAULT 0);
            CREATE TABLE chat (ROWID INTEGER PRIMARY KEY, guid TEXT);
            CREATE TABLE chat_message_join (chat_id INTEGER, message_id INTEGER);
            INSERT INTO chat (ROWID, guid) VALUES (1, 'iMessage;-;+15550000000'), (2, 'iMessage;-;+15559999999');
        """)
        c.commit(); c.close()
        self.n = 0

    def add_chat(self, guid):
        """Add a chat with an arbitrary guid; return its chat_id for ``add``."""
        c = sqlite3.connect(self.path)
        cur = c.execute("INSERT INTO chat (guid) VALUES (?)", (guid,))
        c.commit(); c.close()
        return cur.lastrowid

    def add(self, text, is_from_me, at, chat_id=1, as_blob=False, item_type=0):
        self.n += 1
        ns = int((at.astimezone(timezone.utc) - APPLE_EPOCH).total_seconds() * 1e9)
        blob = make_attributed_body(text) if as_blob else None
        c = sqlite3.connect(self.path)
        cur = c.execute("INSERT INTO message (guid, text, attributedBody, is_from_me, date, item_type) VALUES (?,?,?,?,?,?)",
                        (f"G{self.n}", None if as_blob else text, blob, is_from_me, ns, item_type))
        c.execute("INSERT INTO chat_message_join VALUES (?, ?)", (chat_id, cur.lastrowid))
        c.commit(); c.close()
        return f"G{self.n}"

def make_attributed_body(text):
    data = text.encode()
    n = len(data)
    if n < 0x80: length = bytes([n])
    elif n < 0x10000: length = b"\x81" + n.to_bytes(2, "little")
    else: length = b"\x82" + n.to_bytes(4, "little")
    return b"\x04\x0bstreamtyped\x81\xe8\x03\x84\x01@\x84\x84\x84\x12NSAttributedString\x00\x84\x84\x08NSObject\x00\x85\x92\x84\x84\x84\x08NSString\x01\x94\x84\x01+" + length + data + b"\x86\x84\x02iI\x01"

@pytest.fixture
def fake_chat_db(tmp_path):
    return FakeChatDB(tmp_path / "chat.db")
