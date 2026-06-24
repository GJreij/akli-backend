from flask import Blueprint, request, jsonify
import statistics
from utils.supabase_client import supabase
from services.promo_service import validate_and_apply_promo_code
from services.volume_discount_service import apply_volume_discount
from utils.event_logger import log_event

checkout_bp = Blueprint("checkout", __name__)

# -------------------------------
# CONFIG
# -------------------------------
DELIVERY_DAY_MINIMUM = 25  # if a given day's total < 25, delivery applies for that day


def get_kcal_discount(kcal):
    min_kcal = 1200
    max_kcal = 3000
    max_discount = 0.15

    if kcal <= min_kcal:
        return 0.0
    if kcal >= max_kcal:
        return max_discount
    
    ratio = (kcal - min_kcal) / (max_kcal - min_kcal)
    return ratio * max_discount


@checkout_bp.route("/checkout_summary", methods=["POST"])
def checkout_summary():
    data = request.get_json()
    user_id = data.get("user_id")
    plan = data.get("final_plan")
    promo_code = data.get("promo_code")

    if not user_id or not plan:
        return jsonify({"error": "Missing user_id or final_plan"}), 400

    days = plan.get("days", [])
    if not days:
        return jsonify({"error": "Plan is empty"}), 400

    number_of_days = len(days)

    # ------------------------------------------------------------------
    # STEP 1 — Fetch pricing
    # ------------------------------------------------------------------
    try:
        price_resp = (
            supabase.table("macro_price")
            .select("*")
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        )
        if not price_resp.data:
            raise ValueError("No pricing data found")
        price_data = price_resp.data[0]
    except Exception as e:
        log_event(user_id, "api_error", {"route": "/checkout_summary", "status_code": 500, "error": str(e)})
        return jsonify({"error": f"Failed to fetch pricing data: {str(e)}"}), 500

    protein_price = price_data.get("proteing_g_price", 0) or 0
    carbs_price = price_data.get("carbs_g_price", 0) or 0
    fat_price = price_data.get("fat_g_price", 0) or 0
    day_packaging_price = price_data.get("day_packaging_price", 0) or 0
    recipe_packaging_price = price_data.get("recipe_packaging_price", 0) or 0
    subrecipe_packaging_price = price_data.get("subrecipe_packaging_price", 0) or 0
    delivery_price_per_day = price_data.get("delivery_price", 0) or 0


    # ------------------------------------------------------------------
    # STEP 2 — Aggregate macros & base pricing
    #
    # NOTE on billing model: pricing is per-gram (protein/carbs/fat * price
    # per gram + packaging), and that formula is unchanged here. What
    # changed is the UNIT it's applied to: grams are now summed across the
    # whole week FIRST, and the per-gram formula is applied ONCE to the
    # weekly total — instead of computing 7 separate daily prices and
    # showing all 7 to the customer. Daily prices are still computed below
    # (kitchen/ops + delivery-per-day logic depends on them) but they are no
    # longer the customer-facing billing unit.
    # ------------------------------------------------------------------
    kcal_values, protein_values, carbs_values, fat_values = [], [], [], []
    total_meals = 0
    total_price = 0
    daily_price_details = []

    # Weekly actual-vs-target accuracy (Task 2) and weekly gram totals
    # (Task 3) — summed across all days as we walk the loop.
    week_actual = {"protein": 0.0, "carbs": 0.0, "fat": 0.0, "kcal": 0.0}
    week_target = {"protein": 0.0, "carbs": 0.0, "fat": 0.0, "kcal": 0.0}

    for day in days:
        totals = day.get("totals", {})
        day_price = day_packaging_price

        if totals:
            kcal_values.append(totals.get("kcal", 0))
            protein_values.append(totals.get("protein", 0))
            carbs_values.append(totals.get("carbs", 0))
            fat_values.append(totals.get("fat", 0))

            week_actual["protein"] += totals.get("protein", 0) or 0
            week_actual["carbs"]   += totals.get("carbs", 0) or 0
            week_actual["fat"]     += totals.get("fat", 0) or 0
            week_actual["kcal"]    += totals.get("kcal", 0) or 0

        # Target used for this day: prefer the day's own adjusted_target
        # (set by the weekly-carryover solver pipeline), fall back to the
        # plan-root daily_macro_target so older plans without per-day
        # targets still produce a sensible weekly accuracy figure.
        day_target = day.get("adjusted_target") or plan.get("daily_macro_target") or {}
        week_target["protein"] += float(day_target.get("protein_g") or 0)
        week_target["carbs"]   += float(day_target.get("carbs_g") or 0)
        week_target["fat"]     += float(day_target.get("fat_g") or 0)
        week_target["kcal"]    += float(day_target.get("kcal") or 0)

        for meal in day.get("meals", []):
            total_meals += 1

            macros = meal.get("macros") or {}
            p = macros.get("protein", 0) or 0
            c = macros.get("carbs", 0) or 0
            f = macros.get("fat", 0) or 0


            base_macro_cost = p * protein_price + c * carbs_price + f * fat_price
            discount_pct = get_kcal_discount(totals.get("kcal", 0))
            macro_cost = base_macro_cost * (1 - discount_pct)

            recipe_cost = recipe_packaging_price
            sub_pack_cost = len(meal.get("subrecipes", [])) * subrecipe_packaging_price

            day_price += macro_cost + recipe_cost + sub_pack_cost

        total_price += day_price
        daily_price_details.append({
            "date": day["date"],
            "total_price": round(day_price, 2),
            "meals": len(day.get("meals", []))
        })

    # ------------------------------------------------------------------
    # STEP 2b — Weekly price computed from SUMMED grams (Task 3)
    #
    # Same exact per-gram formula as above (base_macro_cost + kcal
    # discount + packaging), just applied once to the week's total grams
    # instead of once per day. Packaging is summed per-day-occurred since
    # day/recipe/subrecipe containers are a real per-day operational cost
    # regardless of billing granularity.
    # ------------------------------------------------------------------
    week_base_macro_cost = (
        week_actual["protein"] * protein_price
        + week_actual["carbs"] * carbs_price
        + week_actual["fat"] * fat_price
    )
    week_discount_pct = get_kcal_discount(
        (week_actual["kcal"] / number_of_days) if number_of_days else 0
    )
    week_macro_cost = week_base_macro_cost * (1 - week_discount_pct)

    # ------------------------------------------------------------------
    # STEP 2c — Weekly accuracy: actual vs target, as % of goal per macro
    # (Task 2). tolerance_used is computed per-day inside the solver but
    # was never surfaced to the frontend; this is the disclosed, weekly,
    # human-readable form of it.
    # ------------------------------------------------------------------
    def _pct_of_goal(actual: float, target: float) -> int:
        if not target:
            return 100 if not actual else 0
        return round((actual / target) * 100)

    weekly_accuracy = {
        "protein_pct": _pct_of_goal(week_actual["protein"], week_target["protein"]),
        "carbs_pct":   _pct_of_goal(week_actual["carbs"], week_target["carbs"]),
        "fat_pct":     _pct_of_goal(week_actual["fat"], week_target["fat"]),
        "kcal_pct":    _pct_of_goal(week_actual["kcal"], week_target["kcal"]),
    }

    # ------------------------------------------------------------------
    # STEP 3 — Apply automatic volume discount + promo code
    #
    # Volume discounts (automatic_discount_rules) are a different philosophy
    # from promo codes: no code needed, always visible, purely based on order
    # length. When both apply, the promo code stacks SEQUENTIALLY on top of
    # the volume discount — i.e. its percentage is computed off the price
    # that's ALREADY reduced by the volume discount, not the original price.
    # Two 10% deals therefore compound to 19% off, not a flat 20% — this
    # protects margin instead of letting stacked percentages add up freely.
    # If the rule's stackable_with_promo is False, the promo code wins
    # exclusively instead and the volume discount doesn't apply at all.
    # ------------------------------------------------------------------
    volume_result = apply_volume_discount(total_price, number_of_days)
    volume_discount_amount = volume_result["discount_amount"]
    volume_rule = volume_result["rule"]

    base_after_volume = total_price - volume_discount_amount

    promo_result = validate_and_apply_promo_code(
        user_id=user_id,
        promo_code_str=promo_code,
        total_price=total_price,
        number_of_days=number_of_days,
        discount_base=base_after_volume,
    )
    promo_valid = promo_result["status"] == "valid"

    stackable = volume_rule["stackable_with_promo"] if volume_rule else True
    if promo_valid and volume_rule and not stackable:
        # Exclusive deal: the volume discount is voided, so the promo's
        # percentage must be recomputed against the original price instead
        # of the (now-irrelevant) post-volume base.
        volume_discount_amount = 0.0
        promo_result = validate_and_apply_promo_code(
            user_id=user_id,
            promo_code_str=promo_code,
            total_price=total_price,
            number_of_days=number_of_days,
            discount_base=total_price,
        )
        promo_valid = promo_result["status"] == "valid"

    promo_discount_amount = promo_result["discount_amount"] if promo_valid else 0.0

    total_discount = min(volume_discount_amount + promo_discount_amount, total_price)
    final_price_after_discount = round(total_price - total_discount, 2)
    discount_ratio = (final_price_after_discount / total_price) if total_price > 0 else 1.0

    discounted_daily_price_details = []
    for day in daily_price_details:
        original_price = day["total_price"]
        discounted_price = round(original_price * discount_ratio, 2)

        discounted_daily_price_details.append({
            **day,
            "original_total_price": original_price,
            "total_price": discounted_price
        })

    # ------------------------------------------------------------------
    # STEP 4 — Delivery fee logic ✅ (per-day minimum, based on PRE-discount)
    # A promo code can waive delivery entirely (e.g. an Athlete's free-service
    # personal code), independent of the per-day minimum-order-value logic.
    # ------------------------------------------------------------------
    waives_delivery = promo_result["status"] == "valid" and bool(promo_result.get("waives_delivery"))

    delivery_days = 0
    delivery_fee = 0

    final_daily_breakdown = []
    for day in discounted_daily_price_details:
        # discounted day total (after promo)
        discounted_total = day["total_price"]

        # original day total (pre-promo) — you already stored it above
        original_total = day["original_total_price"]

        # ✅ eligibility based on PRE-discount total
        needs_delivery = original_total < DELIVERY_DAY_MINIMUM and not waives_delivery
        day_delivery_fee = delivery_price_per_day if needs_delivery else 0

        if needs_delivery:
            delivery_days += 1
            delivery_fee += day_delivery_fee

        final_daily_breakdown.append({
            **day,
            "delivery_applied": needs_delivery,
            "delivery_fee": round(day_delivery_fee, 2),
            "total_price_with_delivery": round(discounted_total + day_delivery_fee, 2),  # optional, very useful for UX
        })

    final_price_with_delivery = round(final_price_after_discount + delivery_fee, 2)



    # ------------------------------------------------------------------
    # STEP 5 — Averages
    # ------------------------------------------------------------------
    avg_kcal = round(statistics.mean(kcal_values), 1) if kcal_values else 0
    avg_protein = round(statistics.mean(protein_values), 1) if protein_values else 0
    avg_carbs = round(statistics.mean(carbs_values), 1) if carbs_values else 0
    avg_fat = round(statistics.mean(fat_values), 1) if fat_values else 0

    # ------------------------------------------------------------------
    # STEP 5b — Weekly price (Task 3): the same promo discount_ratio and
    # total delivery_fee computed above (from the per-day breakdown) are
    # applied to the gram-summed weekly macro cost, so the customer sees
    # ONE weekly number that is internally consistent with the (still
    # per-day, ops-facing) daily_breakdown total — both derive from the
    # same promo/delivery inputs, just a different grams aggregation.
    # ------------------------------------------------------------------
    weekly_packaging_total = (
        day_packaging_price * number_of_days
        + recipe_packaging_price * sum(len(d.get("meals", [])) for d in days)
        + subrecipe_packaging_price * sum(
            len(m.get("subrecipes", [])) for d in days for m in d.get("meals", [])
        )
    )
    weekly_price_before_discount = round(week_macro_cost + weekly_packaging_total, 2)
    weekly_price_after_discount = round(weekly_price_before_discount * discount_ratio, 2)
    weekly_price_final = round(weekly_price_after_discount + delivery_fee, 2)

    # ------------------------------------------------------------------
    # STEP 6 — Response
    # ------------------------------------------------------------------
    summary = {
        "user_id": user_id,
        "total_meals": total_meals,
        "macro_summary": {
            "avg_kcal": avg_kcal,
            "avg_protein": avg_protein,
            "avg_carbs": avg_carbs,
            "avg_fat": avg_fat,
        },
        # Task 2: weekly accuracy, disclosed rather than dropped — the
        # solver's per-day tolerance_used never reached the frontend before;
        # this is the simple weekly percent-of-goal form of that signal.
        "weekly_accuracy": weekly_accuracy,
        "price_breakdown": {
            "protein_price_per_g": protein_price,
            "carbs_price_per_g": carbs_price,
            "fat_price_per_g": fat_price,
            "day_packaging_price": day_packaging_price,
            "recipe_packaging_price": recipe_packaging_price,
            "subrecipe_packaging_price": subrecipe_packaging_price,

            "total_price_before_discount": round(total_price, 2),
            "discount_amount": round(total_discount, 2),
            "final_price_before_delivery": final_price_after_discount,

            "volume_discount": {
                "amount": volume_discount_amount,
                "rule_name": volume_rule["name"] if volume_rule else None,
                "min_order_days": volume_rule["min_order_days"] if volume_rule else None,
            },
            "promo_discount_amount": promo_discount_amount,

            "delivery": {
                "fee_per_day": delivery_price_per_day,
                "minimum_per_day_for_free_delivery": DELIVERY_DAY_MINIMUM,
                "delivery_days": delivery_days,
                "delivery_fee": round(delivery_fee, 2),
                "is_free_delivery": delivery_fee == 0,
                "waived_by_promo": waives_delivery,
            },

            "final_price": final_price_with_delivery,

            "promo_code_status": promo_result["status"],
            "promo_code_used": promo_code,
            "promo_message": promo_result["promo_message"],
            "promo_code_id": promo_result.get("promo_code_id"),
            "affiliate_id": promo_result.get("affiliate_id"),
            "commission_rate": promo_result.get("commission_rate"),

            # Task 3: ONE weekly price, computed by applying the unchanged
            # per-gram formula to the week's SUMMED actual grams. This is
            # the number meant to be shown to the customer as "the price."
            "weekly_price": {
                "price_before_discount": weekly_price_before_discount,
                "discount_amount": round(weekly_price_before_discount - weekly_price_after_discount, 2),
                "price_before_delivery": weekly_price_after_discount,
                "delivery_fee": round(delivery_fee, 2),
                "final_price": weekly_price_final,
            },

            # Kept for kitchen/ops use (delivery-per-day eligibility,
            # operational cost tracking) — no longer the customer-facing
            # billing unit, see "weekly_price" above.
            "daily_breakdown": final_daily_breakdown
        }
    }

    log_event(user_id, "checkout_viewed", {
        "total_meals": total_meals,
        "num_days": number_of_days,
        "total_price": round(total_price, 2),
        "final_price": final_price_with_delivery,
        "promo_code_used": promo_code or None,
        "promo_status": promo_result["status"],
    })
    return jsonify(summary), 200
