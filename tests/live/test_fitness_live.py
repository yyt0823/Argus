"""Live fitness test against the real Claude CLI (user's subscription). Run with ARGUS_LIVE=1.

Uses the repo's persona (CLAUDE.md) and a SYNTHETIC plan with generic numbers, never the user's.
"""

import os
import shutil
from datetime import date, datetime
from pathlib import Path

import pytest

from argus.config import Config, FitnessConfig
from argus.scheduler import day_key
from argus.sdk_session import make_session_factory
from argus.store import Store
from argus.tools.memory import MemoryStore

pytestmark = pytest.mark.skipif(os.environ.get("ARGUS_LIVE") != "1", reason="set ARGUS_LIVE=1")

REPO = Path(__file__).resolve().parents[2]

PLAN = """# Test fitness plan (synthetic)

Goal: slow fat loss while keeping muscle. Budgets come from nutrition_status.

## Food reference table (per portion as listed)

| Food | Portion | kcal | Protein g | Fat g | Carbs g |
|---|---|---|---|---|---|
| Oats (燕麦) | 50 g dry | 190 | 7 | 3.5 | 33 |
| Milk (牛奶), whole | 250 ml | 155 | 8.5 | 8.5 | 12 |
| Whey protein powder (蛋白粉) | 1 scoop, 35 g | 130 | 28 | 1.5 | 3 |
| Tea egg (茶叶蛋) | 1 egg, 50 g | 70 | 6.5 | 5 | 0.5 |

## Preferences

- Likes beef and tofu. Prefers low-GI carbs.
"""


def _mentions_remaining(reply: str) -> bool:
    return "剩" in reply or "remaining" in reply.lower()


async def test_live_food_log_and_correction(tmp_path: Path):
    shutil.copy(REPO / "CLAUDE.md", tmp_path / "CLAUDE.md")
    plan = tmp_path / "data" / "fitness" / "CLAUDE.md"
    plan.parent.mkdir(parents=True)
    plan.write_text(PLAN, encoding="utf-8")
    fitness = FitnessConfig(
        enabled=True, training_days=("mon", "wed", "fri"), training_kcal=2000, rest_kcal=1800,
        min_kcal=1200, protein_g=(100, 150), carbs_g=(150, 250), fat_g=(50, 70),
    )
    cfg = Config(root=tmp_path, self_handles=("+15550000000",), fitness=fitness)
    store, memory = Store(cfg.db_path), MemoryStore(cfg.memory_dir)
    today: date = day_key(datetime.now(), cfg.rollover_hour)
    stamp = datetime.now().strftime("[%Y-%m-%d %H:%M]")

    s = await make_session_factory(cfg, store, memory)(None)
    try:
        r1 = await s.ask(f"{stamp} 早餐吃了燕麦 50 克、牛奶 250 毫升、蛋白粉 1 勺、茶叶蛋 1 个")
        print("r1:", r1)
        rows = store.foods_for_day(today)
        print("rows after r1:", rows)
        assert len(rows) == 1, rows
        assert 520 * 0.8 <= rows[0]["kcal"] <= 520 * 1.2, rows[0]
        assert 52 * 0.8 <= rows[0]["protein_g"] <= 52 * 1.2, rows[0]
        assert _mentions_remaining(r1)
        first = rows[0]

        r2 = await s.ask(f"{stamp} 刚才的牛奶其实是 200 毫升")
        print("r2:", r2)
        rows = store.foods_for_day(today)
        print("rows after r2:", rows)
        assert len(rows) == 1, rows  # updated in place, or deleted + re-added: one live row either way
        assert rows[0]["kcal"] <= first["kcal"], (first, rows[0])
    finally:
        await s.close()
        store.close()
