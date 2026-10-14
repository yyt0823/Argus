# Argus Fitness + Nutrition Log — Design and Plan

- Date: 2026-10-14
- Status: Requirements given by the user directly ("record what I eat — very important; tell me how many calories, protein, fat and carbs I have left, and give me a suggestion"); user asked for full implementation without further check-ins.
- Scope: Roadmap sub-project 4 (fitness), food + weight + workout logging. Builds on Core and the mail sub-project (internal tasks).

## 1. Goals

1. The user texts what they ate ("中午吃了虾 200 克、红薯 250 克、蔬菜"). Argus estimates calories, protein, fat, carbs, added sugar and sodium level, **records it durably**, and replies with what was logged, today's totals, **what remains** of each daily budget, and one concrete suggestion for the next meal.
2. The user texts weight / waist / workouts; Argus records them.
3. Questions like "这周怎么样" / "上周吃得怎么样" are answered from the recorded data (weekly average weight vs previous week, daily calories/protein, days over budget).
4. Corrections work: "刚才那个记错了，虾是 150 克" updates or deletes the entry.

### Non-goals

- Barcode / photo food recognition. Exercise-calorie burn tracking. Automatic plan changes (the model may suggest; the user decides).

## 2. Source of truth for the plan

The user's existing plan lives at `data/fitness/CLAUDE.md` (git-ignored, personal). It contains body data, budgets, food reference table, preferences (dislikes fishy taste and chicken breast), training plan, menu templates and answering rules. Argus reads it through a tool, so the user can edit the file and Argus follows immediately.

Machine-readable budgets go in `argus.toml` under `[fitness]` (git-ignored; code defaults are generic placeholders):

```toml
[fitness]
enabled = true
plan_path = "data/fitness/CLAUDE.md"
training_days = ["mon", "wed", "fri"]
training_kcal = 1800
rest_kcal = 1600
min_kcal = 1500
protein_g = [160, 170]
carbs_g = [100, 150]
fat_g = [40, 50]
added_sugar_g_max = 25
sodium_mg_max = 2000
```

A day is a **training day** if a workout is logged for it, or (if none logged) its weekday is in `training_days`. Days follow Argus's 04:00 day boundary (`day_key`), so a 01:00 snack counts for the previous day.

## 3. Data (SQLite)

| Table | Columns |
|---|---|
| `food_log` | id, day (YYYY-MM-DD), logged_at, meal (`breakfast`/`lunch`/`dinner`/`snack`/`pre_workout`/`other`), description, kcal, protein_g, fat_g, carbs_g, added_sugar_g, sodium (`low`/`medium`/`high`), note, deleted (0/1) |
| `weight_log` | id, day, logged_at, weight_kg, waist_cm (nullable), note |
| `workout_log` | id, day, logged_at, kind (`A`/`B`/`cardio`/`other`), description, note |

Deletes are soft (`deleted=1`) so a mistaken correction can be undone and nothing is silently lost. Existing entries from `data/fitness/food-log.md` and `weight-log.md` are imported once (idempotent import keyed by a `source` marker in `note`).

## 4. Tools (added to the chat session's tool server)

| Tool | Input | Output |
|---|---|---|
| `fitness_plan` | — | Full text of the plan file |
| `food_log_add` | `description`, `kcal`, `protein_g`, `fat_g`, `carbs_g` (numbers ≥ 0), optional `added_sugar_g`, `sodium`, `meal`, `day` (YYYY-MM-DD, default today), `note` | Confirmation with entry id + `nutrition_status` for that day |
| `food_log_update` | `id` + any of the numeric/text fields | Updated entry + day status |
| `food_log_delete` | `id` | Confirmation + day status |
| `nutrition_status` | optional `day` | Day type (training/rest), entries list, totals, budget, **remaining** per nutrient (kcal, protein range, carbs range, fat range, added sugar), sodium summary, flags (over fat, under protein, below min kcal) |
| `weight_log_add` | `weight_kg`, optional `waist_cm`, `day`, `note` | Confirmation + 7-day average and previous-7-day average |
| `workout_log_add` | `kind`, optional `description`, `day`, `note` | Confirmation; marks the day as a training day |
| `fitness_summary` | `start`, `end` (YYYY-MM-DD; default last 7 days) | Per-day kcal/protein/fat/carbs vs budget, weight average vs previous period, workouts count |

All numbers returned are computed in Python (never by the model) so "remaining" values are exact sums of what was recorded. Validation: negative or absurd values (kcal > 5000 in one entry, protein > 300 g) are rejected with a clear error so the model re-estimates.

## 5. Persona rules (CLAUDE.md additions)

- When the user mentions eating/drinking anything, ALWAYS call `food_log_add` (one call per meal; estimate using the plan's reference table first, otherwise typical values), then reply: what was logged (kcal + protein + fat + carbs, added sugar, sodium level), today's remaining from `nutrition_status`, and one suggestion that respects the plan (preferences, low GI, calcium reminder when relevant). Never claim something was recorded unless the tool succeeded.
- Weight → `weight_log_add`; workouts → `workout_log_add`.
- "这周怎么样 / 上周" → `fitness_summary`.
- Corrections → `food_log_update` / `food_log_delete`.
- Read `fitness_plan` before giving diet/training advice in a session.

## 6. Daily touchpoints (via internal tasks)

- The 08:00 briefing also includes yesterday's nutrition summary (totals vs budget, one line) and a reminder to weigh in if no weight was logged in the last 2 days. (The watcher's briefing text asks the model to call `nutrition_status` for yesterday.)

## 7. Testing

- Unit: budget math (training vs rest day, ranges, remaining, flags), day boundary at 04:00, soft delete/update, validation, weekly averages, import of the two Markdown logs (idempotent), each tool handler (never raises, errors → `is_error`).
- Live: one session test — "早餐吃了燕麦 50 克、牛奶 250 毫升、蛋白粉 1 勺、茶叶蛋 1 个" → an entry exists with kcal within ±15% of 520 and protein within ±15% of 52, and the reply mentions remaining calories.
- Conversation regression (live, small): a fixed set of ~5 user messages covering food log, correction, weight, "今天还剩多少", and an unrelated question; assert on tool calls / DB state, not on wording.

## 8. Implementation tasks

| Task | Files |
|---|---|
| F1 Store tables + config `[fitness]` + Markdown import | `argus/store.py`, `argus/config.py`, `argus/fitness/importer.py` |
| F2 Nutrition math (pure functions: day type, totals, remaining, flags, weekly summary) | `argus/fitness/nutrition.py` |
| F3 Tools + server wiring | `argus/tools/fitness.py`, `argus/tools/server.py`, `argus/sdk_session.py` |
| F4 Persona, briefing text, main wiring (run import at startup), docs, live tests | `CLAUDE.md`, `argus/mail/watcher.py` (briefing text), `argus/main.py`, `README.md`, `argus.example.toml`, `tests/live/` |
