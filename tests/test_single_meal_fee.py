# tests/test_single_meal_fee.py
"""
Tests for macro_price.single_meal_fee — an extra packaging fee on a day with
exactly ONE meal (the per-day bag costs the same whatever the day holds).

Rules locked in here:
  - only a 1-meal day pays it; 0 or 2+ meals never do
  - it's per DAY: a multi-day order pays it once per one-meal day
  - it's part of the day's price (so the aggregate still reconciles with the
    per-day totals order_service bills from) and reported separately in
    price_breakdown.single_meal_fee
  - swap/edit repricing (day_price_and_macros) includes it too, so a swap on
    a one-meal day doesn't produce a phantom -$fee delta

Run from the mealplanner-flask/ directory:

    python -m unittest tests.test_single_meal_fee -v
"""

import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("SUPABASE_URL", "https://example.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "test-key")

from services.pricing_service import single_meal_fee  # noqa: E402
from services.day_pricing_helpers import day_price_and_macros  # noqa: E402
from tests.test_checkout_minimum_order import (  # noqa: E402
    _FakeQuery, PRICE_ROW, NO_PROMO_RESULT, _make_test_client,
)

PRICES = {
    "protein_price_per_g": 0.05, "carbs_price_per_g": 0.02, "fat_price_per_g": 0.03,
    "day_packaging_price": 0.0, "recipe_packaging_price": 1.0, "subrecipe_packaging_price": 0.0,
    "single_meal_fee": 1.0,
}


class SingleMealFeeHelperTests(unittest.TestCase):
    def test_only_one_meal_pays(self):
        self.assertEqual(single_meal_fee(1, PRICES), 1.0)
        for n in (0, 2, 3, 4, 5):
            self.assertEqual(single_meal_fee(n, PRICES), 0.0, n)

    def test_missing_or_zero_setting_is_off(self):
        self.assertEqual(single_meal_fee(1, {**PRICES, "single_meal_fee": 0}), 0.0)
        self.assertEqual(single_meal_fee(1, {k: v for k, v in PRICES.items() if k != "single_meal_fee"}), 0.0)

    def test_day_repricing_includes_fee_only_for_one_meal(self):
        serving = {"protein_calculated": 30, "carbs_calculated": 40, "fat_calculated": 10, "kcal_calculated": 370}
        target = {"kcal": 370}
        one, _ = day_price_and_macros([serving], [{"id": 1}], target, PRICES)
        two, _ = day_price_and_macros([serving], [{"id": 1}, {"id": 2}], target, PRICES)
        # Same food, one extra recipe packaging ($1) on the 2-meal day, but
        # the 1-meal day carries the $1 single-meal fee instead.
        self.assertAlmostEqual(one, two, places=2)
        no_fee, _ = day_price_and_macros([serving], [{"id": 1}], target, {**PRICES, "single_meal_fee": 0})
        self.assertAlmostEqual(one - no_fee, 1.0, places=2)


def _meal(recipe_id, meal_type, p=30, c=40, f=10):
    return {
        "recipe_id": recipe_id, "meal_type": meal_type,
        "macros": {"protein": p, "carbs": c, "fat": f, "kcal": p * 4 + c * 4 + f * 9},
        "subrecipes": [],
    }


class CheckoutSingleMealFeeTests(unittest.TestCase):
    def setUp(self):
        self.client = _make_test_client()

    def _run(self, fee):
        row = {**PRICE_ROW, "single_meal_fee": fee, "minimum_order_price": 0}
        plan = {
            "start_date": "2026-11-02", "end_date": "2026-11-04",
            "days": [
                {"date": "2026-11-02", "totals": {"kcal": 370}, "meals": [_meal(1, "lunch")]},
                {"date": "2026-11-03", "totals": {"kcal": 740}, "meals": [_meal(1, "lunch"), _meal(2, "dinner")]},
                {"date": "2026-11-04", "totals": {"kcal": 370}, "meals": [_meal(3, "snack")]},
            ],
        }
        router = lambda name: _FakeQuery({"macro_price": [row], "wallet_transactions": []}.get(name, []))
        with patch("routes.checkout_summary.supabase") as sb, \
             patch("routes.checkout_summary.resolve_delivery_fee_per_day", return_value=(2.0, False)), \
             patch("routes.checkout_summary.apply_volume_discount", return_value={"discount_amount": 0.0, "rule": None}), \
             patch("routes.checkout_summary.validate_and_apply_promo_code", return_value=NO_PROMO_RESULT), \
             patch("routes.checkout_summary.log_event"):
            sb.table.side_effect = router
            resp = self.client.post("/checkout_summary", json={"user_id": "test-user", "final_plan": plan})
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        return resp.get_json()["price_breakdown"]

    def test_fee_lands_on_each_one_meal_day_only(self):
        with_fee, without = self._run(1.0), self._run(0)
        d1, d0 = with_fee["daily_breakdown"], without["daily_breakdown"]
        self.assertEqual([d["single_meal_fee"] for d in d1], [1.0, 0.0, 1.0])
        for a, b in zip(d1, d0):
            self.assertAlmostEqual(a["original_total_price"] - b["original_total_price"], a["single_meal_fee"], places=2)
        self.assertEqual(with_fee["single_meal_fee"], {"fee_per_day": 1.0, "days": 2, "total": 2.0})
        self.assertAlmostEqual(with_fee["total_price_before_discount"] - without["total_price_before_discount"], 2.0, places=2)

    def test_total_still_reconciles_with_per_day_billing(self):
        bd = self._run(1.0)
        self.assertEqual(bd["final_price"], round(sum(d["total_price_with_delivery"] for d in bd["daily_breakdown"]), 2))

    def test_zero_fee_reports_nothing(self):
        bd = self._run(0)
        self.assertEqual(bd["single_meal_fee"]["total"], 0.0)
        self.assertEqual(bd["single_meal_fee"]["days"], 0)


if __name__ == "__main__":
    unittest.main()
