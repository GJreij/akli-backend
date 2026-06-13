import math

from utils.supabase_client import supabase

# -------------------------------
# Defaults for estimation
# -------------------------------
DEFAULT_MEALS_PER_DAY = 3
DEFAULT_AVG_SUBRECIPES_PER_MEAL = 3
DEFAULT_APPLY_KCAL_DISCOUNT = True


def get_kcal_discount(kcal: float) -> float:
    """
    Discount grows linearly from 0% at 1200kcal to 15% at 3000kcal.
    """
    min_kcal = 1200
    max_kcal = 3000
    max_discount = 0.15

    if kcal is None:
        return 0.0

    if kcal <= min_kcal:
        return 0.0
    if kcal >= max_kcal:
        return max_discount

    ratio = (kcal - min_kcal) / (max_kcal - min_kcal)
    return ratio * max_discount


def fetch_latest_prices() -> dict:
    """
    Fetch latest pricing from Supabase macro_price table.
    """
    price_resp = (
        supabase.table("macro_price")
        .select("*")
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    )
    if not price_resp.data:
        raise ValueError("No pricing data found in macro_price")

    price_data = price_resp.data[0] or {}
    return {
        "protein_price_per_g": float(price_data.get("proteing_g_price", 0) or 0),
        "carbs_price_per_g": float(price_data.get("carbs_g_price", 0) or 0),
        "fat_price_per_g": float(price_data.get("fat_g_price", 0) or 0),
        "day_packaging_price": float(price_data.get("day_packaging_price", 0) or 0),
        "recipe_packaging_price": float(price_data.get("recipe_packaging_price", 0) or 0),
        "subrecipe_packaging_price": float(price_data.get("subrecipe_packaging_price", 0) or 0),
    }


def estimate_day_price(
    *,
    protein_g: float,
    carbs_g: float,
    fat_g: float,
    total_kcal: float,
    meals_per_day: int = DEFAULT_MEALS_PER_DAY,
    avg_subrecipes_per_meal: float = DEFAULT_AVG_SUBRECIPES_PER_MEAL,
    apply_kcal_discount: bool = DEFAULT_APPLY_KCAL_DISCOUNT,
) -> dict:
    """
    Returns a detailed price estimate (per day) using the same logic as checkout:
    - macro cost based on grams
    - kcal-based discount (optional)
    - day packaging
    - recipe packaging per meal
    - subrecipe packaging based on avg count
    """
    prices = fetch_latest_prices()

    base_macro_cost = (
        protein_g * prices["protein_price_per_g"]
        + carbs_g * prices["carbs_price_per_g"]
        + fat_g * prices["fat_price_per_g"]
    )

    discount_pct = get_kcal_discount(total_kcal) if apply_kcal_discount else 0.0
    macro_cost_after_discount = base_macro_cost * (1 - discount_pct)

    day_packaging = prices["day_packaging_price"]
    recipes_packaging = meals_per_day * prices["recipe_packaging_price"]
    subrecipes_packaging = meals_per_day * avg_subrecipes_per_meal * prices["subrecipe_packaging_price"]

    estimated_day_price = round(
        day_packaging + macro_cost_after_discount + recipes_packaging + subrecipes_packaging,
        2
    )

    return {
        "estimated_day_price": estimated_day_price,
        "assumptions": {
            "meals_per_day": meals_per_day,
            "avg_subrecipes_per_meal": avg_subrecipes_per_meal,
            "apply_kcal_discount": apply_kcal_discount,
        },
        "breakdown": {
            "base_macro_cost": round(base_macro_cost, 2),
            "kcal_discount_pct": round(discount_pct, 4),
            "macro_cost_after_discount": round(macro_cost_after_discount, 2),
            "day_packaging_cost": round(day_packaging, 2),
            "recipes_packaging_cost": round(recipes_packaging, 2),
            "subrecipes_packaging_cost": round(subrecipes_packaging, 2),
        },
        "prices_used": prices,
    }


def _band(amount: float, pct: float = 0.06, min_width: float = 2.0) -> dict:
    """
    Convert an exact amount into a friendly integer range.
    Example: 23.12 -> {"low": 22, "high": 25}
    """
    half_width = max(min_width / 2, amount * pct)
    low = math.floor(amount - half_width)
    high = math.ceil(amount + half_width)
    if high <= low:
        high = low + 1
    return {"low": low, "high": high}


def estimate_ui_pricing_for_3m1s(
    *,
    protein_g: float,
    carbs_g: float,
    fat_g: float,
    total_kcal: float,
    avg_subrecipes_per_meal: float = DEFAULT_AVG_SUBRECIPES_PER_MEAL,
    snack_kcal_share: float = 0.20,     # 15-25% typical; default 20%
    snack_subrecipes: float = 1.0,      # snack simpler than meals
    apply_kcal_discount: bool = DEFAULT_APPLY_KCAL_DISCOUNT,
) -> dict:
    """
    UI-oriented pricing for: 3 meals + 1 snack.

    Uses estimate_day_price() for the true day cost (with 4 containers/day),
    then provides safe UI ranges.
    """
    MEALS = 3
    SNACKS = 1
    containers = MEALS + SNACKS  # 4

    # Get true estimate using the shared pricing logic
    day_est = estimate_day_price(
        protein_g=protein_g,
        carbs_g=carbs_g,
        fat_g=fat_g,
        total_kcal=total_kcal,
        meals_per_day=containers,  # packaging aligned with 4 containers/day
        avg_subrecipes_per_meal=avg_subrecipes_per_meal,
        apply_kcal_discount=apply_kcal_discount,
    )

    exact_day = float(day_est["estimated_day_price"])

    # Average per container (this is your "(~ $6-7 per meal on average)")
    per_container_exact = exact_day / containers

    # Friendly UI ranges
    day_range = _band(exact_day, pct=0.06, min_width=3.0)
    per_meal_avg_range = _band(per_container_exact, pct=0.08, min_width=1.0)

    # Weekly (7 days)
    exact_week = exact_day * 7
    week_range = _band(exact_week, pct=0.06, min_width=10.0)

    return {
        "scenario": {"meals": MEALS, "snacks": SNACKS, "containers": containers},
        "ranges": {
            "day": day_range,                 # {"low": 22, "high": 25}
            "week": week_range,               # {"low": 155, "high": 175}
            "per_meal_avg": per_meal_avg_range
        },
        "exact": {
            "day": round(exact_day, 2),
            "week": round(exact_week, 2),
            "avg_per_container": round(per_container_exact, 2),
        },
        "ui_copy": {
            "headline": "For a day of 3 meals and 1 snack:",
            "day": f"~ ${day_range['low']}–{day_range['high']} / day",
            "per_meal": f"(≈ ${per_meal_avg_range['low']}–{per_meal_avg_range['high']} per meal on average)",
            "week": f"~ ${week_range['low']}–{week_range['high']} / week",
            "note": "Meals vary in size and macros. Pricing is based on total daily nutrition, not individual dishes.",
        },
        "assumptions": {
            "avg_subrecipes_per_meal": avg_subrecipes_per_meal,
            "snack_kcal_share": max(0.0, min(0.5, float(snack_kcal_share))),
            "snack_subrecipes": snack_subrecipes,
            "apply_kcal_discount": apply_kcal_discount,
        },
        "day_estimate_debug": day_est,  # keep or remove depending on what you want exposed
    }
