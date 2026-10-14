"""Chat tools for the food / weight / workout log and the nutrition budget.

Every number the model reports (totals, remaining, averages) is computed here in Python from the
Store, never by the model. Every write returns the confirmation plus that day's formatted status,
so the model can reply with what remains without a second call. Handlers never raise.
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta
from typing import Any, Callable

from claude_agent_sdk import SdkMcpTool, ToolAnnotations, tool

from argus.config import Config
from argus.fitness.nutrition import day_status, format_day_status, period_summary, weight_averages
from argus.scheduler import day_key
from argus.store import MEALS, SODIUM_LEVELS, WORKOUT_KINDS, Store
from argus.tools.server import _ToolError, _date_arg, _ok, _safe

_READ_ONLY = ToolAnnotations(readOnlyHint=True)

_PLAN_CHARS = 60000
_SUMMARY_DEFAULT_DAYS = 7
_SUMMARY_MAX_DAYS = 92

_NUMERIC = ("kcal", "protein_g", "fat_g", "carbs_g", "added_sugar_g")
_TEXT = ("description", "meal", "sodium", "note")

_DAY_DOC = "Argus day, YYYY-MM-DD (default: today; days roll over at 04:00, so a 01:00 snack is yesterday)."
_ENTRY_NOTE = (
    " All numbers are for the WHOLE entry as eaten (the full portion of the meal or item), not per "
    "100 g or per serving."
)
_FOOD_PROPS: dict[str, dict] = {
    "description": {"type": "string", "description": "What was eaten, with amounts, e.g. '燕麦 50 g, 牛奶 250 ml'."},
    "kcal": {"type": "number", "description": "Energy of the whole entry in kcal (kilocalories), 0-5000."},
    "protein_g": {"type": "number", "description": "Protein of the whole entry in grams, 0-300."},
    "fat_g": {"type": "number", "description": "Fat of the whole entry in grams, 0-500."},
    "carbs_g": {"type": "number", "description": "Carbohydrates of the whole entry in grams, 0-1000."},
    "added_sugar_g": {
        "type": "number",
        "description": "Added (free) sugar of the whole entry in grams; omit if unknown (not natural sugar "
        "in fruit/milk).",
    },
    "sodium": {"type": "string", "enum": list(SODIUM_LEVELS), "description": "Sodium level of the entry."},
    "meal": {"type": "string", "enum": list(MEALS), "description": "Which meal this is."},
    "day": {"type": "string", "description": _DAY_DOC},
    "note": {"type": "string", "description": "Optional free-text note (e.g. how the estimate was made)."},
}


def _num(args: dict[str, Any], key: str, required: bool = True) -> float | None:
    """A number from ``args``; numeric strings are accepted. Range checks are left to the Store."""
    value = args.get(key)
    if value is None or value == "":
        if required:
            raise _ToolError(f"Error: '{key}' is required")
        return None
    if isinstance(value, bool):
        raise _ToolError(f"Error: '{key}' must be a number, got {value!r}")
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            pass
    raise _ToolError(f"Error: '{key}' must be a number, got {value!r}")


def _text(args: dict[str, Any], key: str) -> str | None:
    value = args.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise _ToolError(f"Error: '{key}' must be a string")
    return value.strip() or None


def _id(args: dict[str, Any]) -> int:
    value = args.get("id")
    if isinstance(value, bool) or value is None:
        raise _ToolError("Error: 'id' (the food entry number, e.g. 12 for #12) is required")
    if isinstance(value, str) and value.strip().lstrip("#").isdigit():
        return int(value.strip().lstrip("#"))
    if isinstance(value, int) or (isinstance(value, float) and value.is_integer()):
        return int(value)
    raise _ToolError(f"Error: 'id' must be an integer, got {value!r}")


def _fmt(x: float) -> str:
    return f"{x:g}" if float(x).is_integer() else f"{x:.1f}"


def _entry(row: dict) -> str:
    sugar = "unknown" if row.get("added_sugar_g") is None else f"{_fmt(row['added_sugar_g'])} g"
    meal = f" [{row['meal']}]" if row.get("meal") else ""
    return (
        f"#{row['id']} {row['day']}{meal} {row['description']}: {_fmt(row['kcal'])} kcal, "
        f"protein {_fmt(row['protein_g'])} g, fat {_fmt(row['fat_g'])} g, carbs {_fmt(row['carbs_g'])} g, "
        f"added sugar {sugar}, sodium {row.get('sodium') or 'unknown'}"
    )


def build_fitness_tools(
    store: Store, cfg: Config, now: Callable[[], datetime] = datetime.now
) -> list[SdkMcpTool]:
    """The fitness tools bound to ``store`` and ``cfg.fitness``; "today" is ``day_key(now())``."""
    fit = cfg.fitness

    def today() -> date:
        return day_key(now(), cfg.rollover_hour)

    def day_arg(args: dict[str, Any], key: str = "day") -> date:
        day = _date_arg(args, key)
        if day is None:
            return today()
        if day > today():
            raise _ToolError(f"Error: '{key}' {day.isoformat()} is in the future (today is {today().isoformat()})")
        return day

    def status_text(day: date) -> str:
        foods = store.foods_for_day(day)
        workouts = store.workouts_between(day, day)
        return format_day_status(day_status(day, foods, workouts, fit))

    async def status(day: date) -> str:
        return await asyncio.to_thread(status_text, day)

    @tool(
        "fitness_plan",
        "Read the user's fitness and nutrition plan (Markdown): body data, daily budgets, a food "
        "reference table with per-portion values, food preferences and dislikes, the training plan, menu "
        "templates and answering rules. Read it once per session before estimating food or giving diet "
        "or training advice.",
        {"type": "object", "properties": {}},
        annotations=_READ_ONLY,
    )
    @_safe
    async def fitness_plan(args: dict[str, Any]) -> dict[str, Any]:
        path = cfg.fitness_plan_path
        if not await asyncio.to_thread(path.is_file):
            raise _ToolError(
                f"Error: plan file not found at {path}. Tell the user to create it (or set "
                "fitness.plan_path in argus.toml); meanwhile use typical nutrition values and the "
                "budgets shown by nutrition_status."
            )
        text = await asyncio.to_thread(path.read_text, encoding="utf-8")
        if len(text) > _PLAN_CHARS:
            text = text[:_PLAN_CHARS] + "\n[... plan truncated ...]"
        return _ok(text)

    @tool(
        "food_log_add",
        "Record ONE food entry (one meal, or one item eaten on its own) in the user's food log. Call it "
        "whenever the user says they ate or drank something, BEFORE replying; one call per meal. Estimate "
        "energy in kcal and protein/fat/carbs/added sugar in grams, using the plan's reference table "
        "first, otherwise typical values." + _ENTRY_NOTE + " Values above 5000 kcal or 300 g protein "
        "are rejected: re-estimate. Returns the entry id (#N, needed to correct it later) and the day's "
        "status: entries, totals, budget, remaining per nutrient and flags.",
        {
            "type": "object",
            "properties": _FOOD_PROPS,
            "required": ["description", "kcal", "protein_g", "fat_g", "carbs_g"],
        },
    )
    @_safe
    async def food_log_add(args: dict[str, Any]) -> dict[str, Any]:
        description = _text(args, "description")
        if description is None:
            raise _ToolError("Error: 'description' is required (what was eaten, with amounts)")
        nums = {k: _num(args, k, required=k != "added_sugar_g") for k in _NUMERIC}
        day = day_arg(args)
        fid = await asyncio.to_thread(
            store.add_food,
            day,
            now(),
            description,
            nums["kcal"],
            nums["protein_g"],
            nums["fat_g"],
            nums["carbs_g"],
            added_sugar_g=nums["added_sugar_g"],
            sodium=_text(args, "sodium"),
            meal=_text(args, "meal"),
            note=_text(args, "note"),
        )
        row = next(r for r in await asyncio.to_thread(store.foods_for_day, day) if r["id"] == fid)
        return _ok(f"Logged food entry {_entry(row)}\n\n{await status(day)}")

    @tool(
        "food_log_update",
        "Correct an existing food entry by its id (#N from food_log_add or nutrition_status). Pass only "
        "the fields to change; omitted or null fields are kept. Numbers replace the old values and are "
        "for the WHOLE entry (kcal; grams), not a difference. Use this (not a new food_log_add) when the "
        "user corrects something already logged, e.g. a different amount. Returns the updated entry and "
        "the day's status.",
        {
            "type": "object",
            "properties": {
                "id": {"type": "integer", "description": "Food entry id, e.g. 12 for #12."},
                **_FOOD_PROPS,
            },
            "required": ["id"],
        },
    )
    @_safe
    async def food_log_update(args: dict[str, Any]) -> dict[str, Any]:
        fid = _id(args)
        fields: dict[str, Any] = {}
        for key in _NUMERIC:
            value = _num(args, key, required=False)
            if value is not None:
                fields[key] = value
        for key in _TEXT:
            value = _text(args, key)
            if value is not None:
                fields[key] = value
        if args.get("day"):
            fields["day"] = day_arg(args)
        if not fields:
            raise _ToolError("Error: nothing to update; pass at least one field to change")
        try:
            row = await asyncio.to_thread(lambda: store.update_food(fid, **fields))
        except KeyError:
            raise _ToolError(f"Error: no food entry #{fid} (it does not exist or was deleted)") from None
        day = date.fromisoformat(row["day"])
        return _ok(f"Updated food entry {_entry(row)}\n\n{await status(day)}")

    @tool(
        "food_log_delete",
        "Delete a food entry by its id (#N) when it was logged by mistake or is a duplicate. Returns the "
        "deleted entry and the day's updated status.",
        {
            "type": "object",
            "properties": {"id": {"type": "integer", "description": "Food entry id, e.g. 12 for #12."}},
            "required": ["id"],
        },
        annotations=ToolAnnotations(destructiveHint=True),
    )
    @_safe
    async def food_log_delete(args: dict[str, Any]) -> dict[str, Any]:
        fid = _id(args)
        try:
            row = await asyncio.to_thread(store.delete_food, fid)
        except KeyError:
            raise _ToolError(f"Error: no food entry #{fid} (it does not exist or was already deleted)") from None
        day = date.fromisoformat(row["day"])
        return _ok(f"Deleted food entry {_entry(row)}\n\n{await status(day)}")

    @tool(
        "nutrition_status",
        "Show one day's nutrition: training or rest day, every food entry (#id, kcal, grams), totals, the "
        "day's budget, what REMAINS of each budget (kcal; protein/fat/carbs as a low-to-high gram range; "
        "added sugar in grams), sodium summary and flags (over_kcal, under_min_kcal, over_fat, over_carbs, "
        "over_sugar, protein_low, sodium_high). Use it for '今天还剩多少' and before suggesting a meal.",
        {
            "type": "object",
            "properties": {"day": {"type": "string", "description": _DAY_DOC}},
        },
        annotations=_READ_ONLY,
    )
    @_safe
    async def nutrition_status(args: dict[str, Any]) -> dict[str, Any]:
        return _ok(await status(day_arg(args)))

    @tool(
        "weight_log_add",
        "Record a body-weight measurement in kilograms (kg), optionally waist circumference in "
        "centimetres (cm). Convert 斤 to kg (1 斤 = 0.5 kg) first. Returns a confirmation with the average "
        "weight of the 7 days ending that day and of the 7 days before.",
        {
            "type": "object",
            "properties": {
                "weight_kg": {"type": "number", "description": "Body weight in kg, e.g. 72.4."},
                "waist_cm": {"type": "number", "description": "Waist circumference in cm (optional)."},
                "day": {"type": "string", "description": _DAY_DOC},
                "note": {"type": "string", "description": "Optional note, e.g. 'after breakfast'."},
            },
            "required": ["weight_kg"],
        },
    )
    @_safe
    async def weight_log_add(args: dict[str, Any]) -> dict[str, Any]:
        weight = _num(args, "weight_kg")
        waist = _num(args, "waist_cm", required=False)
        day = day_arg(args)
        note = _text(args, "note")

        def write() -> tuple[float | None, float | None]:
            store.add_weight(day, now(), weight, waist_cm=waist, note=note)
            cur = weight_averages(store.weights_between(day - timedelta(days=6), day))
            prev = weight_averages(store.weights_between(day - timedelta(days=13), day - timedelta(days=7)))
            return cur, prev

        cur, prev = await asyncio.to_thread(write)
        waist_text = f", waist {waist:.1f} cm" if waist is not None else ""
        prev_text = f"{prev:.1f} kg" if prev is not None else "no entries"
        change = f" (change {cur - prev:+.1f} kg)" if prev is not None and cur is not None else ""
        return _ok(
            f"Logged weight {weight:.1f} kg{waist_text} for {day.isoformat()}. "
            f"7-day average {cur:.1f} kg; previous 7 days {prev_text}{change}."
        )

    @tool(
        "workout_log_add",
        "Record a workout for a day. kind: 'A' or 'B' (the plan's strength sessions), 'cardio', or 'other'. "
        "A logged workout makes that day a training day (higher kcal budget). Returns a confirmation and "
        "the day's nutrition status with the updated budget.",
        {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": list(WORKOUT_KINDS), "description": "Workout type."},
                "description": {"type": "string", "description": "What was done, e.g. 'squat 5x5, 40 min'."},
                "day": {"type": "string", "description": _DAY_DOC},
                "note": {"type": "string", "description": "Optional note."},
            },
            "required": ["kind"],
        },
    )
    @_safe
    async def workout_log_add(args: dict[str, Any]) -> dict[str, Any]:
        kind = args.get("kind")
        if kind not in WORKOUT_KINDS:
            raise _ToolError(f"Error: 'kind' must be one of {', '.join(WORKOUT_KINDS)}, got {kind!r}")
        day = day_arg(args)
        wid = await asyncio.to_thread(
            store.add_workout, day, now(), kind, description=_text(args, "description"), note=_text(args, "note")
        )
        return _ok(f"Logged workout #{wid} ({kind}) for {day.isoformat()}.\n\n{await status(day)}")

    @tool(
        "fitness_summary",
        "Summarize a period (default: the last 7 days including today): per-day kcal/protein/fat/carbs vs "
        "budget with flags, logged-day averages, days over budget, average weight vs the previous period of "
        "the same length, and workout count. Use for '这周怎么样' / '上周吃得怎么样' (pass Monday-Sunday "
        f"dates for a calendar week). At most {_SUMMARY_MAX_DAYS} days.",
        {
            "type": "object",
            "properties": {
                "start": {"type": "string", "description": "First day, YYYY-MM-DD (inclusive)."},
                "end": {"type": "string", "description": "Last day, YYYY-MM-DD (inclusive; default today)."},
            },
        },
        annotations=_READ_ONLY,
    )
    @_safe
    async def fitness_summary(args: dict[str, Any]) -> dict[str, Any]:
        end = _date_arg(args, "end") or today()
        start = _date_arg(args, "start") or end - timedelta(days=_SUMMARY_DEFAULT_DAYS - 1)
        if start > end:
            raise _ToolError("Error: 'start' must be on or before 'end'")
        length = (end - start).days + 1
        if length > _SUMMARY_MAX_DAYS:
            raise _ToolError(f"Error: the period is {length} days; at most {_SUMMARY_MAX_DAYS} are allowed")

        def build() -> str:
            prev_end = start - timedelta(days=1)
            prev_start = prev_end - timedelta(days=length - 1)
            return period_summary(
                start,
                end,
                store.foods_between(start, end),
                store.weights_between(start, end),
                store.weights_between(prev_start, prev_end),
                store.workouts_between(start, end),
                fit,
            )

        return _ok(await asyncio.to_thread(build))

    return [
        fitness_plan,
        food_log_add,
        food_log_update,
        food_log_delete,
        nutrition_status,
        weight_log_add,
        workout_log_add,
        fitness_summary,
    ]
