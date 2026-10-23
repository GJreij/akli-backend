from flask import Blueprint, request, jsonify
import statistics
from utils.supabase_client import supabase
from services.promo_service import validate_and_apply_promo_code
from services.volume_discount_service import apply_volume_discount
from services.pricing_service import compute_macro_cost, compute_packaging_cost, resolve_delivery_fee_per_day, single_meal_fee
from utils.event_logger import log_event

checkout_bp = Blueprint("checkout", __name__)

# -------------------------------
# CONFIG
# -------------------------------
DELIVERY_DAY_MINIMUM = 25  # if a given day's total < 25, delivery applies for that day


def _apply_minimum_order_fee(price, minimum_order_price):
    """
    Enforce a floor on a food/packaging price (post-discount, PRE-delivery)
    so a tiny order — or a single tiny DAY inside an otherwise normal order
    — can't check out for a few cents. Applied per day, not to the order
    total: a $1 day shouldn't get a free pass just because other days in the
    same order are full price — each day still ties up its own delivery
    slot. Delivery is computed independently, per day, elsewhere in this
    route, and stacks on top of whatever this returns — it is NOT part of
    the floor itself.

    Returns (adjusted_price, minimum_order_fee) — the fee is the shortfall
    added to reach the floor, or 0.0 if no top-up was needed (including when
    minimum_order_price is unset/0, i.e. disabled).
    """
    if not minimum_order_price or price >= minimum_order_price:
        return price, 0.0
    fee = round(minimum_order_price - price, 2)
    return minimum_order_price, fee


@checkout_bp.route("/checkout_summary", methods=["POST"])
def checkout_summary():
    data = request.get_json()
    user_id = data.get("user_id")
    plan = data.get("final_plan")
    promo_code = data.get("promo_code")
    delivery_address_id = data.get("delivery_address_id")

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
    flat_delivery_price = price_data.get("delivery_price", 0) or 0
    delivery_price_per_day, delivery_fee_is_override = resolve_delivery_fee_per_day(delivery_address_id, flat_delivery_price)
    minimum_order_price = float(price_data.get("minimum_order_price") or 0)

    # Shared `prices` dict shape expected by pricing_service's compute_* helpers
    prices = {
        "protein_price_per_g": protein_price,
        "carbs_price_per_g": carbs_price,
        "fat_price_per_g": fat_price,
        "day_packaging_price": day_packaging_price,
        "recipe_packaging_price": recipe_packaging_price,
        "subrecipe_packaging_price": subrecipe_packaging_price,
        "single_meal_fee": float(price_data.get("single_meal_fee") or 0),
    }
    single_meal_fee_days = 0
    single_meal_fee_total = 0.0


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


            macro_result = compute_macro_cost(
                protein_g=p, carbs_g=c, fat_g=f,
                kcal=totals.get("kcal", 0),
                prices=prices,
            )
            macro_cost = macro_result["macro_cost_after_discount"]

            packaging_cost = compute_packaging_cost(
                meals_count=1,
                subrecipes_count=len(meal.get("subrecipes", [])),
                prices=prices,
            )

            day_price += macro_cost + packaging_cost

        # Part of the day's price like packaging (so discounts and the
        # per-day delivery/minimum thresholds see it), surfaced separately
        # in price_breakdown.single_meal_fee for the checkout UI.
        day_single_fee = single_meal_fee(len(day.get("meals", [])), prices)
        if day_single_fee > 0:
            day_price += day_single_fee
            single_meal_fee_days += 1
            single_meal_fee_total += day_single_fee

        total_price += day_price
        daily_price_details.append({
            "date": day["date"],
            "total_price": round(day_price, 2),
            "meals": len(day.get("meals", [])),
            "single_meal_fee": round(day_single_fee, 2),
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
    week_macro_result = compute_macro_cost(
        protein_g=week_actual["protein"],
        carbs_g=week_actual["carbs"],
        fat_g=week_actual["fat"],
        kcal=(week_actual["kcal"] / number_of_days) if number_of_days else 0,
        prices=prices,
    )
    week_macro_cost = week_macro_result["macro_cost_after_discount"]

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
    total_minimum_order_fee = 0.0
    minimum_order_days_affected = 0

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

        # Minimum order enforcement — PER DAY, not on the order total. A day
        # priced under this floor isn't worth reserving a delivery slot for,
        # even inside a long multi-day order (e.g. one $1 snack day shouldn't
        # skate by just because the other 19 days of a 20-day order are full
        # price). Added on top of the real food price rather than replacing
        # it — discounted_total (day["total_price"]) stays the true
        # post-discount food/packaging cost, since that's what downstream
        # commission/discount-correction math reads off the payment row this
        # day becomes; the fee rides in total_price_with_delivery only, same
        # treatment as delivery_fee.
        _, day_minimum_fee = _apply_minimum_order_fee(discounted_total, minimum_order_price)
        if day_minimum_fee > 0:
            total_minimum_order_fee += day_minimum_fee
            minimum_order_days_affected += 1

        final_daily_breakdown.append({
            **day,
            "delivery_applied": needs_delivery,
            "delivery_fee": round(day_delivery_fee, 2),
            "minimum_order_fee": round(day_minimum_fee, 2),
            "total_price_with_delivery": round(discounted_total + day_delivery_fee + day_minimum_fee, 2),
        })

    total_minimum_order_fee = round(total_minimum_order_fee, 2)
    final_price_before_delivery = round(final_price_after_discount + total_minimum_order_fee, 2)
    final_price_with_delivery = round(final_price_before_delivery + delivery_fee, 2)



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
    weekly_packaging_total = day_packaging_price * number_of_days + compute_packaging_cost(
        meals_count=sum(len(d.get("meals", [])) for d in days),
        subrecipes_count=sum(
            len(m.get("subrecipes", [])) for d in days for m in d.get("meals", [])
        ),
        prices=prices,
    ) + single_meal_fee_total
    weekly_price_before_discount = round(week_macro_cost + weekly_packaging_total, 2)
    weekly_price_after_discount = round(weekly_price_before_discount * discount_ratio, 2)
    weekly_price_final = round(weekly_price_after_discount + delivery_fee, 2)

    # ------------------------------------------------------------------
    # STEP 5c — Wallet balance available to apply at checkout
    # ------------------------------------------------------------------
    try:
        wallet_rows = (
            supabase.table("wallet_transactions")
            .select("amount")
            .eq("user_id", user_id)
            .execute()
            .data
        ) or []
        wallet_balance = round(sum(r.get("amount") or 0 for r in wallet_rows), 2)
    except Exception:
        wallet_balance = 0
    wallet_max_applicable = round(min(wallet_balance, final_price_with_delivery), 2)

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
            "final_price_before_delivery": final_price_before_delivery,

            "volume_discount": {
                "amount": volume_discount_amount,
                "rule_name": volume_rule["name"] if volume_rule else None,
                "min_order_days": volume_rule["min_order_days"] if volume_rule else None,
            },
            "promo_discount_amount": promo_discount_amount,

            # Already INCLUDED in total_price_before_discount (it's part of
            # each one-meal day's price) — shown as an "includes" note, not
            # an extra line to add.
            "single_meal_fee": {
                "fee_per_day": prices["single_meal_fee"],
                "days": single_meal_fee_days,
                "total": round(single_meal_fee_total, 2),
            },

            "minimum_order": {
                "threshold": minimum_order_price,
                "fee_applied": total_minimum_order_fee,
                "is_applied": total_minimum_order_fee > 0,
                "days_affected": minimum_order_days_affected,
            },

            "delivery": {
                "fee_per_day": delivery_price_per_day,
                "minimum_per_day_for_free_delivery": DELIVERY_DAY_MINIMUM,
                "delivery_days": delivery_days,
                "delivery_fee": round(delivery_fee, 2),
                "is_free_delivery": delivery_fee == 0,
                "waived_by_promo": waives_delivery,
                # This address has an admin-set fee different from everyone
                # else's — the checkout UI should call this out rather than
                # just showing a different number with no explanation.
                "is_custom_fee": delivery_fee_is_override,
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
            "daily_breakdown": final_daily_breakdown,

            "wallet_balance": wallet_balance,
            "wallet_max_applicable": wallet_max_applicable,
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
