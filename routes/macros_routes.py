from flask import Blueprint, request, jsonify
import statistics

from config.constants import DIET_MACROS, KCAL_PER_G, MACRO_RANGES
from services.pricing_service import (
    DEFAULT_MEALS_PER_DAY,
    DEFAULT_AVG_SUBRECIPES_PER_MEAL,
    DEFAULT_APPLY_KCAL_DISCOUNT,
    estimate_day_price,
    estimate_ui_pricing_for_3m1s,
)

macros_bp = Blueprint("macros", __name__)


# -------------------------------
# Input parsing helpers
# -------------------------------
def parse_float(value, field_name, *, allow_zero=False) -> float:
    """
    Safely parse float and return clear error messages.
    """
    try:
        v = float(value)
    except (TypeError, ValueError):
        raise ValueError(
            f"{field_name} must be a number. "
            "Use a dot (.) for decimals, not a comma (,)."
        )

    if allow_zero:
        if v < 0:
            raise ValueError(f"{field_name} must be >= 0.")
    else:
        if v <= 0:
            raise ValueError(f"{field_name} must be greater than 0.")

    return v


def parse_int(value, field_name, *, default=None, min_value=1) -> int:
    if value is None:
        if default is None:
            raise ValueError(f"Missing field: {field_name}")
        return default

    try:
        v = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{field_name} must be an integer.")

    if v < min_value:
        raise ValueError(f"{field_name} must be >= {min_value}.")
    return v


def parse_bool(value, default=False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes", "y", "on")
    return bool(value)


# -------------------------------
# Routes
# -------------------------------
@macros_bp.route("/macros", methods=["GET"])
def get_macros():
    """
    GET /macros?kcal=2200&diet=balanced
    Optional query params for price estimate:
      - meals_per_day (int, default 3)
      - avg_subrecipes_per_meal (float, default 0)
      - apply_kcal_discount (bool, default true)
    """
    kcal = request.args.get("kcal", type=float)
    diet_type = request.args.get("diet", "").lower().strip()

    if not kcal or kcal <= 0:
        return jsonify({"error": "Please provide a positive kcal value"}), 400
    if diet_type not in DIET_MACROS:
        return jsonify({"error": f"Diet type must be one of {list(DIET_MACROS.keys())}"}), 400

    # Optional pricing knobs
    try:
        meals_per_day = parse_int(request.args.get("meals_per_day"), "meals_per_day", default=DEFAULT_MEALS_PER_DAY)
        avg_subrecipes_per_meal = parse_float(
            request.args.get("avg_subrecipes_per_meal", DEFAULT_AVG_SUBRECIPES_PER_MEAL),
            "avg_subrecipes_per_meal",
            allow_zero=True
        )
        apply_kcal_discount = parse_bool(request.args.get("apply_kcal_discount"), default=DEFAULT_APPLY_KCAL_DISCOUNT)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    macros_pct = DIET_MACROS[diet_type]

    macros_grams = {
        macro: round((kcal * pct) / KCAL_PER_G[macro], 1)
        for macro, pct in macros_pct.items()
    }

    # Price estimate
    try:
        price_estimate = estimate_day_price(
            protein_g=float(macros_grams.get("protein", 0) or 0),
            carbs_g=float(macros_grams.get("carbs", 0) or 0),
            fat_g=float(macros_grams.get("fat", 0) or 0),
            total_kcal=float(kcal),
            meals_per_day=meals_per_day,
            avg_subrecipes_per_meal=avg_subrecipes_per_meal,
            apply_kcal_discount=apply_kcal_discount,
        )
    except Exception as e:
        return jsonify({"error": f"Failed to estimate price: {str(e)}"}), 500

    return jsonify({
        "diet_type": diet_type,
        "kcal": float(kcal),
        "macros_percentage": {m: int(pct * 100) for m, pct in macros_pct.items()},
        "macros_grams": macros_grams,
        "price_estimate": price_estimate,
    }), 200

@macros_bp.route("/macros/ui-price", methods=["GET"])
def get_ui_price():
    """
    GET /macros/ui-price?kcal=2200&diet=balanced

    Optional query params:
      - avg_subrecipes_per_meal (float, default DEFAULT_AVG_SUBRECIPES_PER_MEAL)
      - snack_kcal_share (float, default 0.20)   # portion of daily kcal assigned to snack conceptually
      - snack_subrecipes (float, default 1.0)
      - apply_kcal_discount (bool, default true)

    Returns UI-friendly pricing strings + ranges for:
      - week
      - day
      - per-meal average (for 3 meals + 1 snack)
    """
    MIN_KCAL = 500
    MAX_KCAL = 3500
    kcal = request.args.get("kcal", type=float)
    diet_type = request.args.get("diet", "").lower().strip()

    if kcal is None:
        return jsonify({"error": "Please provide a kcal value"}), 400

    if kcal < MIN_KCAL or kcal > MAX_KCAL:
        return jsonify({
            "error": f"kcal must be between {MIN_KCAL} and {MAX_KCAL}."
        }), 400
    if diet_type not in DIET_MACROS:
        return jsonify({"error": f"Diet type must be one of {list(DIET_MACROS.keys())}"}), 400

    # Optional knobs
    try:
        avg_subrecipes_per_meal = parse_float(
            request.args.get("avg_subrecipes_per_meal", DEFAULT_AVG_SUBRECIPES_PER_MEAL),
            "avg_subrecipes_per_meal",
            allow_zero=True,
        )
        snack_kcal_share = parse_float(
            request.args.get("snack_kcal_share", 0.20),
            "snack_kcal_share",
            allow_zero=True,
        )
        snack_subrecipes = parse_float(
            request.args.get("snack_subrecipes", 1.0),
            "snack_subrecipes",
            allow_zero=True,
        )
        apply_kcal_discount = parse_bool(
            request.args.get("apply_kcal_discount"),
            default=DEFAULT_APPLY_KCAL_DISCOUNT
        )
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    # Compute macros grams from kcal + diet (same logic as /macros)
    macros_pct = DIET_MACROS[diet_type]
    macros_grams = {
        macro: round((kcal * pct) / KCAL_PER_G[macro], 1)
        for macro, pct in macros_pct.items()
    }

    # UI pricing
    try:
        ui_price = estimate_ui_pricing_for_3m1s(
            protein_g=float(macros_grams.get("protein", 0) or 0),
            carbs_g=float(macros_grams.get("carbs", 0) or 0),
            fat_g=float(macros_grams.get("fat", 0) or 0),
            total_kcal=float(kcal),
            avg_subrecipes_per_meal=avg_subrecipes_per_meal,
            snack_kcal_share=snack_kcal_share,
            snack_subrecipes=snack_subrecipes,
            apply_kcal_discount=apply_kcal_discount,
        )
    except Exception as e:
        return jsonify({"error": f"Failed to estimate UI price: {str(e)}"}), 500

    # Keep response focused for the UI
    return jsonify({
        "diet_type": diet_type,
        "kcal": float(kcal),
        "macros_percentage": {m: int(pct * 100) for m, pct in macros_pct.items()},
        "macros_grams": macros_grams,
        "ui_price": ui_price,
    }), 200

@macros_bp.route("/macros/from-grams", methods=["POST"])
def macros_from_grams():
    """
    POST /macros/from-grams
    Body:
    {
      "protein": 150,
      "carbs": 200,
      "fat": 60,

      "meals_per_day": 3,              (optional)
      "avg_subrecipes_per_meal": 1.5,  (optional)
      "apply_kcal_discount": true      (optional)
    }
    """
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "Invalid or missing JSON body"}), 400

    # Required macros
    try:
        protein_g = parse_float(data.get("protein"), "Protein (g)")
        carbs_g = parse_float(data.get("carbs"), "Carbohydrates (g)")
        fat_g = parse_float(data.get("fat"), "Fat (g)")
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    # Optional pricing knobs
    try:
        meals_per_day = parse_int(data.get("meals_per_day"), "meals_per_day", default=DEFAULT_MEALS_PER_DAY)
        avg_subrecipes_per_meal = parse_float(
            data.get("avg_subrecipes_per_meal", DEFAULT_AVG_SUBRECIPES_PER_MEAL),
            "avg_subrecipes_per_meal",
            allow_zero=True
        )
        apply_kcal_discount = parse_bool(data.get("apply_kcal_discount"), default=DEFAULT_APPLY_KCAL_DISCOUNT)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    # kcal calculation
    kcal_protein = protein_g * KCAL_PER_G["protein"]
    kcal_carbs = carbs_g * KCAL_PER_G["carbs"]
    kcal_fat = fat_g * KCAL_PER_G["fat"]
    total_kcal = kcal_protein + kcal_carbs + kcal_fat

    if total_kcal <= 0:
        return jsonify({"error": "Total calories must be greater than 0"}), 400

    # percentages
    pct_protein = kcal_protein / total_kcal
    pct_carbs = kcal_carbs / total_kcal
    pct_fat = kcal_fat / total_kcal

    # sanity checks
    errors = []
    for macro, pct in {"protein": pct_protein, "carbs": pct_carbs, "fat": pct_fat}.items():
        min_pct, max_pct = MACRO_RANGES[macro]
        if not (min_pct <= pct <= max_pct):
            errors.append(
                f"{macro.capitalize()} percentage ({int(pct*100)}%) "
                f"is outside the recommended range "
                f"({int(min_pct*100)}–{int(max_pct*100)}%)."
            )

    if errors:
        return jsonify({"error": "Macro distribution is unrealistic.", "details": errors}), 400

    # price estimate
    try:
        price_estimate = estimate_day_price(
            protein_g=protein_g,
            carbs_g=carbs_g,
            fat_g=fat_g,
            total_kcal=total_kcal,
            meals_per_day=meals_per_day,
            avg_subrecipes_per_meal=avg_subrecipes_per_meal,
            apply_kcal_discount=apply_kcal_discount,
        )
    except Exception as e:
        return jsonify({"error": f"Failed to estimate price: {str(e)}"}), 500

    return jsonify({
        "total_kcal": round(total_kcal),
        "macros_grams": {
            "protein": protein_g,
            "carbs": carbs_g,
            "fat": fat_g,
        },
        "macros_percentage": {
            "protein": round(pct_protein * 100, 1),
            "carbs": round(pct_carbs * 100, 1),
            "fat": round(pct_fat * 100, 1),
        },
        "kcal_breakdown": {
            "protein": round(kcal_protein),
            "carbs": round(kcal_carbs),
            "fat": round(kcal_fat),
        },
        "price_estimate": price_estimate,
    }), 200
