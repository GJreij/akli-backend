from flask import Blueprint, request, jsonify
from datetime import datetime, timedelta
from collections import deque, defaultdict
import random

from utils.supabase_client import supabase
from services.mealplan_service import (
    optimize_subrecipes,
    apply_weekly_carryover,
    update_cumulative_deviation,
)
from utils.event_logger import log_event

mealplan_bp = Blueprint("mealplan", __name__)


# =============================================================================
# CONFIG
# =============================================================================

CATEGORY_OVERLAP_PENALTY      = 4.0
SAME_RECIPE_YESTERDAY_PENALTY = 10.0
WEEKDAY_POPULARITY_WEIGHT     = 1.5
FLEX_SUB_COUNT_WEIGHT         = 0.5
FLEX_SUM_MAX_WEIGHT           = 0.1
BEST_DAY_TRIES_DEFAULT        = 30
RECENT_GLOBAL_MAXLEN          = 10
MEAL_HISTORY_MAXLEN           = 20
POPULARITY_CAP                = 50   # orders at which a recipe hits max popularity score

# Shared daily template is regenerated if it is older than this many days.
# Prevents stale recipe combinations from being served to new clients.
TEMPLATE_TTL_DAYS = 7

# Users whose daily kcal target exceeds this bypass the shared daily_menu
# template entirely and always receive a fresh personalised day.
# High-calorie targets (athletes, bulking) are incompatible with an
# average-target template, so sharing it makes the LP harder to satisfy.
HIGH_KCAL_THRESHOLD = 2800

# Macro compatibility scoring — rewards recipes whose energy ratio (protein %,
# carbs %, fat %) matches the user's daily macro target split.
MACRO_COMPAT_WEIGHT      = 3.0   # scale: +1.5 perfect match → −1.5 at diff=1.0
MACRO_COMPAT_HARD_FILTER = -2.5  # recipes scoring below this are excluded pre-LP


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
# BATCH PREFETCHERS
# One DB round-trip per data type - called once before the generation loop.
# =============================================================================

def prefetch_flex_stats(recipe_ids: list) -> dict:
    """
    Returns { recipe_id: { sub_count, sum_max } } for all recipe IDs in one query.
    """
    if not recipe_ids:
        return {}

    resp = (
        supabase.table("recipe_subrecipe")
        .select("recipe_id, subrecipe(max_serving)")
        .in_("recipe_id", recipe_ids)
        .execute()
    )

    counts: dict = defaultdict(int)
    maxes:  dict = defaultdict(int)

    for rs in (resp.data or []):
        rid = rs.get("recipe_id")
        if rid is None:
            continue
        sub = rs.get("subrecipe") or {}
        maxes[rid]  += int(sub.get("max_serving") or 3)
        counts[rid] += 1

    return {
        rid: {
            "sub_count": max(counts.get(rid, 0), 1),
            "sum_max":   max(maxes.get(rid, 0),  3),
        }
        for rid in recipe_ids
    }


def prefetch_categories(recipe_ids: list) -> dict:
    """
    Returns { recipe_id: frozenset(categories) } for all recipe IDs in one query.
    Returns empty frozensets gracefully if the table does not exist yet.
    """
    if not recipe_ids:
        return {}

    try:
        resp = (
            supabase.table("recipe_category")
            .select("recipe_id, category")
            .in_("recipe_id", recipe_ids)
            .execute()
        )
    except Exception:
        return {rid: frozenset() for rid in recipe_ids}

    cat_map: dict = defaultdict(set)
    for row in (resp.data or []):
        rid = row.get("recipe_id")
        cat = row.get("category")
        if rid is not None and cat:
            cat_map[rid].add(cat)

    return {rid: frozenset(cat_map.get(rid, set())) for rid in recipe_ids}


def prefetch_weekday_popularity(recipe_ids: list, weekdays: list) -> dict:
    """
    Returns { (recipe_id, weekday): normalized_score } in one query.
    Falls back to 0.5 if the table does not exist yet.
    """
    if not recipe_ids or not weekdays:
        return {}

    try:
        resp = (
            supabase.table("meal_plan_day")
            .select("recipe_id, weekday")
            .in_("recipe_id", recipe_ids)
            .in_("weekday", weekdays)
            .execute()
        )
    except Exception:
        return {(rid, wd): 0.5 for rid in recipe_ids for wd in weekdays}

    counts: dict = defaultdict(int)
    for row in (resp.data or []):
        rid = row.get("recipe_id")
        wd  = row.get("weekday")
        if rid is not None and wd is not None:
            counts[(rid, wd)] += 1

    return {
        (rid, wd): min(counts.get((rid, wd), 0) / POPULARITY_CAP, 1.0)
        for rid in recipe_ids
        for wd in weekdays
    }


def prefetch_recipe_macros(recipe_ids: list) -> dict:
    """
    Returns { recipe_id: { protein, carbs, fat, kcal } } summed across all
    subrecipes at 1 serving each. Used for macro-compatibility scoring.
    The macro energy ratio (protein%/carbs%/fat%) is scale-invariant, so
    1-serving aggregates are sufficient for direction/ratio comparisons.
    """
    if not recipe_ids:
        return {}

    resp = (
        supabase.table("recipe_subrecipe")
        .select("recipe_id, subrecipe(kcal, protein, carbs, fat)")
        .in_("recipe_id", recipe_ids)
        .execute()
    )

    totals: dict = defaultdict(lambda: {"protein": 0.0, "carbs": 0.0, "fat": 0.0, "kcal": 0.0})
    for rs in (resp.data or []):
        rid = rs.get("recipe_id")
        sub = rs.get("subrecipe") or {}
        if rid is None:
            continue
        totals[rid]["protein"] += float(sub.get("protein") or 0)
        totals[rid]["carbs"]   += float(sub.get("carbs")   or 0)
        totals[rid]["fat"]     += float(sub.get("fat")     or 0)
        totals[rid]["kcal"]    += float(sub.get("kcal")    or 0)

    return dict(totals)


# =============================================================================
# MACRO COMPATIBILITY SCORING
# =============================================================================

def _macro_compat_score(recipe_id: int, recipe_macros: dict, macro_target: dict) -> float:
    """
    Returns a score bonus/penalty based on how closely a recipe's macro energy
    ratio (protein%/carbs%/fat% of kcal) matches the user's daily target split.

    The ratio is scale-invariant — serving adjustments cannot fix a bad ratio,
    so this score is a reliable signal of LP solvability for this recipe.

    Returns 0.0 if macro data is missing (neutral, no penalty).
    Score range: roughly +1.5 (perfect match) to -3.5 (completely opposite).
    """
    m = recipe_macros.get(recipe_id)
    if not m or m.get("kcal", 0) <= 0:
        return 0.0

    kcal     = max(m["kcal"], 1.0)
    tgt_kcal = max(macro_target.get("kcal", 1.0), 1.0)

    rec_p = (m["protein"] * 4) / kcal
    rec_c = (m["carbs"]   * 4) / kcal
    rec_f = (m["fat"]     * 9) / kcal

    tgt_p = (macro_target.get("protein_g", 0) * 4) / tgt_kcal
    tgt_c = (macro_target.get("carbs_g",   0) * 4) / tgt_kcal
    tgt_f = (macro_target.get("fat_g",     0) * 9) / tgt_kcal

    # Sum of absolute differences in macro energy fractions (range 0–2).
    diff = abs(rec_p - tgt_p) + abs(rec_c - tgt_c) + abs(rec_f - tgt_f)

    # Map: diff=0 → +1.5, diff=0.5 → 0, diff=1.0 → -1.5, diff≥1.33 → ≤ -2.5
    return MACRO_COMPAT_WEIGHT * (0.5 - diff)


# =============================================================================
# RECIPE SCORING  (pure in-memory - zero DB calls)
# =============================================================================

def composite_score(
    recipe:               dict,
    weekday:              int,
    yesterday_recipe_ids: set,
    yesterday_categories: frozenset,
    user_pref:            dict,
    flex_stats:           dict,
    categories:           dict,
    popularity:           dict,
    recipe_macros:        dict,
    macro_target:         dict,
) -> float:
    """
    Scores a recipe for a meal slot on a specific day. Higher = more likely chosen.

      + random jitter         - variety, prevents deterministic plans
      + user like bonus
      + flex bonus            - LP optimizer friendliness
      + weekday popularity    - historical ordering patterns
      + macro compatibility   - reward recipes whose macro ratio matches target
      - user dislike penalty
      - category overlap      - semantic day-to-day variety
      - same recipe yesterday - hard discourage (not hard block)
    """
    rid   = recipe["id"]
    score = random.uniform(0.0, 1.0)

    if user_pref.get("like"):
        score += 2.0
    if user_pref.get("dislike"):
        score -= 5.0

    flex   = flex_stats.get(rid, {"sub_count": 1, "sum_max": 3})
    score += FLEX_SUB_COUNT_WEIGHT * flex["sub_count"]
    score += FLEX_SUM_MAX_WEIGHT   * flex["sum_max"]

    score += WEEKDAY_POPULARITY_WEIGHT * popularity.get((rid, weekday), 0.5)

    # Reward recipes whose macro energy ratio matches the user's daily target.
    score += _macro_compat_score(rid, recipe_macros, macro_target)

    today_cats = categories.get(rid, frozenset())
    score -= CATEGORY_OVERLAP_PENALTY * len(today_cats & yesterday_categories)

    if rid in yesterday_recipe_ids:
        score -= SAME_RECIPE_YESTERDAY_PENALTY

    return score


def weighted_choice_by_score(candidates: list, scores: list) -> dict:
    """
    Probabilistic selection: shifts scores to non-negative, then weights.
    Lower-scored recipes still have a small chance (maintains variety).
    """
    min_score = min(scores)
    weights   = [max(s - min_score + 0.001, 0.001) for s in scores]
    return random.choices(candidates, weights=weights, k=1)[0]


def score_day(
    recipes_by_meal: dict,
    flex_stats:      dict,
    recipe_macros:   dict | None = None,
    macro_target:    dict | None = None,
) -> float:
    """
    Scores a full-day combination by LP optimizer friendliness.
    Rewards more subrecipes/headroom, penalises single-subrecipe meals.
    When recipe_macros and macro_target are supplied, also applies a
    proportional penalty for days whose max-achievable kcal falls short
    of the calorie target — these days will fail the LP pre-feasibility
    check and should be de-prioritised during candidate selection.
    """
    total_sub, total_sum_max, single_sub_meals = 0, 0, 0
    for info in recipes_by_meal.values():
        flex = flex_stats.get(int(info["recipe_id"]), {"sub_count": 1, "sum_max": 3})
        total_sub     += flex["sub_count"]
        total_sum_max += flex["sum_max"]
        if flex["sub_count"] <= 1:
            single_sub_meals += 1

    base = (10.0 * total_sub) + (1.5 * total_sum_max) - (12.0 * single_sub_meals)

    # Feasibility penalty: estimate max achievable kcal for this day.
    # recipe_macros[rid]["kcal"] = kcal at 1 serving of each subrecipe.
    # Scaling by avg_max_serving (sum_max / sub_count) gives a proportional
    # upper bound. Days that structurally cannot reach the target are penalised
    # proportionally (up to -50 for a day that reaches 0% of the target).
    if recipe_macros and macro_target:
        kcal_t = macro_target.get("kcal", 0.0)
        if kcal_t > 0:
            est_max_kcal = 0.0
            for info in recipes_by_meal.values():
                rid  = int(info["recipe_id"])
                flex = flex_stats.get(rid, {"sub_count": 1, "sum_max": 3})
                avg_max = flex["sum_max"] / max(flex["sub_count"], 1)
                est_max_kcal += recipe_macros.get(rid, {}).get("kcal", 0.0) * avg_max
            shortfall = max(0.0, 1.0 - est_max_kcal / kcal_t)
            base -= 50.0 * shortfall

    return base


# =============================================================================
# DAY BUILDING
# =============================================================================

def build_day_candidate(
    meals_map:            dict,
    scored_recipes:       list,
    allowed_ids_today:    set,
    recent_global:        deque,
    meal_hist:            deque,
    weekday:              int,
    yesterday_recipe_ids: set,
    yesterday_categories: frozenset,
    user_prefs:           dict,
    flex_stats:           dict,
    categories:           dict,
    popularity:           dict,
    recipe_macros:        dict,
    macro_target:         dict,
) -> dict | None:
    """
    Builds one full-day candidate. Two-pass per slot:
      1. Strict: excludes recently seen recipe IDs.
      2. Relaxed: drops recency constraint if strict yields nothing.
    All scoring is pure in-memory.
    Returns None if any slot cannot be filled.
    """
    recipes_by_meal: dict = {}
    used_today:      set  = set()

    for meal_key, meal_type in meals_map.items():

        def score_candidates(strict: bool) -> list:
            pairs = []
            for _, r in scored_recipes:
                rid = r["id"]
                if rid not in allowed_ids_today:
                    continue
                if rid in used_today:
                    continue
                if not r.get(f"could_be_{meal_type}", False):
                    continue
                if strict and (rid in recent_global or rid in meal_hist):
                    continue
                # Hard filter: skip recipes extremely incompatible with the
                # macro target — their energy ratio can't be fixed by serving
                # adjustments, so they will make the LP harder to solve.
                if _macro_compat_score(rid, recipe_macros, macro_target) < MACRO_COMPAT_HARD_FILTER:
                    continue
                sc = composite_score(
                    recipe=r,
                    weekday=weekday,
                    yesterday_recipe_ids=yesterday_recipe_ids,
                    yesterday_categories=yesterday_categories,
                    user_pref=user_prefs.get(rid, {}),
                    flex_stats=flex_stats,
                    categories=categories,
                    popularity=popularity,
                    recipe_macros=recipe_macros,
                    macro_target=macro_target,
                )
                pairs.append((r, sc))
            return pairs

        pairs = score_candidates(strict=True)
        if not pairs:
            pairs = score_candidates(strict=False)
        if not pairs:
            return None

        candidates, scores = zip(*pairs)
        chosen = weighted_choice_by_score(list(candidates), list(scores))

        recipes_by_meal[meal_key] = {
            "recipe_id":   chosen["id"],
            "meal_key":    meal_key,
            "meal_type":   meal_type,
            "recipe_name": chosen.get("name"),
            "photo":       chosen.get("photo"),
        }
        used_today.add(chosen["id"])

    return recipes_by_meal


def get_or_create_daily_template(
    date,
    meals_map:            dict,
    scored_recipes:       list,
    allowed_ids_today:    set,
    recent_global:        deque,
    meal_history:         deque,
    user_prefs:           dict,
    weekday:              int,
    yesterday_recipe_ids: set,
    yesterday_categories: frozenset,
    flex_stats:           dict,
    categories:           dict,
    popularity:           dict,
    recipe_macros:        dict,
    macro_target:         dict,
    best_tries:           int = BEST_DAY_TRIES_DEFAULT,
) -> dict | None:
    """
    Returns {meal_key: meal_info} for the given date.

    If a daily_menu template exists: reuse it, swapping disliked/excluded/missing
    slots using the smart scorer.

    If no template: run best_tries candidates, pick the highest-scoring day,
    persist it so all clients on the same kitchen share the same base.
    """
    # High-calorie users (athletes, aggressive bulking) bypass the shared
    # template. Their kcal target is too far above the population average
    # for a shared template to produce an LP-feasible day. Their generated
    # day is NOT saved back to daily_menu so it doesn't corrupt the shared
    # template for normal-calorie clients.
    is_high_kcal = macro_target.get("kcal", 0) > HIGH_KCAL_THRESHOLD

    rows: list = []
    if not is_high_kcal:
        resp = (
            supabase.table("daily_menu")
            .select("meal_type, recipe_id, created_at")
            .eq("date", str(date))
            .execute()
        )
        rows = resp.data or []

        # TTL check: if the template is older than TEMPLATE_TTL_DAYS, discard
        # it and regenerate so stale recipe combinations don't persist forever.
        # Fails silently if the column is missing or unparseable — old template
        # is kept in that case (safe default).
        if rows:
            try:
                oldest = min(
                    datetime.strptime(r["created_at"][:19], "%Y-%m-%dT%H:%M:%S")
                    for r in rows if r.get("created_at")
                )
                if (datetime.utcnow() - oldest).days > TEMPLATE_TTL_DAYS:
                    rows = []
            except Exception:
                pass

    existing = {row["meal_type"]: row["recipe_id"] for row in rows}

    recipe_lookup = {r["id"]: r for _, r in scored_recipes}

    if existing:
        recipes_by_meal: dict = {}
        needs_swap:      list = []

        for meal_key, meal_type in meals_map.items():
            recipe_id = existing.get(meal_type)
            pref      = user_prefs.get(recipe_id, {})

            if not recipe_id or pref.get("dont_include") or pref.get("dislike"):
                needs_swap.append((meal_key, meal_type))
                continue

            recipe = recipe_lookup.get(recipe_id)
            if not recipe:
                needs_swap.append((meal_key, meal_type))
                continue

            recipes_by_meal[meal_key] = {
                "recipe_id":   recipe_id,
                "meal_key":    meal_key,
                "meal_type":   meal_type,
                "recipe_name": recipe.get("name"),
                "photo":       recipe.get("photo"),
            }

        used_today = {info["recipe_id"] for info in recipes_by_meal.values()}

        for meal_key, meal_type in needs_swap:
            pairs = []
            for _, r in scored_recipes:
                rid = r["id"]
                if rid not in allowed_ids_today or rid in used_today:
                    continue
                if not r.get(f"could_be_{meal_type}", False):
                    continue
                if user_prefs.get(rid, {}).get("dont_include"):
                    continue
                # Apply same macro compatibility hard filter for swapped slots.
                if _macro_compat_score(rid, recipe_macros, macro_target) < MACRO_COMPAT_HARD_FILTER:
                    continue
                sc = composite_score(
                    recipe=r,
                    weekday=weekday,
                    yesterday_recipe_ids=yesterday_recipe_ids,
                    yesterday_categories=yesterday_categories,
                    user_pref=user_prefs.get(rid, {}),
                    flex_stats=flex_stats,
                    categories=categories,
                    popularity=popularity,
                    recipe_macros=recipe_macros,
                    macro_target=macro_target,
                )
                pairs.append((r, sc))

            if not pairs:
                return None

            candidates, scores = zip(*pairs)
            chosen = weighted_choice_by_score(list(candidates), list(scores))
            recipes_by_meal[meal_key] = {
                "recipe_id":   chosen["id"],
                "meal_key":    meal_key,
                "meal_type":   meal_type,
                "recipe_name": chosen.get("name"),
                "photo":       chosen.get("photo"),
            }
            used_today.add(chosen["id"])

        return recipes_by_meal

    # No template - generate candidates, keep best
    best_day       = None
    best_day_score = float("-inf")

    for _ in range(best_tries):
        candidate = build_day_candidate(
            meals_map=meals_map,
            scored_recipes=scored_recipes,
            allowed_ids_today=allowed_ids_today,
            recent_global=recent_global,
            meal_hist=meal_history,
            weekday=weekday,
            yesterday_recipe_ids=yesterday_recipe_ids,
            yesterday_categories=yesterday_categories,
            user_prefs=user_prefs,
            flex_stats=flex_stats,
            categories=categories,
            popularity=popularity,
            recipe_macros=recipe_macros,
            macro_target=macro_target,
        )
        if not candidate:
            continue
        sc = score_day(candidate, flex_stats, recipe_macros=recipe_macros, macro_target=macro_target)
        if sc > best_day_score:
            best_day_score = sc
            best_day       = candidate

    if not best_day:
        return None

    # Only persist the template for normal-calorie clients. High-kcal users
    # get a personalised day that must not overwrite the shared template.
    if not is_high_kcal:
        supabase.table("daily_menu").upsert(
            [
                {
                    "date":      str(date),
                    "meal_type": info["meal_type"],
                    "recipe_id": info["recipe_id"],
                }
                for info in best_day.values()
            ],
            on_conflict="date,meal_type",
            ignore_duplicates=True,
        ).execute()

    return best_day


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
    # 4. Meals map
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

    # ------------------------------------------------------------------
    # 7. Filter excluded recipes + build base scored list
    # ------------------------------------------------------------------
    scored_recipes: list = []
    for r in all_recipes:
        rid  = r["id"]
        pref = user_prefs.get(rid, {})
        if pref.get("dont_include"):
            continue
        base = random.random()
        if pref.get("like"):
            base += 2.0
        if pref.get("dislike"):
            base -= 5.0
        scored_recipes.append((base, r))

    if not scored_recipes:
        return jsonify({"error": "All recipes were excluded by user preferences"}), 400

    scored_recipes.sort(key=lambda x: x[0], reverse=True)

    # ------------------------------------------------------------------
    # 8. Macro target
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
    target_with_kcal = {
        "protein_g": protein_g,
        "carbs_g":   carbs_g,
        "fat_g":     fat_g,
        "kcal":      kcal_t,
    }

    # ------------------------------------------------------------------
    # 9. BATCH PREFETCH - all auxiliary DB lookups done here, once
    # ------------------------------------------------------------------
    all_recipe_ids  = [r["id"] for r in all_recipes]
    active_weekdays = list({d.weekday() for d in available_dates})

    flex_stats    = prefetch_flex_stats(all_recipe_ids)
    categories    = prefetch_categories(all_recipe_ids)
    popularity    = prefetch_weekday_popularity(all_recipe_ids, active_weekdays)
    recipe_macros = prefetch_recipe_macros(all_recipe_ids)

    # ------------------------------------------------------------------
    # 10. Generate plan - sequential, day-aware
    # ------------------------------------------------------------------
    BEST_TRIES = int(data.get("day_build_tries") or BEST_DAY_TRIES_DEFAULT)

    days: list = []
    recent_recipes_global = deque(maxlen=RECENT_GLOBAL_MAXLEN)
    meal_history          = deque(maxlen=MEAL_HISTORY_MAXLEN)
    yesterday_recipe_ids: set       = set()
    yesterday_categories: frozenset = frozenset()

    # Weekly carry-over: tracks accrued (actual - target) per macro across
    # the days generated so far in this call, so day N+1's target can be
    # nudged to compensate for day N's misses (see mealplan_service.py).
    cumulative_deviation: dict = {"protein_g": 0.0, "carbs_g": 0.0, "fat_g": 0.0, "kcal": 0.0}

    for day_index, date in enumerate(available_dates):
        allowed_ids_today = allowed_recipe_ids_by_date.get(date, set())

        recipes_by_meal = get_or_create_daily_template(
            date=date,
            meals_map=meals_map,
            scored_recipes=scored_recipes,
            allowed_ids_today=allowed_ids_today,
            recent_global=recent_recipes_global,
            meal_history=meal_history,
            user_prefs=user_prefs,
            weekday=date.weekday(),
            yesterday_recipe_ids=yesterday_recipe_ids,
            yesterday_categories=yesterday_categories,
            flex_stats=flex_stats,
            categories=categories,
            popularity=popularity,
            recipe_macros=recipe_macros,
            macro_target=target_with_kcal,
            best_tries=BEST_TRIES,
        )

        if not recipes_by_meal:
            return jsonify({
                "error": "Not enough unique recipes for this day",
                "date":  str(date),
            }), 404

        # Update rolling history
        for info in recipes_by_meal.values():
            meal_history.append(info["recipe_id"])
            recent_recipes_global.append(info["recipe_id"])

        # Update "yesterday" context for next iteration - uses prefetched categories
        yesterday_recipe_ids = {info["recipe_id"] for info in recipes_by_meal.values()}
        yesterday_categories = frozenset().union(
            *(categories.get(rid, frozenset()) for rid in yesterday_recipe_ids)
        )

        # Run macro optimizer — first day of the week uses the plain target;
        # subsequent days get a carryover-adjusted target that nudges for
        # whatever the week has under/over-shot so far (capped at +/-25%
        # of that day's original target).
        if day_index == 0:
            day_target = target_with_kcal
        else:
            day_target = apply_weekly_carryover(target_with_kcal, cumulative_deviation)

        optimized_subs, loss, day_totals = optimize_subrecipes(
            recipes_by_meal, day_target
        )

        cumulative_deviation = update_cumulative_deviation(
            cumulative_deviation, day_target, day_totals
        )

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
