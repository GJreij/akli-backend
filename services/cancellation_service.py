# services/cancellation_service.py

import os
import logging
from datetime import datetime, timezone
from utils.supabase_client import supabase
from utils.dates import beirut_iso_date
from services.volume_discount_service import apply_volume_discount

logger = logging.getLogger(__name__)

INTERNAL_ADMIN_SECRET = os.getenv("INTERNAL_ADMIN_SECRET", "")

TERMINAL_DAY_STATUSES = ("cancellation_pending", "cancelled")


class CancellationService:
    def __init__(self):
        self.sb = supabase

    # ---------- CLIENT-FACING: request a cancellation ----------

    def request_cancellation(self, user_id, meal_plan_id, meal_plan_day_ids=None):
        """
        meal_plan_day_ids: optional list of specific meal_plan_day ids to
        cancel. Omitted/None cancels the whole order (all active days) —
        preserves the original whole-order behavior. When provided, only
        those days are touched; the rest of the plan is left untouched.
        """
        plan_res = (
            self.sb.table("meal_plan")
            .select("id, user_id")
            .eq("id", meal_plan_id)
            .maybe_single()
            .execute()
        )
        # supabase-py's maybe_single() returns None (not a response object
        # with .data = None) when zero rows match, in some client versions.
        plan = plan_res.data if plan_res else None
        if not plan:
            return {"error": "Meal plan not found."}, 404
        if str(plan["user_id"]) != str(user_id):
            return {"error": "This order does not belong to this user."}, 403

        days_res = (
            self.sb.table("meal_plan_day")
            .select("id, date, status, delivery_id")
            .eq("meal_plan_id", meal_plan_id)
            .execute()
        )
        days = days_res.data or []
        if not days:
            return {"error": "No order days found for this plan."}, 404

        active_days = [d for d in days if d["status"] not in TERMINAL_DAY_STATUSES]
        if not active_days:
            return {"error": "This order is already cancelled or has no active days."}, 409

        if meal_plan_day_ids:
            requested_ids = set(meal_plan_day_ids)
            active_by_id = {d["id"]: d for d in active_days}
            invalid_ids = requested_ids - set(active_by_id.keys())
            if invalid_ids:
                return {
                    "error": f"Some selected days are not active/cancellable on this order: {sorted(invalid_ids)}",
                }, 400
            target_days = [active_by_id[i] for i in requested_ids]
        else:
            target_days = active_days

        earliest_date = min(d["date"] for d in target_days)
        cutoff = beirut_iso_date(2)
        if earliest_date < cutoff:
            return {
                "error": "Too late to cancel — the earliest selected delivery is within 48 hours.",
            }, 400

        target_day_ids = [d["id"] for d in target_days]

        try:
            insert_res = (
                self.sb.table("cancellation_request")
                .insert({
                    "meal_plan_id": meal_plan_id,
                    "user_id": user_id,
                    "requested_at": datetime.now(timezone.utc).isoformat(),
                    "meal_plan_day_ids": target_day_ids,
                })
                .execute()
            )
        except Exception as e:
            # Most likely the partial unique index (an active request already exists)
            return {"error": f"Could not create cancellation request: {e}"}, 409

        cancellation_request = insert_res.data[0]

        self.sb.table("meal_plan_day").update({
            "status": "cancellation_pending",
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }).in_("id", target_day_ids).execute()

        delivery_ids = [d["delivery_id"] for d in target_days if d.get("delivery_id")]
        if delivery_ids:
            self.sb.table("deliveries").update({
                "status": "cancellation_pending",
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }).in_("id", delivery_ids).eq("status", "pending").execute()

        return {
            "success": True,
            "cancellation_request_id": cancellation_request["id"],
        }, 200

    # ---------- ADMIN-FACING: decide a pending request ----------

    def decide(self, cancellation_request_id, decision, decided_by, note=None, refund_amount=None):
        if decision not in ("approved_wallet", "approved_refund", "approved_no_refund", "rejected"):
            return {"error": "Invalid decision."}, 400

        req_res = (
            self.sb.table("cancellation_request")
            .select("id, meal_plan_id, user_id, status, meal_plan_day_ids")
            .eq("id", cancellation_request_id)
            .maybe_single()
            .execute()
        )
        req = req_res.data if req_res else None
        if not req:
            return {"error": "Cancellation request not found."}, 404
        if req["status"] != "pending":
            return {"error": f"Request already decided ({req['status']})."}, 409

        now = datetime.now(timezone.utc).isoformat()
        self.sb.table("cancellation_request").update({
            "status": decision,
            "decided_by": decided_by,
            "decided_at": now,
            "decision_note": note,
            "refund_amount": refund_amount,
            "updated_at": now,
        }).eq("id", cancellation_request_id).execute()

        # Scope strictly to the days this specific request targeted (may be a
        # subset of the plan for a partial-day cancellation) — not "every
        # cancellation_pending day on the plan", which would also catch days
        # belonging to a different, unrelated request.
        requested_day_ids = req.get("meal_plan_day_ids") or []
        days_res = (
            self.sb.table("meal_plan_day")
            .select("id, delivery_id, status")
            .in_("id", requested_day_ids)
            .eq("status", "cancellation_pending")
            .execute()
        ) if requested_day_ids else None
        pending_days = days_res.data if days_res else []
        day_ids = [d["id"] for d in pending_days]
        delivery_ids = [d["delivery_id"] for d in pending_days if d.get("delivery_id")]

        if decision == "rejected":
            if day_ids:
                self.sb.table("meal_plan_day").update({
                    "status": "pending", "updated_at": now,
                }).in_("id", day_ids).execute()
            if delivery_ids:
                self.sb.table("deliveries").update({
                    "status": "pending", "updated_at": now,
                }).in_("id", delivery_ids).eq("status", "cancellation_pending").execute()
            return {"success": True, "status": "rejected"}, 200

        # approved_wallet / approved_refund: finalize as cancelled and release slots
        if day_ids:
            self.sb.table("meal_plan_day").update({
                "status": "cancelled", "updated_at": now,
            }).in_("id", day_ids).execute()

        if delivery_ids:
            deliveries_res = (
                self.sb.table("deliveries")
                .select("id, delivery_date, delivery_slot_id")
                .in_("id", delivery_ids)
                .execute()
            )
            for delivery in (deliveries_res.data or []):
                self._release_slot(delivery["delivery_slot_id"], delivery["delivery_date"])

            self.sb.table("deliveries").update({
                "status": "cancelled", "updated_at": now,
            }).in_("id", delivery_ids).eq("status", "cancellation_pending").execute()

        commission_voided = self._void_commission_for_cancelled_days(day_ids)
        discount_correction = self._apply_volume_discount_correction(req["meal_plan_id"], req["user_id"])

        return {
            "success": True,
            "status": decision,
            "discount_correction": discount_correction,
            "commission_voided": commission_voided,
        }, 200

    def _void_commission_for_cancelled_days(self, day_ids):
        """
        A cancelled day's `payment` row must stop contributing to
        AffiliateService.get_affiliate_commission_summary — that reads
        commission_amount off `payment` keyed only by status ("paid"/
        "pending"), with no awareness of the order itself being cancelled.
        Without this, a day that already had (or was still accruing) a
        commission snapshot would keep counting toward the affiliate's
        earned/owed balance forever, even after the order was cancelled and
        the client refunded/credited back.

        affiliate_id and commission_rate are left untouched — audit trail of
        which affiliate/rate this day was under — only the owed amount is
        zeroed. Scoped to rows that actually had a commission (skips
        non-affiliate orders, where commission_amount is already null).
        """
        if not day_ids:
            return None

        rows_res = (
            self.sb.table("payment")
            .select("id, commission_amount")
            .in_("meal_plan_day_id", day_ids)
            .not_.is_("commission_amount", None)
            .execute()
        )
        rows = rows_res.data or []
        if not rows:
            return None

        voided_total = round(sum(float(r["commission_amount"] or 0) for r in rows), 2)
        self.sb.table("payment").update({"commission_amount": 0}).in_(
            "id", [r["id"] for r in rows]
        ).execute()

        return {"voided_count": len(rows), "voided_amount": voided_total}

    def _compute_discount_correction(self, meal_plan_id, excluded_day_ids):
        """
        Pure computation, shared by the real (post-decision) correction and
        the client-facing preview shown before they even submit a request.
        `excluded_day_ids` are meal_plan_day ids to treat as NOT remaining —
        already 'cancelled' days are excluded automatically on top of these,
        so a preview can pass the days currently being considered without
        them needing to be cancelled in the DB yet.

        Only the volume-discount portion of each day's discount is ever
        recomputed/corrected here — `payment.discount_amount` is volume
        discount + promo-code discount combined, and a promo code's
        eligibility doesn't change just because the order got shorter. Using
        the combined figure would claw back the customer's promo discount
        every time this fires, even when the volume-discount tier itself
        never moved (see the incident this was split out to fix: a 16→15
        day partial cancellation, still comfortably over the 10-day
        threshold either way, wrongly "corrected" $25.44 that was actually
        the customer's stacked promo code, not a lost volume discount).

        Returns {"remaining_count": int, "amount": float} (amount >0 means
        the volume discount shrank and the client owes more) or None if
        there's nothing to correct, or the order predates original_amount/
        volume_discount_amount being recorded on `payment` (nothing
        reliable to recompute against — older orders never persisted the
        volume/promo split, so there's no safe way to isolate just the
        volume portion for them).
        """
        days_res = (
            self.sb.table("meal_plan_day")
            .select(
                "id, status, "
                "payment(id, amount, original_amount, discount_amount, "
                "volume_discount_amount, promo_discount_amount, delivery_fee_amount)"
            )
            .eq("meal_plan_id", meal_plan_id)
            .execute()
        )
        excluded = set(excluded_day_ids or [])
        remaining_payments = []
        for d in (days_res.data or []):
            if d["id"] in excluded or d["status"] == "cancelled":
                continue
            p = d.get("payment")
            rows = p if isinstance(p, list) else ([p] if p else [])
            remaining_payments.extend(rows)

        if not remaining_payments or any(
            p.get("original_amount") is None or p.get("volume_discount_amount") is None
            for p in remaining_payments
        ):
            return None

        remaining_count = len(remaining_payments)
        remaining_original_total = sum(float(p["original_amount"]) for p in remaining_payments)
        old_volume_discount_total = sum(float(p.get("volume_discount_amount") or 0) for p in remaining_payments)

        new_discount = apply_volume_discount(remaining_original_total, remaining_count)["discount_amount"]
        correction = round(old_volume_discount_total - new_discount, 2)  # >0: volume discount shrank, client owes more

        # old_volume_discount_total is a sum of independently-rounded per-day
        # cents from order time — that alone can drift by up to ~$0.005/day even
        # when the tier hasn't actually changed (confirmed live: an 11-day
        # order dropping to 10, still comfortably 10+, produced a phantom
        # -$0.01 "correction" purely from rounding). Scale the no-op
        # tolerance to the day count instead of a flat cent.
        tolerance = max(0.02, 0.01 * remaining_count)
        if abs(correction) < tolerance:
            return None

        # The new volume discount, spread back over the remaining days by
        # their share of the list price (same split order_service uses at
        # checkout). Applied to the payment rows once the wallet correction
        # succeeds, so the rows keep describing what the client now really
        # pays — see _rebase_remaining_payments.
        new_volume_by_payment = {
            p["id"]: (round(new_discount * float(p["original_amount"]) / remaining_original_total, 2)
                      if remaining_original_total > 0 else 0.0)
            for p in remaining_payments if p.get("id") is not None
        }

        return {
            "remaining_count": remaining_count,
            "amount": correction,
            "new_volume_by_payment": new_volume_by_payment,
            "remaining_payments": remaining_payments,
        }

    def _rebase_remaining_payments(self, computed):
        """
        After a volume-discount correction has been charged/credited through
        the wallet, rewrite each remaining day's volume_discount_amount /
        discount_amount to the NEW discount. Without this the rows still
        claim the old discount, so:
          - a second cancellation recomputes from the stale figure and
            claws the same discount back again (double charge), and
          - swap/edit pricing (day_pricing_helpers.paid_share) keeps scaling
            deltas by a discount the client no longer gets.
        `amount` is deliberately NOT touched: it's what was billed/collected
        for the day, and the correction itself already lives in the wallet
        ledger (counted by the Financial page as an adjustment).
        """
        for p in computed["remaining_payments"]:
            pid = p.get("id")
            if pid is None or pid not in computed["new_volume_by_payment"]:
                continue
            new_volume = computed["new_volume_by_payment"][pid]
            promo = float(p.get("promo_discount_amount") or 0)
            self.sb.table("payment").update({
                "volume_discount_amount": new_volume,
                "discount_amount": round(promo + new_volume, 2),
            }).eq("id", pid).execute()

    # ---------- CLIENT-FACING: preview the discount impact before requesting ----------

    def preview_discount_impact(self, user_id, meal_plan_id, meal_plan_day_ids):
        """Lets the client see, before they even submit a cancellation
        request, whether cancelling their selected days would drop the
        order below its volume-discount tier — same math the real
        correction uses at approval time, just phrased as a heads-up."""
        plan_res = (
            self.sb.table("meal_plan")
            .select("id, user_id")
            .eq("id", meal_plan_id)
            .maybe_single()
            .execute()
        )
        plan = plan_res.data if plan_res else None
        if not plan:
            return {"error": "Meal plan not found."}, 404
        if str(plan["user_id"]) != str(user_id):
            return {"error": "This order does not belong to this user."}, 403

        computed = self._compute_discount_correction(meal_plan_id, excluded_day_ids=meal_plan_day_ids)
        if not computed:
            return {"discount_impact": None}, 200

        amount = computed["amount"]
        note = (
            f"Cancelling these days would drop this order to {computed['remaining_count']} active day(s), "
            f"which changes the automatic discount that applies. "
            f"{'You would be charged an extra' if amount > 0 else 'A'} ${abs(amount):.2f} "
            f"{'once approved, to reflect the correct price for the remaining days.' if amount > 0 else 'credit would be added once approved, since the discount actually improves.'}"
        )
        return {"discount_impact": {"amount": amount, "note": note}}, 200

    def _apply_volume_discount_correction(self, meal_plan_id, user_id):
        """
        Cancelling days can drop an order below (or above, in principle) the
        day-count threshold that earned it an automatic volume discount —
        recompute what the REMAINING days should cost under today's rules
        and charge/credit the wallet for the difference. Scoped to orders
        placed after original_amount/discount_amount started being recorded
        on `payment` — older rows have these as null and are skipped, since
        there's nothing reliable to recompute against.
        """
        computed = self._compute_discount_correction(meal_plan_id, excluded_day_ids=set())
        if not computed:
            return None
        remaining_count = computed["remaining_count"]
        correction = computed["amount"]

        note = (
            f"Volume discount adjusted: this order now has {remaining_count} active day(s) after a "
            f"cancellation, which changed the automatic discount that applies. "
            f"{'An extra' if correction > 0 else 'A'} ${abs(correction):.2f} "
            f"{'was charged' if correction > 0 else 'credit was added'} to your wallet to reflect the "
            f"correct price for the remaining days."
        )
        try:
            self.sb.rpc("apply_meal_swap_wallet_delta", {
                "p_user_id": user_id,
                "p_price_delta": correction,
                "p_related_order_id": meal_plan_id,
                "p_note": note,
                "p_type": "volume_discount_adjustment",
            }).execute()
        except Exception as e:
            # The cancellation itself (days/slots) already went through above
            # and must not be undone by this secondary accounting step — most
            # likely cause is the RPC's own insufficient-balance guard (e.g.
            # a debit correction on an empty wallet). Surface it clearly to
            # the admin instead of silently losing it or blowing up decide().
            # This is the only record of the failure server-side — the route
            # only logs a generic "cancellation_decided" success event, so
            # without this line an admin who doesn't read the response body's
            # discount_correction field would never know the wallet is now
            # out of sync with the order.
            logger.critical(
                "Volume discount correction FAILED after cancellation approval for meal_plan_id %s, "
                "user_id %s, amount %s: %s",
                meal_plan_id, user_id, correction, e,
            )
            note = (
                f"Volume discount adjustment needed: this order now has {remaining_count} active day(s) "
                f"after a cancellation, which changed the automatic discount. A ${abs(correction):.2f} "
                f"{'charge' if correction > 0 else 'credit'} should apply, but it could NOT be applied "
                f"automatically ({e}). Handle it manually if needed."
            )
            return {"amount": correction, "note": note, "applied": False}

        try:
            self._rebase_remaining_payments(computed)
        except Exception as e:
            # The money already moved correctly; only the bookkeeping rows
            # are stale. Log loudly so it can be fixed by hand — a second
            # cancellation on this order would otherwise double-correct.
            logger.critical(
                "Volume discount correction applied but payment rows NOT rebased for meal_plan_id %s: %s",
                meal_plan_id, e,
            )

        return {"amount": correction, "note": note, "applied": True}

    def _release_slot(self, delivery_slot_id, delivery_date):
        """Atomic decrement (current_count = current_count - 1), not a
        read-then-write — the confirm_order increment path already has that
        race; this must not add a second instance of it."""
        self.sb.rpc("decrement_delivery_slot_count", {
            "p_delivery_slot_id": delivery_slot_id,
            "p_delivery_date": delivery_date,
        }).execute()
