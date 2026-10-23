# tests/test_delivery_override.py
"""
An address's admin-set delivery_fee_override is the exact fee for that
address and applies to EVERY delivery day — the $25 free-delivery rule only
governs the standard fee. Before this, any day over $25 silently dropped an
overridden fee to free.

Run from the mealplanner-flask/ directory:

    python -m unittest tests.test_delivery_override -v
"""

import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("SUPABASE_URL", "https://example.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "test-key")

from tests.test_checkout_minimum_order import (  # noqa: E402
    _FakeQuery, PRICE_ROW, NO_PROMO_RESULT, _make_test_client,
)


def _meal(recipe_id, p, c, f):
    return {
        "recipe_id": recipe_id, "meal_type": "lunch",
        "macros": {"protein": p, "carbs": c, "fat": f, "kcal": p * 4 + c * 4 + f * 9},
        "subrecipes": [],
    }


# One big day (well over $25 with PRICE_ROW's per-gram prices) and one small one.
PLAN = {
    "start_date": "2026-11-02", "end_date": "2026-11-03",
    "days": [
        {"date": "2026-11-02", "totals": {"kcal": 2600}, "meals": [_meal(1, 500, 500, 150), _meal(2, 50, 50, 20)]},
        {"date": "2026-11-03", "totals": {"kcal": 370}, "meals": [_meal(3, 30, 40, 10)]},
    ],
}


class DeliveryOverrideTests(unittest.TestCase):
    def setUp(self):
        self.client = _make_test_client()

    def _run(self, fee, is_override, promo=NO_PROMO_RESULT):
        row = {**PRICE_ROW, "minimum_order_price": 0}
        router = lambda name: _FakeQuery({"macro_price": [row], "wallet_transactions": []}.get(name, []))
        with patch("routes.checkout_summary.supabase") as sb, \
             patch("routes.checkout_summary.resolve_delivery_fee_per_day", return_value=(fee, is_override)), \
             patch("routes.checkout_summary.apply_volume_discount", return_value={"discount_amount": 0.0, "rule": None}), \
             patch("routes.checkout_summary.validate_and_apply_promo_code", return_value=promo), \
             patch("routes.checkout_summary.log_event"):
            sb.table.side_effect = router
            resp = self.client.post("/checkout_summary", json={"user_id": "u", "final_plan": PLAN})
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        return resp.get_json()["price_breakdown"]

    def test_big_day_is_free_with_standard_fee(self):
        bd = self._run(2.0, False)
        big, small = bd["daily_breakdown"]
        self.assertGreaterEqual(big["original_total_price"], 25)
        self.assertEqual(big["delivery_fee"], 0)
        self.assertEqual(small["delivery_fee"], 2.0)
        self.assertEqual(bd["delivery"]["minimum_per_day_for_free_delivery"], 25)

    def test_override_is_charged_every_day_even_over_25(self):
        bd = self._run(3.5, True)
        self.assertGreaterEqual(bd["daily_breakdown"][0]["original_total_price"], 25)
        self.assertEqual([d["delivery_fee"] for d in bd["daily_breakdown"]], [3.5, 3.5])
        self.assertEqual(bd["delivery"]["delivery_fee"], 7.0)
        self.assertEqual(bd["delivery"]["delivery_days"], 2)
        self.assertIsNone(bd["delivery"]["minimum_per_day_for_free_delivery"])
        self.assertTrue(bd["delivery"]["is_custom_fee"])
        self.assertEqual(bd["final_price"], round(sum(d["total_price_with_delivery"] for d in bd["daily_breakdown"]), 2))

    def test_zero_override_is_free_every_day(self):
        bd = self._run(0.0, True)
        self.assertEqual([d["delivery_fee"] for d in bd["daily_breakdown"]], [0, 0])
        self.assertTrue(bd["delivery"]["is_free_delivery"])

    def test_delivery_waiving_promo_still_wins_over_override(self):
        promo = {**NO_PROMO_RESULT, "status": "valid", "waives_delivery": True}
        bd = self._run(3.5, True, promo)
        self.assertEqual([d["delivery_fee"] for d in bd["daily_breakdown"]], [0, 0])


if __name__ == "__main__":
    unittest.main()
