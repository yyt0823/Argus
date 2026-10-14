"""Read-only SQL access to data/argus.db for the model's ``query_data`` tool."""

import sqlite3
from pathlib import Path


# Actions refused by the authorizer: ATTACH would create files even on a
# read-only connection, and PRAGMA could switch query_only back off.
_DENIED_ACTIONS = {sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH, sqlite3.SQLITE_PRAGMA}


def _authorizer(action: int, *_: object) -> int:
    return sqlite3.SQLITE_DENY if action in _DENIED_ACTIONS else sqlite3.SQLITE_OK


class QueryError(Exception):
    """The query could not be run: bad SQL, a write attempt, or a missing database."""


def query_readonly(db_path: Path, sql: str, max_rows: int = 200) -> dict:
    """Run one read-only SQL statement and return at most ``max_rows`` rows.

    Returns ``{"columns": [...], "rows": [[...], ...], "truncated": bool}``.
    Writes are blocked twice over: the file is opened with ``mode=ro`` and the
    connection sets ``PRAGMA query_only``; an authorizer also refuses ATTACH,
    DETACH and PRAGMA. Multi-statement input is rejected by
    ``sqlite3`` itself ("You can only execute one statement at a time") before
    anything runs; a single trailing ``;`` is allowed.
    """
    if not Path(db_path).is_file():
        raise QueryError(f"database not found: {db_path}")
    sql = sql.strip()
    if not sql:
        raise QueryError("empty query")
    try:
        conn = sqlite3.connect(f"{Path(db_path).resolve().as_uri()}?mode=ro", uri=True)
    except sqlite3.Error as e:
        raise QueryError(str(e)) from e
    try:
        conn.execute("PRAGMA query_only = ON")
        conn.set_authorizer(_authorizer)
        cur = conn.execute(sql)
        if cur.description is None:
            raise QueryError("statement returned no result set")
        columns = [d[0] for d in cur.description]
        fetched = cur.fetchmany(max_rows + 1)
    except sqlite3.Error as e:
        raise QueryError(str(e)) from e
    finally:
        conn.close()
    return {
        "columns": columns,
        "rows": [list(r) for r in fetched[:max_rows]],
        "truncated": len(fetched) > max_rows,
    }
