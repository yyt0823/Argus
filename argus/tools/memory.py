"""Markdown memory files under ``memory/`` plus their ``MEMORY.md`` index.

Each memory is ``memory/<name>.md``. ``MEMORY.md`` holds one line per memory,
``- [<name>](<name>.md) — <summary>``, sorted by name. Only ``*.md`` files
directly inside the memory directory are ever touched.
"""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path

_NAME_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
_ENTRY_RE = re.compile(r"- \[([^\]]+)\]\(")
_INDEX_FILE = "MEMORY.md"


class MemoryStore:
    """Read and write memory files and keep the index in sync."""

    def __init__(self, memory_dir: Path) -> None:
        self.dir = Path(memory_dir)
        self.dir.mkdir(parents=True, exist_ok=True)

    def list(self) -> list[str]:
        """Return memory names (without ``.md``), sorted, excluding the index."""
        return sorted(
            p.stem for p in self.dir.glob("*.md") if p.is_file() and _is_valid(p.stem)
        )

    def read(self, name: str) -> str:
        return self._path(name).read_text(encoding="utf-8")

    def write(self, name: str, content: str, summary: str) -> None:
        path = self._path(name)
        _atomic_write(path, content)
        summary = " ".join(summary.splitlines()).strip()
        self._update_index(name, f"- [{name}]({name}.md) — {summary}")

    def delete(self, name: str) -> None:
        path = self._path(name)
        path.unlink()  # raises FileNotFoundError if absent
        self._update_index(name, None)

    def index(self) -> str:
        """Return the ``MEMORY.md`` text, or ``""`` if it does not exist."""
        try:
            return (self.dir / _INDEX_FILE).read_text(encoding="utf-8")
        except FileNotFoundError:
            return ""

    def _path(self, name: str) -> Path:
        if not _is_valid(name):
            raise ValueError(f"invalid memory name: {name!r}")
        return self.dir / f"{name}.md"

    def _update_index(self, name: str, line: str | None) -> None:
        """Set (or remove, if ``line`` is None) the index entry for ``name``.

        Lines that are not memory entries (e.g. a hand-written header) are kept
        at the top in their original order.
        """
        other: list[str] = []
        entries: dict[str, str] = {}
        for existing in self.index().splitlines():
            match = _ENTRY_RE.match(existing)
            if match:
                entries[match.group(1)] = existing
            elif existing.strip():
                other.append(existing)
        if line is None:
            entries.pop(name, None)
        else:
            entries[name] = line
        lines = other + [entries[key] for key in sorted(entries)]
        _atomic_write(self.dir / _INDEX_FILE, "".join(f"{ln}\n" for ln in lines))


def _is_valid(name: str) -> bool:
    return bool(_NAME_RE.fullmatch(name)) and name != "memory"


def _atomic_write(path: Path, text: str) -> None:
    """Write ``text`` to a temp file in the same directory, then rename over ``path``."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
