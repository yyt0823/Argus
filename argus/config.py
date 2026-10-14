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


WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


@dataclass(frozen=True)
class FitnessConfig:
    """The optional ``[fitness]`` table: daily nutrition budgets and the plan file.

    Defaults are generic placeholders; personal values belong in ``argus.toml``.
    Ranges are ``(low, high)`` grams per day.
    """

    enabled: bool = True
    plan_path: str = "data/fitness/CLAUDE.md"
    training_days: tuple[str, ...] = ("mon", "wed", "fri")
    training_kcal: float = 2000
    rest_kcal: float = 1800
    min_kcal: float = 1200
    protein_g: tuple[float, float] = (100, 150)
    carbs_g: tuple[float, float] = (150, 250)
    fat_g: tuple[float, float] = (50, 70)
    added_sugar_g_max: float = 25
    sodium_mg_max: float = 2300


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
    fitness: FitnessConfig = field(default_factory=FitnessConfig)

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

    @property
    def fitness_plan_path(self) -> Path:
        """``fitness.plan_path`` resolved against ``root`` (absolute paths are kept)."""
        return self.root / Path(self.fitness.plan_path).expanduser()


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
    kwargs["fitness"] = _load_fitness(raw.get("fitness", {}), path)
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


_FITNESS_NUMBER_KEYS = ("training_kcal", "rest_kcal", "min_kcal", "added_sugar_g_max", "sodium_mg_max")
_FITNESS_RANGE_KEYS = ("protein_g", "carbs_g", "fat_g")
_FITNESS_KEYS = ("enabled", "plan_path", "training_days", *_FITNESS_NUMBER_KEYS, *_FITNESS_RANGE_KEYS)


def _is_number(value: object) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float))


def _load_fitness(raw: object, path: Path) -> FitnessConfig:
    """Validate the optional ``[fitness]`` table; raise ``ValueError`` naming ``fitness.<key>``."""
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: fitness must be a table ([fitness])")
    unknown = sorted(set(raw) - set(_FITNESS_KEYS))
    if unknown:
        raise ValueError(
            f"{path}: unknown key(s) in [fitness]: {', '.join('fitness.' + k for k in unknown)}"
        )

    kwargs: dict = {}
    if "enabled" in raw:
        if not isinstance(raw["enabled"], bool):
            raise ValueError(f"{path}: fitness.enabled must be true or false")
        kwargs["enabled"] = raw["enabled"]
    if "plan_path" in raw:
        value = raw["plan_path"]
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{path}: fitness.plan_path must be a non-empty string, got {value!r}")
        kwargs["plan_path"] = value.strip()
    if "training_days" in raw:
        value = raw["training_days"]
        days = [d.strip().lower() for d in value if isinstance(d, str)] if isinstance(value, list) else None
        if days is None or len(days) != len(value) or not set(days) <= set(WEEKDAYS):
            raise ValueError(
                f"{path}: fitness.training_days must be a list of {'/'.join(WEEKDAYS)}, got {value!r}"
            )
        kwargs["training_days"] = tuple(days)
    for key in _FITNESS_NUMBER_KEYS:
        if key in raw:
            value = raw[key]
            positive = key.endswith("kcal") and key != "min_kcal"
            if not _is_number(value) or value < 0 or (positive and value == 0):
                kind = "a positive" if positive else "a non-negative"
                raise ValueError(f"{path}: fitness.{key} must be {kind} number, got {value!r}")
            kwargs[key] = value
    for key in _FITNESS_RANGE_KEYS:
        if key in raw:
            value = raw[key]
            if (
                not isinstance(value, list)
                or len(value) != 2
                or not all(_is_number(v) and v >= 0 for v in value)
                or value[0] > value[1]
            ):
                raise ValueError(
                    f"{path}: fitness.{key} must be [low, high] non-negative numbers with low <= high, "
                    f"got {value!r}"
                )
            kwargs[key] = (value[0], value[1])
    return FitnessConfig(**kwargs)
