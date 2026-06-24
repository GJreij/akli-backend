# services/affiliate_service.py
from __future__ import annotations

from typing import Any, Dict

from utils.supabase_client import supabase


class AffiliateService:
    def __init__(self):
        self.sb = supabase

    def get_affiliate_commission_summary(self, affiliate_id: int) -> Dict[str, Any]:
        """
        Commission owed to an affiliate, computed from commission_amount
        snapshotted on payment rows at order time (see
        order_service._create_payment_record), split by the customer
        payment's status, minus whatever has already been recorded as paid
        out in affiliate_payouts.
        """
        payments_res = (
            self.sb.table("payment")
            .select("commission_amount, status")
            .eq("affiliate_id", affiliate_id)
            .execute()
        )
        payments = payments_res.data or []

        earned_paid = sum(
            float(p["commission_amount"] or 0)
            for p in payments
            if p.get("status") == "paid"
        )
        earned_pending = sum(
            float(p["commission_amount"] or 0)
            for p in payments
            if p.get("status") == "pending"
        )

        payouts_res = (
            self.sb.table("affiliate_payouts")
            .select("amount")
            .eq("affiliate_id", affiliate_id)
            .execute()
        )
        already_paid_out = sum(float(p["amount"] or 0) for p in (payouts_res.data or []))

        return {
            "affiliate_id": affiliate_id,
            "commission_earned_paid_orders": round(earned_paid, 2),
            "commission_earned_pending_orders": round(earned_pending, 2),
            "already_paid_out": round(already_paid_out, 2),
            "balance_owed": round(earned_paid - already_paid_out, 2),
        }
