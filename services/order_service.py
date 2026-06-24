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

    def confirm_order(self, user_id, meal_plan, checkout_summary, delivery_slot_id, payment_method=None, delivery_address=None, delivery_address_id=None):
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

        # 7) create deliveries + increment counts (uses DELIVERY days)
        deliveries_map = self._create_deliveries_and_increment_counts(
            user_id=user_id,
            delivery_days=delivery_days,
            delivery_slot_id=delivery_slot_id,
            delivery_address=resolved_address,
            slot_day_map=slot_day_map,
        )

        # 8) persist meal plan bundle & get mapping meal_date -> meal_plan_day_id
        day_to_meal_plan_day_id, meal_plan_record = self._store_meal_plan_bundle(
            user_id=user_id,
            meal_plan=meal_plan,
            deliveries_map=deliveries_map,   # keyed by delivery_date
            meal_to_delivery=meal_to_delivery,  # meal_date -> delivery_date
        )

        # 9) payment
        self._create_payment_record(
            ordered_user_id=user_id,
            checkout_summary=checkout_summary,
            day_to_meal_plan_day_id=day_to_meal_plan_day_id,
            payment_method=payment_method,
        )

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


        return {"success": True, "message": "Order successfully confirmed."}, 200

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
        For each DELIVERY day:
          - increment current_count in delivery_slots_daily (no extra SELECTs)
          - insert deliveries row
        Return {delivery_date: delivery_id}
        """
        deliveries_map = {}
        now = datetime.utcnow().isoformat()

        for day in delivery_days:
            slot_day = slot_day_map.get(day)

            if slot_day:
                cur = (slot_day.get("current_count") or 0) + 1
                mx = slot_day.get("max_deliveries") or DEFAULT_MAX_DELIVERIES
                if cur > mx:
                    cur = mx  # clamp, though we already checked capacity
                (
                    self.sb.table("delivery_slots_daily")
                    .update(
                        {
                            "current_count": cur,
                            "updated_at": now,
                        }
                    )
                    .eq("id", slot_day["id"])
                    .execute()
                )
            else:
                # Extremely rare because we created missing rows earlier
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
                slot_day = ins.data[0] if ins.data else None

            # insert delivery
            delivery_ins = (
                self.sb.table("deliveries")
                .insert(
                    {
                        "user_id": user_id,
                        "delivery_date": day,
                        "delivery_slot_id": delivery_slot_id,
                        "delivery_address": delivery_address,
                        "status": "pending",
                        "created_at": now,
                    }
                )
                .execute()
            )

            delivery_id = delivery_ins.data[0]["id"]
            deliveries_map[day] = delivery_id

        return deliveries_map

    def _store_meal_plan_bundle(self, user_id, meal_plan, deliveries_map, meal_to_delivery):
        """
        Insert meal_plan, per-day rows (with status + correct delivery_id),
        update deliveries.meal_plan_day_id, then recipes & subrecipes.
        Returns:
          day_to_meal_plan_day_id: {meal_date_str: meal_plan_day_id}
          meal_plan_record: the inserted meal_plan row
        """
        now = datetime.utcnow().isoformat()

        # meal_plan
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

        day_to_meal_plan_day_id = {}

        for day in (meal_plan.get("days") or []):
            meal_date_str = day["date"]
            totals = day.get("totals") or {}

            # find the delivery date for this meal_date, then the delivery_id
            delivery_date_str = meal_to_delivery.get(meal_date_str)
            delivery_id = deliveries_map.get(delivery_date_str) if delivery_date_str else None

            # 1️⃣ Create meal_plan_day
            day_ins = (
                self.sb.table("meal_plan_day")
                .insert(
                    {
                        "meal_plan_id": plan_id,
                        "date": meal_date_str,
                        "delivery_id": delivery_id,
                        "status": "pending",  # assuming this column still exists
                        "created_at": now,
                    }
                )
                .execute()
            )
            meal_plan_day_id = day_ins.data[0]["id"]
            day_to_meal_plan_day_id[meal_date_str] = meal_plan_day_id

            # 2️⃣ Create daily_macro_order
            daily_macro_ins = (
                self.sb.table("daily_macro_order")
                .insert(
                    {
                        "user_id": user_id,
                        "meal_plan_day_id": meal_plan_day_id,
                        "for_date": meal_date_str,
                        "protein_ordered": totals.get("protein"),
                        "carbs_ordered": totals.get("carbs"),
                        "fat_ordered": totals.get("fat"),
                        "kcal_ordered": totals.get("kcal"),
                        "saturated_fat_ordered": (
                            totals.get("saturated")
                            if "saturated" in totals
                            else None
                        ),
                        "fiber_ordered": totals.get("fiber"),
                        "sugar_ordered": totals.get("sugar"),
                        "created_at": now,
                    }
                )
                .execute()
            )
            daily_macro_order_id = daily_macro_ins.data[0]["id"]

            # 3️⃣ Update meal_plan_day to include daily_macro_order_id
            (
                self.sb.table("meal_plan_day")
                .update(
                    {
                        "daily_macro_order_id": daily_macro_order_id,
                        "updated_at": now,
                    }
                )
                .eq("id", meal_plan_day_id)
                .execute()
            )

            # 4️⃣ Back-link on deliveries (delivery -> meal_plan_day)
            if delivery_id:
                (
                    self.sb.table("deliveries")
                    .update(
                        {
                            "meal_plan_day_id": meal_plan_day_id,
                            "updated_at": now,
                        }
                    )
                    .eq("id", delivery_id)
                    .execute()
                )

            # 5️⃣ Recipes + subrecipes
            for meal in (day.get("meals") or []):
                rec_ins = (
                    self.sb.table("meal_plan_day_recipe")
                    .insert(
                        {
                            "meal_plan_day_id": meal_plan_day_id,
                            "recipe_id": meal["recipe_id"],
                            "meal_type": meal.get("meal_type"),
                            "cooking_status": "pending",    # updated schema
                            "packaging_status": "pending",  # updated schema
                            "created_at": now,
                        }
                    )
                    .execute()
                )
                mpdr_id = rec_ins.data[0]["id"]

                for sub in (meal.get("subrecipes") or []):
                    sub_macros = sub.get("macros") or {}
                    (
                        self.sb.table("meal_plan_day_recipe_serving")
                        .insert(
                            {
                                "meal_plan_day_recipe_id": mpdr_id,
                                "subrecipe_id": sub["subrecipe_id"],
                                "recipe_subrecipe_serving_calculated": sub.get("servings"),
                                "kcal_calculated": sub_macros.get("kcal"),
                                "protein_calculated": sub_macros.get("protein"),
                                "carbs_calculated": sub_macros.get("carbs"),
                                "fat_calculated": sub_macros.get("fat"),
                                "cooking_status": "pending",     # per-serving cooking
                                "portioning_status": "pending",  # per-serving portioning
                                "created_at": now,
                            }
                        )
                        .execute()
                    )

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

        now = datetime.utcnow().isoformat()

        for day_data in daily_breakdown:
            date_str = day_data.get("date")

            # ✅ Always charge the final amount for that day (incl. delivery)
            amount = day_data.get("total_price_with_delivery")
            if amount is None:
                # backward-compatible fallback
                amount = (day_data.get("total_price") or 0) + (day_data.get("delivery_fee") or 0)

            amount = round(float(amount), 2)

            meal_plan_day_id = day_to_meal_plan_day_id.get(date_str)
            if not meal_plan_day_id:
                raise ValueError(f"Missing meal_plan_day_id for date {date_str}")

            commission_amount = (
                round(amount * float(commission_rate), 2)
                if affiliate_id and commission_rate is not None
                else None
            )

            (
                self.sb.table("payment")
                .insert(
                    {
                        "ordered_user_id": ordered_user_id,
                        "amount": amount,
                        "status": "pending",
                        "provider": payment_method,
                        "provider_payment_id": None,
                        "currency": "USD",
                        "meal_plan_day_id": meal_plan_day_id,
                        "affiliate_id": affiliate_id,
                        "commission_rate": commission_rate,
                        "commission_amount": commission_amount,
                        "created_at": now,
                    }
                )
                .execute()
            )

