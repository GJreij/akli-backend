# services/promo_service.py
from __future__ import annotations

from datetime import date
from typing import Any, Dict, Optional

from utils.supabase_client import supabase


def _normalize_code(code: str) -> str:
    # Keep it simple; you can also enforce uppercase everywhere if you want.
    return code.strip()


def _pick_applicable_promo(
    *,
    user_id: str,
    code: str,
) -> Optional[Dict[str, Any]]:
    """
    Deterministic promo resolution, replacing the old partner_client_link-based
    lookup (that table is stale/legacy — see FrontEnd CLAUDE.md):
      1) a code privately scoped to this user (promo_codes.user_id = user_id)
      2) a public code (promo_codes.user_id IS NULL) — may or may not belong
         to an affiliate via affiliate_id; either way it's open to anyone

    Returns the selected promo row dict or None.
    """
    code = _normalize_code(code)

    # Priority 1: privately scoped to this user
    user_res = (
        supabase.table("promo_codes")
        .select("*")
        .eq("is_active", True)
        .eq("user_id", user_id)
        .eq("code", code)
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    )
    if user_res.data:
        return user_res.data[0]

    # Priority 2: public code
    public_res = (
        supabase.table("promo_codes")
        .select("*")
        .eq("is_active", True)
        .is_("user_id", "null")
        .eq("code", code)
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    )
    if public_res.data:
        return public_res.data[0]

    return None


def _resolve_affiliate_commission(
    *, promo: Dict[str, Any], user_id: str
) -> Dict[str, Any]:
    """
    If the promo code belongs to an affiliate, and the orderer isn't the
    affiliate themself (self-use is just a personal discount, not a
    referral), resolve the commission rate to apply: per-code override if
    set, else the affiliate's default rate.
    """
    affiliate_id = promo.get("affiliate_id")
    if not affiliate_id:
        return {"affiliate_id": None, "commission_rate": None}

    aff_res = (
        supabase.table("affiliates")
        .select("id, user_id, commission_rate, status")
        .eq("id", affiliate_id)
        .limit(1)
        .execute()
    )
    if not aff_res.data:
        return {"affiliate_id": None, "commission_rate": None}

    affiliate = aff_res.data[0]
    if affiliate.get("user_id") == user_id or affiliate.get("status") != "active":
        return {"affiliate_id": None, "commission_rate": None}

    rate = promo.get("commission_rate_override")
    if rate is None:
        rate = affiliate.get("commission_rate")

    return {"affiliate_id": affiliate_id, "commission_rate": rate}


def validate_and_apply_promo_code(
    user_id: str,
    promo_code_str: Optional[str],
    total_price: float,
    number_of_days: Optional[int] = None,
    discount_base: Optional[float] = None,
) -> Dict[str, Any]:
    """
    Validates and applies a promo code.
    - Resolves deterministically: code privately scoped to this user, else a
      public code (see _pick_applicable_promo).
    - Keeps the existing usage + date + min order logic, plus a min_order_days
      condition and a max_discount_amount cap.
    - If the code belongs to an affiliate, resolves the commission owed.
    - min_order_value/min_order_days are always checked against the order's
      real total_price. The discount percentage itself is computed against
      discount_base if given (defaults to total_price) — this is how a
      promo code stacks SEQUENTIALLY on top of an already-applied automatic
      volume discount, rather than both being computed off the same
      original price (which would let two 10% deals add up to a flat 20%).
    """
    if discount_base is None:
        discount_base = total_price
    # 0) No code provided
    if not promo_code_str or promo_code_str.strip() == "":
        return {
            "status": "no_code",
            "discount_amount": 0,
            "final_price": round(float(total_price or 0), 2),
            "promo_message": "",
            "affiliate_id": None,
            "commission_rate": None,
            "waives_delivery": False,
        }

    code = _normalize_code(promo_code_str)
    total_price = float(total_price or 0)

    # 1) Resolve which promo row applies (deterministic)
    promo = _pick_applicable_promo(user_id=user_id, code=code)
    if not promo:
        return {
            "status": "invalid",
            "discount_amount": 0,
            "final_price": round(total_price, 2),
            "promo_message": "Promo code is invalid.",
            "affiliate_id": None,
            "commission_rate": None,
            "waives_delivery": False,
        }

    promo_id = promo["id"]

    # 2) Date validity
    today = date.today()

    if promo.get("start_date") and today < date.fromisoformat(promo["start_date"]):
        return {
            "status": "not_started",
            "discount_amount": 0,
            "final_price": round(total_price, 2),
            "promo_message": f"This promo code is not active until {promo['start_date']}.",
            "affiliate_id": None,
            "commission_rate": None,
            "waives_delivery": False,
        }

    if promo.get("end_date") and today > date.fromisoformat(promo["end_date"]):
        return {
            "status": "expired",
            "discount_amount": 0,
            "final_price": round(total_price, 2),
            "promo_message": "This promo code has expired.",
            "affiliate_id": None,
            "commission_rate": None,
            "waives_delivery": False,
        }

    # 3) Global usage limit
    if promo.get("max_global_uses") is not None:
        usage_res = (
            supabase.table("promo_code_usage")
            .select("id", count="exact")
            .eq("promo_code_id", promo_id)
            .execute()
        )
        if (usage_res.count or 0) >= int(promo["max_global_uses"]):
            return {
                "status": "max_global_reached",
                "discount_amount": 0,
                "final_price": round(total_price, 2),
                "promo_message": "This promo code has reached its maximum number of uses.",
                "affiliate_id": None,
                "commission_rate": None,
                "waives_delivery": False,
            }

    # 4) Per-user usage limit
    if promo.get("max_uses_per_user") is not None:
        user_usage = (
            supabase.table("promo_code_usage")
            .select("id", count="exact")
            .eq("promo_code_id", promo_id)
            .eq("user_id", user_id)
            .execute()
        )
        if (user_usage.count or 0) >= int(promo["max_uses_per_user"]):
            return {
                "status": "max_user_reached",
                "discount_amount": 0,
                "final_price": round(total_price, 2),
                "promo_message": "You have already used this promo code the maximum number of times.",
                "affiliate_id": None,
                "commission_rate": None,
                "waives_delivery": False,
            }

    # 5) Minimum order value
    if promo.get("min_order_value") is not None:
        min_order_value = float(promo["min_order_value"])
        if total_price < min_order_value:
            return {
                "status": "order_value_too_low",
                "discount_amount": 0,
                "final_price": round(total_price, 2),
                "promo_message": f"Minimum order value for this promo is ${min_order_value}.",
                "affiliate_id": None,
                "commission_rate": None,
                "waives_delivery": False,
            }

    # 6) Minimum order length (e.g. "10% off if you order 10+ days")
    if promo.get("min_order_days") is not None:
        min_order_days = int(promo["min_order_days"])
        if (number_of_days or 0) < min_order_days:
            return {
                "status": "order_too_short",
                "discount_amount": 0,
                "final_price": round(total_price, 2),
                "promo_message": f"This promo requires ordering at least {min_order_days} days.",
                "affiliate_id": None,
                "commission_rate": None,
                "waives_delivery": False,
            }

    # 7) Discount calculation
    discount_value = float(promo.get("discount_value") or 0)
    discount = 0.0

    discount_type = promo.get("discount_type")

    if discount_type == "percentage":
        discount = discount_base * (discount_value / 100.0)
        msg = f"Promo applied! You saved {discount_value}%."
    elif discount_type == "fixed":
        discount = discount_value
        msg = f"Promo applied! You saved ${discount_value}."
    else:
        # Unknown discount_type; treat as no discount but "applied"
        discount = 0.0
        msg = "Promo applied."

    if promo.get("max_discount_amount") is not None:
        discount = min(discount, float(promo["max_discount_amount"]))
    discount = min(discount, discount_base)

    final_price = max(total_price - discount, 0.0)

    commission_info = _resolve_affiliate_commission(promo=promo, user_id=user_id)

    return {
        "status": "valid",
        "discount_amount": round(discount, 2),
        "final_price": round(final_price, 2),
        "promo_code_id": promo_id,
        "promo_message": msg,
        "affiliate_id": commission_info["affiliate_id"],
        "commission_rate": commission_info["commission_rate"],
        "waives_delivery": bool(promo.get("waives_delivery")),
    }
