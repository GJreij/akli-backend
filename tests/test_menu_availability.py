# tests/test_menu_availability.py
"""
Tests for per-dish weekly-menu availability and the automatic chef's pick
(2026-11-19):
  - available_on: NULL/empty available_dates = every day, else listed dates only
  - _rank_and_schedule never puts a dish on a date it isn't available
  - _segment_key groups dates by weekly menu, not by exact daily pool
  - chefs_picks_by_date: most-ordered per group, min clients, ties -> lower id

Run from the mealplanner-flask/ directory:

    python -m unittest tests.test_menu_availability -v
"""

import os
import sys
import unittest
from datetime import date
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("SUPABASE_URL", "https://example.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "test-key")

import services.daily_menu_service as dms  # noqa: E402
from services.weekly_menu_service import available_on  # noqa: E402

D1, D2, D3 = date(2026, 12, 7), date(2026, 12, 8), date(2026, 12, 9)


class AvailableOnTest(unittest.TestCase):
    def test_unrestricted(self):
        self.assertTrue(available_on(None, "2026-12-07"))
        self.assertTrue(available_on([], "2026-12-07"))

    def test_restricted(self):
        dates = ["2026-12-07", "2026-12-10"]
        self.assertTrue(available_on(dates, "2026-12-10"))
        self.assertFalse(available_on(dates, "2026-12-08"))

    def test_accepts_date_objects_and_timestamps(self):
        self.assertTrue(available_on([D1], "2026-12-07T00:00:00"))


class ScheduleRespectsAvailabilityTest(unittest.TestCase):
    def test_restricted_dish_only_on_its_day(self):
        # Recipe 9 ranks highest but is only cooked on D2.
        allowed = {D1: {1, 2, 3}, D2: {1, 2, 3, 9}, D3: {1, 2, 3}}
        with patch.object(dms, "_quality_score", side_effect=lambda rid, *a, **k: rid):
            out = dms._rank_and_schedule(
                meals_map={"lunch": "lunch"},
                eligible_by_meal_type={"lunch": [1, 2, 3, 9]},
                flex_stats={}, popularity={}, recipe_macros={}, reference_target={},
                dates=[D1, D2, D3], preexisting={}, allowed_by_date=allowed,
            )
        for d in (D1, D3):
            self.assertNotEqual(out[d]["lunch"], 9, d)
            self.assertIn(out[d]["lunch"], allowed[d])

    def test_segment_key_by_menu(self):
        allowed = {D1: {1, 2}, D2: {1, 2, 9}}
        menu = {D1: 36, D2: 36}
        self.assertEqual(dms._segment_key(D1, allowed, menu), dms._segment_key(D2, allowed, menu))
        # without a menu id, differing pools split
        self.assertNotEqual(dms._segment_key(D1, allowed, None), dms._segment_key(D2, allowed, None))


class ChefsPickTest(unittest.TestCase):
    def _picks(self, orders):
        with patch.object(dms, "fetch_other_orders_by_date", return_value=orders):
            return dms.chefs_picks_by_date([D1])

    def test_lunch_and_dinner_pooled_as_main(self):
        orders = {D1: {"lunch": {5: 2, 6: 3}, "dinner": {5: 2}, "breakfast": {7: 4}}}
        self.assertEqual(self._picks(orders), {D1: {"main": 5, "breakfast": 7}})

    def test_minimum_clients(self):
        orders = {D1: {"snack": {8: dms.CHEFS_PICK_MIN_CLIENTS - 1}}}
        self.assertEqual(self._picks(orders), {})

    def test_tie_goes_to_lower_id(self):
        orders = {D1: {"lunch": {12: 4, 4: 4}}}
        self.assertEqual(self._picks(orders)[D1]["main"], 4)


if __name__ == "__main__":
    unittest.main()
