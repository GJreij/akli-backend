"""Advanced planning (beta): the client re-portions "flexible" subrecipes
(subrecipe.is_flexible — rice, chicken, ...) by hand after the automatic
plan has been generated.

The browser only sends WHICH servings it wants. Macros are always
recomputed here from the subrecipe table, so the plan that reaches
/checkout_summary (and from there the price) and /confirm_order carries
real numbers — the same per-gram pricing rules then apply unchanged.

Rules (product decisions, 2026-11-12):
  - only users with user_beta_feature 'advanced_planning'
  - only subrecipes with is_flexible = true, or a recipe's main
    (recipe_subrecipe.is_main), may change — never the side salad
  - servings in steps of 0.25, from 0.25 (never zero) up to 10
  - a day far from its target is only warned about client-side, never blocked
"""
from __future__ import annotations

import copy
from typing import Any, Dict, List

from utils.supabase_client import supabase

FEATURE = "advanced_planning"
SERVING_STEP = 0.25
SERVING_MIN = 0.25
SERVING_MAX = 10.0

MACRO_KEYS = ("protein", "carbs", "fat", "kcal")


class AdvancedPlanningError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def has_feature(user_id: str | None) -> bool:
    if not user_id:
        return False
    resp = (
        supabase.table("user_beta_feature")
        .select("feature")
        .eq("user_id", user_id)
        .eq("feature", FEATURE)
        .limit(1)
        .execute()
    )
    return bool(resp.data)


def _fetch_subrecipes(ids: List[int]) -> Dict[int, Dict[str, Any]]:
    if not ids:
        return {}
    rows = (
        supabase.table("subrecipe")
        .select("id, name, is_flexible, kcal, protein, carbs, fat")
        .in_("id", sorted(set(ids)))
        .execute()
        .data
        or []
    )
    return {r["id"]: r for r in rows}


def _fetch_main_pairs(recipe_ids: List[int]) -> set:
    """{(recipe_id, subrecipe_id)} where the subrecipe is that recipe's main
    (recipe_subrecipe.is_main) — a recipe's main is always adjustable."""
    if not recipe_ids:
        return set()
    rows = (
        supabase.table("recipe_subrecipe")
        .select("recipe_id, subrecipe_id")
        .in_("recipe_id", sorted(set(recipe_ids)))
        .eq("is_main", True)
        .execute()
        .data
        or []
    )
    return {(r["recipe_id"], r["subrecipe_id"]) for r in rows}


def is_adjustable(recipe_id: Any, sub: Dict[str, Any], row: Dict[str, Any] | None, main_pairs: set) -> bool:
    if row is None or sub.get("is_side_salad"):
        return False
    return bool(row.get("is_flexible")) or (recipe_id, row["id"]) in main_pairs


def valid_servings(value: Any) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        raise AdvancedPlanningError("Servings must be a number")
    if v < SERVING_MIN - 1e-9 or v > SERVING_MAX + 1e-9:
        raise AdvancedPlanningError(f"Servings must be between {SERVING_MIN:g} and {SERVING_MAX:g}")
    steps = v / SERVING_STEP
    if abs(steps - round(steps)) > 1e-6:
        raise AdvancedPlanningError(f"Servings must be in steps of {SERVING_STEP:g}")
    return round(steps) * SERVING_STEP


def _recompute_day(day: Dict[str, Any], subs_by_id: Dict[int, Dict[str, Any]]) -> None:
    """Rebuild every subrecipe's macros (per-serving x servings), each meal's
    totals and the day's totals from the DB rows — same shapes as
    /generate_meal_plan (meal/day totals are ints)."""
    day_tot = {k: 0.0 for k in MACRO_KEYS}
    for meal in day.get("meals") or []:
        meal_tot = {k: 0.0 for k in MACRO_KEYS}
        for sub in meal.get("subrecipes") or []:
            row = subs_by_id.get(sub.get("subrecipe_id"))
            servings = float(sub.get("servings") or 0)
            if row is None:
                # Unknown subrecipe — keep what the plan already had.
                macros = sub.get("macros") or {}
            else:
                macros = {k: float(row.get(k) or 0.0) * servings for k in MACRO_KEYS}
                sub["macros"] = macros
            for k in MACRO_KEYS:
                meal_tot[k] += float(macros.get(k) or 0.0)
        meal["macros"] = {k: int(meal_tot[k]) for k in MACRO_KEYS}
        for k in MACRO_KEYS:
            day_tot[k] += meal_tot[k]
    totals = dict(day.get("totals") or {})
    totals.update({k: int(round(day_tot[k])) for k in MACRO_KEYS})
    day["totals"] = totals


def apply_changes(
    user_id: str, plan: Dict[str, Any], changes: List[Dict[str, Any]]
) -> Dict[str, Any]:
    """changes: [{date, meal_key, subrecipe_id, servings}]. Returns a new plan."""
    if not has_feature(user_id):
        raise AdvancedPlanningError("Advanced planning isn't enabled for this account", 403)
    if not isinstance(plan, dict) or not isinstance(plan.get("days"), list):
        raise AdvancedPlanningError("Missing or invalid plan")
    if plan.get("user_id") and plan.get("user_id") != user_id:
        raise AdvancedPlanningError("Plan belongs to a different user", 403)
    if not isinstance(changes, list):
        raise AdvancedPlanningError("changes must be a list")

    new_plan = copy.deepcopy(plan)
    days_by_date = {d.get("date"): d for d in new_plan["days"]}

    all_ids = [
        s.get("subrecipe_id")
        for d in new_plan["days"]
        for m in (d.get("meals") or [])
        for s in (m.get("subrecipes") or [])
    ]
    subs_by_id = _fetch_subrecipes([i for i in all_ids if i is not None])
    main_pairs = _fetch_main_pairs([
        m.get("recipe_id") for d in new_plan["days"] for m in (d.get("meals") or []) if m.get("recipe_id") is not None
    ])

    touched_dates = set()
    applied: List[Dict[str, Any]] = []  # for the event log (see routes/advanced_planning.py)
    for ch in changes:
        day = days_by_date.get(ch.get("date"))
        if day is None:
            raise AdvancedPlanningError(f"Day {ch.get('date')} is not in this plan")
        meal = next((m for m in day.get("meals") or [] if m.get("meal_key") == ch.get("meal_key")), None)
        if meal is None:
            raise AdvancedPlanningError(f"Meal {ch.get('meal_key')} is not on {ch.get('date')}")
        sub = next(
            (s for s in meal.get("subrecipes") or [] if s.get("subrecipe_id") == ch.get("subrecipe_id")),
            None,
        )
        if sub is None:
            raise AdvancedPlanningError("That item is not part of this meal")
        row = subs_by_id.get(sub["subrecipe_id"])
        if not is_adjustable(meal.get("recipe_id"), sub, row, main_pairs):
            raise AdvancedPlanningError(f"{sub.get('name') or 'This item'} can't be adjusted")
        before = sub.get("servings")
        sub["servings"] = valid_servings(ch.get("servings"))
        touched_dates.add(day["date"])
        applied.append({
            "date": day["date"],
            "meal_type": meal.get("meal_type"),
            "recipe": meal.get("recipe_name"),
            "subrecipe_id": sub["subrecipe_id"],
            "subrecipe": sub.get("name"),
            "from": before,
            "to": sub["servings"],
        })

    day_totals_log = []
    for date in sorted(touched_dates):
        day = days_by_date[date]
        before = {k: (day.get("totals") or {}).get(k) for k in MACRO_KEYS}
        _recompute_day(day, subs_by_id)
        day["manual_servings"] = True
        target = day.get("adjusted_target") or plan.get("daily_macro_target") or {}
        day_totals_log.append({
            "date": date,
            "before": before,
            "after": {k: day["totals"][k] for k in MACRO_KEYS},
            "target_kcal": target.get("kcal"),
        })

    if touched_dates:
        new_plan["advanced_planning"] = True
    # Not part of the plan — popped by the route and written to the event log.
    new_plan["_log"] = {"changes": applied, "days": day_totals_log}
    return new_plan


def normalize_for_confirm(user_id: str, plan: Dict[str, Any]) -> Dict[str, Any]:
    """Called by /confirm_order on a plan marked advanced_planning: re-check
    the beta flag and the serving rules on every flexible item of every
    hand-edited day, and recompute those days' macros from the DB so the
    stored meal_plan_day_recipe_serving rows can't carry made-up numbers."""
    if not has_feature(user_id):
        raise AdvancedPlanningError("Advanced planning isn't enabled for this account", 403)
    new_plan = copy.deepcopy(plan)
    days = [d for d in new_plan.get("days") or [] if d.get("manual_servings")]
    ids = [
        s.get("subrecipe_id")
        for d in days
        for m in (d.get("meals") or [])
        for s in (m.get("subrecipes") or [])
    ]
    subs_by_id = _fetch_subrecipes([i for i in ids if i is not None])
    main_pairs = _fetch_main_pairs([
        m.get("recipe_id") for d in days for m in (d.get("meals") or []) if m.get("recipe_id") is not None
    ])
    for day in days:
        for meal in day.get("meals") or []:
            for sub in meal.get("subrecipes") or []:
                row = subs_by_id.get(sub.get("subrecipe_id"))
                if is_adjustable(meal.get("recipe_id"), sub, row, main_pairs):
                    sub["servings"] = valid_servings(sub.get("servings"))
        _recompute_day(day, subs_by_id)
    return new_plan
