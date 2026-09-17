# services/order_service.py

import os
import httpx
from utils.supabase_client import supabase, SUPABASE_URL, SUPABASE_KEY
from datetime import datetime, timedelta

DEFAULT_MAX_DELIVERIES = 20

NOTIFY_URL = f"{SUPABASE_URL}/functions/v1/meal-plan-notify"
NOTIFY_WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "akli_webhook_3f9c2b8d_202")


class OrderService:
    def __init__(self):
        self.sb = supabase

    # ---------- PUBLIC ORCHESTRATOR ----------

    def confirm_order(self, user_id, meal_plan, checkout_summary, delivery_slot_id, payment_method=None, delivery_address=None, delivery_address_id=None, wallet_amount_requested=0, wallet_topup_amount=0):
        """
        Flow:
          1) Extract ordered meal days from meal_plan
          2) Determine slot period (AM/PM) from delivery_slots.start_time
          3) Map meal days -> delivery days based on AM/PM logic
          4) Capacity checks & ensure delivery_slots_daily rows (bulk)
          5) Upsert user_delivery_preference
          6) Resolve delivery address (required)
          7) Create deliveries and increment slot counts (for delivery days)
          8) Create meal_plan + meal_plan_day (+ link correct deliveries)
          9) Create payment rows linked to meal_plan_day
        """

        # 1) meal days from meal_plan
        meal_days = [d["date"] for d in (meal_plan.get("days") or []) if "date" in d]
        if not meal_days:
            return {"error": "No meal days found in meal plan."}, 400

        # 2) determine if the slot is AM or PM based on start_time in delivery_slots
        try:
            slot_period = self._get_slot_period(delivery_slot_id)  # "AM" or "PM"
        except ValueError as e:
            return {"error": str(e)}, 400

        # 3) map meal_day -> delivery_day according to your business logic:
        #    - AM slot: deliver on the same calendar day as the meal
        #    - PM slot: deliver the previous calendar day (evening before)
        delivery_days = []
        meal_to_delivery = {}

        for meal_day_str in meal_days:
            meal_date = datetime.strptime(meal_day_str, "%Y-%m-%d").date()

            if slot_period == "AM":
                delivery_date = meal_date
            else:  # "PM"
                delivery_date = meal_date - timedelta(days=1)

            delivery_str = delivery_date.isoformat()
            delivery_days.append(delivery_str)
            meal_to_delivery[meal_day_str] = delivery_str

        # 4) capacity checks (bulk) on DELIVERY days + ensure rows exist; also return slot_day_map
        full_days, slot_day_map = self._check_and_prepare_slot_days(delivery_days, delivery_slot_id)
        if len(full_days) > 2:
            return {
                "error": "Too many selected delivery days are fully booked. Please change your slot.",
                "full_days": full_days,
            }, 400

        # 5) upsert preference
        self._upsert_user_delivery_preference(user_id, delivery_slot_id)

        # 6) resolve delivery address (required) + partner
        resolved_address = self._resolve_delivery_address(user_id, delivery_address_id, delivery_address)
        if not resolved_address:
            return {"error": "A delivery address is required to confirm this order."}, 400

        # 6.5) apply wallet credit, if requested — done BEFORE any other write
        # so an insufficient-balance failure aborts before deliveries/meal_plan/
        # payment rows are touched, not after (this write path is otherwise a
        # series of separate REST calls with no transaction wrapping it all).
        wallet_allocation_by_day = {}
        wallet_amount_requested = float(wallet_amount_requested or 0)
        if wallet_amount_requested > 0:
            daily_breakdown = (checkout_summary.get("price_breakdown") or {}).get("daily_breakdown") or []
            order_total = sum(float(d.get("total_price_with_delivery") or 0) for d in daily_breakdown)
            if order_total <= 0:
                return {"error": "Cannot apply wallet credit to an order with no charge."}, 400
            if wallet_amount_requested > order_total:
                return {"error": "Wallet amount requested exceeds the order total."}, 400

            try:
                self.sb.rpc("spend_wallet", {
                    "p_user_id": user_id,
                    "p_amount": wallet_amount_requested,
                    "p_related_order_id": None,  # meal_plan doesn't exist yet; linked below once it does
                    "p_note": "checkout wallet application",
                }).execute()
            except Exception as e:
                return {"error": f"Wallet spend failed: {e}"}, 400

            remaining = wallet_amount_requested
            for i, d in enumerate(daily_breakdown):
                day_total = float(d.get("total_price_with_delivery") or 0)
                if i == len(daily_breakdown) - 1:
                    share = round(remaining, 2)  # last day absorbs rounding drift
                else:
                    share = round(day_total * wallet_amount_requested / order_total, 2)
                wallet_allocation_by_day[d["date"]] = share
                remaining -= share

        # 7) create deliveries + increment counts (uses DELIVERY days)
        deliveries_map = self._create_deliveries_and_increment_counts(
            user_id=user_id,
            delivery_days=delivery_days,
            delivery_slot_id=delivery_slot_id,
            delivery_address=resolved_address,
            slot_day_map=slot_day_map,
        )

        # 8) persist meal plan bundle & get mapping meal_date -> meal_plan_day_id
        #
        # Steps 7 and 8 are two separate calls, not one transaction — if this
        # one throws, the deliveries rows (and slot-count bump) from step 7
        # are already committed. Left alone, that strands a "pending"
        # delivery with no meal plan, recipes, or payment ever attached
        # (confirmed 3 real occurrences of exactly that in production data).
        # Compensate by best-effort undoing step 7, then re-raise so the
        # route's existing handler still logs and returns the real error.
        try:
            day_to_meal_plan_day_id, meal_plan_record = self._store_meal_plan_bundle(
                user_id=user_id,
                meal_plan=meal_plan,
                deliveries_map=deliveries_map,   # keyed by delivery_date
                meal_to_delivery=meal_to_delivery,  # meal_date -> delivery_date
            )
        except Exception:
            self._rollback_deliveries(deliveries_map, delivery_days, delivery_slot_id)
            raise

        # 9) payment — a wallet top-up amount (new money the client is
        # adding to their wallet, on top of paying for the order) rides on
        # the LAST day's payment row so what's actually collected reconciles;
        # it is NOT credited to the wallet here (payment starts "pending" for
        # cash/Whish/Neo — crediting before that would create wallet money
        # that doesn't exist yet). It's credited once admin marks the order's
        # payment "paid" (see admin/financial/actions.ts::setPaymentStatus).
        wallet_topup_amount = min(round(float(wallet_topup_amount or 0), 2), 100.0)
        self._create_payment_record(
            ordered_user_id=user_id,
            checkout_summary=checkout_summary,
            day_to_meal_plan_day_id=day_to_meal_plan_day_id,
            payment_method=payment_method,
            wallet_allocation_by_day=wallet_allocation_by_day,
            topup_amount=wallet_topup_amount,
        )

        if wallet_topup_amount > 0:
            self.sb.table("wallet_checkout_topup").insert({
                "meal_plan_id": meal_plan_record["id"],
                "user_id": user_id,
                "amount": wallet_topup_amount,
                "credited": False,
            }).execute()

        # Link the wallet debit (created in step 6.5, before meal_plan existed)
        # back to the order now that it has an id — best-effort, purely for
        # traceability, doesn't affect the balance guard already enforced.
        if wallet_amount_requested > 0:
            try:
                unlinked = (
                    self.sb.table("wallet_transactions")
                    .select("id")
                    .eq("user_id", user_id)
                    .eq("type", "checkout_spend")
                    .is_("related_order_id", "null")
                    .order("created_at", desc=True)
                    .limit(1)
                    .execute()
                    .data
                )
                if unlinked:
                    self.sb.table("wallet_transactions").update(
                        {"related_order_id": meal_plan_record["id"]}
                    ).eq("id", unlinked[0]["id"]).execute()
            except Exception:
                pass

        # 10) notify admin now that payment rows exist (the order email needs
        # the payment method, which isn't written until step 9 — sending the
        # notification here instead of via a meal_plan INSERT trigger avoids
        # firing before the payment row exists)
        self._notify_order_email(meal_plan_record)
        # Save promo_code_usage if promo was valid
        price_info = checkout_summary.get("price_breakdown", {})
        promo_code_id = price_info.get("promo_code_id")
        promo_status = price_info.get("promo_code_status")

        if promo_code_id and promo_status == "valid":
            supabase.table("promo_code_usage").insert({
                "user_id": user_id,
                "promo_code_id": promo_code_id
            }).execute()


        return {
            "success": True,
            "message": "Order successfully confirmed.",
            "order_id": meal_plan_record["id"],
        }, 200

    # ---------- SLOT PERIOD HELPER ----------

    def _get_slot_period(self, delivery_slot_id):
        """
        Look up delivery_slots.start_time and infer whether the slot is AM or PM.
        Assumes start_time is stored as "HH:MM" (24h format).
        Returns "AM" or "PM".
        """
        res = (
            self.sb.table("delivery_slots")
            .select("start_time")
            .eq("id", delivery_slot_id)
            .execute()
        )

        data = res.data or []
        if not data:
            raise ValueError("Delivery slot not found.")

        start_time_str = data[0].get("start_time")
        if not start_time_str:
            raise ValueError("Delivery slot start_time is missing.")

        # Parse "HH:MM"
        try:
            hour = int(start_time_str.split(":")[0])
        except Exception:
            raise ValueError("Invalid start_time format for delivery slot.")

        return "AM" if hour < 12 else "PM"

    # ---------- HELPERS ----------

    def _check_and_prepare_slot_days(self, delivery_days, delivery_slot_id):
        """
        Bulk version:
        - Fetch existing delivery_slots_daily rows for all delivery_days.
        - Insert missing rows in one batch.
        - Return:
            full_days: list of days where current_count >= max_deliveries
            slot_day_map: {delivery_date: row}
        """
        # Fetch existing rows in one query
        res = (
            self.sb.table("delivery_slots_daily")
            .select("*")
            .eq("delivery_slot_id", delivery_slot_id)
            .in_("delivery_date", delivery_days)
            .execute()
        )
        slot_day_map = {row["delivery_date"]: row for row in (res.data or [])}

        # Find missing days
        missing_days = [d for d in delivery_days if d not in slot_day_map]

        # Insert missing rows in batch
        if missing_days:
            now = datetime.utcnow().isoformat()
            insert_payload = [
                {
                    "delivery_slot_id": delivery_slot_id,
                    "delivery_date": d,
                    "current_count": 0,
                    "max_deliveries": DEFAULT_MAX_DELIVERIES,
                    "created_at": now,
                }
                for d in missing_days
            ]
            insert_res = (
                self.sb.table("delivery_slots_daily")
                .insert(insert_payload)
                .execute()
            )
            for row in insert_res.data or []:
                slot_day_map[row["delivery_date"]] = row

        # Now compute full_days using the combined map
        full_days = []
        for d in delivery_days:
            row = slot_day_map.get(d)
            if not row:
                continue
            cur = row.get("current_count") or 0
            mx = row.get("max_deliveries") or DEFAULT_MAX_DELIVERIES
            if cur >= mx:
                full_days.append(d)

        return full_days, slot_day_map

    def _upsert_user_delivery_preference(self, user_id, delivery_slot_id):
        """
        Keep safe logic: select then update/insert.
        This is called rarely, so performance impact is small.
        """
        res = (
            self.sb.table("user_delivery_preference")
            .select("id, delivery_slot_id")
            .eq("user_id", user_id)
            .execute()
        )

        now = datetime.utcnow().isoformat()

        if res.data:
            pref = res.data[0]
            if pref.get("delivery_slot_id") != delivery_slot_id:
                self.sb.table("user_delivery_preference").update(
                    {
                        "delivery_slot_id": delivery_slot_id,
                        "updated_at": now,
                    }
                ).eq("id", pref["id"]).execute()
        else:
            self.sb.table("user_delivery_preference").insert(
                {
                    "user_id": user_id,
                    "delivery_slot_id": delivery_slot_id,
                    "created_at": now,
                }
            ).execute()

    def _resolve_delivery_address(self, user_id, delivery_address_id=None, delivery_address_text=None):
        """
        Resolve the delivery address text to stamp onto deliveries rows.
        Priority:
          1) explicit delivery_address_id -> user_delivery_address row (must belong to user)
          2) explicit delivery_address_text override from frontend
          3) user's saved default address in user_delivery_address
        Returns the address text, or None if nothing could be resolved.
        """
        if delivery_address_id:
            res = (
                self.sb.table("user_delivery_address")
                .select("address_text")
                .eq("id", delivery_address_id)
                .eq("user_id", user_id)
                .limit(1)
                .execute()
            )
            if res.data:
                return res.data[0]["address_text"]

        if delivery_address_text:
            return delivery_address_text

        default_res = (
            self.sb.table("user_delivery_address")
            .select("address_text")
            .eq("user_id", user_id)
            .eq("is_default", True)
            .limit(1)
            .execute()
        )
        if default_res.data:
            return default_res.data[0]["address_text"]

        return None

    def _create_deliveries_and_increment_counts(
        self,
        user_id,
        delivery_days,
        delivery_slot_id,
        delivery_address,
        slot_day_map,
    ):
        """
        Batched version of the above: one bulk upsert for existing
        delivery_slots_daily count bumps, one bulk insert for all deliveries
        rows — instead of 2 sequential REST calls PER delivery day. A large
        order used to mean 200+ sequential round trips in this method alone;
        this brings it down to 2-3 regardless of order length (see
        _store_meal_plan_bundle for why that matters: enough of those adding
        up made real orders exceed both gunicorn's and Heroku's router's
        30s request timeouts, silently truncating the write mid-order).
        Return {delivery_date: delivery_id}
        """
        now = datetime.utcnow().isoformat()

        # Bump current_count for days whose delivery_slots_daily row already
        # existed (bulk upsert — one call for every day, not one call each).
        slot_update_payload = []
        for day in delivery_days:
            slot_day = slot_day_map.get(day)
            if not slot_day:
                continue
            cur = (slot_day.get("current_count") or 0) + 1
            mx = slot_day.get("max_deliveries") or DEFAULT_MAX_DELIVERIES
            if cur > mx:
                cur = mx  # clamp, though we already checked capacity
            slot_update_payload.append({"id": slot_day["id"], "current_count": cur, "updated_at": now})
        if slot_update_payload:
            self.sb.table("delivery_slots_daily").upsert(slot_update_payload).execute()

        # Extremely rare (rows were already created in bulk earlier): handle
        # any still-missing day individually, one at a time is fine here.
        for day in delivery_days:
            if slot_day_map.get(day):
                continue
            ins = (
                self.sb.table("delivery_slots_daily")
                .insert(
                    {
                        "delivery_slot_id": delivery_slot_id,
                        "delivery_date": day,
                        "current_count": 1,
                        "max_deliveries": DEFAULT_MAX_DELIVERIES,
                        "created_at": now,
                    }
                )
                .execute()
            )
            slot_day_map[day] = ins.data[0] if ins.data else None

        # Bulk-insert all deliveries rows in one call, matched back by
        # delivery_date (unique within this single order's delivery_days).
        delivery_payload = [
            {
                "user_id": user_id,
                "delivery_date": day,
                "delivery_slot_id": delivery_slot_id,
                "delivery_address": delivery_address,
                "status": "pending",
                "created_at": now,
            }
            for day in delivery_days
        ]
        delivery_rows = (
            self.sb.table("deliveries").insert(delivery_payload).execute().data
            if delivery_payload
            else []
        )
        deliveries_map = {row["delivery_date"]: row["id"] for row in delivery_rows}
        return deliveries_map

    def _rollback_deliveries(self, deliveries_map, delivery_days, delivery_slot_id):
        """Compensation for a failed _store_meal_plan_bundle: delete the
        deliveries rows _create_deliveries_and_increment_counts just created
        and revert the delivery_slots_daily.current_count bump that came with
        them. Best-effort — this runs from an except block reacting to an
        already-failed write, so a cleanup failure here is logged rather than
        raised; the original exception is what the caller needs to see, and
        is re-raised by the caller right after this returns."""
        delivery_ids = list(deliveries_map.values())
        if delivery_ids:
            try:
                self.sb.table("deliveries").delete().in_("id", delivery_ids).execute()
            except Exception as cleanup_err:
                print(f"[order_service] rollback: failed to delete stranded deliveries {delivery_ids}: {cleanup_err}")

        for day in delivery_days:
            if day not in deliveries_map:
                continue  # this day's delivery row was never created — nothing to revert
            try:
                row = (
                    self.sb.table("delivery_slots_daily")
                    .select("id, current_count")
                    .eq("delivery_slot_id", delivery_slot_id)
                    .eq("delivery_date", day)
                    .limit(1)
                    .execute()
                    .data
                )
                if row:
                    new_count = max((row[0].get("current_count") or 0) - 1, 0)
                    self.sb.table("delivery_slots_daily").update(
                        {"current_count": new_count}
                    ).eq("id", row[0]["id"]).execute()
            except Exception as cleanup_err:
                print(f"[order_service] rollback: failed to revert slot count for {day}: {cleanup_err}")

    def _store_meal_plan_bundle(self, user_id, meal_plan, deliveries_map, meal_to_delivery):
        """
        Insert meal_plan, per-day rows (with status + correct delivery_id),
        update deliveries.meal_plan_day_id, then recipes & subrecipes.

        Batched: this used to be ~13 sequential REST calls PER DAY (a plain
        18-day order was 200+ round trips in one request). That routinely
        pushed real orders past both gunicorn's worker timeout AND Heroku's
        own (non-configurable) 30s router timeout — in one confirmed
        incident it got a request killed mid-write, leaving days/recipes
        written but payment (created after this function returns) never
        reached at all. Every insert/update below is now one bulk call
        across the whole order regardless of how many days it has, so the
        full write finishes in roughly constant time instead of scaling
        with day count.

        Returns:
          day_to_meal_plan_day_id: {meal_date_str: meal_plan_day_id}
          meal_plan_record: the inserted meal_plan row
        """
        now = datetime.utcnow().isoformat()
        days = meal_plan.get("days") or []

        # meal_plan (single row — nothing to batch here)
        plan_ins = (
            self.sb.table("meal_plan")
            .insert(
                {
                    "user_id": user_id,
                    "start_date": meal_plan["start_date"],
                    "end_date": meal_plan["end_date"],
                    "created_at": now,
                }
            )
            .execute()
        )
        meal_plan_record = plan_ins.data[0]
        plan_id = meal_plan_record["id"]

        # 1️⃣ Bulk-create every meal_plan_day row, matched back by date
        # (unique within this one meal_plan's own day list).
        day_payload = []
        for day in days:
            meal_date_str = day["date"]
            delivery_date_str = meal_to_delivery.get(meal_date_str)
            delivery_id = deliveries_map.get(delivery_date_str) if delivery_date_str else None
            day_payload.append(
                {
                    "meal_plan_id": plan_id,
                    "date": meal_date_str,
                    "delivery_id": delivery_id,
                    "status": "pending",
                    "created_at": now,
                }
            )
        day_rows = self.sb.table("meal_plan_day").insert(day_payload).execute().data if day_payload else []
        day_to_meal_plan_day_id = {row["date"]: row["id"] for row in day_rows}

        # 2️⃣ Bulk-create every daily_macro_order row, matched back by for_date.
        dmo_payload = []
        for day in days:
            meal_date_str = day["date"]
            totals = day.get("totals") or {}
            dmo_payload.append(
                {
                    "user_id": user_id,
                    "meal_plan_day_id": day_to_meal_plan_day_id[meal_date_str],
                    "for_date": meal_date_str,
                    "protein_ordered": totals.get("protein"),
                    "carbs_ordered": totals.get("carbs"),
                    "fat_ordered": totals.get("fat"),
                    "kcal_ordered": totals.get("kcal"),
                    "saturated_fat_ordered": (totals.get("saturated") if "saturated" in totals else None),
                    "fiber_ordered": totals.get("fiber"),
                    "sugar_ordered": totals.get("sugar"),
                    "created_at": now,
                }
            )
        dmo_rows = self.sb.table("daily_macro_order").insert(dmo_payload).execute().data if dmo_payload else []
        date_to_dmo_id = {row["for_date"]: row["id"] for row in dmo_rows}

        # 3️⃣ Bulk-update meal_plan_day.daily_macro_order_id (one upsert call
        # carrying every day's id + new value, instead of one UPDATE per day).
        mpd_update_payload = [
            {"id": mpd_id, "daily_macro_order_id": date_to_dmo_id[date_str], "updated_at": now}
            for date_str, mpd_id in day_to_meal_plan_day_id.items()
            if date_str in date_to_dmo_id
        ]
        if mpd_update_payload:
            self.sb.table("meal_plan_day").upsert(mpd_update_payload).execute()

        # 4️⃣ Bulk-update deliveries.meal_plan_day_id the same way.
        delivery_update_payload = []
        for day in days:
            meal_date_str = day["date"]
            delivery_date_str = meal_to_delivery.get(meal_date_str)
            delivery_id = deliveries_map.get(delivery_date_str) if delivery_date_str else None
            if delivery_id:
                delivery_update_payload.append(
                    {"id": delivery_id, "meal_plan_day_id": day_to_meal_plan_day_id[meal_date_str], "updated_at": now}
                )
        if delivery_update_payload:
            self.sb.table("deliveries").upsert(delivery_update_payload).execute()

        # 5️⃣ Bulk-create every meal_plan_day_recipe row across every day,
        # matched back by (meal_plan_day_id, meal_type) — unique per day in
        # this system (one recipe per meal slot per day).
        recipe_payload = []
        for day in days:
            mpd_id = day_to_meal_plan_day_id[day["date"]]
            for meal in (day.get("meals") or []):
                recipe_payload.append(
                    {
                        "meal_plan_day_id": mpd_id,
                        "recipe_id": meal["recipe_id"],
                        "meal_type": meal.get("meal_type"),
                        "cooking_status": "pending",
                        "packaging_status": "pending",
                        "created_at": now,
                    }
                )
        recipe_rows = (
            self.sb.table("meal_plan_day_recipe").insert(recipe_payload).execute().data
            if recipe_payload
            else []
        )
        key_to_mpdr_id = {(row["meal_plan_day_id"], row["meal_type"]): row["id"] for row in recipe_rows}

        # 6️⃣ Bulk-create every meal_plan_day_recipe_serving row across every
        # recipe across every day, in one final call.
        serving_payload = []
        for day in days:
            mpd_id = day_to_meal_plan_day_id[day["date"]]
            for meal in (day.get("meals") or []):
                mpdr_id = key_to_mpdr_id[(mpd_id, meal.get("meal_type"))]
                for sub in (meal.get("subrecipes") or []):
                    sub_macros = sub.get("macros") or {}
                    serving_payload.append(
                        {
                            "meal_plan_day_recipe_id": mpdr_id,
                            "subrecipe_id": sub["subrecipe_id"],
                            "recipe_subrecipe_serving_calculated": sub.get("servings"),
                            "kcal_calculated": sub_macros.get("kcal"),
                            "protein_calculated": sub_macros.get("protein"),
                            "carbs_calculated": sub_macros.get("carbs"),
                            "fat_calculated": sub_macros.get("fat"),
                            "cooking_status": "pending",
                            "portioning_status": "pending",
                            "created_at": now,
                        }
                    )
        if serving_payload:
            self.sb.table("meal_plan_day_recipe_serving").insert(serving_payload).execute()

        return day_to_meal_plan_day_id, meal_plan_record

    def _notify_order_email(self, meal_plan_record):
        """Trigger the admin order-confirmation email now that the payment
        row for this order has been written."""
        try:
            httpx.post(
                NOTIFY_URL,
                json={
                    "type": "INSERT",
                    "schema": "public",
                    "table": "meal_plan",
                    "record": meal_plan_record,
                },
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {SUPABASE_KEY}",
                    "x-webhook-secret": NOTIFY_WEBHOOK_SECRET,
                },
                timeout=5,
            )
        except httpx.HTTPError:
            pass  # order is already confirmed; don't fail the request over a notify error

    def _create_payment_record(
        self,
        ordered_user_id,
        checkout_summary,
        day_to_meal_plan_day_id,
        payment_method=None,
        wallet_allocation_by_day=None,
        topup_amount=0,
    ):
        """
        Create one payment per meal day, linked to meal_plan_day.
        Uses in-memory map instead of SELECT per day.

        Affiliate commission (if the order used an affiliate's promo code) is
        snapshotted onto each payment row at the rate resolved by
        promo_service at checkout time, so later rate changes don't affect
        already-placed orders.
        """
        price_breakdown = checkout_summary.get("price_breakdown") or {}
        daily_breakdown = price_breakdown.get("daily_breakdown") or []
        affiliate_id = price_breakdown.get("affiliate_id")
        commission_rate = price_breakdown.get("commission_rate")
        wallet_allocation_by_day = wallet_allocation_by_day or {}

        # Order-level discount totals, to be allocated across days below in
        # proportion to each day's own pre-discount price — same
        # distribution the discount_ratio in checkout_summary.py already
        # applies uniformly. Needed so a later partial cancellation can
        # recompute and correct the volume-discount portion alone, without
        # touching the promo-code portion (see CancellationService.
        # _compute_discount_correction).
        order_total_before_discount = float(price_breakdown.get("total_price_before_discount") or 0)
        order_volume_discount = float((price_breakdown.get("volume_discount") or {}).get("amount") or 0)

        now = datetime.utcnow().isoformat()
        topup_amount = round(float(topup_amount or 0), 2)

        payment_payload = []
        for i, day_data in enumerate(daily_breakdown):
            date_str = day_data.get("date")

            # ✅ Always charge the final amount for that day (incl. delivery)
            amount = day_data.get("total_price_with_delivery")
            if amount is None:
                # backward-compatible fallback
                amount = (day_data.get("total_price") or 0) + (day_data.get("delivery_fee") or 0)

            amount = round(float(amount), 2)
            wallet_applied = round(float(wallet_allocation_by_day.get(date_str) or 0), 2)
            amount = round(amount - wallet_applied, 2)

            # A wallet top-up (new money, not a discount) rides on the LAST
            # day's payment row so the total actually collected reconciles —
            # tracked separately in wallet_checkout_topup, never split across
            # days (that would corrupt each day's own commission/wallet math).
            if topup_amount > 0 and i == len(daily_breakdown) - 1:
                amount = round(amount + topup_amount, 2)

            meal_plan_day_id = day_to_meal_plan_day_id.get(date_str)
            if not meal_plan_day_id:
                raise ValueError(f"Missing meal_plan_day_id for date {date_str}")

            # Commission is earned on the food price only, not the delivery
            # fee — day_data["total_price"] is the post-discount meal price
            # before delivery is added (see checkout_summary.py STEP 3/4).
            commission_base = float(day_data.get("total_price") or 0)
            commission_amount = (
                round(commission_base * float(commission_rate), 2)
                if affiliate_id and commission_rate is not None
                else None
            )

            # Snapshot this day's pre-discount price, discount share, and
            # delivery fee — untouched by wallet-spend/topup below, and the
            # only way a later cancellation can tell whether the remaining
            # days still qualify for the volume discount they originally got
            # (see CancellationService._apply_volume_discount_correction).
            original_amount = day_data.get("original_total_price")
            discount_amount = round(
                float(day_data.get("original_total_price") or 0) - float(day_data.get("total_price") or 0), 2
            )
            delivery_fee_amount = round(float(day_data.get("delivery_fee") or 0), 2)

            # This day's share of the order-level volume discount, by its
            # share of the order's pre-discount total. promo_discount_amount
            # is the remainder rather than an independent proportional
            # calc, so the two always sum to exactly discount_amount above
            # (no rounding drift between the split and the total).
            if order_total_before_discount > 0:
                day_share = float(original_amount or 0) / order_total_before_discount
                volume_discount_amount = round(order_volume_discount * day_share, 2)
            else:
                volume_discount_amount = 0.0
            promo_discount_amount = round(discount_amount - volume_discount_amount, 2)

            payment_payload.append(
                {
                    "ordered_user_id": ordered_user_id,
                    "amount": amount,
                    "wallet_amount_applied": wallet_applied,
                    "status": "pending",
                    "provider": payment_method,
                    "provider_payment_id": None,
                    "currency": "USD",
                    "meal_plan_day_id": meal_plan_day_id,
                    "affiliate_id": affiliate_id,
                    "commission_rate": commission_rate,
                    "commission_amount": commission_amount,
                    "original_amount": original_amount,
                    "discount_amount": discount_amount,
                    "volume_discount_amount": volume_discount_amount,
                    "promo_discount_amount": promo_discount_amount,
                    "delivery_fee_amount": delivery_fee_amount,
                    "created_at": now,
                }
            )

        # One bulk insert for every day's payment row instead of one REST
        # call per day — see _store_meal_plan_bundle for why that matters.
        if payment_payload:
            self.sb.table("payment").insert(payment_payload).execute()

