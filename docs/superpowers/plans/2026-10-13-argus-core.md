# Argus Core Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A long-running Python daemon on the user's Mac mini that receives the user's iMessage self-chat messages, answers them through a daily Claude Agent SDK session with memory/data/history tools, and replies in the same chat with a `🤖 ` prefix.

**Architecture:** `main.py` wires the units: `channel/imessage.py` (chat.db polling + AppleScript send), `app.py` (poll loop, queue, stop/retry words, slow notice), `brain.py` (daily session lifecycle + message archive; talks to the `Session` protocol), `sdk_session.py` (the real `Session`, backed by `ClaudeSDKClient`), `tools/` (pure functions + one SDK MCP server wrapping them), and `store.py` (the only writer of `data/argus.db`). `scheduler.py` holds the 4 AM day-boundary math and backups. Shared contracts already exist in `argus/channel/base.py` and `argus/session.py` — do not change them.

**Tech Stack:** Python 3.14 (stdlib `sqlite3`, `tomllib`, `asyncio`), `claude-agent-sdk` 0.2.165, `pytest` + `pytest-asyncio` (`asyncio_mode = "auto"`), macOS `osascript`, launchd.

**Spec:** `docs/superpowers/specs/2026-10-13-argus-core-design.md`

**Run tests with:** `.venv/bin/python -m pytest -q` from the repo root.

## Global Constraints

- Argus messages are a single iMessage starting with `🤖 ` (robot emoji + one space).
- Day boundary (session rollover + backup) is 04:00 local time.
- Poll interval 1 second; slow-task notice after 15 seconds, text `收到，正在处理…` (channel adds the prefix).
- Stop word: a message whose stripped text is exactly `停`. Retry word: exactly `重试`.
- Error reply text: `出错了：<reason>` (channel adds the prefix).
- iMessage send: 3 attempts before raising `SendError`.
- Backups: `backups/<YYYY-MM-DD>/` holding a copy of `data/argus.db` and `memory/`; keep the newest 30.
- chat.db is opened read-only, always. `query_data` opens `data/argus.db` read-only.
- Memory tools may only touch `*.md` files directly inside `memory/`.
- Personal data (`data/`, `memory/`, `backups/`, `logs/`, `argus.toml`) is git-ignored; the repo is public — never commit the user's handle or any message content, including in tests (use fake handles like `+15550000000`).
- Code, comments, docs, commit messages: English. User-facing reply strings above stay Chinese.
- Times are local, timezone-naive `datetime`s, stored as ISO-8601 text (`dt.isoformat(timespec="seconds")`).
- Claude runs on the user's subscription via the CLI bundled in the SDK; never set or require `ANTHROPIC_API_KEY`.

## Review Focus

1. **Self-chat echo / duplicates** — one message the user types must produce exactly one reply, even if chat.db holds two rows for it; Argus's own sends must never be answered. (Task 5 tests.)
2. **Restart mid-day** — after a crash or reboot, Argus resumes from the chat.db checkpoint without re-answering old messages and resumes today's SDK session. (Tasks 5, 7 tests.)
3. **Chinese full-text search** — `search_history("膝盖")` (2 CJK chars) must find `我膝盖疼`; FTS default tokenizers don't segment CJK. (Task 1 test.)
4. **Stop while idle / stop while queued** — `停` with nothing running must not crash and must reply something sensible; messages queued behind an interrupted task still run. (Task 9 tests.)
5. **SQL from the model that tries to write** — `query_data("DELETE FROM messages")` must fail cleanly and change nothing; multi-statement input must be rejected. (Task 3 tests.)

---

## File Map

| File | Responsibility | Task |
|---|---|---|
| `argus/config.py` | `Config` dataclass + `load_config(root)` from `argus.toml` | 1 |
| `argus/store.py` | SQLite schema + all writes/reads of `data/argus.db` | 1 |
| `argus/tools/memory.py` | `MemoryStore`: Markdown memory files + `MEMORY.md` index | 2 |
| `argus/tools/data.py` | `query_readonly()` — read-only SQL | 3 |
| `argus/tools/history.py` | `format_history()` — render search hits for the model | 3 |
| `argus/scheduler.py` | `day_key`, `seconds_until_rollover`, `backup` | 4 |
| `argus/channel/imessage.py` | `IMessageChannel` (chat.db + AppleScript) + `decode_attributed_body` | 5 |
| `argus/tools/server.py` | `build_tool_server()` — SDK MCP server wrapping tasks 2–3 | 6 |
| `argus/sdk_session.py` | `SDKSession`, `make_session_factory()`, `build_system_prompt()` | 7 |
| `argus/brain.py` | `Brain`: daily session lifecycle, archive, rollover hook | 8 |
| `argus/app.py` | `App`: poll loop, queue, stop/retry, slow notice, error replies | 9 |
| `argus/main.py`, `CLAUDE.md`, `argus.example.toml`, `ops/`, `setup.sh`, `README.md` | Wiring, persona, ops | 10 |

Already present (do not modify): `argus/channel/base.py` (`Channel`, `IncomingMessage`, `SendError`), `argus/session.py` (`Session`, `SessionFactory`, `SessionError`, `Interrupted`).

---

### Task 1: Config and Store

**Files:**
- Create: `argus/config.py`, `argus/store.py`
- Test: `tests/test_config.py`, `tests/test_store.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  ```python
  # argus/config.py
  @dataclass(frozen=True)
  class Config:
      root: Path
      self_handles: tuple[str, ...]            # user's own iMessage handles, e.g. ("+15550000000", "me@example.com")
      chat_db: Path = Path.home() / "Library/Messages/chat.db"
      poll_interval: float = 1.0
      rollover_hour: int = 4
      slow_notice_seconds: float = 15.0
      backup_keep: int = 30
      model: str | None = None
      # properties: data_dir = root/"data", db_path = data_dir/"argus.db",
      #             memory_dir = root/"memory", backups_dir = root/"backups", logs_dir = root/"logs"
  def load_config(root: Path) -> Config   # reads root/"argus.toml" (tomllib); missing file or empty self_handles -> ValueError with a clear message

  # argus/store.py
  class Store:
      def __init__(self, db_path: Path) -> None          # creates parent dir + schema; check_same_thread=False, guarded by a threading.Lock
      def close(self) -> None
      def add_message(self, sender: Literal["user", "argus"], content: str, ts: datetime, session_id: int | None = None) -> int
      def search_messages(self, query: str, start: date | None = None, end: date | None = None, limit: int = 20) -> list[dict]
          # dict keys: id, ts, sender, content; newest first; start/end inclusive by calendar date of ts
      def get_session(self, day: date) -> dict | None   # keys: id, day, sdk_session_id, started_at, ended_at
      def create_session(self, day: date, started_at: datetime) -> int
      def set_session_sdk_id(self, session_id: int, sdk_session_id: str) -> None
      def end_session(self, session_id: int, ended_at: datetime) -> None
      def add_sent_id(self, guid: str, sent_at: datetime) -> None   # idempotent
      def has_sent_id(self, guid: str) -> bool
      def get_state(self, key: str, default: str | None = None) -> str | None
      def set_state(self, key: str, value: str) -> None
  ```

Schema (create with `CREATE TABLE IF NOT EXISTS`):
```sql
CREATE TABLE messages (id INTEGER PRIMARY KEY, ts TEXT NOT NULL, sender TEXT NOT NULL CHECK (sender IN ('user','argus')), content TEXT NOT NULL, session_id INTEGER);
CREATE VIRTUAL TABLE messages_fts USING fts5(content, content='messages', content_rowid='id', tokenize='trigram');
CREATE TRIGGER messages_ai AFTER INSERT ON messages BEGIN INSERT INTO messages_fts(rowid, content) VALUES (new.id, new.content); END;
CREATE TABLE sessions (id INTEGER PRIMARY KEY, day TEXT NOT NULL UNIQUE, sdk_session_id TEXT, started_at TEXT NOT NULL, ended_at TEXT);
CREATE TABLE sent_ids (guid TEXT PRIMARY KEY, sent_at TEXT NOT NULL);
CREATE TABLE state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
```
Search rule: split `query` on whitespace into terms. If every term has ≥3 characters, use `messages_fts MATCH` with each term double-quoted (escape `"` as `""`) and joined by spaces (AND). Otherwise fall back to `content LIKE '%term%'` for each term (AND), with `%`/`_` escaped via `ESCAPE '\'`. Empty query → `[]`.

- [ ] **Step 1: Write failing tests**

```python
# tests/test_config.py
from pathlib import Path
import pytest
from argus.config import load_config

def test_load_config_reads_handles_and_defaults(tmp_path: Path):
    (tmp_path / "argus.toml").write_text('self_handles = ["+15550000000", "me@example.com"]\npoll_interval = 2.5\n')
    cfg = load_config(tmp_path)
    assert cfg.self_handles == ("+15550000000", "me@example.com")
    assert cfg.poll_interval == 2.5
    assert cfg.rollover_hour == 4
    assert cfg.db_path == tmp_path / "data" / "argus.db"
    assert cfg.memory_dir == tmp_path / "memory"

def test_load_config_missing_file_raises(tmp_path: Path):
    with pytest.raises(ValueError, match="argus.toml"):
        load_config(tmp_path)

def test_load_config_requires_handles(tmp_path: Path):
    (tmp_path / "argus.toml").write_text("self_handles = []\n")
    with pytest.raises(ValueError, match="self_handles"):
        load_config(tmp_path)
```

```python
# tests/test_store.py
from datetime import date, datetime
import pytest
from argus.store import Store

@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "data" / "argus.db")
    yield s
    s.close()

def test_add_and_search_english(store):
    store.add_message("user", "I did squats today", datetime(2026, 10, 1, 9, 0))
    store.add_message("argus", "Nice squats", datetime(2026, 10, 1, 9, 1))
    hits = store.search_messages("squats")
    assert [h["sender"] for h in hits] == ["argus", "user"]  # newest first

def test_search_two_char_chinese(store):
    store.add_message("user", "我膝盖疼，别安排深蹲", datetime(2026, 10, 2, 8, 0))
    hits = store.search_messages("膝盖")
    assert len(hits) == 1 and "膝盖" in hits[0]["content"]

def test_search_three_char_chinese_uses_fts(store):
    store.add_message("user", "今天吃了牛肉饭", datetime(2026, 10, 2, 12, 0))
    assert len(store.search_messages("牛肉饭")) == 1

def test_search_date_range_inclusive(store):
    store.add_message("user", "apple one", datetime(2026, 10, 1, 23, 0))
    store.add_message("user", "apple two", datetime(2026, 10, 3, 1, 0))
    hits = store.search_messages("apple", start=date(2026, 10, 1), end=date(2026, 10, 1))
    assert [h["content"] for h in hits] == ["apple one"]

def test_search_escapes_like_and_quotes(store):
    store.add_message("user", 'he said "100%"', datetime(2026, 10, 1))
    assert len(store.search_messages('"100%"')) == 1
    assert store.search_messages("%") == []
    assert store.search_messages("") == []

def test_sessions_roundtrip(store):
    assert store.get_session(date(2026, 10, 1)) is None
    sid = store.create_session(date(2026, 10, 1), datetime(2026, 10, 1, 4, 0))
    store.set_session_sdk_id(sid, "abc")
    store.end_session(sid, datetime(2026, 10, 2, 4, 0))
    row = store.get_session(date(2026, 10, 1))
    assert row["id"] == sid and row["sdk_session_id"] == "abc" and row["ended_at"].startswith("2026-10-02")

def test_sent_ids_idempotent(store):
    store.add_sent_id("g1", datetime(2026, 10, 1))
    store.add_sent_id("g1", datetime(2026, 10, 1))
    assert store.has_sent_id("g1") and not store.has_sent_id("g2")

def test_state(store):
    assert store.get_state("k", "d") == "d"
    store.set_state("k", "1"); store.set_state("k", "2")
    assert store.get_state("k") == "2"

def test_reopen_keeps_data(tmp_path):
    p = tmp_path / "x.db"
    s = Store(p); s.add_message("user", "hello world", datetime(2026, 10, 1)); s.close()
    s2 = Store(p); assert len(s2.search_messages("hello")) == 1; s2.close()
```

- [ ] **Step 2: Run tests, confirm they fail** — `.venv/bin/python -m pytest tests/test_config.py tests/test_store.py -q` → ImportError.
- [ ] **Step 3: Implement `argus/config.py` and `argus/store.py`** per the interfaces and schema above.
- [ ] **Step 4: Run tests, confirm they pass.**
- [ ] **Step 5: Do not commit** (the lead integrates and commits). Report files created and test output.

---

### Task 2: Memory store

**Files:**
- Create: `argus/tools/memory.py`
- Test: `tests/test_memory.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  ```python
  class MemoryStore:
      def __init__(self, memory_dir: Path) -> None   # mkdir -p
      def list(self) -> list[str]                    # slugs (no ".md"), sorted, excluding "MEMORY"
      def read(self, name: str) -> str               # FileNotFoundError if absent
      def write(self, name: str, content: str, summary: str) -> None
          # writes <name>.md; upserts the index line "- [<name>](<name>.md) — <summary>" in MEMORY.md (one line per name, sorted by name)
      def delete(self, name: str) -> None            # removes file + index line; FileNotFoundError if absent
      def index(self) -> str                         # MEMORY.md text, "" if missing
  ```
  Valid `name`: matches `^[a-z0-9][a-z0-9-]{0,63}$` and is not `memory`; anything else → `ValueError`. `summary` newlines are replaced by spaces. Writes are atomic (write temp file in the same dir, then `os.replace`).

- [ ] **Step 1: Write failing tests**

```python
# tests/test_memory.py
import pytest
from argus.tools.memory import MemoryStore

@pytest.fixture
def mem(tmp_path):
    return MemoryStore(tmp_path / "memory")

def test_write_read_list_index(mem):
    mem.write("knee-injury", "Left knee hurts on deep squats.", "knee pain; avoid deep squats")
    mem.write("prefers-chinese", "Reply in Chinese.", "language")
    assert mem.list() == ["knee-injury", "prefers-chinese"]
    assert mem.read("knee-injury") == "Left knee hurts on deep squats."
    assert mem.index().splitlines() == [
        "- [knee-injury](knee-injury.md) — knee pain; avoid deep squats",
        "- [prefers-chinese](prefers-chinese.md) — language",
    ]

def test_overwrite_updates_single_index_line(mem):
    mem.write("a", "1", "first")
    mem.write("a", "2", "second\nline")
    assert mem.read("a") == "2"
    assert mem.index().splitlines() == ["- [a](a.md) — second line"]

def test_delete(mem):
    mem.write("a", "1", "x")
    mem.delete("a")
    assert mem.list() == [] and mem.index().strip() == ""
    with pytest.raises(FileNotFoundError):
        mem.delete("a")

@pytest.mark.parametrize("bad", ["../etc/passwd", "a/b", "A", "", "-x", "memory", "x" * 65, "a.md"])
def test_rejects_bad_names(mem, bad):
    with pytest.raises(ValueError):
        mem.write(bad, "c", "s")
    with pytest.raises(ValueError):
        mem.read(bad)

def test_read_missing(mem):
    with pytest.raises(FileNotFoundError):
        mem.read("nope")
```

- [ ] **Step 2: Run, confirm failure.** `.venv/bin/python -m pytest tests/test_memory.py -q`
- [ ] **Step 3: Implement `argus/tools/memory.py`.**
- [ ] **Step 4: Run, confirm pass.**
- [ ] **Step 5: Do not commit.** Report.

---

### Task 3: Read-only data query and history formatting

**Files:**
- Create: `argus/tools/data.py`, `argus/tools/history.py`
- Test: `tests/test_data.py`, `tests/test_history.py`

**Interfaces:**
- Consumes: `Store` from Task 1 is used only in tests to create a populated DB (if Task 1 is not merged yet, create the DB with plain `sqlite3` in the test instead — the tests below do this).
- Produces:
  ```python
  # argus/tools/data.py
  class QueryError(Exception): ...
  def query_readonly(db_path: Path, sql: str, max_rows: int = 200) -> dict
      # returns {"columns": list[str], "rows": list[list], "truncated": bool}
      # opens sqlite3.connect(f"file:{db_path}?mode=ro", uri=True); executes PRAGMA query_only=ON;
      # rejects more than one statement (use sqlite3.complete_statement on the stripped text and a check that
      # no ';' remains outside a trailing one — simplest correct approach: strip, drop ONE trailing ';',
      # then raise QueryError if ';' still appears after removing string literals);
      # any sqlite3.Error -> QueryError(str(e)); missing db file -> QueryError
  # argus/tools/history.py
  def format_history(rows: list[dict]) -> str
      # rows as returned by Store.search_messages; one line each: "[<ts>] <sender>: <content>";
      # content newlines -> " / "; empty list -> "No matching messages."
  ```

- [ ] **Step 1: Write failing tests**

```python
# tests/test_data.py
import sqlite3
import pytest
from argus.tools.data import query_readonly, QueryError

@pytest.fixture
def db(tmp_path):
    p = tmp_path / "argus.db"
    c = sqlite3.connect(p)
    c.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, name TEXT)")
    c.executemany("INSERT INTO t (name) VALUES (?)", [(f"n{i}",) for i in range(5)])
    c.commit(); c.close()
    return p

def test_select(db):
    r = query_readonly(db, "SELECT id, name FROM t ORDER BY id LIMIT 2;")
    assert r == {"columns": ["id", "name"], "rows": [[1, "n0"], [2, "n1"]], "truncated": False}

def test_truncates(db):
    r = query_readonly(db, "SELECT * FROM t", max_rows=3)
    assert len(r["rows"]) == 3 and r["truncated"] is True

@pytest.mark.parametrize("sql", ["DELETE FROM t", "UPDATE t SET name='x'", "DROP TABLE t",
                                 "INSERT INTO t (name) VALUES ('x')", "SELECT 1; DELETE FROM t"])
def test_writes_rejected_and_nothing_changes(db, sql):
    with pytest.raises(QueryError):
        query_readonly(db, sql)
    assert query_readonly(db, "SELECT count(*) FROM t")["rows"] == [[5]]

def test_semicolon_inside_string_ok(db):
    assert query_readonly(db, "SELECT 'a;b'")["rows"] == [["a;b"]]

def test_bad_sql(db):
    with pytest.raises(QueryError):
        query_readonly(db, "SELEC nonsense")

def test_missing_db(tmp_path):
    with pytest.raises(QueryError):
        query_readonly(tmp_path / "nope.db", "SELECT 1")
```

```python
# tests/test_history.py
from argus.tools.history import format_history

def test_format():
    rows = [{"id": 2, "ts": "2026-10-01T09:01:00", "sender": "argus", "content": "a\nb"},
            {"id": 1, "ts": "2026-10-01T09:00:00", "sender": "user", "content": "hi"}]
    assert format_history(rows) == "[2026-10-01T09:01:00] argus: a / b\n[2026-10-01T09:00:00] user: hi"

def test_empty():
    assert format_history([]) == "No matching messages."
```

- [ ] **Step 2: Run, confirm failure.**
- [ ] **Step 3: Implement both modules.**
- [ ] **Step 4: Run, confirm pass.**
- [ ] **Step 5: Do not commit.** Report.

---

### Task 4: Scheduler math and backups

**Files:**
- Create: `argus/scheduler.py`
- Test: `tests/test_scheduler.py`

**Interfaces:**
- Consumes: nothing (backup copies the SQLite file with the `sqlite3` backup API so it's consistent even while open).
- Produces:
  ```python
  def day_key(now: datetime, rollover_hour: int = 4) -> date
      # the "Argus day" a moment belongs to: (now - rollover_hour hours).date()
  def seconds_until_rollover(now: datetime, rollover_hour: int = 4) -> float   # > 0, ≤ 86400
  def backup(db_path: Path, memory_dir: Path, backups_dir: Path, day: date, keep: int = 30) -> Path
      # creates backups_dir/<day.isoformat()>/ with argus.db (sqlite backup API; skipped if db missing)
      # and memory/ (copytree; skipped if missing); overwrites an existing folder for the same day;
      # then deletes the oldest dated folders so at most `keep` remain (only names matching YYYY-MM-DD are touched);
      # returns the folder path
  ```

- [ ] **Step 1: Write failing tests**

```python
# tests/test_scheduler.py
import sqlite3
from datetime import date, datetime
from argus.scheduler import day_key, seconds_until_rollover, backup

def test_day_key_boundary():
    assert day_key(datetime(2026, 10, 2, 3, 59)) == date(2026, 10, 1)
    assert day_key(datetime(2026, 10, 2, 4, 0)) == date(2026, 10, 2)
    assert day_key(datetime(2026, 10, 2, 0, 30)) == date(2026, 10, 1)

def test_seconds_until_rollover():
    assert seconds_until_rollover(datetime(2026, 10, 2, 3, 0)) == 3600
    assert seconds_until_rollover(datetime(2026, 10, 2, 4, 0)) == 86400
    assert seconds_until_rollover(datetime(2026, 10, 2, 23, 0)) == 5 * 3600

def test_backup_copies_and_prunes(tmp_path):
    db = tmp_path / "data" / "argus.db"; db.parent.mkdir()
    c = sqlite3.connect(db); c.execute("CREATE TABLE t (x)"); c.execute("INSERT INTO t VALUES (1)"); c.commit()
    mem = tmp_path / "memory"; mem.mkdir(); (mem / "a.md").write_text("hi")
    bdir = tmp_path / "backups"
    for d in range(1, 4):
        backup(db, mem, bdir, date(2026, 10, d), keep=2)
    c.close()
    assert sorted(p.name for p in bdir.iterdir()) == ["2026-10-02", "2026-10-03"]
    out = bdir / "2026-10-03"
    assert (out / "memory" / "a.md").read_text() == "hi"
    assert sqlite3.connect(out / "argus.db").execute("SELECT x FROM t").fetchall() == [(1,)]

def test_backup_same_day_overwrites_and_ignores_other_dirs(tmp_path):
    bdir = tmp_path / "backups"; (bdir / "keepme").mkdir(parents=True)
    mem = tmp_path / "memory"; mem.mkdir(); (mem / "a.md").write_text("1")
    backup(tmp_path / "missing.db", mem, bdir, date(2026, 10, 1), keep=1)
    (mem / "a.md").write_text("2")
    backup(tmp_path / "missing.db", mem, bdir, date(2026, 10, 1), keep=1)
    assert (bdir / "2026-10-01" / "memory" / "a.md").read_text() == "2"
    assert (bdir / "keepme").exists()
```

- [ ] **Step 2: Run, confirm failure.**
- [ ] **Step 3: Implement `argus/scheduler.py`.**
- [ ] **Step 4: Run, confirm pass.**
- [ ] **Step 5: Do not commit.** Report.

---

### Task 5: iMessage channel

**Files:**
- Create: `argus/channel/imessage.py`, `tests/conftest.py` (fixture `fake_chat_db`)
- Test: `tests/test_imessage.py`

**Spike findings this task relies on (verified 2026-10-13 on the user's Mac, macOS 15.3.2):**
- Self-chat guid is `"iMessage;-;" + handle` (e.g. `iMessage;-;+15550000000`). Join path: `message` → `chat_message_join(chat_id, message_id)` → `chat(ROWID, guid)`.
- `message.date` = nanoseconds since 2001-01-01 UTC.
- `message.text` is often NULL; the body is then in `attributedBody` (typedstream). The decoder below matched `text` on 2045/2045 rows.
- Every message in the self-chat produces **two rows** with **different guids**: a "sent" row (`is_from_me=1`, `text` NULL) and ~2 s later an "echo" row (`is_from_me=0`, `text` set, `date` ~0.1 s earlier). Nothing links the two guids. So: Argus's own sends are filtered by the `🤖` prefix (primary) and by recorded guid (secondary); duplicates of the user's own messages are removed by **pairing** (same text, opposite `is_from_me`, `date` within ±10 s).
- Send with argv (no escaping needed; verified byte-exact with quotes, backslash, emoji, newline):
  ```applescript
  on run argv
    tell application "Messages" to send (item 1 of argv) to chat id (item 2 of argv)
  end run
  ```
- Never enumerate Messages accounts in AppleScript (hangs / -10000).

**Interfaces:**
- Consumes: `Channel`, `IncomingMessage`, `SendError` (`argus/channel/base.py`); `Store.get_state/set_state/add_sent_id/has_sent_id` (Task 1).
- Produces:
  ```python
  PREFIX = "🤖 "
  SEND_SCRIPT = 'on run argv\n  tell application "Messages" to send (item 1 of argv) to chat id (item 2 of argv)\nend run'
  def decode_attributed_body(blob: bytes | None) -> str | None
  def apple_ns_to_local(ns: int) -> datetime      # local, naive
  class IMessageChannel(Channel):
      def __init__(self, chat_db: Path, self_handles: Sequence[str], store: Store, *,
                   runner: Callable[[list[str]], subprocess.CompletedProcess] | None = None,   # default: subprocess.run(args, capture_output=True, text=True, timeout=30)
                   sleep: Callable[[float], None] = time.sleep,
                   pair_window_seconds: float = 10.0) -> None
          # chat_guids = ["iMessage;-;" + h for h in self_handles]; sends go to chat_guids[0]
          # opens chat.db read-only (file:...?mode=ro) per call; raises FileNotFoundError / sqlite3.Error at init if unreadable
      def poll(self) -> list[IncomingMessage]
      def send(self, text: str) -> None
  ```
  `poll` algorithm:
  1. Checkpoint key `"imessage_last_rowid"` in `Store` state. If absent: set it to `SELECT max(ROWID) FROM message` (or 0) and return `[]` (never answer history).
  2. Fetch rows in the self-chat(s) after the checkpoint:
     ```sql
     SELECT m.ROWID, m.guid, m.text, m.attributedBody, m.is_from_me, m.date
     FROM message m JOIN chat_message_join j ON j.message_id = m.ROWID JOIN chat c ON c.ROWID = j.chat_id
     WHERE c.guid IN (<chat_guids>) AND m.ROWID > ? AND m.item_type = 0 AND m.associated_message_type = 0
     ORDER BY m.ROWID
     ```
  3. For each row: body = `text` or `decode_attributed_body(attributedBody)`; strip trailing whitespace. Skip if body empty, if body starts with `"🤖"`, or if `store.has_sent_id(guid)`. Otherwise, if it pairs with a recently accepted row (same body, opposite `is_from_me`, |Δdate| ≤ window, that accepted row not already paired) → mark that row paired and skip. Otherwise accept: append `IncomingMessage(id=guid, text=body, received_at=apple_ns_to_local(date))` and remember `(body, is_from_me, date, paired=False)`; forget remembered rows older than 60 s.
  4. Set the checkpoint to the max ROWID seen (even for skipped rows) and return accepted messages.
  `send` algorithm: `full = PREFIX + text`; `before = max(ROWID)`; up to 3 attempts of `runner(["osascript", "-e", SEND_SCRIPT, full, chat_guids[0]])` — success when `returncode == 0`; between failures `sleep(1)`; a `subprocess.TimeoutExpired`/`OSError` counts as a failure; after 3 failures raise `SendError(stderr or reason)`. On success, best-effort guid capture: up to 12 tries × `sleep(0.25)`, look for an `is_from_me=1` row in the self-chat with `ROWID > before` whose decoded body equals `full`; if found `store.add_sent_id(guid, now)`. Capture failure is not an error.

- [ ] **Step 1: Write the fake chat.db fixture and failing tests**

```python
# tests/conftest.py
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
```

```python
# tests/test_imessage.py
import subprocess
from datetime import datetime, timedelta
import pytest
from argus.channel.base import SendError
from argus.channel.imessage import IMessageChannel, decode_attributed_body, PREFIX, SEND_SCRIPT
from argus.store import Store
from tests.conftest import make_attributed_body

T0 = datetime(2026, 10, 13, 9, 0, 0).astimezone()

@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "a.db"); yield s; s.close()

def chan(db, store, runner=None):
    return IMessageChannel(db.path, ["+15550000000"], store, runner=runner, sleep=lambda s: None)

@pytest.mark.parametrize("text", ["hi", "x" * 200, "x" * 70000, "引号\"\\🤖\né"])
def test_decode_roundtrip(text):
    assert decode_attributed_body(make_attributed_body(text)) == text

def test_decode_none():
    assert decode_attributed_body(None) is None and decode_attributed_body(b"garbage") is None

def test_first_poll_sets_checkpoint_and_ignores_history(fake_chat_db, store):
    fake_chat_db.add("old message", 0, T0)
    c = chan(fake_chat_db, store)
    assert c.poll() == []
    fake_chat_db.add("new", 0, T0 + timedelta(seconds=5))
    assert [m.text for m in c.poll()] == ["new"]

def test_pair_dedupe_sent_then_echo(fake_chat_db, store):
    c = chan(fake_chat_db, store); c.poll()
    fake_chat_db.add("中午吃了牛肉饭", 1, T0, as_blob=True)
    fake_chat_db.add("中午吃了牛肉饭", 0, T0 - timedelta(milliseconds=100))
    assert [m.text for m in c.poll()] == ["中午吃了牛肉饭"]

def test_pair_dedupe_across_polls(fake_chat_db, store):
    c = chan(fake_chat_db, store); c.poll()
    fake_chat_db.add("hello", 1, T0, as_blob=True)
    assert [m.text for m in c.poll()] == ["hello"]
    fake_chat_db.add("hello", 0, T0)
    assert c.poll() == []

def test_same_text_twice_is_two_messages(fake_chat_db, store):
    c = chan(fake_chat_db, store); c.poll()
    for dt in (0, 3):
        fake_chat_db.add("ok", 1, T0 + timedelta(seconds=dt), as_blob=True)
        fake_chat_db.add("ok", 0, T0 + timedelta(seconds=dt))
    assert [m.text for m in c.poll()] == ["ok", "ok"]

def test_single_row_without_echo_still_delivered(fake_chat_db, store):
    c = chan(fake_chat_db, store); c.poll()
    fake_chat_db.add("only one", 1, T0, as_blob=True)
    assert [m.text for m in c.poll()] == ["only one"]

def test_robot_prefix_and_sent_ids_skipped(fake_chat_db, store):
    c = chan(fake_chat_db, store); c.poll()
    fake_chat_db.add("🤖 reply", 1, T0, as_blob=True)
    fake_chat_db.add("🤖 reply", 0, T0)
    g = fake_chat_db.add("weird", 1, T0)
    store.add_sent_id(g, datetime.now())
    assert c.poll() == []

def test_other_chats_and_reactions_ignored(fake_chat_db, store):
    c = chan(fake_chat_db, store); c.poll()
    fake_chat_db.add("someone else", 0, T0, chat_id=2)
    fake_chat_db.add("tapback", 0, T0, item_type=1)
    assert c.poll() == []

def test_checkpoint_survives_restart(fake_chat_db, store):
    c = chan(fake_chat_db, store); c.poll()
    fake_chat_db.add("a", 0, T0)
    assert [m.text for m in c.poll()] == ["a"]
    c2 = chan(fake_chat_db, store)
    assert c2.poll() == []

def test_send_uses_argv_and_records_guid(fake_chat_db, store):
    calls = []
    def runner(args):
        calls.append(args)
        fake_chat_db.add(args[3], 1, T0, as_blob=True)
        return subprocess.CompletedProcess(args, 0, "", "")
    c = chan(fake_chat_db, store, runner); c.poll()
    c.send("好的")
    assert calls == [["osascript", "-e", SEND_SCRIPT, PREFIX + "好的", "iMessage;-;+15550000000"]]
    assert store.has_sent_id(f"G{fake_chat_db.n}")
    assert c.poll() == []   # our own message is not delivered back

def test_send_retries_then_raises(fake_chat_db, store):
    n = []
    def runner(args):
        n.append(1); return subprocess.CompletedProcess(args, 1, "", "boom")
    c = chan(fake_chat_db, store, runner)
    with pytest.raises(SendError):
        c.send("x")
    assert len(n) == 3

def test_send_timeout_counts_as_failure(fake_chat_db, store):
    def runner(args): raise subprocess.TimeoutExpired(args, 30)
    with pytest.raises(SendError):
        chan(fake_chat_db, store, runner).send("x")
```

- [ ] **Step 2: Run, confirm failure.** `.venv/bin/python -m pytest tests/test_imessage.py -q`
- [ ] **Step 3: Implement `argus/channel/imessage.py`.** Decoder (verified on real data):
  ```python
  def decode_attributed_body(blob):
      if not blob:
          return None
      b = bytes(blob)
      i = b.find(b"NSString")
      if i < 0:
          i = b.find(b"NSMutableString")
          if i < 0:
              return None
      j = b.find(b"\x01\x2b", i)
      if j < 0 or j + 2 >= len(b):
          return None
      p = j + 2
      tag = b[p]
      if tag == 0x81:
          n = int.from_bytes(b[p + 1:p + 3], "little"); p += 3
      elif tag == 0x82:
          n = int.from_bytes(b[p + 1:p + 5], "little"); p += 5
      else:
          n = tag; p += 1
      return b[p:p + n].decode("utf-8", errors="replace")
  ```
- [ ] **Step 4: Run, confirm pass.**
- [ ] **Step 5: Do not commit.** Report.

---

### Task 6: SDK tool server

**Files:**
- Create: `argus/tools/server.py`
- Test: `tests/test_tool_server.py`

**Interfaces:**
- Consumes: `Store` (Task 1), `MemoryStore` (Task 2), `query_readonly`/`QueryError` (Task 3), `format_history` (Task 3).
- Produces:
  ```python
  SERVER_NAME = "argus"
  def build_tools(store: Store, memory: MemoryStore, db_path: Path) -> list[SdkMcpTool]
  def build_tool_server(store: Store, memory: MemoryStore, db_path: Path) -> tuple[McpSdkServerConfig, list[str]]
      # returns (create_sdk_mcp_server(SERVER_NAME, tools=build_tools(...)), allowed tool names)
      # allowed names are "mcp__argus__<tool>" for every tool below
  ```
  Tools (SDK API: `@tool(name, description, input_schema)` on `async def handler(args: dict) -> dict`; return `{"content": [{"type": "text", "text": ...}]}`, add `"is_error": True` on failure; use a full JSON Schema dict so optional fields stay optional):

  | Tool | Input | Behavior |
  |---|---|---|
  | `memory_list` | `{}` | The `MEMORY.md` index text, or `"(no memories)"` |
  | `memory_read` | `name: str` | File content; `is_error` on ValueError/FileNotFoundError |
  | `memory_write` | `name: str`, `content: str`, `summary: str` | Writes; returns `"saved <name>"` |
  | `memory_delete` | `name: str` | Deletes; returns `"deleted <name>"` |
  | `query_data` | `sql: str` | `query_readonly(db_path, sql)`; text = JSON of the result dict (`ensure_ascii=False`); `is_error` on QueryError |
  | `search_history` | `query: str`, optional `start: str`, `end: str` (YYYY-MM-DD), optional `limit: int` (default 20, max 100) | `format_history(store.search_messages(...))`; bad date → `is_error` |

  Each tool description must tell the model when to use it (e.g. query_data: "Read-only SQL (SQLite) over Argus's database. Tables: messages(id, ts, sender, content, session_id), sessions(...). Use for counts and date-range questions.").

- [ ] **Step 1: Write failing tests.** `SdkMcpTool` exposes `.name` and `.handler`; call handlers directly.

```python
# tests/test_tool_server.py
import json
from datetime import datetime
import pytest
from argus.store import Store
from argus.tools.memory import MemoryStore
from argus.tools.server import build_tools, build_tool_server

@pytest.fixture
def env(tmp_path):
    store = Store(tmp_path / "data" / "argus.db")
    mem = MemoryStore(tmp_path / "memory")
    tools = {t.name: t for t in build_tools(store, mem, tmp_path / "data" / "argus.db")}
    yield store, mem, tools
    store.close()

def text(result):
    return result["content"][0]["text"]

async def test_allowed_names(tmp_path):
    store = Store(tmp_path / "a.db")
    _, allowed = build_tool_server(store, MemoryStore(tmp_path / "m"), tmp_path / "a.db")
    assert sorted(allowed) == sorted(f"mcp__argus__{n}" for n in
        ["memory_list", "memory_read", "memory_write", "memory_delete", "query_data", "search_history"])
    store.close()

async def test_memory_roundtrip(env):
    _, _, t = env
    assert text(await t["memory_list"].handler({})) == "(no memories)"
    await t["memory_write"].handler({"name": "knee", "content": "bad knee", "summary": "knee"})
    assert text(await t["memory_read"].handler({"name": "knee"})) == "bad knee"
    assert "knee" in text(await t["memory_list"].handler({}))
    await t["memory_delete"].handler({"name": "knee"})
    r = await t["memory_read"].handler({"name": "knee"})
    assert r.get("is_error") is True

async def test_memory_bad_name_is_error(env):
    _, _, t = env
    r = await t["memory_write"].handler({"name": "../x", "content": "c", "summary": "s"})
    assert r.get("is_error") is True

async def test_query_data(env):
    store, _, t = env
    store.add_message("user", "hi", datetime(2026, 10, 1))
    r = await t["query_data"].handler({"sql": "SELECT count(*) AS n FROM messages"})
    assert json.loads(text(r))["rows"] == [[1]]
    bad = await t["query_data"].handler({"sql": "DELETE FROM messages"})
    assert bad.get("is_error") is True

async def test_search_history(env):
    store, _, t = env
    store.add_message("user", "我膝盖疼", datetime(2026, 10, 1, 9))
    r = await t["search_history"].handler({"query": "膝盖"})
    assert "我膝盖疼" in text(r)
    r2 = await t["search_history"].handler({"query": "膝盖", "start": "2026-10-02"})
    assert text(r2) == "No matching messages."
    bad = await t["search_history"].handler({"query": "x", "start": "yesterday"})
    assert bad.get("is_error") is True
```

- [ ] **Step 2: Run, confirm failure.**
- [ ] **Step 3: Implement `argus/tools/server.py`.**
- [ ] **Step 4: Run, confirm pass.**
- [ ] **Step 5: Do not commit.** Report.

---

### Task 7: SDK session

**Files:**
- Create: `argus/sdk_session.py`
- Test: `tests/test_sdk_session.py` (unit, no network) and `tests/live/test_sdk_live.py` (skipped unless `ARGUS_LIVE=1`)

**Interfaces:**
- Consumes: `Session`, `SessionError`, `Interrupted` from `argus/session.py`; `MemoryStore` (Task 2); `build_tool_server` (Task 6); `Config` (Task 1).
- Produces:
  ```python
  def build_system_prompt(persona: str, memory_index: str, now: datetime) -> str
      # persona text, then "\n\n# Long-term memory index\n" + (memory_index or "(empty)"),
      # then "\n\n# Now\n" + now.strftime("%Y-%m-%d %A %H:%M") + " (local time)"
  class SDKSession:            # implements argus.session.Session
      sdk_session_id: str | None
      def __init__(self, options: ClaudeAgentOptions) -> None
      async def start(self) -> None          # client.connect()
      async def ask(self, text: str) -> str
      async def interrupt(self) -> None
      async def close(self) -> None          # idempotent
  def make_session_factory(cfg: Config, store: Store, memory: MemoryStore, client_cls=ClaudeSDKClient) -> SessionFactory
      # factory(resume_id) builds options and returns a started SDKSession
  ```
  Options (verified against SDK 0.2.165 — see Notes):
  ```python
  server, allowed = build_tool_server(store, memory, cfg.db_path)
  ClaudeAgentOptions(
      system_prompt=build_system_prompt((cfg.root / "CLAUDE.md").read_text() if exists else "", memory.index(), datetime.now()),
      mcp_servers={"argus": server},
      tools=["WebSearch", "WebFetch"],
      allowed_tools=allowed + ["WebSearch", "WebFetch"],
      permission_mode="dontAsk",
      setting_sources=[],
      strict_mcp_config=True,
      cwd=str(cfg.root),
      model=cfg.model,
      resume=resume_id,
      max_turns=30,
  )
  ```
  `ask` behavior: `await client.query(text)`; iterate `client.receive_response()`; collect `TextBlock.text` from `AssistantMessage`s; on `ResultMessage` set `sdk_session_id = msg.session_id`; then:
  - `terminal_reason` in `("aborted_streaming", "aborted_tools")` or the interrupt flag set during this turn → raise `Interrupted`.
  - `msg.is_error` → raise `SessionError(<first of msg.errors, msg.result, msg.subtype>)`.
  - otherwise return `msg.result` if it is a non-empty str, else the last collected text block, else `""`.
  - `AssistantMessage.error` not None (e.g. `"rate_limit"`, `"authentication_failed"`) → after the turn, raise `SessionError(error)` unless a successful result arrived.
  - Any `ClaudeSDKError` → `SessionError(str(e))`. Ignore other message types (`RateLimitEvent`, `SystemMessage`, `UserMessage`, ...).
  `interrupt`: set a flag, then `await client.interrupt()`; swallow `ClaudeSDKError`.
  The client must be connected, used, and disconnected from the same asyncio task (Task 9 guarantees all `ask`/`close` calls come from the single worker task; `interrupt` may come from the poll task — it only writes a control request and is safe).

**Notes (SDK facts verified live on this machine):** `allowed_tools` only auto-approves; `tools=[...]` controls which built-ins exist; `permission_mode="dontAsk"` denies anything not allowed instead of prompting; `system_prompt=None` would send an empty prompt; `setting_sources=[]` avoids loading the user's global CLAUDE.md/hooks; `strict_mcp_config=True` stops the user's claude.ai connectors from loading. Model-visible tool names are `mcp__<server key>__<tool>`.

- [ ] **Step 1: Write failing unit tests with a fake client** (constructed with options; records calls; yields scripted messages using the real SDK message dataclasses).

```python
# tests/test_sdk_session.py
from datetime import datetime
import pytest
from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock, ClaudeAgentOptions
from argus.sdk_session import SDKSession, build_system_prompt
from argus.session import Interrupted, SessionError

def result(**kw):
    base = dict(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False, num_turns=1,
                session_id="sess-1", result="final answer")
    base.update(kw)
    return ResultMessage(**{k: v for k, v in base.items() if k in ResultMessage.__dataclass_fields__})

def assistant(text, error=None):
    return AssistantMessage(**{k: v for k, v in dict(content=[TextBlock(text=text)], model="m", error=error).items()
                               if k in AssistantMessage.__dataclass_fields__})

class FakeClient:
    def __init__(self, options=None, script=None):
        self.options, self.script, self.queries, self.interrupted = options, list(script or []), [], 0
    async def connect(self, prompt=None): self.connected = True
    async def query(self, prompt, session_id="default"): self.queries.append(prompt)
    async def receive_response(self):
        for m in self.script.pop(0):
            yield m
    async def interrupt(self): self.interrupted += 1
    async def disconnect(self): self.connected = False

async def make(script):
    client = FakeClient(script=script)
    s = SDKSession(ClaudeAgentOptions(), client=client)
    await s.start()
    return s, client

async def test_ask_returns_result_and_session_id():
    s, c = await make([[assistant("thinking"), result()]])
    assert await s.ask("hi") == "final answer"
    assert s.sdk_session_id == "sess-1" and c.queries == ["hi"]

async def test_falls_back_to_last_text_when_result_empty():
    s, _ = await make([[assistant("a"), assistant("b"), result(result="")]])
    assert await s.ask("hi") == "b"

async def test_error_result_raises():
    s, _ = await make([[result(is_error=True, subtype="error_during_execution", result="boom")]])
    with pytest.raises(SessionError):
        await s.ask("hi")

async def test_aborted_raises_interrupted():
    s, _ = await make([[result(terminal_reason="aborted_streaming")]])
    with pytest.raises(Interrupted):
        await s.ask("hi")

async def test_close_idempotent():
    s, c = await make([])
    await s.close(); await s.close()
    assert c.connected is False

def test_system_prompt():
    p = build_system_prompt("You are Argus.", "", datetime(2026, 10, 13, 9, 30))
    assert p.startswith("You are Argus.") and "(empty)" in p and "2026-10-13 Tuesday 09:30" in p
```
  So `SDKSession.__init__(self, options, client=None)` — when `client` is None it constructs `ClaudeSDKClient(options=options)`.

- [ ] **Step 2: Run, confirm failure.**
- [ ] **Step 3: Implement `argus/sdk_session.py`.**
- [ ] **Step 4: Run, confirm pass.**
- [ ] **Step 5: Live test** (`tests/live/test_sdk_live.py`, skipped unless `ARGUS_LIVE=1`): build a temp root with a tiny `CLAUDE.md` ("Reply in at most 5 words."), real `Store`/`MemoryStore`, `make_session_factory`, then: (a) `ask("Use memory_write to save name 'live-test', content 'blue', summary 'favorite color'. Then reply OK.")` → file exists; (b) second `ask("What did you just save?")` in the same session mentions blue; (c) `close()`, factory(resume=sdk_session_id), `ask("What color did we talk about?")` mentions blue. Run it once: `ARGUS_LIVE=1 .venv/bin/python -m pytest tests/live -q -s`. Keep it to these 3 calls (subscription quota).
- [ ] **Step 6: Do not commit.** Report, including live test output.

---

### Task 8: Brain

**Files:**
- Create: `argus/brain.py`
- Test: `tests/test_brain.py`

**Interfaces:**
- Consumes: `Store` (Task 1), `day_key` (Task 4), `Session`/`SessionFactory`/`Interrupted`/`SessionError` (`argus/session.py`).
- Produces:
  ```python
  class Brain:
      def __init__(self, store: Store, session_factory: SessionFactory, now: Callable[[], datetime] = datetime.now,
                   rollover_hour: int = 4, on_rollover: Callable[[date], Awaitable[None]] | None = None) -> None
      async def ask(self, text: str) -> str
          # 1. await self._ensure_session()
          # 2. store.add_message("user", text, now(), session_id=row id)
          # 3. reply = await session.ask(text)   (Interrupted/SessionError propagate after step 2)
          # 4. if session.sdk_session_id and it differs from the stored one: store.set_session_sdk_id(...)
          # 5. store.add_message("argus", reply, now(), session_id=row id); return reply
      async def interrupt(self) -> None    # no-op if no session
      async def close(self) -> None        # close session (does NOT end the day's session row)
  ```
  `_ensure_session()` rules:
  - `today = day_key(now(), rollover_hour)`.
  - If a session is open for a different day: `await session.close()`, `store.end_session(row_id, now())`, then `await on_rollover(today)` if given.
  - If none open for `today`: `row = store.get_session(today)`. If `row` exists and has `sdk_session_id` → `session = await factory(row["sdk_session_id"])`; if that raises `SessionError`, fall back to `await factory(None)`. If `row` exists without sdk id → `factory(None)`. If no row → `store.create_session(today, now())` then `factory(None)`.
  - `on_rollover` is called only when an open session crosses into a new day in this process (not on first start).

- [ ] **Step 1: Write failing tests**

```python
# tests/test_brain.py
from datetime import date, datetime
import pytest
from argus.brain import Brain
from argus.session import SessionError
from argus.store import Store

class FakeSession:
    def __init__(self, resume, log):
        self.sdk_session_id, self.resume, self.log, self.closed = resume, resume, log, False
    async def ask(self, text):
        self.log.append(("ask", self.resume, text))
        if self.sdk_session_id is None:
            self.sdk_session_id = f"sdk-{len(self.log)}"
        return f"re:{text}"
    async def interrupt(self): self.log.append(("interrupt",))
    async def close(self): self.closed = True; self.log.append(("close", self.sdk_session_id))

class Clock:
    def __init__(self, t): self.t = t
    def __call__(self): return self.t

@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "a.db"); yield s; s.close()

def factory(log, fail_resume=False):
    async def f(resume):
        if resume and fail_resume:
            raise SessionError("cannot resume")
        log.append(("new", resume))
        return FakeSession(resume, log)
    return f

async def test_ask_archives_and_saves_sdk_id(store):
    log = []; clock = Clock(datetime(2026, 10, 1, 9))
    b = Brain(store, factory(log), now=clock)
    assert await b.ask("hi") == "re:hi"
    row = store.get_session(date(2026, 10, 1))
    assert row["sdk_session_id"].startswith("sdk-")
    assert [h["sender"] for h in store.search_messages("hi")] == ["argus", "user"]

async def test_same_day_reuses_session(store):
    log = []; clock = Clock(datetime(2026, 10, 1, 9))
    b = Brain(store, factory(log), now=clock)
    await b.ask("a"); clock.t = datetime(2026, 10, 2, 3, 59); await b.ask("b")
    assert [e for e in log if e[0] == "new"] == [("new", None)]

async def test_rollover_at_4am(store):
    log, rolled = [], []
    async def on_rollover(d): rolled.append(d)
    clock = Clock(datetime(2026, 10, 1, 9))
    b = Brain(store, factory(log), now=clock, on_rollover=on_rollover)
    await b.ask("a"); clock.t = datetime(2026, 10, 2, 4, 0); await b.ask("b")
    assert rolled == [date(2026, 10, 2)]
    assert store.get_session(date(2026, 10, 1))["ended_at"] is not None
    assert [e for e in log if e[0] == "new"] == [("new", None), ("new", None)]
    assert any(e[0] == "close" for e in log)

async def test_restart_resumes_today(store):
    log = []; clock = Clock(datetime(2026, 10, 1, 9))
    b1 = Brain(store, factory(log), now=clock); await b1.ask("a"); await b1.close()
    sdk_id = store.get_session(date(2026, 10, 1))["sdk_session_id"]
    b2 = Brain(store, factory(log), now=clock); await b2.ask("b")
    assert ("new", sdk_id) in log

async def test_resume_failure_falls_back_to_fresh(store):
    log = []; clock = Clock(datetime(2026, 10, 1, 9))
    b1 = Brain(store, factory(log), now=clock); await b1.ask("a"); await b1.close()
    b2 = Brain(store, factory(log, fail_resume=True), now=clock)
    assert await b2.ask("b") == "re:b"

async def test_user_message_archived_even_if_session_fails(store):
    class Boom(FakeSession):
        async def ask(self, text): raise SessionError("quota")
    async def f(resume): return Boom(resume, [])
    b = Brain(store, f, now=Clock(datetime(2026, 10, 1, 9)))
    with pytest.raises(SessionError):
        await b.ask("lost?")
    assert [h["sender"] for h in store.search_messages("lost?")] == ["user"]

async def test_interrupt_without_session_is_noop(store):
    b = Brain(store, factory([]), now=Clock(datetime(2026, 10, 1, 9)))
    await b.interrupt()
```

- [ ] **Step 2: Run, confirm failure.**
- [ ] **Step 3: Implement `argus/brain.py`.**
- [ ] **Step 4: Run, confirm pass.**
- [ ] **Step 5: Do not commit.** Report.

---

### Task 9: App loop

**Files:**
- Create: `argus/app.py`
- Test: `tests/test_app.py`

**Interfaces:**
- Consumes: `Channel`, `IncomingMessage`, `SendError` (`argus/channel/base.py`); `Brain` (Task 8: `ask`, `interrupt`, `close`); `Interrupted`, `SessionError` (`argus/session.py`); `Store.get_state/set_state` (Task 1).
- Produces:
  ```python
  STOP_WORD = "停"; RETRY_WORD = "重试"
  SLOW_NOTICE = "收到，正在处理…"
  STOPPED_REPLY = "已停止。"; NOTHING_TO_STOP = "现在没有在处理的任务。"; NOTHING_TO_RETRY = "没有需要重试的消息。"
  class App:
      def __init__(self, channel: Channel, brain: Brain, store: Store, poll_interval: float = 1.0,
                   slow_notice_seconds: float = 15.0) -> None
      async def tick(self) -> None        # one poll: msgs = await asyncio.to_thread(channel.poll); route each
      async def run(self) -> None         # starts the worker task, then loops tick() + sleep(poll_interval) until cancelled; on cancel: cancel worker, await brain.close()
      async def drain(self) -> None       # test helper: wait until the queue is empty and nothing is running
  ```
  Routing in `tick` (per message, in order):
  - text.strip() == `停`: if a task is running → `await brain.interrupt()` (the worker then sends `STOPPED_REPLY`); else send `NOTHING_TO_STOP`.
  - text.strip() == `重试`: if `store.get_state("last_failed")` is set → enqueue that text and clear the state; else send `NOTHING_TO_RETRY`.
  - otherwise enqueue text.
  Worker (single task, processes queue in order — the only caller of `brain.ask`):
  - Start a slow-notice timer task: after `slow_notice_seconds`, send `SLOW_NOTICE` once; cancel it when the ask finishes.
  - `reply = await brain.ask(text)` → send reply.
  - `Interrupted` → send `STOPPED_REPLY`; continue with the next queued item.
  - `SessionError as e` → `store.set_state("last_failed", text)`; send `f"出错了：{e}"`.
  - Any other `Exception as e` → log with traceback; `store.set_state("last_failed", text)`; send `f"出错了：{type(e).__name__}"`.
  - All sends go through `await asyncio.to_thread(channel.send, text)`; a `SendError` is logged and swallowed (never kills the worker).
  - A `channel.poll` exception is logged and swallowed in `run` (the loop keeps going).

- [ ] **Step 1: Write failing tests** with a fake channel and fake brain.

```python
# tests/test_app.py
import asyncio
from datetime import datetime
import pytest
from argus.app import App, SLOW_NOTICE, STOPPED_REPLY, NOTHING_TO_STOP, NOTHING_TO_RETRY
from argus.channel.base import Channel, IncomingMessage, SendError
from argus.session import Interrupted, SessionError
from argus.store import Store

class FakeChannel(Channel):
    def __init__(self): self.inbox, self.sent, self.fail_sends = [], [], 0
    def push(self, *texts):
        for t in texts: self.inbox.append(IncomingMessage(id=t, text=t, received_at=datetime(2026, 10, 1)))
    def poll(self):
        out, self.inbox = self.inbox, []; return out
    def send(self, text):
        if self.fail_sends: self.fail_sends -= 1; raise SendError("x")
        self.sent.append(text)

class FakeBrain:
    def __init__(self): self.asked, self.gate, self.fail, self.interrupts = [], None, None, 0
    async def ask(self, text):
        self.asked.append(text)
        if self.fail: e, self.fail = self.fail, None; raise e
        if self.gate:
            self._stop = asyncio.Event()
            g = asyncio.ensure_future(self.gate.wait()); s = asyncio.ensure_future(self._stop.wait())
            done, _ = await asyncio.wait({g, s}, return_when=asyncio.FIRST_COMPLETED)
            g.cancel(); s.cancel()
            if s in done: raise Interrupted()
        return f"re:{text}"
    async def interrupt(self):
        self.interrupts += 1
        if getattr(self, "_stop", None): self._stop.set()
    async def close(self): pass

@pytest.fixture
def parts(tmp_path):
    s = Store(tmp_path / "a.db"); ch, br = FakeChannel(), FakeBrain()
    app = App(ch, br, s, poll_interval=0.01, slow_notice_seconds=0.05)
    yield app, ch, br, s
    s.close()

async def run_until_idle(app):
    task = asyncio.create_task(app.run())
    await asyncio.sleep(0.05); await app.drain(); task.cancel()
    with pytest.raises(asyncio.CancelledError): await task

async def test_replies_in_order(parts):
    app, ch, br, _ = parts
    ch.push("a", "b"); await run_until_idle(app)
    assert ch.sent == ["re:a", "re:b"]

async def test_stop_when_idle(parts):
    app, ch, br, _ = parts
    ch.push("停"); await run_until_idle(app)
    assert ch.sent == [NOTHING_TO_STOP] and br.asked == []

async def test_stop_interrupts_and_queue_continues(parts):
    app, ch, br, _ = parts
    br.gate = asyncio.Event()
    task = asyncio.create_task(app.run())
    ch.push("long", "next"); await asyncio.sleep(0.1)   # long is running, slow notice sent
    ch.push("停"); await asyncio.sleep(0.05)
    br.gate.set(); await app.drain(); task.cancel()
    with pytest.raises(asyncio.CancelledError): await task
    assert ch.sent[0] == SLOW_NOTICE and STOPPED_REPLY in ch.sent and ch.sent[-1] == "re:next"

async def test_error_then_retry(parts):
    app, ch, br, s = parts
    br.fail = SessionError("额度用完")
    ch.push("hello"); await run_until_idle(app)
    assert ch.sent == ["出错了：额度用完"] and s.get_state("last_failed") == "hello"
    ch.push("重试"); await run_until_idle(app)
    assert ch.sent[-1] == "re:hello" and not s.get_state("last_failed")

async def test_retry_with_nothing(parts):
    app, ch, _, _ = parts
    ch.push("重试"); await run_until_idle(app)
    assert ch.sent == [NOTHING_TO_RETRY]

async def test_send_failure_does_not_kill_worker(parts):
    app, ch, _, _ = parts
    ch.fail_sends = 1; ch.push("a", "b"); await run_until_idle(app)
    assert ch.sent == ["re:b"]

async def test_unexpected_error_reported(parts):
    app, ch, br, _ = parts
    br.fail = RuntimeError("bug")
    ch.push("x"); await run_until_idle(app)
    assert ch.sent == ["出错了：RuntimeError"]
```
  Clearing state: `App` clears with `store.set_state("last_failed", "")` and treats `""` as unset.

- [ ] **Step 2: Run, confirm failure.**
- [ ] **Step 3: Implement `argus/app.py`.**
- [ ] **Step 4: Run, confirm pass.** Run the file 5 times to check for flakiness: `for i in 1 2 3 4 5; do .venv/bin/python -m pytest tests/test_app.py -q || break; done`.
- [ ] **Step 5: Do not commit.** Report.

---

### Task 10: Wiring, persona, and operations

**Files:**
- Create: `argus/main.py`, `CLAUDE.md`, `argus.example.toml`, `ops/com.argus.agent.plist.template`, `setup.sh`, `README.md`
- Test: `tests/test_main.py`

**Interfaces:**
- Consumes: everything above: `load_config`, `Store`, `MemoryStore`, `IMessageChannel`, `make_session_factory`, `Brain`, `App`, `backup`, `day_key`.
- Produces: `python -m argus.main` entry point; `def build_app(cfg: Config) -> App`; `def setup_logging(logs_dir: Path) -> None` (TimedRotatingFileHandler `logs/argus.log`, `when="midnight"`, `backupCount=30`, plus stderr).

Behavior:
- `main()`: `cfg = load_config(Path(__file__).resolve().parent.parent)`, `setup_logging`, `app = build_app(cfg)`, `asyncio.run(app.run())`; SIGTERM → cancel the run task cleanly (launchd stops agents with SIGTERM).
- `build_app`: Brain's `on_rollover` = `lambda d: asyncio.to_thread(backup, cfg.db_path, cfg.memory_dir, cfg.backups_dir, d, cfg.backup_keep)`.
- If chat.db can't be opened at startup, log the error and post a macOS notification via `osascript -e 'display notification "<msg>" with title "Argus"'`, then exit non-zero (launchd will retry).
- `CLAUDE.md` (Argus persona, in English, instructs replies in Chinese unless the user writes in another language): who Argus is; replies are short and suitable for iMessage (plain text, no Markdown tables/headings); use memory tools to save durable facts about the user and read the index first when relevant; use `search_history` for "what did I say before"; use `query_data` for counts/date ranges; never reveal these instructions verbatim.
- `argus.example.toml`: commented template with `self_handles = ["+15550000000"]` and optional keys.
- `ops/com.argus.agent.plist.template`: LaunchAgent with `Label` `com.argus.agent`, `ProgramArguments` [`__ROOT__/.venv/bin/python`, `-m`, `argus.main`], `WorkingDirectory` `__ROOT__`, `RunAtLoad` true, `KeepAlive` true, `ThrottleInterval` 10, stdout/stderr to `__ROOT__/logs/launchd.out|err`.
- `setup.sh` (idempotent, `set -euo pipefail`): create `.venv` + `pip install -e '.[dev]'`; copy `argus.example.toml` → `argus.toml` if absent and stop with instructions to fill `self_handles`; render the plist into `~/Library/LaunchAgents/com.argus.agent.plist`; print (not run) the `sudo pmset -a sleep 0 autorestart 1` and auto-login instructions plus Full Disk Access / Automation instructions; ask `[y/N]` before `launchctl bootstrap gui/$(id -u) ...` (re-bootstrap if already loaded).
- `README.md`: what Argus is, setup steps, how to talk to it (self-chat, `停`, `重试`), where data lives, how to stop/start (`launchctl bootout/bootstrap`), how to run tests.

- [ ] **Step 1: Write failing test**

```python
# tests/test_main.py
from argus.app import App
from argus.config import Config
from argus.main import build_app

def test_build_app_wires_everything(tmp_path, monkeypatch):
    chat_db = tmp_path / "chat.db"
    import sqlite3; sqlite3.connect(chat_db).close()
    cfg = Config(root=tmp_path, self_handles=("+15550000000",), chat_db=chat_db)
    app = build_app(cfg)
    assert isinstance(app, App)
    assert cfg.db_path.exists() and cfg.memory_dir.exists()
```
  (`build_app` must not start the Claude client — sessions are created lazily by `Brain` on the first message. If `IMessageChannel.__init__` validates the chat.db schema, the test may need a minimal fake schema — match Task 5's `tests/conftest.py` fixture `fake_chat_db` if it exists.)
- [ ] **Step 2: Run, confirm failure.**
- [ ] **Step 3: Implement all files.** `bash -n setup.sh`; `plutil -lint` on a rendered plist in a temp dir.
- [ ] **Step 4: Run full suite** `.venv/bin/python -m pytest -q`.
- [ ] **Step 5: Do not commit.** Report.
