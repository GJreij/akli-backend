from flask import Blueprint, request, jsonify
import statistics
from utils.supabase_client import supabase
from services.promo_service import validate_and_apply_promo_code
from utils.event_logger import log_event

checkout_bp = Blueprint("checkout", __name__)

# -------------------------------
# CONFIGuration
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
    # ------------------------------------------------------------------
    kcal_values, protein_values, carbs_values, fat_values = [], [], [], []
    total_meals = 0
    total_price = 0
    daily_price_details = []

    for day in days:
        totals = day.get("totals", {})
        day_price = day_packaging_price

        if totals:
            kcal_values.append(totals.get("kcal", 0))
            protein_values.append(totals.get("protein", 0))
            carbs_values.append(totals.get("carbs", 0))
            fat_values.append(totals.get("fat", 0))

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
    # STEP 3 — Apply promo code
    # ------------------------------------------------------------------
    promo_result = validate_and_apply_promo_code(
        user_id=user_id,
        promo_code_str=promo_code,
        total_price=total_price
    )

    if promo_result["status"] == "valid" and total_price > 0:
        discount_ratio = promo_result["final_price"] / total_price
    else:
        discount_ratio = 1.0

    discounted_daily_price_details = []
    for day in daily_price_details:
        original_price = day["total_price"]
        discounted_price = round(original_price * discount_ratio, 2)

        discounted_daily_price_details.append({
            **day,
            "original_total_price": original_price,
            "total_price": discounted_price
        })

    final_price_after_discount = promo_result["final_price"]

    # ------------------------------------------------------------------
    # STEP 4 — Delivery fee logic ✅ (per-day minimum, based on PRE-discount)
    # ------------------------------------------------------------------
    delivery_days = 0
    delivery_fee = 0

    final_daily_breakdown = []
    for day in discounted_daily_price_details:
        # discounted day total (after promo)
        discounted_total = day["total_price"]

        # original day total (pre-promo) — you already stored it above
        original_total = day["original_total_price"]

        # ✅ eligibility based on PRE-discount total
        needs_delivery = original_total < DELIVERY_DAY_MINIMUM
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
        "price_breakdown": {
            "protein_price_per_g": protein_price,
            "carbs_price_per_g": carbs_price,
            "fat_price_per_g": fat_price,
            "day_packaging_price": day_packaging_price,
            "recipe_packaging_price": recipe_packaging_price,
            "subrecipe_packaging_price": subrecipe_packaging_price,

            "total_price_before_discount": round(total_price, 2),
            "discount_amount": promo_result["discount_amount"],
            "final_price_before_delivery": final_price_after_discount,

            "delivery": {
                "fee_per_day": delivery_price_per_day,
                "minimum_per_day_for_free_delivery": DELIVERY_DAY_MINIMUM,
                "delivery_days": delivery_days,
                "delivery_fee": round(delivery_fee, 2),
                "is_free_delivery": delivery_fee == 0
            },

            "final_price": final_price_with_delivery,

            "promo_code_status": promo_result["status"],
            "promo_code_used": promo_code,
            "promo_message": promo_result["promo_message"],
            "promo_code_id": promo_result.get("promo_code_id"),

            # ✅ This must be the final breakdown including delivery flags/fees
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
