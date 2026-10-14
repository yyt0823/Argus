"""Runtime configuration loaded from ``argus.toml`` in the project root."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Config:
    root: Path
    self_handles: tuple[str, ...]  # the user's own iMessage handles (phone numbers / emails)
    chat_db: Path = Path.home() / "Library/Messages/chat.db"
    poll_interval: float = 1.0
    rollover_hour: int = 4
    slow_notice_seconds: float = 15.0
    backup_keep: int = 30
    model: str | None = None

    @property
    def data_dir(self) -> Path:
        return self.root / "data"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "argus.db"

    @property
    def memory_dir(self) -> Path:
        return self.root / "memory"

    @property
    def backups_dir(self) -> Path:
        return self.root / "backups"

    @property
    def logs_dir(self) -> Path:
        return self.root / "logs"


_OPTIONAL_KEYS = ("poll_interval", "rollover_hour", "slow_notice_seconds", "backup_keep", "model")


def load_config(root: Path) -> Config:
    """Read ``root/argus.toml``; raise ``ValueError`` if it is missing or has no ``self_handles``."""
    path = root / "argus.toml"
    if not path.is_file():
        raise ValueError(f"config file not found: {path} (copy argus.example.toml to argus.toml)")
    with path.open("rb") as f:
        raw = tomllib.load(f)

    handles = raw.get("self_handles")
    if (
        not isinstance(handles, list)
        or not handles
        or not all(isinstance(h, str) and h.strip() for h in handles)
    ):
        raise ValueError(f"{path}: self_handles must be a non-empty list of iMessage handles")

    kwargs: dict = {k: raw[k] for k in _OPTIONAL_KEYS if k in raw}
    if "chat_db" in raw:
        kwargs["chat_db"] = Path(raw["chat_db"]).expanduser()
    return Config(root=root, self_handles=tuple(h.strip() for h in handles), **kwargs)
