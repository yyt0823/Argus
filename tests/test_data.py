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

def test_runaway_query_times_out(db):
    import time
    sql = "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM c) SELECT count(*) FROM c"
    t0 = time.monotonic()
    with pytest.raises(QueryError, match="timed out"):
        query_readonly(db, sql, timeout_seconds=0.2)
    assert time.monotonic() - t0 < 1.0

def test_fast_query_unaffected_by_timeout(db):
    assert query_readonly(db, "SELECT count(*) FROM t", timeout_seconds=0.2)["rows"] == [[5]]
