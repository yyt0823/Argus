from datetime import date, datetime

import pytest

from argus.config import Config, FitnessConfig
from argus.store import Store
from argus.tools.fitness import build_fitness_tools

# 2026-10-14 is a Wednesday (training day by weekday); 2026-10-15 is a Thursday (rest).
NOW = datetime(2026, 10, 14, 12, 0, 0)

FIT = FitnessConfig(
    training_days=("mon", "wed", "fri"),
    training_kcal=1800,
    rest_kcal=1600,
    min_kcal=1500,
    protein_g=(160, 170),
    carbs_g=(100, 150),
    fat_g=(40, 50),
    added_sugar_g_max=25,
)


def text(result):
    return result["content"][0]["text"]


class Clock:
    def __init__(self, at):
        self.at = at

    def __call__(self):
        return self.at


@pytest.fixture
def env(tmp_path):
    cfg = Config(root=tmp_path, self_handles=("+15550000000",), fitness=FIT)
    store = Store(tmp_path / "argus.db")
    clock = Clock(NOW)
    tools = {t.name: t.handler for t in build_fitness_tools(store, cfg, now=clock)}
    yield cfg, store, clock, tools
    store.close()


BREAKFAST = {"description": "oats 50 g, milk 250 ml", "kcal": 520, "protein_g": 52, "fat_g": 15, "carbs_g": 60}


def test_tool_names(env):
    _, _, _, tools = env
    assert set(tools) == {
        "fitness_plan", "food_log_add", "food_log_update", "food_log_delete",
        "nutrition_status", "weight_log_add", "workout_log_add", "fitness_summary",
    }


def test_descriptions_mention_units_and_whole_entry(tmp_path):
    cfg = Config(root=tmp_path, self_handles=("+1",), fitness=FIT)
    store = Store(tmp_path / "a.db")
    tools = {t.name: t for t in build_fitness_tools(store, cfg)}
    desc = tools["food_log_add"].description.lower()
    assert "kcal" in desc and "grams" in desc and "whole" in desc
    props = tools["food_log_add"].input_schema["properties"]
    assert {"description", "kcal", "protein_g", "fat_g", "carbs_g", "added_sugar_g", "sodium", "meal", "day",
            "note"} <= set(props)
    store.close()


# --- fitness_plan ---

async def test_plan_reads_file(env):
    cfg, _, _, tools = env
    cfg.fitness_plan_path.parent.mkdir(parents=True)
    cfg.fitness_plan_path.write_text("# Plan\nEat protein.", encoding="utf-8")
    r = await tools["fitness_plan"]({})
    assert not r.get("is_error") and "Eat protein." in text(r)


async def test_plan_missing_is_error(env):
    _, _, _, tools = env
    r = await tools["fitness_plan"]({})
    assert r["is_error"] and "not found" in text(r) and "plan_path" in text(r)


# --- food_log_add + remaining math ---

async def test_add_returns_confirmation_and_remaining(env):
    _, store, _, tools = env
    r = await tools["food_log_add"]({**BREAKFAST, "meal": "breakfast", "added_sugar_g": 5, "sodium": "low"})
    assert not r.get("is_error")
    t = text(r)
    rows = store.foods_for_day(date(2026, 10, 14))
    assert len(rows) == 1 and rows[0]["meal"] == "breakfast" and rows[0]["logged_at"] == "2026-10-14T12:00:00"
    assert f"#{rows[0]['id']}" in t
    assert "training day" in t

    r = await tools["food_log_add"]({"description": "shrimp", "kcal": 400, "protein_g": 40, "fat_g": 10,
                                     "carbs_g": 30})
    t = text(r)
    # 1800-920=880; protein 160-92=68 .. 170-92=78; fat 40-25=15 .. 50-25=25; carbs 100-90=10 .. 150-90=60
    assert "Totals: 920 kcal, protein 92 g, fat 25 g, carbs 90 g" in t
    assert "Remaining: 880 kcal, protein 68 to 78 g, fat 15 to 25 g, carbs 10 to 60 g, added sugar 20 g" in t


async def test_add_rest_day_budget(env):
    _, _, clock, tools = env
    clock.at = datetime(2026, 10, 15, 9, 0)
    t = text(await tools["food_log_add"](BREAKFAST))
    assert "rest day" in t and "Remaining: 1080 kcal" in t


async def test_add_day_boundary_at_four(env):
    _, store, clock, tools = env
    clock.at = datetime(2026, 10, 15, 1, 30)  # before 04:00 -> still the 14th
    await tools["food_log_add"](BREAKFAST)
    assert len(store.foods_for_day(date(2026, 10, 14))) == 1
    clock.at = datetime(2026, 10, 15, 4, 0)  # at 04:00 -> the 15th
    await tools["food_log_add"](BREAKFAST)
    assert len(store.foods_for_day(date(2026, 10, 15))) == 1


async def test_add_explicit_day(env):
    _, store, _, tools = env
    r = await tools["food_log_add"]({**BREAKFAST, "day": "2026-10-13"})
    assert not r.get("is_error") and "2026-10-13" in text(r)
    assert len(store.foods_for_day(date(2026, 10, 13))) == 1


@pytest.mark.parametrize("bad", [
    {"kcal": -1}, {"kcal": 6000}, {"protein_g": 400}, {"kcal": "lots"}, {"description": ""},
    {"meal": "brunch"}, {"sodium": "extreme"}, {"day": "14/10/2026"}, {"day": "2026-10-20"},
])
async def test_add_validation_errors(env, bad):
    _, store, _, tools = env
    r = await tools["food_log_add"]({**BREAKFAST, **bad})
    assert r["is_error"], r
    assert store.foods_between(date(2026, 1, 1), date(2026, 12, 31)) == []


async def test_add_missing_required(env):
    _, _, _, tools = env
    args = dict(BREAKFAST)
    del args["fat_g"]
    r = await tools["food_log_add"](args)
    assert r["is_error"] and "fat_g" in text(r)


async def test_add_accepts_numeric_strings(env):
    _, store, _, tools = env
    r = await tools["food_log_add"]({**BREAKFAST, "kcal": "520"})
    assert not r.get("is_error")
    assert store.foods_for_day(date(2026, 10, 14))[0]["kcal"] == 520


# --- update / delete ---

async def test_update_changes_entry_and_status(env):
    _, store, _, tools = env
    await tools["food_log_add"](BREAKFAST)
    fid = store.foods_for_day(date(2026, 10, 14))[0]["id"]
    r = await tools["food_log_update"]({"id": fid, "kcal": 480, "protein_g": 50, "description": None})
    assert not r.get("is_error"), r
    rows = store.foods_for_day(date(2026, 10, 14))
    assert len(rows) == 1 and rows[0]["kcal"] == 480 and rows[0]["description"] == BREAKFAST["description"]
    assert f"Updated food entry #{fid}" in text(r) and "Remaining: 1320 kcal" in text(r)


async def test_update_errors(env):
    _, store, _, tools = env
    await tools["food_log_add"](BREAKFAST)
    fid = store.foods_for_day(date(2026, 10, 14))[0]["id"]
    assert (await tools["food_log_update"]({"id": 999, "kcal": 1}))["is_error"]
    assert "999" in text(await tools["food_log_update"]({"id": 999, "kcal": 1}))
    assert (await tools["food_log_update"]({"id": fid}))["is_error"]
    assert (await tools["food_log_update"]({"id": fid, "kcal": -5}))["is_error"]
    assert (await tools["food_log_update"]({"kcal": 5}))["is_error"]
    assert (await tools["food_log_update"]({"id": "abc", "kcal": 5}))["is_error"]


async def test_update_moves_day(env):
    _, store, _, tools = env
    await tools["food_log_add"](BREAKFAST)
    fid = store.foods_for_day(date(2026, 10, 14))[0]["id"]
    r = await tools["food_log_update"]({"id": fid, "day": "2026-10-13"})
    assert not r.get("is_error")
    assert store.foods_for_day(date(2026, 10, 14)) == []
    assert "2026-10-13" in text(r)


async def test_delete_soft_and_status(env):
    _, store, _, tools = env
    await tools["food_log_add"](BREAKFAST)
    fid = store.foods_for_day(date(2026, 10, 14))[0]["id"]
    r = await tools["food_log_delete"]({"id": fid})
    assert not r.get("is_error") and f"Deleted food entry #{fid}" in text(r)
    assert "Remaining: 1800 kcal" in text(r)
    assert store.foods_for_day(date(2026, 10, 14)) == []
    again = await tools["food_log_delete"]({"id": fid})
    assert again["is_error"] and str(fid) in text(again)


# --- nutrition_status ---

async def test_status_today_and_other_day(env):
    _, _, _, tools = env
    await tools["food_log_add"](BREAKFAST)
    t = text(await tools["nutrition_status"]({}))
    assert "2026-10-14" in t and "Remaining: 1280 kcal" in t
    t = text(await tools["nutrition_status"]({"day": "2026-10-13"}))
    assert "2026-10-13" in t and "Entries: none" in t
    assert (await tools["nutrition_status"]({"day": "nope"}))["is_error"]


# --- weight ---

async def test_weight_add_and_averages(env):
    _, store, clock, tools = env
    await tools["weight_log_add"]({"weight_kg": 80, "day": "2026-10-05"})  # previous 7 days
    await tools["weight_log_add"]({"weight_kg": 79, "day": "2026-10-10"})
    r = await tools["weight_log_add"]({"weight_kg": 78, "waist_cm": 85})
    assert not r.get("is_error"), r
    t = text(r)
    assert "78.0 kg" in t and "waist 85.0 cm" in t
    assert "7-day average 78.5 kg" in t and "previous 7 days 80.0 kg" in t
    assert len(store.weights_between(date(2026, 10, 14), date(2026, 10, 14))) == 1


async def test_weight_validation(env):
    _, _, _, tools = env
    assert (await tools["weight_log_add"]({"weight_kg": 0}))["is_error"]
    assert (await tools["weight_log_add"]({"weight_kg": 700}))["is_error"]
    assert (await tools["weight_log_add"]({}))["is_error"]
    assert (await tools["weight_log_add"]({"weight_kg": 70, "waist_cm": -1}))["is_error"]


# --- workout ---

async def test_workout_makes_rest_day_training(env):
    _, store, clock, tools = env
    clock.at = datetime(2026, 10, 15, 19, 0)  # Thursday: rest by weekday
    r = await tools["workout_log_add"]({"kind": "A", "description": "squat day"})
    assert not r.get("is_error"), r
    assert "training day" in text(r) and "Budget: 1800 kcal" in text(r)
    assert len(store.workouts_between(date(2026, 10, 15), date(2026, 10, 15))) == 1
    assert (await tools["workout_log_add"]({"kind": "yoga"}))["is_error"]
    assert (await tools["workout_log_add"]({}))["is_error"]


# --- summary ---

async def test_summary_default_last_seven_days(env):
    _, _, _, tools = env
    await tools["food_log_add"](BREAKFAST)
    await tools["weight_log_add"]({"weight_kg": 78})
    t = text(await tools["fitness_summary"]({}))
    assert "Summary 2026-10-08 to 2026-10-14 (7 days)" in t
    assert "2026-10-14 Wed training: 520/1800 kcal" in t
    assert "Weight: avg 78.0 kg" in t


async def test_summary_explicit_range_and_errors(env):
    _, _, _, tools = env
    t = text(await tools["fitness_summary"]({"start": "2026-10-01", "end": "2026-10-03"}))
    assert "Summary 2026-10-01 to 2026-10-03 (3 days)" in t
    assert (await tools["fitness_summary"]({"start": "2026-10-05", "end": "2026-10-01"}))["is_error"]
    assert (await tools["fitness_summary"]({"start": "2025-01-01", "end": "2026-10-01"}))["is_error"]
    assert (await tools["fitness_summary"]({"start": "x"}))["is_error"]


async def test_handlers_never_raise(env):
    _, store, _, tools = env
    store.close()
    for name, h in tools.items():
        if name == "fitness_plan":
            continue
        r = await h({**BREAKFAST, "id": 1, "weight_kg": 70, "kind": "A"})
        assert r["is_error"], name


# --- system turns and send-time day (H6, M7) ---
from argus import turn_context


@pytest.mark.parametrize("name,args", [
    ("food_log_add", BREAKFAST),
    ("food_log_update", {"id": 1, "kcal": 1}),
    ("food_log_delete", {"id": 1}),
    ("weight_log_add", {"weight_kg": 80}),
    ("workout_log_add", {"kind": "A"}),
])
async def test_write_tools_refused_in_system_turn(env, name, args):
    _, store, _, tools = env
    await tools["food_log_add"](BREAKFAST)
    with turn_context.turn(system=True, at=NOW):
        r = await tools[name](args)
        assert not (await tools["nutrition_status"]({})).get("is_error")
    assert r["is_error"] is True and "direct request from the user" in text(r)
    assert len(store.foods_for_day(date(2026, 10, 14))) == 1
    assert store.foods_for_day(date(2026, 10, 14))[0]["kcal"] == 520
    assert store.weights_between(date(2026, 10, 1), date(2026, 10, 15)) == []
    assert store.workouts_between(date(2026, 10, 1), date(2026, 10, 15)) == []


async def test_default_day_uses_message_send_time(env):
    _, store, clock, tools = env
    clock.at = datetime(2026, 10, 15, 4, 5)  # handled after rollover...
    with turn_context.turn(system=False, at=datetime(2026, 10, 15, 3, 55)):  # ...but sent before it
        r = await tools["food_log_add"](BREAKFAST)
        await tools["weight_log_add"]({"weight_kg": 80})
        await tools["workout_log_add"]({"kind": "A"})
    assert not r.get("is_error")
    assert len(store.foods_for_day(date(2026, 10, 14))) == 1
    assert [w["day"] for w in store.weights_between(date(2026, 10, 14), date(2026, 10, 15))] == ["2026-10-14"]
    assert [w["day"] for w in store.workouts_between(date(2026, 10, 14), date(2026, 10, 15))] == ["2026-10-14"]
    # outside a turn: now
    await tools["food_log_add"](BREAKFAST)
    assert len(store.foods_for_day(date(2026, 10, 15))) == 1
