# services/volume_discount_service.py
from __future__ import annotations

from datetime import date
from typing import Any, Dict, Optional

from utils.supabase_client import supabase


def resolve_volume_discount_rule(number_of_days: int) -> Optional[Dict[str, Any]]:
    """
    Automatic, no-code discounts based purely on order length (e.g. "order 3+
    days, get 10% off automatically") — distinct from promo_codes, which are
    only ever shared deliberately (social media, affiliates). Finds the
    active, date-valid rule with the highest min_order_days the order still
    meets, i.e. the best tier this order qualifies for.
    """
    today = date.today().isoformat()

    res = (
        supabase.table("automatic_discount_rules")
        .select("*")
        .eq("is_active", True)
        .lte("min_order_days", number_of_days)
        .order("min_order_days", desc=True)
        .execute()
    )

    for rule in res.data or []:
        if rule.get("start_date") and today < rule["start_date"]:
            continue
        if rule.get("end_date") and today > rule["end_date"]:
            continue
        return rule

    return None


def apply_volume_discount(total_price: float, number_of_days: int) -> Dict[str, Any]:
    rule = resolve_volume_discount_rule(number_of_days)
    if not rule:
        return {"discount_amount": 0.0, "rule": None}

    discount_value = float(rule.get("discount_value") or 0)
    if rule.get("discount_type") == "percentage":
        discount = total_price * (discount_value / 100.0)
    else:
        discount = discount_value

    if rule.get("max_discount_amount") is not None:
        discount = min(discount, float(rule["max_discount_amount"]))

    discount = min(discount, total_price)

    return {
        "discount_amount": round(discount, 2),
        "rule": {
            "id": rule["id"],
            "name": rule["name"],
            "min_order_days": rule["min_order_days"],
            "discount_type": rule["discount_type"],
            "discount_value": discount_value,
            "stackable_with_promo": rule.get("stackable_with_promo", True),
        },
    }
