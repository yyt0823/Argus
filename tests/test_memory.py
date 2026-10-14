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
