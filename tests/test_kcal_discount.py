# tests/test_kcal_discount.py
"""
The kcal discount curve is configured in macro_price
(kcal_discount_start_kcal / kcal_discount_end_kcal / kcal_discount_max_pct)
instead of being hard-coded. Defaults reproduce the old fixed curve
(1200 -> 3000 kcal, up to 22%), so prices don't move until a value changes.

Run from the mealplanner-flask/ directory:

    python -m unittest tests.test_kcal_discount -v
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("SUPABASE_URL", "https://example.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "test-key")

from services.pricing_service import get_kcal_discount, kcal_discount_settings, compute_macro_cost  # noqa: E402

BASE = {"protein_price_per_g": 0.072, "carbs_price_per_g": 0.036, "fat_price_per_g": 0.048}


class KcalDiscountTests(unittest.TestCase):
    def test_defaults_match_the_old_fixed_curve(self):
        self.assertEqual(get_kcal_discount(1200), 0.0)
        self.assertAlmostEqual(get_kcal_discount(2100), 0.11)
        self.assertAlmostEqual(get_kcal_discount(3000), 0.22)
        self.assertAlmostEqual(get_kcal_discount(4000), 0.22)
        # A macro_price row carrying the default values behaves identically.
        row = kcal_discount_settings({"kcal_discount_start_kcal": 1200, "kcal_discount_end_kcal": 3000, "kcal_discount_max_pct": 22})
        for k in (900, 1500, 2246, 2999, 3500):
            self.assertAlmostEqual(get_kcal_discount(k, row), get_kcal_discount(k))

    def test_custom_settings_change_the_curve(self):
        s = {"kcal_discount_start_kcal": 1500, "kcal_discount_end_kcal": 2500, "kcal_discount_max_pct": 10}
        self.assertEqual(get_kcal_discount(1500, s), 0.0)
        self.assertAlmostEqual(get_kcal_discount(2000, s), 0.05)
        self.assertAlmostEqual(get_kcal_discount(2500, s), 0.10)
        self.assertAlmostEqual(get_kcal_discount(9999, s), 0.10)

    def test_zero_pct_turns_it_off(self):
        self.assertEqual(get_kcal_discount(2800, {"kcal_discount_max_pct": 0}), 0.0)

    def test_end_not_above_start_is_a_step_not_a_crash(self):
        s = {"kcal_discount_start_kcal": 2000, "kcal_discount_end_kcal": 2000, "kcal_discount_max_pct": 15}
        self.assertEqual(get_kcal_discount(1999, s), 0.0)
        self.assertAlmostEqual(get_kcal_discount(2001, s), 0.15)

    def test_null_columns_fall_back_to_defaults(self):
        row = kcal_discount_settings({"kcal_discount_start_kcal": None})
        self.assertEqual(row, {"kcal_discount_start_kcal": 1200.0, "kcal_discount_end_kcal": 3000.0, "kcal_discount_max_pct": 22.0})

    def test_macro_cost_uses_the_settings(self):
        args = dict(protein_g=194, carbs_g=250, fat_g=53, kcal=2246)
        default = compute_macro_cost(**args, prices=BASE)
        off = compute_macro_cost(**args, prices={**BASE, "kcal_discount_max_pct": 0})
        self.assertAlmostEqual(round(default["macro_cost_after_discount"], 2), 22.25)
        self.assertAlmostEqual(round(off["macro_cost_after_discount"], 2), 25.51)


if __name__ == "__main__":
    unittest.main()
