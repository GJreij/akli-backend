# tests/test_volume_discount_rebase.py
"""
10-day order with the 10% volume discount, client cancels a day (9 left,
under the 10-day threshold) -> the lost discount is charged to the wallet.
The remaining days' payment rows must then stop claiming the 10%, or:
  - a SECOND cancellation re-reads the stale 10% and charges it again, and
  - a later swap/edit (day_pricing_helpers.paid_share) still scales its
    delta by 90% although the client now pays full price.

Run from the mealplanner-flask/ directory:

    python -m unittest tests.test_volume_discount_rebase -v
"""

import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("SUPABASE_URL", "https://example.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "test-key")

from services.cancellation_service import CancellationService  # noqa: E402
from services.day_pricing_helpers import paid_share, charged_delta  # noqa: E402


class _Db:
    """Tiny in-memory stand-in: meal_plan_day rows with an embedded payment,
    payment updates by id, payment lookups by meal_plan_day_id, and a
    record of wallet RPCs."""

    def __init__(self, days):
        self.days = days            # [{"id", "status", "payment": {...}}]
        self.wallet = []

    def _payments(self):
        return [d["payment"] for d in self.days]

    def table(self, name):
        return _Query(self, name)

    def rpc(self, fn, params):
        self.wallet.append(params["p_price_delta"])
        return SimpleNamespace(execute=lambda: SimpleNamespace(data=None))


class _Query:
    def __init__(self, db, name):
        self.db, self.name, self.filters, self.patch = db, name, {}, None

    def select(self, *a, **k):
        return self

    def update(self, values):
        self.patch = values
        return self

    def eq(self, col, val):
        self.filters[col] = val
        return self

    def limit(self, *a):
        return self

    def execute(self):
        if self.name == "meal_plan_day":
            return SimpleNamespace(data=self.db.days)
        if self.name == "payment" and self.patch is not None:
            for p in self.db._payments():
                if p["id"] == self.filters.get("id"):
                    p.update(self.patch)
            return SimpleNamespace(data=[])
        if self.name == "payment":
            mpd = self.filters.get("meal_plan_day_id")
            return SimpleNamespace(data=[d["payment"] for d in self.db.days if d["id"] == mpd])
        return SimpleNamespace(data=[])


def _ten_day_order():
    days = []
    for i in range(1, 11):
        days.append({
            "id": i, "status": "pending",
            "payment": {
                "id": 100 + i, "amount": 18.0, "original_amount": 20.0,
                "discount_amount": 2.0, "volume_discount_amount": 2.0,
                "promo_discount_amount": 0.0, "delivery_fee_amount": 0.0,
            },
        })
    return days


def _volume_rule(total, n):
    # The real rule: 10% off from 10 days.
    return {"discount_amount": round(total * 0.10, 2) if n >= 10 else 0.0, "rule": None}


class CancelThenEditTests(unittest.TestCase):
    def setUp(self):
        self.db = _Db(_ten_day_order())
        self.svc = CancellationService()
        self.svc.sb = self.db

    def _cancel(self, day_id):
        next(d for d in self.db.days if d["id"] == day_id)["status"] = "cancelled"
        with patch("services.cancellation_service.apply_volume_discount", side_effect=_volume_rule):
            return self.svc._apply_volume_discount_correction(meal_plan_id=1, user_id="u")

    def test_first_cancel_charges_lost_discount_and_rebases_rows(self):
        result = self._cancel(10)
        # 9 remaining days x $2 of volume discount = $18 owed back.
        self.assertTrue(result["applied"])
        self.assertEqual(self.db.wallet, [18.0])
        for d in self.db.days[:9]:
            self.assertEqual(d["payment"]["volume_discount_amount"], 0.0)
            self.assertEqual(d["payment"]["discount_amount"], 0.0)
            self.assertEqual(d["payment"]["amount"], 18.0)  # billed amount untouched
        # The cancelled day's row is left as it was.
        self.assertEqual(self.db.days[9]["payment"]["volume_discount_amount"], 2.0)

    def test_edit_after_cancel_moves_the_full_difference(self):
        self._cancel(10)
        share = paid_share(self.db, 3)
        self.assertEqual(share, 1.0)
        self.assertEqual(charged_delta(-4.0, share), -4.0)

    def test_second_cancel_does_not_charge_the_discount_again(self):
        self._cancel(10)
        result = self._cancel(9)
        self.assertIsNone(result)                 # nothing left to correct
        self.assertEqual(self.db.wallet, [18.0])  # still only the one charge

    def test_promo_part_is_kept_when_volume_part_is_removed(self):
        for d in self.db.days:
            d["payment"].update({"discount_amount": 6.0, "promo_discount_amount": 4.0, "amount": 14.0})
        self._cancel(10)
        p = self.db.days[0]["payment"]
        self.assertEqual(p["volume_discount_amount"], 0.0)
        self.assertEqual(p["discount_amount"], 4.0)
        self.assertAlmostEqual(paid_share(self.db, 1), 0.8)


if __name__ == "__main__":
    unittest.main()
