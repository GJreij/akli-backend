# services/day_pricing_helpers.py
#
# Shared "what does this day currently cost" logic used by both
# MealSwapService and DayEditService — computed from the day's CURRENT
# stored macros (not re-derived from the original checkout price), using the
# exact same formula the LP's swap/edit price expression uses, so a computed
# price delta measures only the swap/edit's own impact, independent of
# whatever promo/affiliate discount applied at original checkout.

from services.pricing_service import compute_macro_cost, compute_packaging_cost, single_meal_fee


def sum_macros(rows):
    return {
        "protein": sum(r.get("protein_calculated") or 0 for r in rows),
        "carbs":   sum(r.get("carbs_calculated") or 0 for r in rows),
        "fat":     sum(r.get("fat_calculated") or 0 for r in rows),
        "kcal":    sum(r.get("kcal_calculated") or 0 for r in rows),
    }


def wallet_balance(sb, user_id) -> float:
    """Server-side SUM via get_wallet_balance() — a plain unpaginated
    `.select("amount")` here would get silently truncated by PostgREST's
    default row cap once a user's wallet_transactions history grows past it,
    undercounting or overcounting the balance shown in a preview."""
    res = sb.rpc("get_wallet_balance", {"p_user_id": user_id}).execute()
    return round(float(res.data or 0), 2)


def day_price_and_macros(serving_rows, day_recipes, macro_target, prices):
    """
    Current (pre-change) price + macros for a whole day — see module
    docstring. kcal discount is computed from the day's TARGET kcal,
    matching the LP's own linearization (see
    mealplan_service._solve_lp_once point 8).
    """
    day_macros = sum_macros(serving_rows)

    kcal_t = macro_target.get("kcal") or day_macros["kcal"]
    macro_cost = compute_macro_cost(
        protein_g=day_macros["protein"], carbs_g=day_macros["carbs"], fat_g=day_macros["fat"],
        kcal=kcal_t, prices=prices, apply_kcal_discount=True,
    )
    packaging_cost = compute_packaging_cost(
        meals_count=len(day_recipes), subrecipes_count=len(serving_rows), prices=prices,
    )
    day_price = round(
        prices["day_packaging_price"] + macro_cost["macro_cost_after_discount"] + packaging_cost
        + single_meal_fee(len(day_recipes), prices), 2
    )
    return day_price, day_macros


def paid_share(sb, meal_plan_day_id) -> float:
    """
    Fraction of this day's list price the client actually paid for the food,
    from its payment row: (original_amount - discount_amount) / original_amount.
    A 20% promo -> 0.8, the 10+ day volume discount -> 0.9, a 100% promo -> 0.

    Swap/edit price deltas are computed at LIST price (day_price_and_macros),
    so they must be scaled by this before touching the wallet — otherwise a
    discounted day refunds (or charges) at full price, e.g. a 100%-promo day
    paying out real wallet money on an edit. No payment row / no recorded
    list price (older orders) -> 1.0, the previous behaviour.
    """
    res = (
        sb.table("payment")
        .select("original_amount, discount_amount")
        .eq("meal_plan_day_id", meal_plan_day_id)
        .limit(1)
        .execute()
    )
    row = (res.data or [None])[0]
    if not row:
        return 1.0
    original = float(row.get("original_amount") or 0)
    if original <= 0:
        return 1.0
    discount = float(row.get("discount_amount") or 0)
    return max(0.0, min(1.0, (original - discount) / original))


def charged_delta(list_price_delta: float, share: float) -> float:
    """What actually moves through the wallet for a list-price difference."""
    # + 0.0 turns a -0.0 (negative delta on a fully discounted day) into 0.0,
    # so it never shows up as "-$0.00".
    return round(list_price_delta * share, 2) + 0.0


def list_price_budget(wallet_balance: float, share: float) -> float:
    """How much LIST price increase a wallet balance covers at this share
    (inverse of charged_delta); unlimited when the day was fully discounted."""
    return float("inf") if share <= 0 else wallet_balance / share
