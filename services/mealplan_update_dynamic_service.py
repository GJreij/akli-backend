from datetime import datetime
from typing import Dict, Any, List, Tuple
from collections import defaultdict
import copy

from utils.supabase_client import supabase
from services.mealplan_service import (
    optimize_subrecipes,
    apply_weekly_carryover,
    update_cumulative_deviation,
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
            "action": "delete" | "replace",
            "old_recipe_id": ...,
            "new_recipe_id": ... (if replace),
            "include_macros_in_rest": bool
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

            if last_log.get("Delete"):
                day_actions[meal_key] = {
                    "action": "delete",
                    "old_recipe_id": last_log.get("old_recipe_id"),
                    "include_macros_in_rest": last_log.get(
                        "include_macros_in_rest", True
                    ),
                }
            elif last_log.get("new_recipe_id") and not last_log.get("Delete"):
                day_actions[meal_key] = {
                    "action": "replace",
                    "old_recipe_id": last_log.get("old_recipe_id"),
                    "new_recipe_id": last_log.get("new_recipe_id"),
                    "include_macros_in_rest": last_log.get(
                        "include_macros_in_rest", True
                    ),
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
            "recipe_subrecipe(subrecipe(id, name, kcal, protein, carbs, fat, max_serving))"
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

    # Global target defined at the root of the plan from /generate_meal_plan
    global_daily_target: Dict[str, Any] = updated_plan.get("daily_macro_target", {}) or {}
    new_days: List[Dict[str, Any]] = []

    # Weekly carry-over: accrue (actual - target) across days IN ORDER as we
    # walk the week, so a re-optimized day's target reflects what happened
    # on every earlier day this week (changed or not).
    cumulative_deviation: Dict[str, float] = {
        "protein_g": 0.0, "carbs_g": 0.0, "fat_g": 0.0, "kcal": 0.0,
    }

    for day in updated_plan.get("days", []):
        date = day["date"]
        day_change = changes.get(date)

        # 1. Skip days explicitly deleted
        if day_change and day_change.get("deleted_day"):
            continue
        # ✅ NEW: If this day has no change logs, keep it intact (no re-optimization)
        if not day_change:
            new_days.append(day)
            # Still fold this day's already-solved actuals into the running
            # cumulative deviation using whatever target it was solved
            # against, so later re-optimized days account for it.
            prior_target = day.get("adjusted_target") or global_daily_target
            if prior_target and day.get("totals"):
                cumulative_deviation = update_cumulative_deviation(
                    cumulative_deviation, prior_target, day["totals"]
                )
            continue
        # 2. Determine baseline target for this specific day
        #    If the day was adjusted previously, re-use that. Otherwise,
        #    fall back to the global daily target.
        baseline_target: Dict[str, Any] = day.get("adjusted_target") or global_daily_target

        updated_meals: List[Dict[str, Any]] = []
        deleted_meal_types_for_day = set()
        reduce_macros_pct = 0.0  # cumulative % reduction of daily macros

        # 3. Apply meal-level changes (replace/delete) and track reductions
        for meal in day.get("meals", []):
            meal_key = meal["meal_key"]
            meal_type = meal["meal_type"]
            change = (day_change or {}).get(meal_key)

            if not change:
                # No change for this meal
                updated_meals.append(meal)
                continue

            action = change.get("action")

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
                    if meal_type == "breakfast":
                        reduce_macros_pct += 0.30
                    elif meal_type == "snack":
                        reduce_macros_pct += 0.20
                    elif meal_type in ["lunch", "dinner"]:
                        reduce_macros_pct += 0.40

                    # We still drop the meal entirely from the day
                    continue

        # 4. If all meal types of the day were deleted → drop the entire day
        #    (we consider there are 4 possible meal types in the system)
        if len(deleted_meal_types_for_day) >= 4:
            continue

        # 5. Adjust macro target based on reduced percentage
        adjusted_target = copy.deepcopy(baseline_target)

        if reduce_macros_pct > 0:
            pct = min(reduce_macros_pct, 1.0)

            for key in ["protein_g", "carbs_g", "fat_g"]:
                if adjusted_target.get(key) is not None:
                    adjusted_target[key] = round(adjusted_target[key] * (1 - pct), 2)

            # 🔴 THIS WAS MISSING
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
            }
            for m in updated_meals
        }

        # If no meals remain for this day, skip it
        if not recipes_by_meal:
            continue

        # 7. Re-optimize macros for this day using the current adjusted target
        optimized_subs, loss, day_totals = optimize_subrecipes(
        recipes_by_meal,
        adjusted_target,
        allow_under_kcal=(reduce_macros_pct > 0)
            )

        cumulative_deviation = update_cumulative_deviation(
            cumulative_deviation, adjusted_target, day_totals
        )

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

        # 10. Construct the updated day, including the persisted adjusted_target
        updated_day = {
            "date": date,
            "weekday": day["weekday"],
            "is_weekend": day["is_weekend"],
            "macro_error": loss,
            "totals": day_totals,
            "meals": updated_meals,
            # Persist day-specific target so that future updates
            # keep the same reservation of calories.
            "adjusted_target": adjusted_target,
        }

        new_days.append(updated_day)

    updated_plan["days"] = new_days
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
    """
    consolidated = consolidate_changes(raw_change_logs)
    updated = apply_changes_and_optimize(original_plan, consolidated)
    return updated
