from typing import Dict, Any, List, Tuple
from collections import defaultdict

from pulp import (
    LpProblem, LpMinimize, LpVariable, lpSum, LpInteger, value,
    PULP_CBC_CMD, LpStatus
)
from utils.supabase_client import supabase


# =============================================================================
# CONFIG
# =============================================================================

# Tolerance ladder: solver tries each in order, first feasible wins.
KCAL_TOLERANCES  = [0.08, 0.10, 0.15, 0.20, 0.40]
# Paired macro tolerance ladder — slightly wider than kcal since individual
# macros are harder to hit exactly with discrete integer servings.
MACRO_TOLERANCES = [0.12, 0.15, 0.20, 0.30, 0.40]

# Half-step granularity tried after integer step fails for each tolerance.
SERVING_STEP_FINE = 0.5

# Minimum servings per step size.
SERVING_MIN_BY_STEP = {
    1.0: 1.0,
    0.5: 0.5,
}

DEFAULT_MAX_SERVING = 3

# Meal-type kcal distribution caps (relative to TOTAL solved kcal, not target).
BREAKFAST_MAX_PCT        = 0.40
SNACK_MAX_PCT            = 0.25
DINNER_LUNCH_DIFF_PCT    = 0.40   # |dinner - lunch| / smaller <= 40%
NO_DINNER_YES_LUNCH_PCT  = 0.60
NO_LUNCH_YES_DINNER_PCT  = 0.60

# Objective weights — all expressed as fractions of their macro targets,
# so a 10 g overshoot on protein is equally bad as 10 g on carbs (percentage-wise).
WEIGHT_PROTEIN   = 1.0
WEIGHT_CARBS     = 1.0
WEIGHT_FAT       = 1.0
WEIGHT_KCAL_SOFT = 0.30


# =============================================================================
# DATA FETCHING
# =============================================================================

def get_recipe_subrecipes(recipe_id: int) -> List[Dict[str, Any]]:
    """Return subrecipes linked to a recipe, enriched with per-serving macros."""
    resp = (
        supabase.table("recipe_subrecipe")
        .select("subrecipe(id, name, max_serving, kcal, protein, carbs, fat)")
        .eq("recipe_id", recipe_id)
        .execute()
    )

    subrecipes = []
    for rs in resp.data or []:
        sub = rs.get("subrecipe") or {}
        subrecipes.append({
            "id":          sub.get("id"),
            "name":        sub.get("name"),
            "max_serving": sub.get("max_serving") or DEFAULT_MAX_SERVING,
            "macros": {
                "kcal":    float(sub.get("kcal")    or 0.0),
                "protein": float(sub.get("protein") or 0.0),
                "carbs":   float(sub.get("carbs")   or 0.0),
                "fat":     float(sub.get("fat")     or 0.0),
            },
        })

    return subrecipes


# =============================================================================
# HELPERS
# =============================================================================

def _compute_totals(all_subs: List[Dict], servings: Dict[int, float]) -> Dict[str, float]:
    """Sum macros across all subrecipes given a servings dict {index: serving_count}."""
    P = sum(servings[i] * s["macros"]["protein"] for i, s in enumerate(all_subs))
    C = sum(servings[i] * s["macros"]["carbs"]   for i, s in enumerate(all_subs))
    F = sum(servings[i] * s["macros"]["fat"]      for i, s in enumerate(all_subs))
    K = sum(servings[i] * s["macros"]["kcal"]     for i, s in enumerate(all_subs))
    return {"protein": P, "carbs": C, "fat": F, "kcal": K}


def _build_result(
    all_subs: List[Dict],
    recipes_by_meal: Dict[str, Dict],
    servings_map: Dict[int, float],
    loss: float | None,
    tolerance_label: Any,
) -> Tuple[List[Dict], float | None, Dict]:
    """Package solver output into the canonical return format."""
    totals = _compute_totals(all_subs, servings_map)

    optimized = []
    for i, s in enumerate(all_subs):
        serv_val  = float(servings_map[i])
        meal_key  = s["meal"]
        meal_type = recipes_by_meal.get(meal_key, {}).get("meal_type")
        mps       = s["macros"]

        optimized.append({
            "subrecipe_id": s["subrecipe_id"],
            "name":         s["name"],
            "meal_name":    meal_key,
            "meal_type":    meal_type,
            "servings":     serv_val,
            "macros": {
                "protein": mps["protein"] * serv_val,
                "carbs":   mps["carbs"]   * serv_val,
                "fat":     mps["fat"]     * serv_val,
                "kcal":    mps["kcal"]    * serv_val,
            },
        })

    day_totals = {
        "protein":        int(round(totals["protein"])),
        "carbs":          int(round(totals["carbs"])),
        "fat":            int(round(totals["fat"])),
        "kcal":           int(round(totals["kcal"])),
        "tolerance_used": tolerance_label,
    }

    return optimized, loss, day_totals


# =============================================================================
# SAFE FALLBACK (greedy heuristic — used only when LP is infeasible at all tolerances)
# =============================================================================

def _safe_fallback(
    all_subs: List[Dict],
    recipes_by_meal: Dict[str, Dict],
    P_t: float,
    C_t: float,
    F_t: float,
    kcal_t: float,
    allow_under_kcal: bool,
) -> Tuple[List[Dict], float | None, Dict]:
    """
    Greedy fallback: start at 1 serving each, then greedily add servings to
    minimise protein deficit first (protein/kcal ratio), then to fill calories.
    """
    servings = {i: 1 for i in range(len(all_subs))}

    def best_protein_per_kcal() -> int:
        return max(
            range(len(all_subs)),
            key=lambda i: (
                all_subs[i]["macros"]["protein"] / max(all_subs[i]["macros"]["kcal"], 1)
                if servings[i] < all_subs[i]["max_serving"] else -1
            ),
        )

    def best_kcal() -> int:
        return max(
            range(len(all_subs)),
            key=lambda i: (
                all_subs[i]["macros"]["kcal"]
                if servings[i] < all_subs[i]["max_serving"] else -1
            ),
        )

    totals = _compute_totals(all_subs, servings)

    # Phase 1: push protein toward target
    while totals["protein"] < P_t and totals["kcal"] < 1.2 * kcal_t:
        idx = best_protein_per_kcal()
        if servings[idx] >= all_subs[idx]["max_serving"]:
            break
        servings[idx] += 1
        totals = _compute_totals(all_subs, servings)

    # Phase 2: fill calories (only if under-kcal is not allowed)
    if not allow_under_kcal:
        while totals["kcal"] < 0.80 * kcal_t:
            idx = best_kcal()
            if servings[idx] >= all_subs[idx]["max_serving"]:
                break
            servings[idx] += 1
            totals = _compute_totals(all_subs, servings)

    return _build_result(all_subs, recipes_by_meal, servings, None, "SAFE_FALLBACK")


# =============================================================================
# CORE LP SOLVER
# =============================================================================

def _solve_lp_once(
    all_subs: List[Dict],
    recipes_by_meal: Dict[str, Dict],
    P_t: float,
    C_t: float,
    F_t: float,
    kcal_t: float,
    serving_step: float,
    tol: float,
    macro_tol: float,
    allow_under_kcal: bool,
) -> Tuple[List[Dict], float, Dict] | None:
    """
    Build and solve one LP instance.

    Key design decisions
    --------------------
    1. Objective is PERCENTAGE-normalised to prevent the solver from trading
       one macro for another based on absolute gram differences.

    2. Kcal deviation is soft-penalised in the objective AND hard-bounded by
       the tolerance band.

    3. Protein, carbs, and fat each have their own hard band (macro_tol),
       so the solver cannot satisfy kcal while leaving any individual macro
       far from its target.

    4. Meal-type distribution caps are relative to total_K (not fixed kcal_t).

    Returns None if the LP is infeasible or non-optimal.
    """
    serving_min = SERVING_MIN_BY_STEP.get(serving_step, 1.0)
    label = f"MealPlan_tol{int(tol * 100)}_step{serving_step}"

    prob = LpProblem(label, LpMinimize)

    # ------------------------------------------------------------------
    # Decision variables
    # ------------------------------------------------------------------
    if serving_step == 1.0:
        x = {
            i: LpVariable(
                f"x_{i}",
                lowBound=int(serving_min),
                upBound=int(s["max_serving"]),
                cat=LpInteger,
            )
            for i, s in enumerate(all_subs)
        }
        servings_expr = x
    else:
        # Half-step: encode as integer multiples of serving_step
        min_units = int(round(serving_min / serving_step))
        y = {
            i: LpVariable(
                f"y_{i}",
                lowBound=min_units,
                upBound=int(round(float(all_subs[i]["max_serving"]) / serving_step)),
                cat=LpInteger,
            )
            for i in range(len(all_subs))
        }
        servings_expr = {i: serving_step * y[i] for i in range(len(all_subs))}

    # ------------------------------------------------------------------
    # Aggregate macro expressions
    # ------------------------------------------------------------------
    total_P = lpSum(servings_expr[i] * s["macros"]["protein"] for i, s in enumerate(all_subs))
    total_C = lpSum(servings_expr[i] * s["macros"]["carbs"]   for i, s in enumerate(all_subs))
    total_F = lpSum(servings_expr[i] * s["macros"]["fat"]     for i, s in enumerate(all_subs))
    total_K = lpSum(servings_expr[i] * s["macros"]["kcal"]    for i, s in enumerate(all_subs))

    # ------------------------------------------------------------------
    # Absolute deviation variables (|total - target| via two-sided constraints)
    # ------------------------------------------------------------------
    dev_P = LpVariable("dev_P", lowBound=0)
    dev_C = LpVariable("dev_C", lowBound=0)
    dev_F = LpVariable("dev_F", lowBound=0)
    dev_K = LpVariable("dev_K", lowBound=0)

    prob += (total_P - P_t) <=  dev_P
    prob += (P_t - total_P) <=  dev_P
    prob += (total_C - C_t) <=  dev_C
    prob += (C_t - total_C) <=  dev_C
    prob += (total_F - F_t) <=  dev_F
    prob += (F_t - total_F) <=  dev_F
    prob += (total_K - kcal_t) <=  dev_K
    prob += (kcal_t - total_K) <=  dev_K

    # ------------------------------------------------------------------
    # Objective: percentage-normalised macro deviations + soft kcal penalty
    # ------------------------------------------------------------------
    safe_P = max(P_t, 1.0)
    safe_C = max(C_t, 1.0)
    safe_F = max(F_t, 1.0)
    safe_K = max(kcal_t, 1.0)

    prob += (
        WEIGHT_PROTEIN     * (dev_P / safe_P)
        + WEIGHT_CARBS     * (dev_C / safe_C)
        + WEIGHT_FAT       * (dev_F / safe_F)
        + WEIGHT_KCAL_SOFT * (dev_K / safe_K)
    )

    # ------------------------------------------------------------------
    # Hard kcal band constraint
    # ------------------------------------------------------------------
    prob += total_K <= (1.0 + tol) * kcal_t
    if not allow_under_kcal:
        prob += total_K >= (1.0 - tol) * kcal_t

    # ------------------------------------------------------------------
    # Hard per-macro band constraints
    # Unlike kcal, macros are only soft in the objective above, which lets
    # the solver hit kcal while ignoring individual macros. These hard bounds
    # force all three macros to land within macro_tol of their targets.
    # ------------------------------------------------------------------
    if P_t > 0:
        prob += total_P >= (1.0 - macro_tol) * P_t
        prob += total_P <= (1.0 + macro_tol) * P_t
    if C_t > 0:
        prob += total_C >= (1.0 - macro_tol) * C_t
        prob += total_C <= (1.0 + macro_tol) * C_t
    if F_t > 0:
        prob += total_F >= (1.0 - macro_tol) * F_t
        prob += total_F <= (1.0 + macro_tol) * F_t

    # ------------------------------------------------------------------
    # Meal-type kcal distribution constraints
    # Caps are relative to total_K (not the fixed kcal_t) so they stay
    # proportionally meaningful when the solver drifts within the band.
    # Uses a single elif chain to avoid multiple conflicting blocks.
    # ------------------------------------------------------------------
    kcal_by_type: Dict[str, Any] = defaultdict(int)
    for i, s in enumerate(all_subs):
        meal_key  = s["meal"]
        meal_type = recipes_by_meal.get(meal_key, {}).get("meal_type")
        if meal_type:
            kcal_by_type[meal_type] = kcal_by_type[meal_type] + servings_expr[i] * s["macros"]["kcal"]

    types = set(kcal_by_type.keys())

    has_breakfast = "breakfast" in types
    has_lunch     = "lunch"     in types
    has_dinner    = "dinner"    in types
    has_snack     = "snack"     in types

    if has_breakfast and has_lunch and has_dinner and has_snack:
        prob += kcal_by_type["snack"]     <= SNACK_MAX_PCT     * total_K
        prob += kcal_by_type["breakfast"] <= BREAKFAST_MAX_PCT * total_K
        prob += kcal_by_type["dinner"] - kcal_by_type["lunch"] <= DINNER_LUNCH_DIFF_PCT * kcal_by_type["lunch"]
        prob += kcal_by_type["lunch"] - kcal_by_type["dinner"] <= DINNER_LUNCH_DIFF_PCT * kcal_by_type["dinner"]

    elif has_snack and has_lunch and has_dinner and not has_breakfast:
        prob += kcal_by_type["snack"] <= SNACK_MAX_PCT * total_K
        prob += kcal_by_type["dinner"] - kcal_by_type["lunch"] <= DINNER_LUNCH_DIFF_PCT * kcal_by_type["lunch"]
        prob += kcal_by_type["lunch"] - kcal_by_type["dinner"] <= DINNER_LUNCH_DIFF_PCT * kcal_by_type["dinner"]

    elif has_lunch and has_dinner and not has_snack and not has_breakfast:
        prob += kcal_by_type["dinner"] - kcal_by_type["lunch"] <= DINNER_LUNCH_DIFF_PCT * kcal_by_type["lunch"]
        prob += kcal_by_type["lunch"] - kcal_by_type["dinner"] <= DINNER_LUNCH_DIFF_PCT * kcal_by_type["dinner"]

    elif has_breakfast and has_lunch and has_snack and not has_dinner:
        prob += kcal_by_type["snack"]     <= SNACK_MAX_PCT     * total_K
        prob += kcal_by_type["breakfast"] <= BREAKFAST_MAX_PCT * total_K
        prob += kcal_by_type["lunch"]     <= NO_DINNER_YES_LUNCH_PCT * total_K

    elif has_breakfast and has_dinner and has_snack and not has_lunch:
        prob += kcal_by_type["snack"]     <= SNACK_MAX_PCT     * total_K
        prob += kcal_by_type["breakfast"] <= BREAKFAST_MAX_PCT * total_K
        prob += kcal_by_type["dinner"]    <= NO_LUNCH_YES_DINNER_PCT * total_K

    elif has_snack and has_dinner and not has_lunch and not has_breakfast:
        prob += kcal_by_type["snack"] <= SNACK_MAX_PCT * total_K

    elif has_snack and has_lunch and not has_dinner and not has_breakfast:
        prob += kcal_by_type["snack"] <= SNACK_MAX_PCT * total_K

    elif has_breakfast and has_snack and not has_lunch and not has_dinner:
        prob += kcal_by_type["snack"]     <= SNACK_MAX_PCT     * total_K
        prob += kcal_by_type["breakfast"] <= BREAKFAST_MAX_PCT * total_K

    elif has_breakfast and not has_snack and not has_lunch and not has_dinner:
        prob += kcal_by_type["breakfast"] <= BREAKFAST_MAX_PCT * total_K

    # All other single-meal or unrecognised combinations: no distribution constraint.

    # ------------------------------------------------------------------
    # Solve
    # ------------------------------------------------------------------
    prob.solve(PULP_CBC_CMD(msg=False))

    if LpStatus[prob.status] != "Optimal":
        return None

    # Reconstruct integer servings from LP solution
    solved_servings = {
        i: float(value(servings_expr[i]))
        for i in range(len(all_subs))
    }

    total_error = float(value(
        WEIGHT_PROTEIN     * (dev_P / safe_P)
        + WEIGHT_CARBS     * (dev_C / safe_C)
        + WEIGHT_FAT       * (dev_F / safe_F)
        + WEIGHT_KCAL_SOFT * (dev_K / safe_K)
    ))

    day_totals = {
        "protein":           int(round(value(total_P))),
        "carbs":             int(round(value(total_C))),
        "fat":               int(round(value(total_F))),
        "kcal":              int(round(value(total_K))),
        "tolerance_used":    tol,
        "serving_step_used": serving_step,
    }

    optimized = []
    for i, s in enumerate(all_subs):
        serv_val  = solved_servings[i]
        meal_key  = s["meal"]
        meal_type = recipes_by_meal.get(meal_key, {}).get("meal_type")
        mps       = s["macros"]

        optimized.append({
            "subrecipe_id": s["subrecipe_id"],
            "name":         s["name"],
            "meal_name":    meal_key,
            "meal_type":    meal_type,
            "servings":     serv_val,
            "macros": {
                "protein": mps["protein"] * serv_val,
                "carbs":   mps["carbs"]   * serv_val,
                "fat":     mps["fat"]     * serv_val,
                "kcal":    mps["kcal"]    * serv_val,
            },
        })

    return optimized, total_error, day_totals


# =============================================================================
# PUBLIC ENTRY POINT
# =============================================================================

def optimize_subrecipes(
    recipes_by_meal: Dict[str, Dict[str, Any]],
    macro_target: Dict[str, float],
    allow_under_kcal: bool = False,
) -> Tuple[List[Dict[str, Any]], float | None, Dict[str, Any]]:
    """
    Given a dict of meals for one day and a daily macro target, determine the
    optimal number of servings per subrecipe using integer linear programming.

    Parameters
    ----------
    recipes_by_meal : { meal_key: { recipe_id, meal_type, ... } }
    macro_target    : { protein_g, carbs_g, fat_g, kcal }
    allow_under_kcal: if True, the solver may go below (1-tol)*kcal without
                      penalty (used when a meal has been deleted/eaten out).

    Returns
    -------
    (optimized_subs, total_error, day_totals)
    - optimized_subs : list of subrecipe dicts with solved servings + macros
    - total_error    : normalised objective value (None for fallback)
    - day_totals     : { protein, carbs, fat, kcal, tolerance_used }
    """

    # ------------------------------------------------------------------
    # 1. Flatten all subrecipes across meals
    # ------------------------------------------------------------------
    all_subs: List[Dict] = []
    for meal_key, info in recipes_by_meal.items():
        subs = get_recipe_subrecipes(info["recipe_id"])
        for s in subs:
            all_subs.append({
                "meal":         meal_key,
                "subrecipe_id": s["id"],
                "name":         s["name"],
                "macros":       s["macros"],
                "max_serving":  float(int(s.get("max_serving") or DEFAULT_MAX_SERVING)),
            })

    if not all_subs:
        return [], 0.0, {
            "protein": 0, "carbs": 0, "fat": 0, "kcal": 0,
            "tolerance_used": None,
        }

    # ------------------------------------------------------------------
    # 2. Resolve targets
    # ------------------------------------------------------------------
    P_t    = float(macro_target.get("protein_g") or 0.0)
    C_t    = float(macro_target.get("carbs_g")   or 0.0)
    F_t    = float(macro_target.get("fat_g")     or 0.0)
    kcal_t = float(macro_target.get("kcal")      or (4.0 * (P_t + C_t) + 9.0 * F_t))

    # Guard: if all targets are zero we have nothing to optimise.
    if kcal_t <= 0:
        return _safe_fallback(
            all_subs, recipes_by_meal, P_t, C_t, F_t, kcal_t, allow_under_kcal
        )

    # ------------------------------------------------------------------
    # 3. Tolerance ladder: paired kcal + macro tolerances, integer then half-step.
    # ------------------------------------------------------------------
    for tol, macro_tol in zip(KCAL_TOLERANCES, MACRO_TOLERANCES):
        for step in (1.0, SERVING_STEP_FINE):
            result = _solve_lp_once(
                all_subs=all_subs,
                recipes_by_meal=recipes_by_meal,
                P_t=P_t,
                C_t=C_t,
                F_t=F_t,
                kcal_t=kcal_t,
                serving_step=step,
                tol=tol,
                macro_tol=macro_tol,
                allow_under_kcal=allow_under_kcal,
            )
            if result is not None:
                return result

    # ------------------------------------------------------------------
    # 4. All LP attempts failed — use greedy safe fallback.
    # ------------------------------------------------------------------
    return _safe_fallback(
        all_subs, recipes_by_meal, P_t, C_t, F_t, kcal_t, allow_under_kcal
    )
