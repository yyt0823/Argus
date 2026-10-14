"""Runtime configuration loaded from ``argus.toml`` in the project root."""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from datetime import time
from pathlib import Path


@dataclass(frozen=True)
class MailConfig:
    """The optional ``[mail]`` table: mail triage, alerts, and the daily briefing."""

    enabled: bool = True
    briefing_time: time = time(8, 0)
    quiet_start: time = time(23, 0)
    quiet_end: time = time(8, 0)
    check_interval_minutes: float = 5.0
    triage_model: str = "haiku"

    def in_quiet_hours(self, t: time) -> bool:
        """True if ``t`` is in ``[quiet_start, quiet_end)``; the range may cross midnight.

        Equal start and end mean no quiet hours.
        """
        if self.quiet_start <= self.quiet_end:
            return self.quiet_start <= t < self.quiet_end
        return t >= self.quiet_start or t < self.quiet_end


@dataclass(frozen=True)
class Config:
    root: Path
    # "self": Messages is signed into the user's own Apple ID; self_handles are the user's handles.
    # "contact": Messages is signed into a dedicated bot Apple ID; self_handles are the owner's
    # handles (the iPhone's phone number / email) that Argus talks to.
    self_handles: tuple[str, ...]
    chat_db: Path = Path.home() / "Library/Messages/chat.db"
    poll_interval: float = 1.0
    rollover_hour: int = 4
    slow_notice_seconds: float = 15.0
    backup_keep: int = 30
    model: str | None = None
    mode: str = "self"
    mail: MailConfig = field(default_factory=MailConfig)

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


MODES = ("self", "contact")

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

    mode = raw.get("mode", "self")
    if mode not in MODES:
        raise ValueError(f"{path}: mode must be one of {', '.join(repr(m) for m in MODES)}, got {mode!r}")

    kwargs: dict = {k: raw[k] for k in _OPTIONAL_KEYS if k in raw}
    kwargs["mode"] = mode
    if "chat_db" in raw:
        kwargs["chat_db"] = Path(raw["chat_db"]).expanduser()
    kwargs["mail"] = _load_mail(raw.get("mail", {}), path)
    return Config(root=root, self_handles=tuple(h.strip() for h in handles), **kwargs)


_HHMM = re.compile(r"([01]\d|2[0-3]):([0-5]\d)")
_MAIL_TIME_KEYS = ("briefing_time", "quiet_start", "quiet_end")
_MAIL_KEYS = ("enabled", "check_interval_minutes", "triage_model", *_MAIL_TIME_KEYS)


def _load_mail(raw: object, path: Path) -> MailConfig:
    """Validate the optional ``[mail]`` table; raise ``ValueError`` naming ``mail.<key>``."""
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: mail must be a table ([mail])")
    unknown = sorted(set(raw) - set(_MAIL_KEYS))
    if unknown:
        raise ValueError(f"{path}: unknown key(s) in [mail]: {', '.join(unknown)}")

    kwargs: dict = {}
    if "enabled" in raw:
        if not isinstance(raw["enabled"], bool):
            raise ValueError(f"{path}: mail.enabled must be true or false")
        kwargs["enabled"] = raw["enabled"]
    for key in _MAIL_TIME_KEYS:
        if key in raw:
            value = raw[key]
            m = _HHMM.fullmatch(value) if isinstance(value, str) else None
            if m is None:
                raise ValueError(f'{path}: mail.{key} must be a "HH:MM" string, got {value!r}')
            kwargs[key] = time(int(m[1]), int(m[2]))
    if "check_interval_minutes" in raw:
        value = raw["check_interval_minutes"]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
            raise ValueError(f"{path}: mail.check_interval_minutes must be a positive number, got {value!r}")
        kwargs["check_interval_minutes"] = float(value)
    if "triage_model" in raw:
        value = raw["triage_model"]
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{path}: mail.triage_model must be a non-empty string, got {value!r}")
        kwargs["triage_model"] = value.strip()
    return MailConfig(**kwargs)
