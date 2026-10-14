"""Nutrition math: day type, totals, budget, remaining, flags, and period summaries.

Pure functions over Store rows (dicts from ``foods_*`` / ``weights_between`` / ``workouts_between``);
no I/O. All numbers the model reports come from here, never from the model's own arithmetic.

Rows imported from the Markdown logs with unknown totals (kcal stored as 0) or unknown protein are
"no data" for those numbers: they never raise ``under_min_kcal`` / ``protein_low``, and a day holding
only such rows does not count as a logged day in ``period_summary``.
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Literal

from argus.config import WEEKDAYS, FitnessConfig
from argus.fitness.importer import PROTEIN_UNKNOWN, TOTALS_UNKNOWN
from argus.store import IMPORTED_PREFIX

_RANGES = ("protein_g", "fat_g", "carbs_g")
_SODIUM = ("low", "medium", "high")


@dataclass(frozen=True)
class DayStatus:
    day: date
    day_type: Literal["training", "rest"]
    entries: list[dict]
    totals: dict
    budget: dict
    remaining: dict
    flags: list[str]


def _import_remarks(f: dict) -> list[str]:
    note = f.get("note") or ""
    if not note.startswith(IMPORTED_PREFIX):
        return []
    return [r.strip() for r in note[len(IMPORTED_PREFIX):].split(";")]


def totals_unknown(f: dict) -> bool:
    """True for an imported row whose day totals were not recorded (its kcal/protein 0 means "no data")."""
    return TOTALS_UNKNOWN in _import_remarks(f)


def protein_unknown(f: dict) -> bool:
    """True for an imported row whose protein was not recorded (or whose totals were not)."""
    remarks = _import_remarks(f)
    return TOTALS_UNKNOWN in remarks or PROTEIN_UNKNOWN in remarks


def is_training_day(day: date, workouts_that_day: list[dict], cfg: FitnessConfig) -> bool:
    """A logged workout makes it a training day; otherwise the weekday decides."""
    return bool(workouts_that_day) or WEEKDAYS[day.weekday()] in cfg.training_days


def day_status(day: date, foods: list[dict], workouts_that_day: list[dict], cfg: FitnessConfig) -> DayStatus:
    training = is_training_day(day, workouts_that_day, cfg)
    totals: dict = {
        "kcal": sum(f["kcal"] for f in foods),
        "protein_g": sum(f["protein_g"] for f in foods),
        "fat_g": sum(f["fat_g"] for f in foods),
        "carbs_g": sum(f["carbs_g"] for f in foods),
        "added_sugar_g": sum(f["added_sugar_g"] for f in foods if f.get("added_sugar_g") is not None),
        "added_sugar_unknown": sum(1 for f in foods if f.get("added_sugar_g") is None),
        "sodium": {level: sum(1 for f in foods if f.get("sodium") == level) for level in _SODIUM},
    }
    budget: dict = {
        "kcal": cfg.training_kcal if training else cfg.rest_kcal,
        "min_kcal": cfg.min_kcal,
        "protein_g": tuple(cfg.protein_g),
        "fat_g": tuple(cfg.fat_g),
        "carbs_g": tuple(cfg.carbs_g),
        "added_sugar_g_max": cfg.added_sugar_g_max,
    }
    remaining: dict = {"kcal": budget["kcal"] - totals["kcal"]}
    for key in _RANGES:
        lo, hi = budget[key]
        remaining[key] = (lo - totals[key], hi - totals[key])
    remaining["added_sugar_g"] = budget["added_sugar_g_max"] - totals["added_sugar_g"]

    kcal_unknown = any(totals_unknown(f) for f in foods)
    prot_unknown = any(protein_unknown(f) for f in foods)
    flags: list[str] = []
    if totals["kcal"] > budget["kcal"]:
        flags.append("over_kcal")
    if totals["kcal"] < budget["min_kcal"] and not kcal_unknown:
        flags.append("under_min_kcal")
    if totals["fat_g"] > budget["fat_g"][1]:
        flags.append("over_fat")
    if totals["carbs_g"] > budget["carbs_g"][1]:
        flags.append("over_carbs")
    if totals["added_sugar_g"] > budget["added_sugar_g_max"]:
        flags.append("over_sugar")
    if totals["protein_g"] < budget["protein_g"][0] and not prot_unknown:
        flags.append("protein_low")
    if totals["sodium"]["high"]:
        flags.append("sodium_high")
    if kcal_unknown:
        flags.append("totals_unknown")
    elif prot_unknown:
        flags.append("protein_unknown")
    return DayStatus(
        day=day,
        day_type="training" if training else "rest",
        entries=list(foods),
        totals=totals,
        budget=budget,
        remaining=remaining,
        flags=flags,
    )


def _r(x: float) -> int:
    """Round half away from zero (stable, unlike banker's rounding)."""
    return int(math.copysign(math.floor(abs(x) + 0.5), x)) if x else 0


def _weekday(day: date) -> str:
    return WEEKDAYS[day.weekday()].capitalize()


def _entry_line(f: dict) -> str:
    at = str(f.get("logged_at") or "")[11:16]
    label = f"{f['meal']}: " if f.get("meal") else ""
    sugar = "?" if f.get("added_sugar_g") is None else _r(f["added_sugar_g"])
    sodium = f.get("sodium") or "?"
    return (
        f"  #{f['id']} {at} {label}{f['description']} - {_r(f['kcal'])} kcal, P {_r(f['protein_g'])} g, "
        f"F {_r(f['fat_g'])} g, C {_r(f['carbs_g'])} g, added sugar {sugar} g, sodium {sodium}"
    )


def format_day_status(s: DayStatus) -> str:
    t, b, rem = s.totals, s.budget, s.remaining
    lines = [f"{s.day.isoformat()} ({_weekday(s.day)}) {s.day_type} day"]
    if s.entries:
        lines.append("Entries:")
        lines.extend(_entry_line(f) for f in s.entries)
    else:
        lines.append("Entries: none")
    unknown = t.get("added_sugar_unknown", 0)
    sugar_note = f" ({unknown} {'entry' if unknown == 1 else 'entries'} unknown)" if unknown and s.entries else ""
    sodium = ", ".join(f"{level} x{t['sodium'][level]}" for level in reversed(_SODIUM))
    lines.append(
        f"Totals: {_r(t['kcal'])} kcal, protein {_r(t['protein_g'])} g, fat {_r(t['fat_g'])} g, "
        f"carbs {_r(t['carbs_g'])} g, added sugar {_r(t['added_sugar_g'])} g{sugar_note}; sodium {sodium}"
    )
    lines.append(
        f"Budget: {_r(b['kcal'])} kcal (min {_r(b['min_kcal'])}), "
        + ", ".join(f"{k.removesuffix('_g')} {_r(b[k][0])}-{_r(b[k][1])} g" for k in _RANGES)
        + f", added sugar <= {_r(b['added_sugar_g_max'])} g"
    )
    lines.append(
        f"Remaining: {_r(rem['kcal'])} kcal, "
        + ", ".join(f"{k.removesuffix('_g')} {_r(rem[k][0])} to {_r(rem[k][1])} g" for k in _RANGES)
        + f", added sugar {_r(rem['added_sugar_g'])} g"
    )
    lines.append(f"Flags: {', '.join(s.flags) if s.flags else 'none'}")
    return "\n".join(lines)


def weight_averages(weights: list[dict]) -> float | None:
    """Mean ``weight_kg`` of the entries, or ``None`` if there are none."""
    if not weights:
        return None
    return sum(w["weight_kg"] for w in weights) / len(weights)


def _n(count: int, word: str) -> str:
    return f"{count} {word}" if count == 1 else f"{count} {word}s" if word != "entry" else f"{count} entries"


def period_summary(
    start: date,
    end: date,
    foods: list[dict],
    weights: list[dict],
    prev_weights: list[dict],
    workouts: list[dict],
    cfg: FitnessConfig,
) -> str:
    """Per-day totals vs budget for ``start..end`` (inclusive), weight trend, and workout count."""
    days = [start + timedelta(days=i) for i in range((end - start).days + 1)]
    lines = [f"Summary {start.isoformat()} to {end.isoformat()} ({_n(len(days), 'day')})"]
    logged: list[DayStatus] = []
    for day in days:
        key = day.isoformat()
        day_foods = [f for f in foods if f["day"] == key]
        s = day_status(day, day_foods, [w for w in workouts if w["day"] == key], cfg)
        head = f"{key} {_weekday(day)} {s.day_type}:"
        if not day_foods:
            lines.append(f"{head} no entries")
            continue
        if all(totals_unknown(f) for f in day_foods):
            lines.append(f"{head} no data (imported, totals unknown)")
            continue
        logged.append(s)
        t, b = s.totals, s.budget
        parts = ", ".join(
            f"{k[0].upper()} {_r(t[k])} ({_r(b[k][0])}-{_r(b[k][1])})" for k in _RANGES
        )
        flags = f" [{', '.join(s.flags)}]" if s.flags else ""
        lines.append(f"{head} {_r(t['kcal'])}/{_r(b['kcal'])} kcal, {parts}{flags}")

    if logged:
        avg_kcal = sum(s.totals["kcal"] for s in logged) / len(logged)
        with_protein = [s for s in logged if not any(protein_unknown(f) for f in s.entries)]
        avg_protein = (
            f"{_r(sum(s.totals['protein_g'] for s in with_protein) / len(with_protein))} g"
            if with_protein else "unknown"
        )
        over = sum("over_kcal" in s.flags for s in logged)
        low = sum("protein_low" in s.flags for s in logged)
        lines.append(
            f"Logged days: {len(logged)}/{len(days)}, avg {_r(avg_kcal)} kcal, avg protein {avg_protein}; "
            f"over kcal {over} day(s), protein low {low} day(s)"
        )
    else:
        lines.append(f"Logged days: 0/{len(days)}")

    avg, prev = weight_averages(weights), weight_averages(prev_weights)
    if avg is None:
        previous = "no entries" if prev is None else f"avg {prev:.1f} kg, {_n(len(prev_weights), 'entry')}"
        lines.append(f"Weight: no entries this period (previous: {previous})")
    elif prev is None:
        lines.append(f"Weight: avg {avg:.1f} kg ({_n(len(weights), 'entry')}), previous period: no entries")
    else:
        lines.append(
            f"Weight: avg {avg:.1f} kg ({_n(len(weights), 'entry')}) vs previous {prev:.1f} kg "
            f"({_n(len(prev_weights), 'entry')}), change {avg - prev:+.1f} kg"
        )

    kinds = Counter(w["kind"] for w in workouts)
    detail = f" ({', '.join(f'{k} x{n}' for k, n in sorted(kinds.items()))})" if kinds else ""
    lines.append(f"Workouts: {len(workouts)}{detail}")
    return "\n".join(lines)
