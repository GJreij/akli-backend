# tests/test_discount_aware_pricing.py
"""
Two places that ignored what the client actually paid:

1. The per-day minimum order fee was judged on the DISCOUNTED day price, so
   a 100% promo code still charged the full $minimum (and a big discount
   could push a normal day under it). It's now judged on the pre-discount
   price and discounted like the food.
2. Swap/edit wallet deltas moved the full LIST-price difference, so a
   discounted day refunded/charged at full price (a 100%-promo day paid out
   real wallet money). They're now scaled by the day's paid share.

Run from the mealplanner-flask/ directory:

    python -m unittest tests.test_discount_aware_pricing -v
"""

import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("SUPABASE_URL", "https://example.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "test-key")

from services.day_pricing_helpers import paid_share, charged_delta, list_price_budget  # noqa: E402
from tests.test_checkout_minimum_order import (  # noqa: E402
    _FakeQuery, PRICE_ROW, NO_PROMO_RESULT, _make_test_client,
)


class _FakeSb:
    def __init__(self, rows):
        self._rows = rows

    def table(self, name):
        return _FakeQuery(self._rows)


class PaidShareTests(unittest.TestCase):
    def test_share_from_payment_row(self):
        self.assertAlmostEqual(paid_share(_FakeSb([{"original_amount": 20, "discount_amount": 4}]), 1), 0.8)
        self.assertAlmostEqual(paid_share(_FakeSb([{"original_amount": 22.56, "discount_amount": 22.56}]), 1), 0.0)
        self.assertAlmostEqual(paid_share(_FakeSb([{"original_amount": 20, "discount_amount": 0}]), 1), 1.0)

    def test_missing_data_keeps_old_behaviour(self):
        self.assertEqual(paid_share(_FakeSb([]), 1), 1.0)
        self.assertEqual(paid_share(_FakeSb([{"original_amount": None, "discount_amount": 3}]), 1), 1.0)

    def test_share_is_clamped(self):
        self.assertEqual(paid_share(_FakeSb([{"original_amount": 10, "discount_amount": 12}]), 1), 0.0)
        self.assertEqual(paid_share(_FakeSb([{"original_amount": 10, "discount_amount": -5}]), 1), 1.0)

    def test_lodina_case_moves_nothing(self):
        # Real case: 100% promo day, day edit priced -$7.38 at list price.
        self.assertEqual(charged_delta(-7.38, 0.0), 0.0)
        self.assertEqual(charged_delta(-7.38, 0.8), -5.9)
        self.assertEqual(charged_delta(3.10, 0.9), 2.79)
        self.assertEqual(charged_delta(3.10, 1.0), 3.10)

    def test_list_price_budget_inverts_the_share(self):
        self.assertAlmostEqual(list_price_budget(8.0, 0.8), 10.0)
        self.assertEqual(list_price_budget(8.0, 0.0), float("inf"))


def _meal(recipe_id, p, c, f):
    return {
        "recipe_id": recipe_id, "meal_type": "lunch",
        "macros": {"protein": p, "carbs": c, "fat": f, "kcal": p * 4 + c * 4 + f * 9},
        "subrecipes": [],
    }


def _promo(fraction):
    """A valid promo taking `fraction` off whatever base checkout passes in."""
    def promo(*args, **kwargs):
        base = kwargs.get("discount_base", 0) or 0
        return {**NO_PROMO_RESULT, "status": "valid", "discount_amount": round(base * fraction, 2)}
    return promo


class MinimumOrderWithDiscountTests(unittest.TestCase):
    NORMAL_DAY = {"date": "2026-11-02", "totals": {"kcal": 900}, "meals": [_meal(1, 60, 80, 20)]}  # a few $ over the minimum
    TINY_DAY = {"date": "2026-11-03", "totals": {"kcal": 62}, "meals": [_meal(2, 1, 10, 2)]}        # ~ $1 list

    def setUp(self):
        self.client = _make_test_client()

    def _run(self, days, promo_fraction=None):
        row = {**PRICE_ROW, "minimum_order_price": 5.0}
        router = lambda name: _FakeQuery({"macro_price": [row], "wallet_transactions": []}.get(name, []))
        promo_kwargs = (
            {"side_effect": _promo(promo_fraction)} if promo_fraction is not None
            else {"return_value": NO_PROMO_RESULT}
        )
        plan = {"start_date": days[0]["date"], "end_date": days[-1]["date"], "days": days}
        with patch("routes.checkout_summary.supabase") as sb, \
             patch("routes.checkout_summary.resolve_delivery_fee_per_day", return_value=(2.0, False)), \
             patch("routes.checkout_summary.apply_volume_discount", return_value={"discount_amount": 0.0, "rule": None}), \
             patch("routes.checkout_summary.validate_and_apply_promo_code", **promo_kwargs), \
             patch("routes.checkout_summary.log_event"):
            sb.table.side_effect = router
            resp = self.client.post("/checkout_summary", json={"user_id": "u", "final_plan": plan})
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        return resp.get_json()["price_breakdown"]

    def test_hundred_percent_promo_has_no_minimum_fee(self):
        bd = self._run([self.NORMAL_DAY, self.TINY_DAY], promo_fraction=1.0)
        self.assertEqual(bd["minimum_order"]["fee_applied"], 0.0)
        self.assertEqual([d["minimum_order_fee"] for d in bd["daily_breakdown"]], [0.0, 0.0])
        self.assertEqual([d["total_price"] for d in bd["daily_breakdown"]], [0.0, 0.0])

    def test_discount_never_pushes_a_normal_day_under_the_minimum(self):
        day = self._run([self.NORMAL_DAY], promo_fraction=0.5)["daily_breakdown"][0]
        self.assertGreaterEqual(day["original_total_price"], 5)
        self.assertLess(day["total_price"], 5)
        self.assertEqual(day["minimum_order_fee"], 0.0)

    def test_tiny_day_fee_is_discounted_like_the_food(self):
        full = self._run([self.TINY_DAY])["daily_breakdown"][0]
        disc = self._run([self.TINY_DAY], promo_fraction=0.2)["daily_breakdown"][0]
        self.assertGreater(full["minimum_order_fee"], 0)
        # Within a cent: checkout's discount_ratio comes from rounded totals.
        self.assertAlmostEqual(disc["minimum_order_fee"], full["minimum_order_fee"] * 0.8, delta=0.011)


if __name__ == "__main__":
    unittest.main()
