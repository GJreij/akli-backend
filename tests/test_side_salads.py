# tests/test_side_salads.py
"""
Tests for the per-client side salad rules (mealplan_service.apply_side_salad_rules).

Rules locked in here:
  - a recipe without a side salad is never touched
  - no preferences -> the recipe's own salad is kept
  - removed from this meal, or the client never wants salad -> dropped
  - disliked salad -> Akli House Salad instead, keeping the recipe's
    max_serving / is_main for that slot
  - house salad also disliked (or the recipe's salad IS the house salad and
    it's disliked) -> dropped

Run from the mealplanner-flask/ directory:

    python -m unittest tests.test_side_salads -v
"""

import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("SUPABASE_URL", "https://example.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "test-key")

from services.mealplan_service import apply_side_salad_rules  # noqa: E402

HOUSE = {"id": 44, "name": "Akli House Salad", "macros": {"kcal": 85.0, "protein": 2.0, "carbs": 12.0, "fat": 3.0}}


def _recipe(salad_id=158):
    subs = [
        {"id": 46, "name": "Tawouk", "max_serving": None, "is_main": True, "is_side_salad": False,
         "macros": {"kcal": 212.0, "protein": 35.0, "carbs": 3.0, "fat": 6.0}},
    ]
    if salad_id is not None:
        subs.append({"id": salad_id, "name": "Light Fattoush", "max_serving": 1, "is_main": False, "is_side_salad": True,
                     "macros": {"kcal": 93.0, "protein": 3.0, "carbs": 14.0, "fat": 3.0}})
    return subs


def _prefs(never=False, disliked=()):
    return {"never": never, "disliked": set(disliked)}


@patch("services.mealplan_service._get_house_salad", return_value=HOUSE)
class SideSaladRulesTest(unittest.TestCase):

    def test_recipe_without_salad_untouched(self, _house):
        subs = _recipe(salad_id=None)
        self.assertEqual(apply_side_salad_rules(subs, _prefs(never=True), removed=True), subs)

    def test_no_prefs_keeps_own_salad(self, _house):
        out = apply_side_salad_rules(_recipe(), _prefs())
        self.assertEqual([s["id"] for s in out], [46, 158])

    def test_removed_from_meal_drops_salad(self, _house):
        out = apply_side_salad_rules(_recipe(), _prefs(), removed=True)
        self.assertEqual([s["id"] for s in out], [46])

    def test_never_salad_drops_it(self, _house):
        out = apply_side_salad_rules(_recipe(), _prefs(never=True))
        self.assertEqual([s["id"] for s in out], [46])

    def test_disliked_falls_back_to_house_salad_keeping_slot(self, _house):
        out = apply_side_salad_rules(_recipe(), _prefs(disliked=[158]))
        salad = out[-1]
        self.assertEqual(salad["id"], 44)
        self.assertEqual(salad["name"], "Akli House Salad")
        self.assertEqual(salad["macros"]["kcal"], 85.0)
        self.assertEqual(salad["max_serving"], 1)
        self.assertFalse(salad["is_main"])
        self.assertTrue(salad["is_side_salad"])

    def test_house_salad_also_disliked_drops_it(self, _house):
        out = apply_side_salad_rules(_recipe(), _prefs(disliked=[158, 44]))
        self.assertEqual([s["id"] for s in out], [46])

    def test_disliked_house_salad_recipe_drops_it(self, _house):
        out = apply_side_salad_rules(_recipe(salad_id=44), _prefs(disliked=[44]))
        self.assertEqual([s["id"] for s in out], [46])

    def test_disliking_another_salad_changes_nothing(self, _house):
        out = apply_side_salad_rules(_recipe(), _prefs(disliked=[160]))
        self.assertEqual([s["id"] for s in out], [46, 158])

    # --- salad the client explicitly added to this one meal ---

    def test_added_own_salad_overrides_dislike(self, _house):
        out = apply_side_salad_rules(_recipe(), _prefs(disliked=[158, 44]), chosen_id=158)
        self.assertEqual([s["id"] for s in out], [46, 158])

    def test_added_house_salad_overrides_never(self, _house):
        out = apply_side_salad_rules(_recipe(), _prefs(never=True), chosen_id=44)
        self.assertEqual([s["id"] for s in out], [46, 44])
        self.assertEqual(out[-1]["max_serving"], 1)

    def test_removed_beats_added(self, _house):
        out = apply_side_salad_rules(_recipe(), _prefs(), removed=True, chosen_id=44)
        self.assertEqual([s["id"] for s in out], [46])

    def test_choice_not_offered_for_this_recipe_is_ignored(self, _house):
        # e.g. Tabbouleh (160) left over after the dish changed: normal rules apply
        out = apply_side_salad_rules(_recipe(), _prefs(never=True), chosen_id=160)
        self.assertEqual([s["id"] for s in out], [46])


if __name__ == "__main__":
    unittest.main()
