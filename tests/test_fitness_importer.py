from datetime import date
import pytest
from argus.store import Store
from argus.fitness.importer import import_markdown_logs

FOOD_MD = """# 饮食记录

| 日期 | 训练 | 早餐 | 晚餐 | 全天热量 | 蛋白质 | 备注 |
|---|---|---|---|---|---|---|
| 2026-01-05 | A | 燕麦 50 克、鸡蛋 2 个 | 牛肉 200 克、米饭 1 碗 | 约 1500 | 约 120 | 正常 |
| 2026-01-06 | 无 | 酸奶 1 杯 | | 早餐约 300 | 早餐约 20 | 只记了早餐 |
| 2026-01-07 | 无 | 面包 | 面条 | | | |
| 2026-01-08 | 未说明 | 豆浆 | 鱼 150 克 | 1800 千卡 | 95.5 克 | |
| 说明 | 不是日期 | | | 约 999 | | |
"""

WEIGHT_MD = """# 体重记录

说明文字。

| 日期 | 体重（公斤） | 腰围（厘米） | 备注 |
|---|---|---|---|
| 2026-01-05 | 80.0 | 90 | 起点 |
| 2026-01-07 | 79.4 | | |
| 2026-01-08 | | | 忘了称 |

## 周平均

| 周 | 平均体重 | 与上周比 |
|---|---|---|
| 2026-01-05 到 01-11 | 79.7 | 起点 |
"""

@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "argus.db")
    yield s
    s.close()

@pytest.fixture
def fdir(tmp_path):
    d = tmp_path / "fitness"
    d.mkdir()
    (d / "food-log.md").write_text(FOOD_MD, encoding="utf-8")
    (d / "weight-log.md").write_text(WEIGHT_MD, encoding="utf-8")
    return d

def _foods(store):
    return {r["day"]: r for r in store.foods_between(date(2026, 1, 1), date(2026, 12, 31))}

def test_imports_food_rows(store, fdir):
    result = import_markdown_logs(store, fdir)
    assert result["food_imported"] == 4 and result["food_skipped"] == 0
    foods = _foods(store)
    assert sorted(foods) == ["2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08"]
    r = foods["2026-01-05"]
    assert r["meal"] == "other"
    assert r["description"] == "燕麦 50 克、鸡蛋 2 个 / 牛肉 200 克、米饭 1 碗"
    assert (r["kcal"], r["protein_g"], r["fat_g"], r["carbs_g"]) == (1500, 120, 0, 0)
    assert r["added_sugar_g"] is None and r["sodium"] is None
    assert r["note"].startswith("[imported]") and "正常" in r["note"]
    assert "partial" not in r["note"] and "unknown" not in r["note"]

def test_partial_day_keeps_number_and_notes_it(store, fdir):
    import_markdown_logs(store, fdir)
    r = _foods(store)["2026-01-06"]
    assert r["kcal"] == 300 and r["protein_g"] == 20
    assert r["description"] == "酸奶 1 杯"
    assert "partial day" in r["note"] and "只记了早餐" in r["note"]

def test_missing_totals_are_zero_and_noted(store, fdir):
    import_markdown_logs(store, fdir)
    r = _foods(store)["2026-01-07"]
    assert r["kcal"] == 0 and r["protein_g"] == 0
    assert "totals unknown" in r["note"]
    assert r["description"] == "面包 / 面条"

def test_plain_numbers_with_units(store, fdir):
    import_markdown_logs(store, fdir)
    r = _foods(store)["2026-01-08"]
    assert r["kcal"] == 1800 and r["protein_g"] == 95.5 and "partial" not in r["note"]

def test_imports_weight_rows(store, fdir):
    result = import_markdown_logs(store, fdir)
    assert result["weight_imported"] == 2
    rows = store.weights_between(date(2026, 1, 1), date(2026, 12, 31))
    assert [(r["day"], r["weight_kg"], r["waist_cm"]) for r in rows] == [
        ("2026-01-05", 80.0, 90.0), ("2026-01-07", 79.4, None)]
    assert rows[0]["note"].startswith("[imported]") and "起点" in rows[0]["note"]

def test_import_is_idempotent(store, fdir):
    import_markdown_logs(store, fdir)
    again = import_markdown_logs(store, fdir)
    assert again["food_imported"] == 0 and again["food_skipped"] == 4
    assert again["weight_imported"] == 0 and again["weight_skipped"] == 2
    assert len(_foods(store)) == 4
    assert len(store.weights_between(date(2026, 1, 1), date(2026, 12, 31))) == 2

def test_deleted_imported_entry_is_not_resurrected(store, fdir):
    import_markdown_logs(store, fdir)
    store.delete_food(_foods(store)["2026-01-05"]["id"])
    import_markdown_logs(store, fdir)
    assert "2026-01-05" not in _foods(store)

def test_new_rows_imported_later(store, fdir):
    import_markdown_logs(store, fdir)
    (fdir / "food-log.md").write_text(FOOD_MD + "| 2026-01-09 | B | 粥 | 饺子 | 约 1400 | 约 80 | |\n", encoding="utf-8")
    result = import_markdown_logs(store, fdir)
    assert result["food_imported"] == 1 and _foods(store)["2026-01-09"]["kcal"] == 1400

def test_manual_entries_do_not_block_import(store, fdir):
    from datetime import datetime
    store.add_food(date(2026, 1, 5), datetime(2026, 1, 5, 9), "manual", 100, 1, 1, 1)
    import_markdown_logs(store, fdir)
    assert len(store.foods_for_day(date(2026, 1, 5))) == 2

def test_missing_files_are_fine(store, tmp_path):
    assert import_markdown_logs(store, tmp_path / "nope") == {
        "food_imported": 0, "food_skipped": 0, "weight_imported": 0, "weight_skipped": 0, "invalid": 0}

def test_escaped_pipe_in_cell(store, tmp_path):
    (tmp_path / "food-log.md").write_text(
        "| 日期 | 训练 | 早餐 | 晚餐 | 全天热量 | 蛋白质 | 备注 |\n|---|---|---|---|---|---|---|\n"
        "| 2026-02-01 | 无 | a \\| b | c | 约 100 | 约 10 | |\n", encoding="utf-8")
    import_markdown_logs(store, tmp_path)
    assert store.foods_for_day(date(2026, 2, 1))[0]["description"] == "a | b / c"

def test_bad_rows_are_skipped_not_raised(store, tmp_path):
    (tmp_path / "food-log.md").write_text(
        "| 日期 | 训练 | 早餐 | 晚餐 | 全天热量 | 蛋白质 | 备注 |\n|---|---|---|---|---|---|---|\n"
        "| 2026-13-01 | 无 | bad date | | 约 100 | | |\n"
        "| 2026-02-02 | 无 | absurd | | 约 99999 | | |\n"
        "| 2026-02-03 | 无 | ok | | 约 100 | 约 5 | |\n", encoding="utf-8")
    result = import_markdown_logs(store, tmp_path)
    assert result["food_imported"] == 1 and result["invalid"] == 1
    assert [r["description"] for r in store.foods_between(date(2026, 1, 1), date(2026, 12, 31))] == ["ok"]
