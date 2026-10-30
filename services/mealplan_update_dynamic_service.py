from datetime import datetime
from typing import Dict, Any, List, Tuple
from collections import defaultdict
import copy

from utils.supabase_client import supabase
from utils.event_logger import log_event
from services.mealplan_service import (
    optimize_subrecipes,
    apply_weekly_carryover,
    update_cumulative_deviation,
    side_salad_options,
)


# ------------------------------------------------------------------
# STEP 1. Consolidate all user changes
# ------------------------------------------------------------------
def consolidate_changes(change_logs: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Take raw change logs and keep only the final state per day & meal_key.

    Output format:
      {
        "2025-01-01": {
          "breakfast": {
            "action": "delete" | "replace" | "add" | "side_salad",
            "old_recipe_id": ...,
            "new_recipe_id": ... (if replace),
            "include_macros_in_rest": bool,
            "removed": bool (if side_salad — remove / add back the meal's side salad),
            "choice": int | None (if side_salad and the log carries side_salad_subrecipe_id —
                      the salad the client added to this meal, None = back to their prefs)
          },
          ...
        },
        "2025-01-02": {
          "deleted_day": True
        }
      }
    """
    # Normalize created_at to datetime
    for log in change_logs:
        if isinstance(log.get("created_at"), str):
            # strip trailing Z if present
            log["created_at"] = datetime.fromisoformat(
                log["created_at"].replace("Z", "")
            )

    # Sort logs chronologically
    change_logs.sort(key=lambda x: x["created_at"])

    # Group by date
    grouped: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for entry in change_logs:
        grouped[entry["date"]].append(entry)

    final_state: Dict[str, Any] = {}

    for date, entries in grouped.items():
        # Check if the entire day has been deleted (no meal_key on log)
        deleted_day_entry = next(
            (e for e in reversed(entries) if e.get("Delete") and not e.get("meal_key")),
            None
        )
        if deleted_day_entry:
            final_state[date] = {"deleted_day": True}
            continue

        # Group by meal_key for this day
        meals_by_key: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for e in entries:
            if e.get("meal_key"):
                meals_by_key[e["meal_key"]].append(e)

        day_actions: Dict[str, Any] = {}
        for meal_key, logs in meals_by_key.items():
            # Take the last log for that meal_key
            last_log = sorted(logs, key=lambda x: x["created_at"])[-1]

            if "side_salad_removed" in last_log:
                day_actions[meal_key] = {
                    "action": "side_salad",
                    "removed": bool(last_log.get("side_salad_removed")),
                }
                if "side_salad_subrecipe_id" in last_log:
                    day_actions[meal_key]["choice"] = last_log.get("side_salad_subrecipe_id")
            elif last_log.get("Delete"):
                day_actions[meal_key] = {
                    "action": "delete",
                    "old_recipe_id": last_log.get("old_recipe_id"),
                    "include_macros_in_rest": last_log.get(
                        "include_macros_in_rest", True
                    ),
                }
            elif last_log.get("new_recipe_id") and last_log.get("old_recipe_id"):
                day_actions[meal_key] = {
                    "action": "replace",
                    "old_recipe_id": last_log.get("old_recipe_id"),
                    "new_recipe_id": last_log.get("new_recipe_id"),
                    "include_macros_in_rest": last_log.get(
                        "include_macros_in_rest", True
                    ),
                }
            elif last_log.get("new_recipe_id") and not last_log.get("old_recipe_id"):
                # Brand-new meal added to a slot that didn't exist on this day
                # before (either it was removed earlier, or excluded entirely
                # from the original plan generation).
                day_actions[meal_key] = {
                    "action": "add",
                    "new_recipe_id": last_log.get("new_recipe_id"),
                    "meal_type": last_log.get("meal_type"),
                }

        if day_actions:
            final_state[date] = day_actions

    return final_state


# ------------------------------------------------------------------
# STEP 2. Fetch recipe + subrecipes/macros from Supabase
# ------------------------------------------------------------------
def fetch_recipe_details(recipe_id: int) -> Dict[str, Any]:
    """
    Fetch a recipe and its subrecipes/macros from Supabase and return
    data in a format compatible with the meal plan structure.
    """
    resp = (
        supabase.table("recipe")
        .select(
            "id, name, photo, "
            "could_be_breakfast, could_be_lunch, could_be_dinner, could_be_snack, "
            "recipe_subrecipe(subrecipe(id, name, kcal, protein, carbs, fat))"
        )
        .eq("id", recipe_id)
        .single()
        .execute()
    )

    recipe = resp.data
    if not recipe:
        return {}

    subrecipes = []
    total_macros = {"protein": 0.0, "carbs": 0.0, "fat": 0.0, "kcal": 0.0}

    for rs in recipe.get("recipe_subrecipe", []):
        s = rs.get("subrecipe", {}) or {}
        macros = {
            "protein": s.get("protein") or 0.0,
            "carbs": s.get("carbs") or 0.0,
            "fat": s.get("fat") or 0.0,
            "kcal": s.get("kcal") or 0.0,
        }
        subrecipes.append(
            {
                "subrecipe_id": s.get("id"),
                "name": s.get("name"),
                "servings": 1,
                "macros": macros,
            }
        )
        for k in total_macros:
            total_macros[k] += macros[k]

    # Guess meal types based on boolean flags on the recipe
    meal_types: List[str] = []
    for t in ["breakfast", "lunch", "dinner", "snack"]:
        if recipe.get(f"could_be_{t}"):
            meal_types.append(t)

    return {
        "recipe_id": recipe["id"],
        "recipe_name": recipe.get("name"),
        "photo": recipe.get("photo"),
        "meal_types": meal_types,
        "subrecipes": subrecipes,
        "macros": {k: round(v) for k, v in total_macros.items()},
    }


# ------------------------------------------------------------------
# STEP 3. Apply user changes + re-optimize macros dynamically
# ------------------------------------------------------------------
def apply_changes_and_optimize(
    current_plan: Dict[str, Any],
    changes: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Apply consolidated changes to the current plan and re-run the optimizer.

    IMPORTANT: We now PERSIST a per-day adjusted macro target under
      day["adjusted_target"]
    so that:
      - If the user marks a meal as 'eating out and DON'T include macros',
        the day's target is permanently reduced for future updates.
      - Subsequent updates (on other days) will still keep this reduced
        target instead of going back to the global daily target.
    """
    updated_plan = copy.deepcopy(current_plan)
    user_id = updated_plan.get("user_id")

    # Global target defined at the root of the plan from /generate_meal_plan
    global_daily_target: Dict[str, Any] = updated_plan.get("daily_macro_target", {}) or {}
    new_days: List[Dict[str, Any]] = []

    # Weekly carry-over: accrue (actual - target) across days IN ORDER as we
    # walk the week, so a re-optimized day's target reflects what happened
    # on every earlier day this week (changed or not). Resets every 7
    # calendar days from the plan's own first day — same fix as the main
    # generate route (2026-08-18): without a reset, this accumulates
    # across the WHOLE plan (which can span many weeks) instead of just
    # "the week," and any persistent one-directional tendency in the
    # actual solves eventually saturates the +/-25% clamp and never
    # recovers for the rest of the plan.
    cumulative_deviation: Dict[str, float] = {
        "protein_g": 0.0, "carbs_g": 0.0, "fat_g": 0.0, "kcal": 0.0,
    }
    plan_start_date = None
    current_week_index = None

    for day in updated_plan.get("days", []):
        date = day["date"]
        day_date = datetime.fromisoformat(date).date()
        if plan_start_date is None:
            plan_start_date = day_date
        week_index = (day_date - plan_start_date).days // 7
        if week_index != current_week_index:
            current_week_index = week_index
            cumulative_deviation = {"protein_g": 0.0, "carbs_g": 0.0, "fat_g": 0.0, "kcal": 0.0}

        day_change = changes.get(date)

        # 1. Skip days explicitly deleted
        if day_change and day_change.get("deleted_day"):
            continue
        # ✅ NEW: If this day has no change logs, keep it intact (no re-optimization)
        if not day_change:
            new_days.append(day)
            # Fold this day's already-solved actuals into the running
            # cumulative deviation, measured against the stable BASE
            # target — never day.get("adjusted_target"). Same runaway-
            # feedback bug as the main generate route (confirmed
            # 2026-08-18): a carryover-inflated adjusted_target used as
            # its own deviation baseline pins the target at the +/-25%
            # clamp for the rest of the plan once it's ever hit.
            if global_daily_target and day.get("totals"):
                cumulative_deviation = update_cumulative_deviation(
                    cumulative_deviation, global_daily_target, day["totals"]
                )
            continue
        # 2. Track which meal types are currently excluded as "eating out"
        #    (persisted across calls) — this is the source of truth for the
        #    day's target reduction, so re-adding a meal can reverse exactly
        #    its own contribution instead of guessing from a compounded number.
        eating_out_set: set = set(day.get("eating_out_meal_types", []))

        updated_meals: List[Dict[str, Any]] = []
        deleted_meal_types_for_day = set()
        added_meal_types_for_day = set()

        # 3. Apply meal-level changes (replace/delete) for meals that already exist
        for meal in day.get("meals", []):
            meal_key = meal["meal_key"]
            meal_type = meal["meal_type"]
            change = (day_change or {}).get(meal_key)

            if not change:
                # No change for this meal
                updated_meals.append(meal)
                continue

            action = change.get("action")

            # --- Remove / add back this meal's side salad ---
            if action == "side_salad":
                meal["side_salad_removed"] = change["removed"]
                if "choice" in change:
                    meal["side_salad_subrecipe_id"] = change["choice"]
                updated_meals.append(meal)
                continue

            # --- Replace recipe ---
            if action == "replace":
                new_recipe_id = change["new_recipe_id"]
                new_recipe = fetch_recipe_details(new_recipe_id)
                if not new_recipe:
                    # If for some reason the new recipe can't be fetched,
                    # keep the original meal as-is
                    updated_meals.append(meal)
                    continue

                meal.update(
                    {
                        "recipe_id": new_recipe["recipe_id"],
                        "recipe_name": new_recipe["recipe_name"],
                        "photo": new_recipe["photo"],
                        "subrecipes": new_recipe["subrecipes"],
                        "macros": new_recipe["macros"],
                        # A removal / added salad was for the old dish, not this one.
                        "side_salad_removed": False,
                        "side_salad_subrecipe_id": None,
                    }
                )
                updated_meals.append(meal)
                continue

            # --- Delete recipe ---
            if action == "delete":
                include_macros = change.get("include_macros_in_rest", True)
                deleted_meal_types_for_day.add(meal_type)

                if include_macros:
                    # Visually remove the meal, but keep the same daily target
                    # so remaining meals try to hit the full macros.
                    continue
                else:
                    # Eating out → reduce the daily target by a fixed percentage
                    eating_out_set.add(meal_type)
                    continue

        # 3b. Apply "add" actions — brand-new meal_keys that don't correspond
        #     to a meal already on this day (re-added after removal, or a
        #     meal type that wasn't on the day at all before).
        existing_meal_keys = {m["meal_key"] for m in day.get("meals", [])}
        for meal_key, change in (day_change or {}).items():
            if change.get("action") != "add" or meal_key in existing_meal_keys:
                continue

            new_recipe_id = change.get("new_recipe_id")
            meal_type = change.get("meal_type")
            if not new_recipe_id or not meal_type:
                continue

            new_recipe = fetch_recipe_details(new_recipe_id)
            if not new_recipe:
                continue

            updated_meals.append(
                {
                    "meal_key": meal_key,
                    "meal_type": meal_type,
                    "recipe_id": new_recipe["recipe_id"],
                    "recipe_name": new_recipe["recipe_name"],
                    "photo": new_recipe["photo"],
                    "subrecipes": new_recipe["subrecipes"],
                    "macros": new_recipe["macros"],
                    "side_salad_removed": False,
                    "side_salad_subrecipe_id": None,
                }
            )
            added_meal_types_for_day.add(meal_type)
            # Re-adding a meal type reverses its own "eating out" exclusion,
            # giving its share of the daily target back.
            eating_out_set.discard(meal_type)

        # 4. If all meal types of the day were deleted (and none re-added) →
        #    drop the entire day (we consider there are 4 possible meal types).
        if len(deleted_meal_types_for_day - added_meal_types_for_day) >= 4:
            continue

        # 5. Recompute the macro target fresh from the global target and the
        #    current eating-out set (always — never compounded from a
        #    previous adjusted_target, so add-back is an exact reversal).
        reduce_macros_pct = 0.0
        for meal_type in eating_out_set:
            if meal_type == "breakfast":
                reduce_macros_pct += 0.30
            elif meal_type == "snack":
                reduce_macros_pct += 0.20
            elif meal_type in ("lunch", "dinner"):
                reduce_macros_pct += 0.40

        adjusted_target = copy.deepcopy(global_daily_target)

        if reduce_macros_pct > 0:
            pct = min(reduce_macros_pct, 1.0)

            for key in ["protein_g", "carbs_g", "fat_g"]:
                if adjusted_target.get(key) is not None:
                    adjusted_target[key] = round(adjusted_target[key] * (1 - pct), 2)

            if adjusted_target.get("kcal") is not None:
                adjusted_target["kcal"] = round(adjusted_target["kcal"] * (1 - pct), 2)

        # 5b. Fold in weekly carry-over from prior days this week (capped at
        #     +/-25% of this day's own baseline target so a bad prior day
        #     can't wreck this day's culinary quality).
        adjusted_target = apply_weekly_carryover(adjusted_target, cumulative_deviation)

        # 6. Prepare recipes_by_meal for optimization
        recipes_by_meal: Dict[str, Dict[str, Any]] = {
            m["meal_key"]: {
                "recipe_id": m["recipe_id"],
                "meal_type": m["meal_type"],
                "recipe_name": m["recipe_name"],
                "photo": m.get("photo"),
                "side_salad_removed": bool(m.get("side_salad_removed")),
                "side_salad_subrecipe_id": m.get("side_salad_subrecipe_id"),
            }
            for m in updated_meals
        }

        # If no meals remain for this day, skip it
        if not recipes_by_meal:
            continue

        # 7. Re-optimize macros for this day using the current adjusted target.
        #    A day whose only change is removing / adding back side salads
        #    pins every other meal to its current servings, so the calories
        #    land on the plate the salad left (bigger portion there) instead
        #    of reshuffling the rest of the day. Falls back to a free solve if
        #    that meal alone can't absorb it.
        salad_toggled = {k for k, c in day_change.items() if isinstance(c, dict) and c.get("action") == "side_salad"}
        reference_servings = None
        if salad_toggled and len(salad_toggled) == len(day_change):
            reference_servings = {
                (m["meal_key"], s["subrecipe_id"]): s["servings"]
                for m in updated_meals
                if m["meal_key"] not in salad_toggled
                for s in (m.get("subrecipes") or [])
            }

        optimized_subs, loss, day_totals = optimize_subrecipes(
        recipes_by_meal,
        adjusted_target,
        allow_under_kcal=(reduce_macros_pct > 0),
        user_id=user_id,
        reference_servings=reference_servings,
        max_movement_pct=0.0 if reference_servings else None,
            )
        if reference_servings and day_totals.get("tolerance_used") == "SAFE_FALLBACK":
            optimized_subs, loss, day_totals = optimize_subrecipes(
                recipes_by_meal,
                adjusted_target,
                allow_under_kcal=(reduce_macros_pct > 0),
                user_id=user_id,
            )

        # Same fix as above: measure against the stable base target, not
        # the carryover-adjusted (and possibly eating-out-reduced) one
        # that was actually solved against.
        cumulative_deviation = update_cumulative_deviation(
            cumulative_deviation, global_daily_target, day_totals
        )

        if day_totals.get("tolerance_used") == "SAFE_FALLBACK":
            log_event(user_id, "mealplan_lp_fallback", {
                "date": date,
                "recipe_ids": [m["recipe_id"] for m in updated_meals],
                "source": "update_meal_plan",
            })

        # 8. Group optimized subrecipes back by meal_key
        subs_by_meal: Dict[str, List[Dict[str, Any]]] = {
            k: [] for k in recipes_by_meal.keys()
        }
        for sub in optimized_subs:
            meal_name = sub.get("meal_name")
            if meal_name in subs_by_meal:
                subs_by_meal[meal_name].append(
                    {
                        "subrecipe_id": sub["subrecipe_id"],
                        "name": sub["name"],
                        "servings": sub["servings"],
                        "macros": sub["macros"],
                        "is_side_salad": sub.get("is_side_salad", False),
                    }
                )

        # 9. Recalculate meal macros using the optimized subrecipes
        for meal in updated_meals:
            sub_list = subs_by_meal.get(meal["meal_key"], [])
            if not sub_list:
                continue

            total_protein = sum(s["macros"]["protein"] for s in sub_list)
            total_carbs = sum(s["macros"]["carbs"] for s in sub_list)
            total_fat = sum(s["macros"]["fat"] for s in sub_list)
            total_kcal = sum(s["macros"]["kcal"] for s in sub_list)

            meal["macros"] = {
                "protein": round(total_protein),
                "carbs": round(total_carbs),
                "fat": round(total_fat),
                "kcal": round(total_kcal),
            }
            meal["subrecipes"] = sub_list
            meal["side_salad_options"] = side_salad_options(meal["recipe_id"])

        # 10. Construct the updated day, including the persisted adjusted_target
        #     and the eating-out set (the latter is what's actually replayed
        #     to recompute adjusted_target on every future update — see step 5).
        updated_day = {
            "date": date,
            "weekday": day["weekday"],
            "is_weekend": day["is_weekend"],
            "macro_error": loss,
            "totals": day_totals,
            "meals": updated_meals,
            "adjusted_target": adjusted_target,
            "eating_out_meal_types": sorted(eating_out_set),
        }

        new_days.append(updated_day)

    updated_plan["days"] = new_days
    return updated_plan


# ------------------------------------------------------------------
# STEP 5. Refresh plan_summary against the UPDATED days
# ------------------------------------------------------------------
def _refresh_summary(updated_plan: Dict[str, Any]) -> Dict[str, Any]:
    """
    apply_changes_and_optimize() only rewrites updated_plan["days"] — it
    deep-copies (and otherwise never touches) plan_summary from the
    ORIGINAL generate_meal_plan response. Confirmed bug (2026-08-18):
    after any swap/remove/add, plan_summary stayed frozen at whatever was
    true BEFORE the edit.

    plan_summary is a LIST of per-period objects ({start_date, end_date,
    used, not_used} — one per contiguous weekly_menu-eligibility segment,
    see build_week_templates' _pool_signature segmentation). Recomputed
    here per period, scoped to that period's own date range and using
    each period's own used+not_used union as its eligible-recipe universe
    (recovered from the ORIGINAL entries rather than re-derived from
    scratch, which would need the weekly_menu/eligibility context this
    function doesn't have).
    """
    days = updated_plan.get("days", [])
    original_periods = updated_plan.get("plan_summary") or []

    recipe_dates_in_plan: Dict[int, set] = defaultdict(set)
    recipe_names: Dict[int, str] = {}
    for day in days:
        for meal in day.get("meals", []):
            recipe_dates_in_plan[meal["recipe_id"]].add(day["date"])
            if meal.get("recipe_name"):
                recipe_names[meal["recipe_id"]] = meal["recipe_name"]

    new_periods = []
    for period in original_periods:
        start_date, end_date = period.get("start_date"), period.get("end_date")
        original_entries = (period.get("used") or []) + (period.get("not_used") or [])
        for entry in original_entries:
            recipe_names.setdefault(entry["recipe_id"], entry.get("recipe_name"))
        eligible_ids = {entry["recipe_id"] for entry in original_entries}

        period_dates_in_plan = {
            rid: {d for d in dates if start_date <= d <= end_date}
            for rid, dates in recipe_dates_in_plan.items()
        }
        used_counts = {
            rid: len(dates)
            for rid, dates in period_dates_in_plan.items()
            if rid in eligible_ids and dates
        }
        not_used_ids = eligible_ids - set(used_counts.keys())

        new_periods.append({
            "start_date": start_date,
            "end_date": end_date,
            "used": [
                {"recipe_id": rid, "recipe_name": recipe_names.get(rid), "times_used": count}
                for rid, count in sorted(used_counts.items(), key=lambda kv: -kv[1])
            ],
            "not_used": [
                {"recipe_id": rid, "recipe_name": recipe_names.get(rid)}
                for rid in sorted(not_used_ids)
            ],
        })

    updated_plan["plan_summary"] = new_periods

    return updated_plan


# ------------------------------------------------------------------
# STEP 4. Entry point — main function
# ------------------------------------------------------------------
def update_meal_plan(
    original_plan: Dict[str, Any],
    raw_change_logs: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """
    High-level entry point called by the Flask endpoint.
    - Consolidates raw change logs
    - Applies them on top of the provided meal plan
    - Re-optimizes macros while persisting per-day macro targets
    - Refreshes plan_summary so it reflects the edit
    """
    consolidated = consolidate_changes(raw_change_logs)
    updated = apply_changes_and_optimize(original_plan, consolidated)
    updated = _refresh_summary(updated)
    return updated
