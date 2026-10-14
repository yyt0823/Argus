from dataclasses import FrozenInstanceError
from datetime import date
import pytest
from argus.config import FitnessConfig
from argus.fitness.nutrition import (
    DayStatus, day_status, format_day_status, is_training_day, period_summary, weight_averages,
)

CFG = FitnessConfig(training_days=("mon", "wed", "fri"), training_kcal=1800, rest_kcal=1600, min_kcal=1500,
                    protein_g=(160, 170), carbs_g=(100, 150), fat_g=(40, 50), added_sugar_g_max=25)
MON, TUE = date(2026, 10, 12), date(2026, 10, 13)  # 2026-10-12 is a Monday

def food(day=TUE, kcal=0, p=0, f=0, c=0, sugar=None, sodium=None, desc="x", meal=None, at="12:00:00", id=1):
    return {"id": id, "day": day.isoformat(), "logged_at": f"{day.isoformat()}T{at}", "meal": meal,
            "description": desc, "kcal": kcal, "protein_g": p, "fat_g": f, "carbs_g": c,
            "added_sugar_g": sugar, "sodium": sodium, "note": None, "deleted": 0}

def workout(day, kind="A"):
    return {"id": 1, "day": day.isoformat(), "logged_at": f"{day.isoformat()}T18:00:00", "kind": kind,
            "description": None, "note": None}

def weight(day, kg):
    return {"id": 1, "day": day.isoformat(), "logged_at": f"{day.isoformat()}T07:00:00", "weight_kg": kg,
            "waist_cm": None, "note": None}

# --- training day ---

def test_training_by_weekday_fallback():
    assert MON.weekday() == 0
    assert is_training_day(MON, [], CFG) is True
    assert is_training_day(TUE, [], CFG) is False

def test_training_by_logged_workout_on_rest_weekday():
    assert is_training_day(TUE, [workout(TUE, "cardio")], CFG) is True

def test_training_days_empty_config():
    assert is_training_day(MON, [], FitnessConfig(training_days=())) is False

# --- day status ---

def test_rest_day_budget_and_remaining():
    s = day_status(TUE, [food(kcal=500, p=40, f=10, c=60, sugar=5, id=1),
                         food(kcal=300.4, p=20.5, f=5, c=20, sugar=None, id=2)], [], CFG)
    assert isinstance(s, DayStatus) and s.day == TUE and s.day_type == "rest"
    assert len(s.entries) == 2
    assert s.totals["kcal"] == pytest.approx(800.4)
    assert s.totals["protein_g"] == pytest.approx(60.5)
    assert s.totals["fat_g"] == 15 and s.totals["carbs_g"] == 80
    assert s.totals["added_sugar_g"] == 5
    assert s.budget == {"kcal": 1600, "min_kcal": 1500, "protein_g": (160, 170), "fat_g": (40, 50),
                        "carbs_g": (100, 150), "added_sugar_g_max": 25}
    assert s.remaining["kcal"] == pytest.approx(799.6)
    assert s.remaining["protein_g"] == pytest.approx((99.5, 109.5))
    assert s.remaining["fat_g"] == (25, 35)
    assert s.remaining["carbs_g"] == (20, 70)
    assert s.remaining["added_sugar_g"] == 20

def test_training_day_budget():
    s = day_status(MON, [], [], CFG)
    assert s.day_type == "training" and s.budget["kcal"] == 1800 and s.remaining["kcal"] == 1800

def test_logged_workout_makes_training_budget():
    s = day_status(TUE, [], [workout(TUE)], CFG)
    assert s.day_type == "training" and s.budget["kcal"] == 1800

def test_empty_day_totals():
    s = day_status(TUE, [], [], CFG)
    assert s.totals["kcal"] == 0 and s.totals["added_sugar_g"] == 0
    assert s.totals["sodium"] == {"low": 0, "medium": 0, "high": 0}
    assert s.entries == []

def test_all_sugar_none_counts_as_zero_and_unknown():
    s = day_status(TUE, [food(sugar=None, id=1), food(sugar=None, id=2), food(sugar=2, id=3)], [], CFG)
    assert s.totals["added_sugar_g"] == 2 and s.totals["added_sugar_unknown"] == 2

def test_sodium_counts():
    s = day_status(TUE, [food(sodium="low"), food(sodium="high"), food(sodium="high"), food(sodium=None)], [], CFG)
    assert s.totals["sodium"] == {"low": 1, "medium": 0, "high": 2}

def test_negative_remaining_when_over():
    s = day_status(TUE, [food(kcal=2000, p=200, f=60, c=200, sugar=30)], [], CFG)
    assert s.remaining["kcal"] == -400
    assert s.remaining["protein_g"] == (-40, -30)
    assert s.remaining["fat_g"] == (-20, -10)
    assert s.remaining["carbs_g"] == (-100, -50)
    assert s.remaining["added_sugar_g"] == -5

def test_flags_over_everything():
    s = day_status(TUE, [food(kcal=2000, p=200, f=60, c=200, sugar=30, sodium="high")], [], CFG)
    assert s.flags == ["over_kcal", "over_fat", "over_carbs", "over_sugar", "sodium_high"]

def test_flags_low_day():
    s = day_status(TUE, [food(kcal=1000, p=100, f=20, c=50, sugar=0, sodium="low")], [], CFG)
    assert s.flags == ["under_min_kcal", "protein_low"]

def test_flags_none_when_in_range():
    s = day_status(TUE, [food(kcal=1550, p=165, f=45, c=120, sugar=10, sodium="medium")], [], CFG)
    assert s.flags == []

def test_flag_boundaries_are_inclusive():
    s = day_status(TUE, [food(kcal=1600, p=160, f=50, c=150, sugar=25)], [], CFG)
    assert s.flags == []
    s = day_status(TUE, [food(kcal=1500, p=160, f=50, c=150)], [], CFG)
    assert "under_min_kcal" not in s.flags

def test_day_status_is_frozen():
    s = day_status(TUE, [], [], CFG)
    with pytest.raises(FrozenInstanceError):
        s.day = MON

# --- formatting ---

def test_format_day_status_stable():
    s = day_status(MON, [
        food(MON, kcal=520.4, p=52.6, f=15, c=55, sugar=3, sodium="low", desc="oats + milk", meal="breakfast",
             at="08:05:00", id=7),
        food(MON, kcal=900, p=60, f=40, c=80, sodium="high", desc="steak", meal=None, at="19:00:00", id=9),
    ], [], CFG)
    assert format_day_status(s) == (
        "2026-10-12 (Mon) training day\n"
        "Entries:\n"
        "  #7 08:05 breakfast: oats + milk - 520 kcal, P 53 g, F 15 g, C 55 g, added sugar 3 g, sodium low\n"
        "  #9 19:00 steak - 900 kcal, P 60 g, F 40 g, C 80 g, added sugar ? g, sodium high\n"
        "Totals: 1420 kcal, protein 113 g, fat 55 g, carbs 135 g, added sugar 3 g (1 entry unknown); "
        "sodium high x1, medium x0, low x1\n"
        "Budget: 1800 kcal (min 1500), protein 160-170 g, fat 40-50 g, carbs 100-150 g, added sugar <= 25 g\n"
        "Remaining: 380 kcal, protein 47 to 57 g, fat -15 to -5 g, carbs -35 to 15 g, added sugar 22 g\n"
        "Flags: under_min_kcal, over_fat, protein_low, sodium_high"
    )

def test_format_empty_day():
    text = format_day_status(day_status(TUE, [], [], CFG))
    assert text.startswith("2026-10-13 (Tue) rest day\nEntries: none\n")
    assert "Remaining: 1600 kcal" in text and text.endswith("Flags: under_min_kcal, protein_low")

def test_format_rounds_half_away_from_zero():
    text = format_day_status(day_status(TUE, [food(kcal=100.5, p=0.5, f=2.5)], [], CFG))
    assert "101 kcal, P 1 g, F 3 g" in text

# --- weights + period summary ---

def test_weight_averages():
    assert weight_averages([]) is None
    assert weight_averages([weight(MON, 70.0), weight(TUE, 71.0)]) == pytest.approx(70.5)

def test_period_summary_with_weights():
    start, end = date(2026, 10, 12), date(2026, 10, 14)
    foods = [food(start, kcal=1700, p=165, f=45, c=120, id=1), food(date(2026, 10, 13), kcal=1900, p=100, f=60, c=90, id=2)]
    text = period_summary(start, end, foods,
                          weights=[weight(start, 70.2), weight(end, 69.8)],
                          prev_weights=[weight(date(2026, 10, 5), 71.0), weight(date(2026, 10, 7), 70.6)],
                          workouts=[workout(start, "A"), workout(date(2026, 10, 14), "cardio")], cfg=CFG)
    assert text == (
        "Summary 2026-10-12 to 2026-10-14 (3 days)\n"
        "2026-10-12 Mon training: 1700/1800 kcal, P 165 (160-170), F 45 (40-50), C 120 (100-150)\n"
        "2026-10-13 Tue rest: 1900/1600 kcal, P 100 (160-170), F 60 (40-50), C 90 (100-150) "
        "[over_kcal, over_fat, protein_low]\n"
        "2026-10-14 Wed training: no entries\n"
        "Logged days: 2/3, avg 1800 kcal, avg protein 133 g; over kcal 1 day(s), protein low 1 day(s)\n"
        "Weight: avg 70.0 kg (2 entries) vs previous 70.8 kg (2 entries), change -0.8 kg\n"
        "Workouts: 2 (A x1, cardio x1)"
    )

def test_period_summary_without_weights_or_food():
    text = period_summary(TUE, TUE, [], [], [], [], CFG)
    assert text == (
        "Summary 2026-10-13 to 2026-10-13 (1 day)\n"
        "2026-10-13 Tue rest: no entries\n"
        "Logged days: 0/1\n"
        "Weight: no entries this period (previous: no entries)\n"
        "Workouts: 0"
    )

def test_period_summary_weight_without_previous():
    text = period_summary(TUE, TUE, [], [weight(TUE, 70.0)], [], [], CFG)
    assert "Weight: avg 70.0 kg (1 entry), previous period: no entries" in text

def test_period_summary_previous_only():
    text = period_summary(TUE, TUE, [], [], [weight(MON, 70.0)], [], CFG)
    assert "Weight: no entries this period (previous: avg 70.0 kg, 1 entry)" in text
