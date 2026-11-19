# tests/test_kitchen_batching.py
"""
Tests for the kitchen-batching rules in services/daily_menu_service.py
(2026-11-19):
  - build_batch_counts sums other clients' orders across meal types and
    counts the day's template picks as full batches
  - a swap prefers a recipe already being cooked that day on a near-tie,
    but never over a recipe this client hasn't had yet
  - a stale template is kept once other clients have ordered on its date,
    and regenerated (overwritten, fresh created_at) when nobody has
  - cancelled days don't count as other clients' orders

Run from the mealplanner-flask/ directory:

    python -m unittest tests.test_kitchen_batching -v
"""

import os
import random
import sys
import unittest
from datetime import date, datetime
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("SUPABASE_URL", "https://example.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "test-key")

import services.daily_menu_service as dms  # noqa: E402

MACROS = {"kcal": 500.0, "protein_g": 35.0, "carbs_g": 50.0, "fat_g": 15.0}
TARGET = {"kcal": 2000.0, "protein_g": 140.0, "carbs_g": 200.0, "fat_g": 60.0}


def _lookup(ids, mt="lunch"):
    return {rid: {"id": rid, "name": f"R{rid}", f"could_be_{mt}": True} for rid in ids}


class BuildBatchCountsTest(unittest.TestCase):
    def test_sums_meal_types_and_floors_template(self):
        other = {"lunch": {1: 2, 2: 1}, "dinner": {1: 1}}
        tpl = {"lunch": {"recipe_id": 2}, "snack": {"recipe_id": 9}}
        counts = dms.build_batch_counts(other, tpl)
        self.assertEqual(counts[1], 3)
        self.assertEqual(counts[2], dms.BATCH_FULL)
        self.assertEqual(counts[9], dms.BATCH_FULL)

    def test_empty(self):
        self.assertEqual(dms.build_batch_counts(None, None), {})


class BatchAwareSwapTest(unittest.TestCase):
    def _swap(self, plan_counts, batch_counts):
        ids = {1, 2, 3}
        macros = {rid: dict(MACROS) for rid in ids}
        # template recipe 3 is disliked -> slot must be swapped
        with patch.object(dms, "macro_compat_score", return_value=0.0):
            out = dms.apply_client_overrides(
                template_for_date={"lunch": {"recipe_id": 3, "meal_type": "lunch"}},
                meals_map={"lunch": "lunch"},
                allowed_ids_today=ids,
                user_prefs={3: {"dislike": True}},
                recipe_lookup=_lookup(ids),
                recipe_macros=macros,
                macro_target=TARGET,
                plan_counts=plan_counts,
                previous_day_recipes=set(),
                rng=random.Random(0),
                batch_counts=batch_counts,
            )
        return out["lunch"]["recipe_id"]

    def test_prefers_recipe_already_cooked(self):
        for seed_counts in ({2: 5}, {2: dms.BATCH_FULL}):
            self.assertEqual(self._swap({}, seed_counts), 2)

    def test_variety_still_wins(self):
        # client already had recipe 2 twice; batching must not push a 3rd
        self.assertEqual(self._swap({2: 2}, {2: 10}), 1)

    def test_no_batch_info_is_unchanged_behaviour(self):
        picks = {self._swap({}, None) for _ in range(5)}
        self.assertTrue(picks <= {1, 2})


class _Resp:
    def __init__(self, data):
        self.data = data


class _FakeTable:
    def __init__(self, store, writes):
        self.store, self.writes, self._up = store, writes, None

    def select(self, *_a, **_k):
        return self

    def in_(self, *_a, **_k):
        return self

    def upsert(self, rows, **kw):
        self._up = (rows, kw)
        return self

    def execute(self):
        if self._up:
            self.writes.append(self._up)
            return _Resp(self._up[0])
        return _Resp(list(self.store))


class _FakeSupabase:
    def __init__(self, rows):
        self.rows, self.writes = rows, []

    def table(self, _name):
        return _FakeTable(self.rows, self.writes)


class TemplateRetentionTest(unittest.TestCase):
    D = date(2026, 11, 23)

    def _build(self, other_orders):
        stale = [{"date": "2026-11-23", "meal_type": "lunch", "recipe_id": 1, "created_at": "2026-10-01T00:00:00"}]
        fake = _FakeSupabase(stale)
        recipes = [{"id": rid, "name": f"R{rid}", "could_be_lunch": True} for rid in (1, 2, 3)]
        with patch.object(dms, "supabase", fake), \
             patch.object(dms, "_quality_score", side_effect=lambda rid, *a, **k: {1: 0, 2: 5, 3: 1}[rid]):
            out = dms.build_week_templates(
                meals_map={"lunch": "lunch"}, available_dates=[self.D],
                allowed_recipe_ids_by_date={self.D: {1, 2, 3}}, all_recipes=recipes,
                flex_stats={}, popularity={}, recipe_macros={}, reference_target=TARGET,
                other_orders=other_orders,
            )
        return out[self.D]["lunch"]["recipe_id"], fake.writes

    def test_stale_template_kept_when_others_ordered(self):
        rid, writes = self._build({self.D: {"dinner": {3: 1}}})
        self.assertEqual(rid, 1)
        self.assertEqual(writes, [])

    def test_stale_template_regenerated_and_overwritten_when_nobody_ordered(self):
        rid, writes = self._build({})
        self.assertEqual(rid, 2)
        self.assertEqual(len(writes), 1)
        rows, kw = writes[0]
        self.assertFalse(kw.get("ignore_duplicates", False))
        self.assertEqual(rows[0]["recipe_id"], 2)
        self.assertIn("created_at", rows[0])


class OtherOrdersSkipCancelledTest(unittest.TestCase):
    def test_cancelled_days_ignored(self):
        days = [
            {"id": 10, "date": "2026-11-23", "status": "pending", "meal_plan": {"user_id": "a"}},
            {"id": 11, "date": "2026-11-23", "status": "cancelled", "meal_plan": {"user_id": "b"}},
        ]
        recs = [
            {"id": 1, "meal_plan_day_id": 10, "recipe_id": 5, "meal_type": "lunch"},
            {"id": 2, "meal_plan_day_id": 11, "recipe_id": 6, "meal_type": "lunch"},
        ]
        calls = iter([days, recs])
        with patch.object(dms, "fetch_all", side_effect=lambda *_a, **_k: next(calls)), \
             patch.object(dms, "supabase", _FakeSupabase([])):
            out = dms.fetch_other_orders_by_date([date(2026, 11, 23)], exclude_user_id="me")
        self.assertEqual(out, {date(2026, 11, 23): {"lunch": {5: 1}}})


if __name__ == "__main__":
    unittest.main()
