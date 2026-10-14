"""One-time, idempotent import of the user's Markdown logs into the Store.

``food-log.md`` has a table ``| 日期 | 训练 | 早餐 | 晚餐 | 全天热量 | 蛋白质 | 备注 |``; each dated row
becomes ONE ``food_log`` entry (``meal="other"``) holding the day's recorded totals. ``weight-log.md``
has ``| 日期 | 体重（公斤） | 腰围（厘米） | 备注 |``. Imported rows carry the ``[imported]`` note prefix;
a day that already has an imported row (even a deleted one) is skipped, so re-running is safe.
Numbers are never invented: missing totals are stored as 0 with a note saying so.
"""

from __future__ import annotations

import re
from datetime import date, datetime, time
from pathlib import Path

from argus.store import IMPORTED_PREFIX, Store

FOOD_FILE = "food-log.md"
WEIGHT_FILE = "weight-log.md"
_FOOD_HEADER = ("日期", "训练", "早餐", "晚餐", "全天热量", "蛋白质", "备注")
_WEIGHT_HEADER = ("日期", "体重（公斤）", "腰围（厘米）", "备注")

_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_NUMBER = re.compile(r"\d+(?:\.\d+)?")
# A complete value: optional "约", a number, an optional unit.
_FULL_VALUE = re.compile(r"约?\s*(\d+(?:\.\d+)?)\s*(?:千卡|大卡|kcal|克|g)?", re.IGNORECASE)
_IMPORTED_AT = time(12, 0)


def import_markdown_logs(store: Store, fitness_dir: Path) -> dict:
    """Import ``food-log.md`` and ``weight-log.md`` from ``fitness_dir``; missing files are skipped.

    Returns counts: ``food_imported``, ``food_skipped`` (day already imported), ``weight_imported``,
    ``weight_skipped``, and ``invalid`` (rows the Store rejected, e.g. absurd values; never raised).
    """
    result = {"food_imported": 0, "food_skipped": 0, "weight_imported": 0, "weight_skipped": 0, "invalid": 0}
    food_path = fitness_dir / FOOD_FILE
    if food_path.is_file():
        done = store.imported_days("food")
        for row in _table_rows(food_path.read_text(encoding="utf-8"), _FOOD_HEADER):
            day = date.fromisoformat(row["日期"])
            if day in done:
                result["food_skipped"] += 1
                continue
            try:
                _import_food(store, day, row)
            except ValueError:
                result["invalid"] += 1
                continue
            done.add(day)
            result["food_imported"] += 1
    weight_path = fitness_dir / WEIGHT_FILE
    if weight_path.is_file():
        done = store.imported_days("weight")
        for row in _table_rows(weight_path.read_text(encoding="utf-8"), _WEIGHT_HEADER):
            day = date.fromisoformat(row["日期"])
            weight = _parse_value(row["体重（公斤）"])
            if weight is None:
                continue  # nothing recorded that day
            if day in done:
                result["weight_skipped"] += 1
                continue
            waist = _parse_value(row["腰围（厘米）"])
            try:
                store.add_weight(
                    day,
                    datetime.combine(day, _IMPORTED_AT),
                    weight[0],
                    waist_cm=waist[0] if waist else None,
                    note=_note(row["备注"]),
                )
            except ValueError:
                result["invalid"] += 1
                continue
            done.add(day)
            result["weight_imported"] += 1
    return result


def _import_food(store: Store, day: date, row: dict[str, str]) -> None:
    parts = [p for p in (row["早餐"], row["晚餐"]) if p]
    description = " / ".join(parts) or "(no description)"
    kcal = _parse_value(row["全天热量"])
    protein = _parse_value(row["蛋白质"])
    remarks: list[str] = []
    if kcal is None:
        remarks.append("totals unknown")
    elif not kcal[1] or (protein is not None and not protein[1]):
        remarks.append("partial day")
    if kcal is not None and protein is None:
        remarks.append("protein unknown")
    if row["训练"]:
        remarks.append(f"训练: {row['训练']}")
    if row["备注"]:
        remarks.append(row["备注"])
    store.add_food(
        day,
        datetime.combine(day, _IMPORTED_AT),
        description,
        kcal=kcal[0] if kcal else 0,
        protein_g=protein[0] if protein else 0,
        fat_g=0,
        carbs_g=0,
        meal="other",
        note=_note("; ".join(remarks)),
    )


def _note(text: str) -> str:
    return f"{IMPORTED_PREFIX} {text}".rstrip()


def _parse_value(cell: str) -> tuple[float, bool] | None:
    """``(number, complete)``: "约 1270" → (1270, True); "早餐约 550" → (550, False); "" → None."""
    m = _FULL_VALUE.fullmatch(cell.strip())
    if m:
        return float(m[1]), True
    m = _NUMBER.search(cell)
    return (float(m[0]), False) if m else None


def _split_row(line: str) -> list[str]:
    body = line.strip()
    body = body[1:] if body.startswith("|") else body
    body = body[:-1] if body.endswith("|") and not body.endswith("\\|") else body
    cells = re.split(r"(?<!\\)\|", body)
    return [c.strip().replace("\\|", "|") for c in cells]


def _table_rows(text: str, header: tuple[str, ...]) -> list[dict[str, str]]:
    """Dated rows of every Markdown table whose header is exactly ``header``."""
    rows: list[dict[str, str]] = []
    in_table = False
    for line in text.splitlines():
        if not line.strip().startswith("|"):
            in_table = False
            continue
        cells = _split_row(line)
        if tuple(cells) == header:
            in_table = True
            continue
        if not in_table or not _is_date(cells[0]):
            continue  # separator, non-dated, or another table's row
        cells += [""] * (len(header) - len(cells))
        rows.append(dict(zip(header, cells)))
    return rows


def _is_date(cell: str) -> bool:
    if not _DATE.fullmatch(cell):
        return False
    try:
        date.fromisoformat(cell)
    except ValueError:
        return False
    return True
