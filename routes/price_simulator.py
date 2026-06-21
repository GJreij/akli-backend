from flask import Blueprint, request, jsonify
from services.pricing_service import get_kcal_discount, fetch_latest_prices

simple_price_bp = Blueprint("simple_price_simulator", __name__)


@simple_price_bp.route("/simple_price_simulator", methods=["POST"])
def simple_price_simulator():
    """
    INPUT:
    {
      "protein_g": 150,
      "carbs_g": 200,
      "fat_g": 60,
      "meals_per_day": 3,
      "avg_subrecipes_per_meal": 1.5,

      "apply_kcal_discount": true
    }

    OUTPUT:
    {
      "avg_day_price": ...,
      "breakdown": {...}
    }
    """
    data = request.get_json() or {}

    # Required inputs
    try:
        protein_g = float(data["protein_g"])
        carbs_g = float(data["carbs_g"])
        fat_g = float(data["fat_g"])
        meals_per_day = int(data["meals_per_day"])
        avg_subrecipes_per_meal = float(data["avg_subrecipes_per_meal"])
    except KeyError as e:
        return jsonify({"error": f"Missing field: {str(e)}"}), 400
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid input types"}), 400

    if meals_per_day <= 0:
        return jsonify({"error": "meals_per_day must be >= 1"}), 400
    if protein_g < 0 or carbs_g < 0 or fat_g < 0:
        return jsonify({"error": "Macros must be >= 0"}), 400
    if avg_subrecipes_per_meal < 0:
        return jsonify({"error": "avg_subrecipes_per_meal must be >= 0"}), 400

    apply_kcal_discount = bool(data.get("apply_kcal_discount", True))

    # Fetch prices
    try:
        prices = fetch_latest_prices()
    except Exception as e:
        return jsonify({"error": f"Failed to fetch pricing data: {str(e)}"}), 500

    # Estimate kcal from macros (standard: P=4, C=4, F=9)
    estimated_kcal = protein_g * 4 + carbs_g * 4 + fat_g * 9

    # Macro cost (per day)
    base_macro_cost = (
        protein_g * prices["protein_price_per_g"]
        + carbs_g * prices["carbs_price_per_g"]
        + fat_g * prices["fat_price_per_g"]
    )

    discount_pct = get_kcal_discount(estimated_kcal) if apply_kcal_discount else 0.0
    macro_cost_after_discount = base_macro_cost * (1 - discount_pct)

    # Packaging costs
    day_packaging = prices["day_packaging_price"]
    recipes_packaging = meals_per_day * prices["recipe_packaging_price"]
    subrecipes_packaging = meals_per_day * avg_subrecipes_per_meal * prices["subrecipe_packaging_price"]

    avg_day_price = round(
        day_packaging + macro_cost_after_discount + recipes_packaging + subrecipes_packaging,
        2
    )

    # Task 3: customer-facing pricing is weekly, not daily. This endpoint
    # only receives a single day's macros as input (it's a simulator, not a
    # solved week), so the weekly figure here is the same per-gram formula
    # applied to grams x 7 — equivalent to summing 7 identical days, which
    # is the simulator's best available approximation of a real week.
    avg_week_price = round(avg_day_price * 7, 2)

    return jsonify({
        "inputs": {
            "protein_g": protein_g,
            "carbs_g": carbs_g,
            "fat_g": fat_g,
            "meals_per_day": meals_per_day,
            "avg_subrecipes_per_meal": avg_subrecipes_per_meal,
            "estimated_kcal": round(estimated_kcal, 0),
            "apply_kcal_discount": apply_kcal_discount
        },
        "avg_day_price": avg_day_price,
        "avg_week_price": avg_week_price,
        "breakdown": {
            "prices_used": {
                "protein_price_per_g": prices["protein_price_per_g"],
                "carbs_price_per_g": prices["carbs_price_per_g"],
                "fat_price_per_g": prices["fat_price_per_g"],
                "day_packaging_price": prices["day_packaging_price"],
                "recipe_packaging_price": prices["recipe_packaging_price"],
                "subrecipe_packaging_price": prices["subrecipe_packaging_price"]
            },
            "base_macro_cost": round(base_macro_cost, 2),
            "kcal_discount_pct": round(discount_pct, 4),
            "macro_cost_after_discount": round(macro_cost_after_discount, 2),
            "day_packaging_cost": round(day_packaging, 2),
            "recipes_packaging_cost": round(recipes_packaging, 2),
            "subrecipes_packaging_cost": round(subrecipes_packaging, 2)
        }
    }), 200
