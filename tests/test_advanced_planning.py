import unittest
from unittest.mock import patch

from services import advanced_planning_service as aps

SUBS = {
    46: {"id": 46, "name": "Tawouk", "is_flexible": True, "kcal": 212, "protein": 34.8, "carbs": 3.8, "fat": 6.6},
    68: {"id": 68, "name": "Rice Broth", "is_flexible": True, "kcal": 88, "protein": 1.7, "carbs": 19.7, "fat": 0.3},
    166: {"id": 166, "name": "Light Toum", "is_flexible": False, "kcal": 37, "protein": 3.7, "carbs": 3.1, "fat": 1.0},
}


def _plan():
    return {
        "user_id": "u1",
        "days": [{
            "date": "2026-11-16",
            "totals": {"protein": 0, "carbs": 0, "fat": 0, "kcal": 0, "tolerance_used": 0.05},
            "meals": [{
                "meal_key": "lunch", "meal_type": "lunch", "recipe_id": 1,
                "macros": {},
                "subrecipes": [
                    {"subrecipe_id": 46, "name": "Tawouk", "servings": 1.0, "macros": {}},
                    {"subrecipe_id": 68, "name": "Rice Broth", "servings": 1.0, "macros": {}},
                    {"subrecipe_id": 166, "name": "Light Toum", "servings": 1.0, "macros": {}},
                ],
            }],
        }, {
            "date": "2026-11-17",
            "totals": {"protein": 1, "carbs": 2, "fat": 3, "kcal": 4},
            "meals": [],
        }],
    }


def _fetch(ids):
    return {i: SUBS[i] for i in ids if i in SUBS}


@patch.object(aps, "_fetch_main_pairs", return_value=set())
@patch.object(aps, "_fetch_subrecipes", side_effect=_fetch)
@patch.object(aps, "has_feature", return_value=True)
class ApplyChangesTest(unittest.TestCase):
    def test_recipe_main_is_adjustable(self, _feat, _subs, mains):
        mains.return_value = {(1, 166)}  # Light Toum as the main of recipe 1
        out = aps.apply_changes("u1", _plan(), [
            {"date": "2026-11-16", "meal_key": "lunch", "subrecipe_id": 166, "servings": 2},
        ])
        self.assertEqual(out["days"][0]["meals"][0]["subrecipes"][2]["servings"], 2)
        # ...but only in that recipe
        mains.return_value = {(99, 166)}
        with self.assertRaises(aps.AdvancedPlanningError):
            aps.apply_changes("u1", _plan(), [
                {"date": "2026-11-16", "meal_key": "lunch", "subrecipe_id": 166, "servings": 2},
            ])

    def test_flexible_change_recomputes_macros(self, *_):
        out = aps.apply_changes("u1", _plan(), [
            {"date": "2026-11-16", "meal_key": "lunch", "subrecipe_id": 46, "servings": 2.25},
        ])
        day = out["days"][0]
        tawouk = day["meals"][0]["subrecipes"][0]
        self.assertEqual(tawouk["servings"], 2.25)
        self.assertAlmostEqual(tawouk["macros"]["protein"], 34.8 * 2.25)
        self.assertEqual(day["totals"]["kcal"], round(212 * 2.25 + 88 + 37))
        self.assertEqual(day["totals"]["tolerance_used"], 0.05)  # other keys kept
        self.assertTrue(day["manual_servings"])
        self.assertTrue(out["advanced_planning"])
        # untouched day left alone
        self.assertEqual(out["days"][1]["totals"]["kcal"], 4)
        self.assertNotIn("manual_servings", out["days"][1])

    def test_locked_item_rejected(self, *_):
        with self.assertRaises(aps.AdvancedPlanningError):
            aps.apply_changes("u1", _plan(), [
                {"date": "2026-11-16", "meal_key": "lunch", "subrecipe_id": 166, "servings": 2},
            ])

    def test_bounds_and_step(self, *_):
        for bad in (0, -1, 0.1, 10.25, 1.3, "abc"):
            with self.assertRaises(aps.AdvancedPlanningError, msg=bad):
                aps.apply_changes("u1", _plan(), [
                    {"date": "2026-11-16", "meal_key": "lunch", "subrecipe_id": 68, "servings": bad},
                ])
        for ok in (0.25, 10, 3.75):
            aps.apply_changes("u1", _plan(), [
                {"date": "2026-11-16", "meal_key": "lunch", "subrecipe_id": 68, "servings": ok},
            ])

    def test_original_not_mutated(self, *_):
        plan = _plan()
        aps.apply_changes("u1", plan, [
            {"date": "2026-11-16", "meal_key": "lunch", "subrecipe_id": 46, "servings": 3},
        ])
        self.assertEqual(plan["days"][0]["meals"][0]["subrecipes"][0]["servings"], 1.0)

    def test_other_users_plan_rejected(self, *_):
        with self.assertRaises(aps.AdvancedPlanningError):
            aps.apply_changes("u2", _plan(), [])

    def test_normalize_rebuilds_tampered_macros(self, *_):
        out = aps.apply_changes("u1", _plan(), [
            {"date": "2026-11-16", "meal_key": "lunch", "subrecipe_id": 46, "servings": 2},
        ])
        out["days"][0]["meals"][0]["subrecipes"][0]["macros"]["protein"] = 1
        out["days"][0]["totals"]["kcal"] = 5
        fixed = aps.normalize_for_confirm("u1", out)
        self.assertAlmostEqual(fixed["days"][0]["meals"][0]["subrecipes"][0]["macros"]["protein"], 69.6)
        self.assertEqual(fixed["days"][0]["totals"]["kcal"], round(212 * 2 + 88 + 37))


class FeatureGateTest(unittest.TestCase):
    @patch.object(aps, "has_feature", return_value=False)
    def test_without_flag(self, _):
        with self.assertRaises(aps.AdvancedPlanningError) as cm:
            aps.apply_changes("u1", _plan(), [])
        self.assertEqual(cm.exception.status, 403)
        with self.assertRaises(aps.AdvancedPlanningError):
            aps.normalize_for_confirm("u1", _plan())


if __name__ == "__main__":
    unittest.main()
