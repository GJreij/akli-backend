from flask import Blueprint, request, jsonify
from datetime import datetime, timedelta
import random
from collections import defaultdict

from utils.supabase_client import supabase
from services.mealplan_service import (
    optimize_subrecipes,
    apply_weekly_carryover,
    update_cumulative_deviation,
    side_salad_options,
)
from services import daily_menu_service as dms
from utils.event_logger import log_event

mealplan_bp = Blueprint("mealplan", __name__)


# =============================================================================
# CONFIG
# =============================================================================

# Users whose daily kcal target exceeds this bypass the shared daily_menu
# template entirely and always receive a fresh personalised day (never
# persisted back to daily_menu, so it can't corrupt the shared template for
# normal-calorie clients). High-calorie targets (athletes, bulking) are
# incompatible with an average-target template, so sharing it makes the LP
# harder to satisfy.
HIGH_KCAL_THRESHOLD = 2800


# =============================================================================
# HELPERS
# =============================================================================

def _parse_date(s: str):
    return datetime.strptime(s, "%Y-%m-%d").date()


def _daterange(d1, d2):
    cur = d1
    while cur <= d2:
        yield cur
        cur += timedelta(days=1)


def _is_weekend(d) -> bool:
    return d.weekday() >= 5


# =============================================================================
# ROUTES
# =============================================================================

@mealplan_bp.route("/check_meal_plan_conflict", methods=["POST"])
def check_meal_plan_conflict():
    data           = request.get_json() or {}
    user_id        = data.get("user_id")
    start_date_str = data.get("start_date")
    end_date_str   = data.get("end_date")

    if not user_id:
        return jsonify({"error": "user_id is required"}), 400

    try:
        start_date = _parse_date(start_date_str)
        end_date   = _parse_date(end_date_str)
    except Exception:
        return jsonify({"error": "Invalid date format. Expected YYYY-MM-DD"}), 400

    if end_date < start_date:
        return jsonify({"error": "end_date must be >= start_date"}), 400

    resp = (
        supabase.table("meal_plan")
        .select("id, start_date, end_date, created_at")
        .eq("user_id", user_id)
        .lte("start_date", str(end_date))
        .gte("end_date", str(start_date))
        .execute()
    )
    conflicts = resp.data or []

    has_conflict = len(conflicts) > 0
    log_event(user_id, "meal_plan_conflict_checked", {
        "start_date": str(start_date),
        "end_date": str(end_date),
        "has_conflict": has_conflict,
        "conflict_count": len(conflicts),
    })
    return jsonify({
        "has_conflict": has_conflict,
        "conflicts":    conflicts,
        "selected":     {"start_date": str(start_date), "end_date": str(end_date)},
    }), 200


@mealplan_bp.route("/generate_meal_plan", methods=["POST"])
def generate_meal_plan():
    data = request.get_json() or {}

    # ------------------------------------------------------------------
    # 1. Parse + validate
    # ------------------------------------------------------------------
    user_id          = data.get("user_id")
    start_date_str   = data.get("start_date")
    end_date_str     = data.get("end_date")
    include_weekends = data.get("include_weekends", False)
    raw_meals        = data.get("meals")
    kcal_override    = data.get("kcal_override")   # optional: client-computed reduced target
    kitchen_id       = data.get("kitchen_id")
    # Optional, for reproducible tests only — production omits it so each
    # generate breaks near-ties differently (variety on regenerate).
    variety_seed     = data.get("variety_seed")

    if not user_id:
        return jsonify({"error": "user_id is required"}), 400

    try:
        start_date = _parse_date(start_date_str)
        end_date   = _parse_date(end_date_str)
    except Exception:
        return jsonify({"error": "Invalid date format. Expected YYYY-MM-DD"}), 400

    if end_date < start_date:
        return jsonify({"error": "end_date must be >= start_date"}), 400

    # ------------------------------------------------------------------
    # 2. Kitchen closures
    # ------------------------------------------------------------------
    closures_q = (
        supabase.table("kitchen_closure")
        .select("closure_date")
        .gte("closure_date", str(start_date))
        .lte("closure_date", str(end_date))
    )
    if kitchen_id is not None:
        closures_q = closures_q.eq("kitchen_id", kitchen_id)

    closed_dates: set = set()
    for row in (closures_q.execute().data or []):
        try:
            closed_dates.add(_parse_date(row["closure_date"]))
        except Exception:
            continue

    # ------------------------------------------------------------------
    # 3. Available dates
    # ------------------------------------------------------------------
    requested_dates = list(_daterange(start_date, end_date))
    candidate_dates = requested_dates if include_weekends else [
        d for d in requested_dates if not _is_weekend(d)
    ]
    available_dates = [d for d in candidate_dates if d not in closed_dates]
    excluded_dates  = sorted(set(candidate_dates) - set(available_dates))

    if not available_dates:
        return jsonify({
            "error":          "kitchen_closed",
            "message":        "The kitchen is closed for all selected dates. Please choose different dates.",
            "start_date":     str(start_date),
            "end_date":       str(end_date),
            "excluded_dates": [str(d) for d in excluded_dates],
        }), 400

    # ------------------------------------------------------------------
    # 4. Meals map — every downstream step (template build, per-client
    #    override, repair, advisory) is scoped to exactly this dict. A
    #    partial request must never read/write/return a meal type that
    #    isn't in here, even for a date that already has a full existing
    #    daily_menu template from someone else's earlier full request.
    # ------------------------------------------------------------------
    allowed_meal_types = {"breakfast", "lunch", "dinner", "snack"}

    if raw_meals:
        meals_map = {k: v for k, v in raw_meals.items() if v in allowed_meal_types}
        if not meals_map:
            return jsonify({"error": "Invalid meals map"}), 400
    else:
        meals_map = {
            "breakfast": "breakfast",
            "lunch":     "lunch",
            "snack":     "snack",
            "dinner":    "dinner",
        }

    # ------------------------------------------------------------------
    # 5. Weekly menus - recipe pool
    # ------------------------------------------------------------------
    weekly_menus = (
        supabase.table("weekly_menu")
        .select("id, week_start_date, week_end_date, weekly_menu_recipe(recipe(*))")
        .lte("week_start_date", str(end_date))
        .gte("week_end_date",   str(start_date))
        .execute()
        .data or []
    )
    if not weekly_menus:
        return jsonify({"error": "No weekly menus found for this date range"}), 404

    allowed_recipe_ids_by_date: dict = defaultdict(set)
    recipes_by_id: dict = {}

    for wm in weekly_menus:
        try:
            ws = _parse_date(wm["week_start_date"])
            we = _parse_date(wm["week_end_date"])
        except Exception:
            continue
        for wmr in (wm.get("weekly_menu_recipe") or []):
            recipe = (wmr or {}).get("recipe")
            if not recipe or not recipe.get("id"):
                continue
            rid = recipe["id"]
            recipes_by_id[rid] = recipe
            for d in _daterange(ws, we):
                allowed_recipe_ids_by_date[d].add(rid)

    all_recipes = list(recipes_by_id.values())
    if not all_recipes:
        return jsonify({"error": "No recipes found inside weekly menus"}), 404

    for d in available_dates:
        if not allowed_recipe_ids_by_date.get(d):
            return jsonify({
                "error":        "No recipes available for at least one selected day",
                "missing_date": str(d),
            }), 404

    # ------------------------------------------------------------------
    # 6. User preferences
    # ------------------------------------------------------------------
    prefs_resp = (
        supabase.table("user_recipe_preferences")
        .select("recipe_id, like, dislike, dont_include")
        .eq("user_id", user_id)
        .execute()
    )
    user_prefs = {p["recipe_id"]: p for p in (prefs_resp.data or [])}

    if all(user_prefs.get(r["id"], {}).get("dont_include") for r in all_recipes):
        return jsonify({"error": "All recipes were excluded by user preferences"}), 400

    # ------------------------------------------------------------------
    # 7. Macro target
    # ------------------------------------------------------------------
    macro_resp = (
        supabase.table("daily_macro_target")
        .select("protein_g, carbs_g, fat_g, kcal_target")
        .eq("user_id", user_id)
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    )
    if not macro_resp.data:
        return jsonify({"error": "No diet set, we're working on it!"}), 400

    t         = macro_resp.data[0]
    protein_g = float(t.get("protein_g")   or 0)
    carbs_g   = float(t.get("carbs_g")     or 0)
    fat_g     = float(t.get("fat_g")       or 0)
    kcal_db   = float(t.get("kcal_target") or (4 * (protein_g + carbs_g) + 9 * fat_g))
    # kcal_override lets the client reduce the daily target when the user is
    # "eating out" for excluded meal types (those calories are not Akli's).
    kcal_t = float(kcal_override) if kcal_override and float(kcal_override) > 0 else kcal_db
    # Scale macros proportionally so they stay relatable to the new kcal target
    # instead of solving toward stale grams sized for the original kcal target.
    macro_scale = (kcal_t / kcal_db) if kcal_db > 0 else 1.0
    target_with_kcal = {
        "protein_g": protein_g * macro_scale,
        "carbs_g":   carbs_g * macro_scale,
        "fat_g":     fat_g * macro_scale,
        "kcal":      kcal_t,
    }
    is_high_kcal = target_with_kcal["kcal"] > HIGH_KCAL_THRESHOLD

    # ------------------------------------------------------------------
    # 8. BATCH PREFETCH - all auxiliary DB lookups done here, once
    # ------------------------------------------------------------------
    all_recipe_ids  = [r["id"] for r in all_recipes]
    active_weekdays = list({d.weekday() for d in available_dates})

    flex_stats       = dms.prefetch_flex_stats(all_recipe_ids)
    recipe_macros    = dms.prefetch_recipe_macros(all_recipe_ids)
    popularity       = dms.prefetch_weekday_popularity(all_recipe_ids, active_weekdays)
    last_eaten       = dms.fetch_last_eaten(user_id, all_recipe_ids)
    other_orders     = dms.fetch_other_orders_by_date(available_dates, exclude_user_id=user_id)
    reference_target = dms.population_reference_target()

    eligible_by_meal_type: dict = defaultdict(list)
    for r in all_recipes:
        for mt in set(meals_map.values()):
            if r.get(f"could_be_{mt}"):
                eligible_by_meal_type[mt].append(r["id"])

    # ------------------------------------------------------------------
    # 9. Whole-range SHARED template (kitchen batching + quality-ranked,
    #    cooldown-spaced scheduling). Built once, regardless of THIS
    #    client's kcal tier — other normal-calorie clients on the same
    #    dates still benefit from it existing/being kept fresh. Layer 2
    #    below decides whether THIS client actually reads from it.
    # ------------------------------------------------------------------
    week_templates = dms.build_week_templates(
        meals_map=meals_map,
        available_dates=available_dates,
        allowed_recipe_ids_by_date=allowed_recipe_ids_by_date,
        all_recipes=all_recipes,
        flex_stats=flex_stats,
        popularity=popularity,
        recipe_macros=recipe_macros,
        reference_target=reference_target,
        other_orders=other_orders,
    )

    # ------------------------------------------------------------------
    # 10. Generate plan - sequential, day-aware (carryover requires this)
    # ------------------------------------------------------------------
    days: list = []
    # {recipe_id: times already in THIS client's plan, any meal type} —
    # drives the least-used-first variety rule in apply_client_overrides /
    # repair_day_if_needed.
    plan_counts: dict = defaultdict(int)
    previous_day_recipes: set = set()  # this client's own picks from the immediately preceding day only
    rng = random.Random(variety_seed)
    recipe_lookup = recipes_by_id
    salad_options_cache: dict = {}  # recipe_id -> salads the client may add to it

    # Repair escalation cap: a real 10-day request against the hardest
    # test client measured 35.93s/32.45s with both dinner+lunch trials
    # enabled on every day — over Heroku's 30s hard timeout. Past 7 days,
    # only the dinner trial runs (1 extra solve/day max instead of 2),
    # trading some repair success rate for staying under the limit.
    REPAIR_TRIAL_CAP_DAYS = 7
    repair_trial_types = (
        ("dinner",) if len(available_dates) > REPAIR_TRIAL_CAP_DAYS else ("dinner", "lunch")
    )

    cumulative_deviation: dict = {"protein_g": 0.0, "carbs_g": 0.0, "fat_g": 0.0, "kcal": 0.0}
    current_week_index: int | None = None

    for day_index, date in enumerate(available_dates):
        # Reset the carryover accumulator at each 7-calendar-day boundary
        # from the range's own start date — confirmed bug (2026-08-18):
        # cumulative_deviation was never reset across the WHOLE requested
        # range (could be 30+ days), despite being named/documented as
        # WEEKLY carryover. Any persistent one-directional real-world
        # tendency (e.g. this catalog running fat-heavy relative to a
        # flat fat target) eventually saturates the +/-25% clamp and
        # pins there for the rest of the range — a real 30-weekday test
        # showed adjusted fat pinned at exactly the -25% floor for 22
        # straight days, and carbs at the +25% ceiling, even after fixing
        # the separate self-referential-deviation bug (see
        # update_cumulative_deviation call below).
        week_index = (date - available_dates[0]).days // 7
        if week_index != current_week_index:
            current_week_index = week_index
            cumulative_deviation = {"protein_g": 0.0, "carbs_g": 0.0, "fat_g": 0.0, "kcal": 0.0}

        allowed_ids_today = allowed_recipe_ids_by_date.get(date, set())

        # High-kcal clients bypass the shared template entirely (empty
        # template_for_date makes every slot go through personal
        # selection) and are never persisted back to daily_menu — that
        # write only happens inside build_week_templates, which never
        # sees this client's picks.
        template_for_date = {} if is_high_kcal else week_templates.get(date, {})

        recipes_by_meal = dms.apply_client_overrides(
            template_for_date=template_for_date,
            meals_map=meals_map,
            allowed_ids_today=allowed_ids_today,
            user_prefs=user_prefs,
            recipe_lookup=recipe_lookup,
            recipe_macros=recipe_macros,
            macro_target=target_with_kcal,
            plan_counts=plan_counts,
            previous_day_recipes=previous_day_recipes,
            rng=rng,
        )

        if not recipes_by_meal:
            return jsonify({
                "error": "Not enough unique recipes for this day",
                "date":  str(date),
            }), 404

        # Run macro optimizer — first day of the week uses the plain target;
        # subsequent days get a carryover-adjusted target that nudges for
        # whatever the week has under/over-shot so far (capped at +/-25%
        # of that day's original target).
        if day_index == 0:
            day_target = target_with_kcal
        else:
            day_target = apply_weekly_carryover(target_with_kcal, cumulative_deviation)

        optimized_subs, loss, day_totals = optimize_subrecipes(
            recipes_by_meal, day_target, user_id=user_id
        )

        # Single bounded repair step (point 3): at most one extra solve,
        # regardless of range length.
        repaired_recipes_by_meal, repaired_totals, swapped_meal_key, repaired_subs, repaired_loss = dms.repair_day_if_needed(
            recipes_by_meal=recipes_by_meal,
            meals_map=meals_map,
            day_totals=day_totals,
            macro_target=day_target,
            allowed_ids_today=allowed_ids_today,
            eligible_by_meal_type=eligible_by_meal_type,
            recipe_macros=recipe_macros,
            plan_counts=plan_counts,
            previous_day_recipes=previous_day_recipes,
            last_eaten=last_eaten,
            flex_stats=flex_stats,
            recipe_lookup=recipe_lookup,
            trial_meal_types=repair_trial_types,
            user_id=user_id,
        )

        if swapped_meal_key is not None:
            # A real substitution happened. repair_day_if_needed already
            # solved this exact candidate internally to decide the swap
            # was good — reuse that result instead of re-solving it here
            # (used to call optimize_subrecipes a second time on the same
            # inputs, doubling the LP cost of every successful repair for
            # no reason).
            optimized_subs, loss, day_totals = repaired_subs, repaired_loss, repaired_totals
            recipes_by_meal = repaired_recipes_by_meal

        # Deviation must accrue against the client's stable BASE target,
        # never the carryover-adjusted day_target — confirmed bug
        # (2026-08-18): using day_target here means once carryover pushes
        # a day's target up (or down) toward the +/-25% clamp, a real day
        # that can't fully reach that already-inflated number reads as
        # "still under," pushing the next day's target further toward the
        # same clamp, which then permanently pins the target at the
        # ceiling/floor for the rest of the range (confirmed via real
        # data: a 22-weekday request pinned at +25% for the last 14 days
        # straight). Measuring against target_with_kcal breaks the loop.
        cumulative_deviation = update_cumulative_deviation(
            cumulative_deviation, target_with_kcal, day_totals
        )

        if day_totals.get("tolerance_used") == "SAFE_FALLBACK":
            log_event(user_id, "mealplan_lp_fallback", {
                "date": str(date),
                "recipe_ids": [info["recipe_id"] for info in recipes_by_meal.values()],
            })

        for info in recipes_by_meal.values():
            plan_counts[info["recipe_id"]] += 1
        previous_day_recipes = {info["recipe_id"] for info in recipes_by_meal.values()}

        # Group optimized subrecipes back by meal slot
        subs_by_meal: dict = {k: [] for k in recipes_by_meal}
        for sub in optimized_subs:
            mk = sub["meal_name"]
            if mk in subs_by_meal:
                subs_by_meal[mk].append({
                    "subrecipe_id": sub["subrecipe_id"],
                    "name":         sub["name"],
                    "servings":     sub["servings"],
                    "macros":       sub["macros"],
                    "is_side_salad": sub.get("is_side_salad", False),
                })

        # Compute per-meal macro totals
        macros_per_meal: dict = {
            mk: {
                "protein": int(sum(s["macros"]["protein"] for s in subs)),
                "carbs":   int(sum(s["macros"]["carbs"]   for s in subs)),
                "fat":     int(sum(s["macros"]["fat"]     for s in subs)),
                "kcal":    int(sum(s["macros"]["kcal"]    for s in subs)),
            }
            for mk, subs in subs_by_meal.items()
        }

        meals_list = [
            {
                "meal_key":    meal_key,
                "meal_type":   info["meal_type"],
                "recipe_id":   info["recipe_id"],
                "recipe_name": info["recipe_name"],
                "photo":       info["photo"],
                "macros":      macros_per_meal.get(meal_key, {}),
                "subrecipes":  subs_by_meal.get(meal_key, []),
                "side_salad_removed": False,
                "side_salad_subrecipe_id": None,
                "side_salad_options": side_salad_options(info["recipe_id"], salad_options_cache),
            }
            for meal_key, info in recipes_by_meal.items()
        ]

        days.append({
            "date":        str(date),
            "weekday":     date.weekday(),
            "is_weekend":  _is_weekend(date),
            "macro_error": loss,
            "totals":      day_totals,
            "meals":       meals_list,
            # Persisted so a later /update_meal_plan re-optimization (see
            # mealplan_update_dynamic_service.py) carries the same
            # carryover-adjusted target forward instead of reverting to the
            # flat global target.
            "adjusted_target": day_target,
        })

    # ------------------------------------------------------------------
    # 11. Plan-level summary, split per weekly_menu period the plan spans
    #     — "what you'll eat / what you won't" only means something
    #     scoped to what was actually available to choose from in THAT
    #     window; a single pool blended across a multi-month plan (which
    #     can span several unrelated weekly_menu rosters) isn't
    #     actionable. Reuses the exact same contiguous-run-by-allowed-
    #     pool segmentation build_week_templates uses, so a period
    #     boundary here always lines up with a real weekly_menu boundary,
    #     not an arbitrary date split.
    # ------------------------------------------------------------------
    def _pool_signature(d) -> frozenset:
        allowed = allowed_recipe_ids_by_date.get(d)
        return frozenset(allowed) if allowed else frozenset()

    period_segments: list = []
    for day in days:
        d = _parse_date(day["date"])
        sig = _pool_signature(d)
        if period_segments and period_segments[-1]["sig"] == sig:
            period_segments[-1]["dates"].append(d)
        else:
            period_segments.append({"sig": sig, "dates": [d]})

    plan_summary = []
    for seg in period_segments:
        seg_date_strs = {str(d) for d in seg["dates"]}
        seg_days = [day for day in days if day["date"] in seg_date_strs]

        used_counts: dict = defaultdict(int)
        for day in seg_days:
            for meal in day["meals"]:
                used_counts[meal["recipe_id"]] += 1

        seg_eligible_ids = {
            rid for ids in eligible_by_meal_type.values() for rid in ids
            if not seg["sig"] or rid in seg["sig"]
        }
        not_used_ids = seg_eligible_ids - set(used_counts.keys())

        plan_summary.append({
            "start_date": str(min(seg["dates"])),
            "end_date":   str(max(seg["dates"])),
            "used": [
                {"recipe_id": rid, "recipe_name": recipe_lookup.get(rid, {}).get("name"), "times_used": count}
                for rid, count in sorted(used_counts.items(), key=lambda kv: -kv[1])
            ],
            "not_used": [
                {"recipe_id": rid, "recipe_name": recipe_lookup.get(rid, {}).get("name")}
                for rid in sorted(not_used_ids)
            ],
        })

    log_event(user_id, "meal_plan_generated", {
        "start_date": str(start_date),
        "end_date": str(end_date),
        "num_days": len(days),
        "meals_per_day": len(meals_map),
        "excluded_dates_count": len(excluded_dates),
    })
    return jsonify({
        "user_id":            user_id,
        "start_date":         str(start_date),
        "end_date":           str(end_date),
        "daily_macro_target": target_with_kcal,
        "excluded_dates":     [str(d) for d in excluded_dates],
        "days":               days,
        "plan_summary":       plan_summary,
    }), 200


@mealplan_bp.route("/update_meal_plan", methods=["POST"])
def update_meal_plan_endpoint():
    """
    Input:  { "original_plan": {...}, "change_logs": [...] }
    Output: Updated optimized meal plan (same shape as /generate_meal_plan).
    """
    data          = request.get_json() or {}
    original_plan = data.get("original_plan")
    logs          = data.get("change_logs", [])

    if not original_plan or not isinstance(logs, list):
        log_event(None, "api_error", {"route": "/update_meal_plan", "status_code": 400, "reason": "missing_or_invalid_input"})
        return jsonify({"error": "Missing or invalid input data"}), 400

    from services.mealplan_update_dynamic_service import update_meal_plan
    updated = update_meal_plan(original_plan, logs)

    user_id = original_plan.get("user_id")
    log_event(user_id, "recipe_swap_triggered", {
        "change_count": len(logs),
        "start_date": original_plan.get("start_date"),
        "end_date": original_plan.get("end_date"),
    })
    return jsonify(updated), 200
