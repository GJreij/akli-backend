import math
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
# Capped at 20% max — anything looser than that is no longer treated as
# "solved"; BEST_EFFORT_LP (see below) takes over instead of stretching
# the band further.
KCAL_TOLERANCES  = [0.08, 0.10, 0.15, 0.20]
# Paired macro tolerance ladder — slightly wider than kcal since individual
# macros are harder to hit exactly with discrete integer servings.
MACRO_TOLERANCES = [0.12, 0.15, 0.18, 0.20]

# Half-step granularity tried after integer step fails for each tolerance.
SERVING_STEP_FINE = 0.5

# Minimum servings per step size.
SERVING_MIN_BY_STEP = {
    1.0: 1.0,
    0.5: 0.5,
}

DEFAULT_MAX_SERVING = 3

# Objective weights — all expressed as fractions of their macro targets,
# so a 10 g overshoot on protein is equally bad as 10 g on carbs (percentage-wise).
WEIGHT_PROTEIN   = 1.0
WEIGHT_CARBS     = 1.0
WEIGHT_FAT       = 1.0
WEIGHT_KCAL_SOFT = 0.30

# Maximum factor by which max_serving may be auto-scaled when the day's
# recipe combination structurally cannot reach the calorie target at max servings.
# Prevents LP infeasibility for high-calorie users (athletes, etc.).
MAX_SERVING_SCALE_FACTOR = 3.0

# =============================================================================
# CULINARY CONSTRAINT SETS
# The solver runs two passes before falling back to the greedy heuristic:
#   Pass 1 — STRICT:   tighter culinary guardrails, better plate aesthetics.
#   Pass 2 — RELAXED:  looser guardrails, macro accuracy takes full priority.
# All macro hard-bands (kcal ± tol, protein/carbs/fat ± macro_tol) are
# IDENTICAL in both passes — only the culinary layer changes.
# Tune these values freely after testing; they have no effect on macro maths.
# =============================================================================

# ── STRICT culinary constraints (Pass 1) ─────────────────────────────────────
# Meal-type kcal distribution caps (relative to TOTAL solved kcal, not target).
STRICT_BREAKFAST_MAX_PCT       = 0.40
STRICT_SNACK_MAX_PCT           = 0.25
STRICT_DINNER_LUNCH_DIFF_PCT   = 0.40   # |dinner - lunch| / smaller <= 40 %
STRICT_NO_DINNER_YES_LUNCH_PCT = 0.60
STRICT_NO_LUNCH_YES_DINNER_PCT = 0.60

# ── RELAXED culinary constraints (Pass 2) ────────────────────────────────────
# Slightly wider caps so the LP has more room when strict constraints cause
# infeasibility.  Solo-meal % caps (NO_DINNER / NO_LUNCH variants) are dropped
# entirely in this pass — without the paired meal there is no real distribution
# problem worth enforcing.
# Breakfast is intentionally NOT widened here — 40% is a hard aesthetic
# ceiling regardless of pass, so it stays equal to STRICT_BREAKFAST_MAX_PCT.
RELAXED_BREAKFAST_MAX_PCT      = 0.40
RELAXED_SNACK_MAX_PCT          = 0.35
RELAXED_DINNER_LUNCH_DIFF_PCT  = 0.60   # |dinner - lunch| / smaller <= 60 %

# Intra-meal balance for any subrecipe PAIR that has no explicit rule in
# recipe_subrecipe_rule: no subrecipe may exceed this ratio × any other
# subrecipe in the same meal. Prevents "3 Greek yogurts / ½ granola" style
# domination by default. A pair WITH an explicit rule uses that rule instead
# of this flat ratio — see `get_recipe_rules` / `_resolve_rules_for_day`.
DEFAULT_SERVING_BALANCE_RATIO  = 2.5


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


def get_recipe_rules(recipe_id: int) -> List[Dict[str, Any]]:
    """Return subrecipe scaling rules defined for a recipe (recipe_subrecipe_rule table).

    Each rule is keyed by subrecipe_id (not yet resolved to a flattened
    all_subs index — the caller must do that per-meal, since the same
    recipe/rule can appear in more than one meal in a single day).

    A recipe with no rules defined here falls back to the flat
    DEFAULT_SERVING_BALANCE_RATIO for every subrecipe pair in that meal —
    rules are an opt-in override, never a hard dependency for solving.
    Fails open (returns no rules) on any lookup error rather than crashing
    the whole solver.
    """
    try:
        resp = (
            supabase.table("recipe_subrecipe_rule")
            .select("subrecipe_a_id, subrecipe_b_id, rule_type, ratio, fixed_servings")
            .eq("recipe_id", recipe_id)
            .execute()
        )
        return resp.data or []
    except Exception:
        return []


def _resolve_rules_for_day(
    all_subs: List[Dict], recipes_by_meal: Dict[str, Dict]
) -> List[Dict[str, Any]]:
    """Fetch + resolve recipe_subrecipe_rule entries to all_subs indices for
    every meal in the day. One rule fetch per distinct recipe_id, done once
    up front so every LP attempt / fallback call reuses the same resolved list."""
    id_to_idx: Dict[Tuple[str, Any], int] = {
        (s["meal"], s["subrecipe_id"]): i for i, s in enumerate(all_subs)
    }

    resolved: List[Dict[str, Any]] = []
    for meal_key, info in recipes_by_meal.items():
        for rule in get_recipe_rules(info["recipe_id"]):
            a_idx = id_to_idx.get((meal_key, rule["subrecipe_a_id"]))
            if a_idx is None:
                continue
            b_idx = id_to_idx.get((meal_key, rule["subrecipe_b_id"])) if rule.get("subrecipe_b_id") is not None else None
            resolved.append({
                "a_idx": a_idx,
                "b_idx": b_idx,
                "rule_type": rule["rule_type"],
                "ratio": float(rule["ratio"]) if rule.get("ratio") is not None else None,
                "fixed_servings": float(rule["fixed_servings"]) if rule.get("fixed_servings") is not None else None,
            })
    return resolved


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
    resolved_rules: List[Dict[str, Any]] | None = None,
) -> Tuple[List[Dict], float | None, Dict]:
    """
    Greedy fallback: start at 1 serving each, then greedily add servings to
    minimise protein deficit first (protein/kcal ratio), then to fill calories.

    Every candidate serving bump is checked against the same RELAXED culinary
    caps the LP's Pass 2 enforces (meal-type kcal share, dinner/lunch balance)
    plus this recipe's own subrecipe scaling rules where defined, or the flat
    DEFAULT_SERVING_BALANCE_RATIO for any pair without one — see
    `_respects_caps` below. Without this, a day made of single-subrecipe
    meals (the LP's hardest case, since each meal is then just "N copies of
    one fixed-macro block") could hit this fallback and have the greedy
    kcal-fill phase dump almost the entire day's calories into whichever
    single recipe has the highest kcal/serving — producing a lopsided plate
    (e.g. a 1800 kcal lunch next to a 250 kcal dinner) even though the day's
    macro *totals* look perfectly on target.
    """
    resolved_rules = resolved_rules or []
    servings = {i: 1 for i in range(len(all_subs))}

    meal_of:      Dict[int, str] = {i: s["meal"] for i, s in enumerate(all_subs)}
    meal_type_of: Dict[int, Any] = {
        i: recipes_by_meal.get(s["meal"], {}).get("meal_type")
        for i, s in enumerate(all_subs)
    }
    meal_indices: Dict[str, List[int]] = defaultdict(list)
    for i, s in enumerate(all_subs):
        meal_indices[s["meal"]].append(i)

    # Pre-set fixed subrecipes and pull them out of the bump candidate pool
    # entirely — they never move regardless of macro pressure.
    fixed_indices: set[int] = set()
    for rule in resolved_rules:
        if rule["rule_type"] == "fixed":
            idx = rule["a_idx"]
            servings[idx] = rule["fixed_servings"]
            fixed_indices.add(idx)

    # Explicit-rule pairs, both directions, so checking from either side works.
    rules_by_idx: Dict[int, List[Tuple[int, str, float]]] = defaultdict(list)
    covered_pairs: set[frozenset] = set()
    for rule in resolved_rules:
        if rule["rule_type"] == "fixed" or rule["b_idx"] is None:
            continue
        a, b, rt, ratio = rule["a_idx"], rule["b_idx"], rule["rule_type"], rule["ratio"]
        rules_by_idx[a].append((b, rt, ratio))
        inverse = {"gte": "lte", "gt": "lt", "lte": "gte", "lt": "gt", "eq": "eq"}[rt]
        rules_by_idx[b].append((a, inverse, ratio))
        covered_pairs.add(frozenset((a, b)))

    def _kcal_by_meal_type(servs: Dict[int, float]) -> Dict[str, float]:
        out: Dict[str, float] = defaultdict(float)
        for i, s in enumerate(all_subs):
            mt = meal_type_of[i]
            if mt:
                out[mt] += servs[i] * s["macros"]["kcal"]
        return out

    def _respects_rules(idx: int, trial: Dict[int, float]) -> bool:
        """Would bumping idx to trial[idx] violate any explicit rule it
        participates in, or (for any sibling pair with NO explicit rule)
        the flat DEFAULT_SERVING_BALANCE_RATIO?"""
        for other, rt, ratio in rules_by_idx[idx]:
            mine, theirs = trial[idx], trial[other]
            if rt == "gte" and mine < ratio * theirs:
                return False
            if rt == "gt" and mine < ratio * theirs + 1:
                return False
            if rt == "lte" and mine > ratio * theirs:
                return False
            if rt == "lt" and mine > ratio * theirs - 1:
                return False
            if rt == "eq" and mine != theirs:
                return False

        siblings_no_rule = [
            trial[j] for j in meal_indices[meal_of[idx]]
            if j != idx and frozenset((idx, j)) not in covered_pairs
        ]
        if siblings_no_rule and trial[idx] > DEFAULT_SERVING_BALANCE_RATIO * min(siblings_no_rule):
            return False
        return True

    def _respects_balance_caps(idx: int, servs: Dict[int, float]) -> bool:
        """Would bumping idx's serving by one violate the RELAXED meal-type
        kcal caps or this recipe's own subrecipe scaling rules / default ratio?"""
        trial = dict(servs)
        trial[idx] += 1

        if not _respects_rules(idx, trial):
            return False

        by_type = _kcal_by_meal_type(trial)
        total = sum(by_type.values())
        if total <= 0:
            return True

        mt = meal_type_of[idx]
        if mt == "breakfast" and by_type["breakfast"] > RELAXED_BREAKFAST_MAX_PCT * total:
            return False
        if mt == "snack" and by_type["snack"] > RELAXED_SNACK_MAX_PCT * total:
            return False
        if "lunch" in by_type and "dinner" in by_type:
            lunch, dinner = by_type["lunch"], by_type["dinner"]
            smaller = min(lunch, dinner)
            if smaller > 0 and abs(lunch - dinner) / smaller > RELAXED_DINNER_LUNCH_DIFF_PCT:
                return False
        return True

    def best_protein_per_kcal() -> int | None:
        candidates = [
            i for i in range(len(all_subs))
            if i not in fixed_indices and servings[i] < all_subs[i]["max_serving"] and _respects_balance_caps(i, servings)
        ]
        if not candidates:
            return None
        return max(
            candidates,
            key=lambda i: all_subs[i]["macros"]["protein"] / max(all_subs[i]["macros"]["kcal"], 1),
        )

    def best_kcal() -> int | None:
        candidates = [
            i for i in range(len(all_subs))
            if i not in fixed_indices and servings[i] < all_subs[i]["max_serving"] and _respects_balance_caps(i, servings)
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda i: all_subs[i]["macros"]["kcal"])

    totals = _compute_totals(all_subs, servings)

    # Phase 1: push protein toward target
    while totals["protein"] < P_t and totals["kcal"] < 1.2 * kcal_t:
        idx = best_protein_per_kcal()
        if idx is None:
            break
        servings[idx] += 1
        totals = _compute_totals(all_subs, servings)

    # Phase 2: fill calories (only if under-kcal is not allowed)
    if not allow_under_kcal:
        while totals["kcal"] < 0.80 * kcal_t:
            idx = best_kcal()
            if idx is None:
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
    strict_culinary: bool = True,
    resolved_rules: List[Dict[str, Any]] | None = None,
    hard_bounds: bool = True,
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

    5. strict_culinary=True  → STRICT culinary constraint set (Pass 1).
       strict_culinary=False → RELAXED culinary constraint set (Pass 2).
       Macro hard-bands are identical in both passes.

    Returns None if the LP is infeasible or non-optimal.
    """
    serving_min = SERVING_MIN_BY_STEP.get(serving_step, 1.0)
    culinary_tag = "strict" if strict_culinary else "relaxed"
    label = f"MealPlan_tol{int(tol * 100)}_step{serving_step}_{culinary_tag}"

    # ------------------------------------------------------------------
    # Resolve culinary constraint values from the active constraint set.
    # All variables below map 1-to-1 to a CONFIG constant so you can
    # tune them at the top of the file without touching this logic.
    # ------------------------------------------------------------------
    if strict_culinary:
        _breakfast_max    = STRICT_BREAKFAST_MAX_PCT
        _snack_max        = STRICT_SNACK_MAX_PCT
        _dl_diff          = STRICT_DINNER_LUNCH_DIFF_PCT
        _no_dinner_lunch  = STRICT_NO_DINNER_YES_LUNCH_PCT
        _no_lunch_dinner  = STRICT_NO_LUNCH_YES_DINNER_PCT
        _apply_solo_caps  = True   # solo-meal % caps active in strict mode
    else:
        _breakfast_max    = RELAXED_BREAKFAST_MAX_PCT
        _snack_max        = RELAXED_SNACK_MAX_PCT
        _dl_diff          = RELAXED_DINNER_LUNCH_DIFF_PCT
        _no_dinner_lunch  = None   # dropped in relaxed mode
        _no_lunch_dinner  = None   # dropped in relaxed mode
        _apply_solo_caps  = False

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
    # Hard kcal / per-macro band constraints — skippable via hard_bounds.
    # When hard_bounds=False this becomes a BEST-EFFORT pass: no band can
    # make it infeasible, so it always returns an answer, and the objective
    # above already weighs all four macros simultaneously, so that answer
    # is the mathematically closest achievable point to every target at
    # once — not a single-minded "hit kcal, ignore everything else"
    # compromise. Used as the step between the tolerance ladder and the
    # greedy SAFE_FALLBACK so a structurally-infeasible target still gets a
    # real LP answer instead of the heuristic's uncontrolled macro behaviour.
    # ------------------------------------------------------------------
    if hard_bounds:
        prob += total_K <= (1.0 + tol) * kcal_t
        if not allow_under_kcal:
            prob += total_K >= (1.0 - tol) * kcal_t

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
    # Intra-meal serving balance / explicit subrecipe rules.
    # For any subrecipe PAIR that has an explicit rule in
    # recipe_subrecipe_rule, that rule is used (gt/gte/lte/lt/eq, or a
    # fixed-servings pin). Any pair with NO explicit rule falls back to the
    # flat DEFAULT_SERVING_BALANCE_RATIO so it still can't dominate the
    # meal by default. Both sides share the same lower bound (>= 1), so the
    # flat-ratio case is always feasible; explicit rules are recipe-author
    # decisions assumed to be feasible by construction.
    # ------------------------------------------------------------------
    meal_sub_indices: Dict[str, List[int]] = defaultdict(list)
    for _idx, _s in enumerate(all_subs):
        meal_sub_indices[_s["meal"]].append(_idx)

    _covered_pairs: set[frozenset] = set()
    for _rule in (resolved_rules or []):
        _a_idx, _rt, _ratio = _rule["a_idx"], _rule["rule_type"], _rule["ratio"]
        if _rt == "fixed":
            prob += servings_expr[_a_idx] == _rule["fixed_servings"]
            continue
        _b_idx = _rule["b_idx"]
        if _b_idx is None:
            continue
        _covered_pairs.add(frozenset((_a_idx, _b_idx)))
        if _rt == "gte":
            prob += servings_expr[_a_idx] >= _ratio * servings_expr[_b_idx]
        elif _rt == "lte":
            prob += servings_expr[_a_idx] <= _ratio * servings_expr[_b_idx]
        elif _rt == "gt":
            prob += servings_expr[_a_idx] >= _ratio * servings_expr[_b_idx] + serving_step
        elif _rt == "lt":
            prob += servings_expr[_a_idx] <= _ratio * servings_expr[_b_idx] - serving_step
        elif _rt == "eq":
            prob += servings_expr[_a_idx] == servings_expr[_b_idx]

    for _indices in meal_sub_indices.values():
        if len(_indices) < 2:
            continue
        for _a in _indices:
            for _b in _indices:
                if _a == _b or frozenset((_a, _b)) in _covered_pairs:
                    continue
                prob += servings_expr[_a] <= DEFAULT_SERVING_BALANCE_RATIO * servings_expr[_b]

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
        prob += kcal_by_type["snack"]     <= _snack_max     * total_K
        prob += kcal_by_type["breakfast"] <= _breakfast_max * total_K
        prob += kcal_by_type["dinner"] - kcal_by_type["lunch"] <= _dl_diff * kcal_by_type["lunch"]
        prob += kcal_by_type["lunch"] - kcal_by_type["dinner"] <= _dl_diff * kcal_by_type["dinner"]

    elif has_snack and has_lunch and has_dinner and not has_breakfast:
        prob += kcal_by_type["snack"] <= _snack_max * total_K
        prob += kcal_by_type["dinner"] - kcal_by_type["lunch"] <= _dl_diff * kcal_by_type["lunch"]
        prob += kcal_by_type["lunch"] - kcal_by_type["dinner"] <= _dl_diff * kcal_by_type["dinner"]

    elif has_lunch and has_dinner and not has_snack and not has_breakfast:
        prob += kcal_by_type["dinner"] - kcal_by_type["lunch"] <= _dl_diff * kcal_by_type["lunch"]
        prob += kcal_by_type["lunch"] - kcal_by_type["dinner"] <= _dl_diff * kcal_by_type["dinner"]

    elif has_breakfast and has_lunch and has_snack and not has_dinner:
        prob += kcal_by_type["snack"]     <= _snack_max     * total_K
        prob += kcal_by_type["breakfast"] <= _breakfast_max * total_K
        if _apply_solo_caps:
            prob += kcal_by_type["lunch"] <= _no_dinner_lunch * total_K

    elif has_breakfast and has_dinner and has_snack and not has_lunch:
        prob += kcal_by_type["snack"]     <= _snack_max     * total_K
        prob += kcal_by_type["breakfast"] <= _breakfast_max * total_K
        if _apply_solo_caps:
            prob += kcal_by_type["dinner"] <= _no_lunch_dinner * total_K

    elif has_snack and has_dinner and not has_lunch and not has_breakfast:
        prob += kcal_by_type["snack"] <= _snack_max * total_K

    elif has_snack and has_lunch and not has_dinner and not has_breakfast:
        prob += kcal_by_type["snack"] <= _snack_max * total_K

    elif has_breakfast and has_snack and not has_lunch and not has_dinner:
        prob += kcal_by_type["snack"]     <= _snack_max     * total_K
        prob += kcal_by_type["breakfast"] <= _breakfast_max * total_K

    elif has_breakfast and not has_snack and not has_lunch and not has_dinner:
        prob += kcal_by_type["breakfast"] <= _breakfast_max * total_K

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
        "tolerance_used":    tol if hard_bounds else "BEST_EFFORT_LP",
        "serving_step_used": serving_step,
        "culinary_pass":     "strict" if strict_culinary else "relaxed",
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

    # ------------------------------------------------------------------
    # 2b. Pre-feasibility guard: if maxing every subrecipe at its current
    #     max_serving still can't reach (1 - widest_tol) × kcal_t, the LP
    #     will be structurally infeasible at every tolerance level.
    #     Solution: uniformly scale max_serving up, capped at
    #     MAX_SERVING_SCALE_FACTOR, so the ceiling is always reachable.
    # ------------------------------------------------------------------
    if kcal_t > 0:
        max_achievable_kcal = sum(s["max_serving"] * s["macros"]["kcal"] for s in all_subs)
        min_needed_kcal     = (1.0 - KCAL_TOLERANCES[-1]) * kcal_t
        if max_achievable_kcal < min_needed_kcal:
            scale = min(
                (kcal_t / max(max_achievable_kcal, 1.0)) * 1.05,
                MAX_SERVING_SCALE_FACTOR,
            )
            for s in all_subs:
                s["max_serving"] = math.ceil(s["max_serving"] * scale)

    # Resolve any per-recipe subrecipe rules once, up front, for every meal
    # in the day — reused by every LP attempt and by the greedy fallback.
    resolved_rules = _resolve_rules_for_day(all_subs, recipes_by_meal)

    # Guard: if all targets are zero we have nothing to optimise.
    if kcal_t <= 0:
        return _safe_fallback(
            all_subs, recipes_by_meal, P_t, C_t, F_t, kcal_t, allow_under_kcal, resolved_rules
        )

    # ------------------------------------------------------------------
    # 3. Two-pass tolerance ladder.
    #
    #    Pass 1 — STRICT culinary constraints:
    #      Tighter balance ratios and meal-type caps.  Best plate aesthetics.
    #
    #    Pass 2 — RELAXED culinary constraints:
    #      Wider balance ratios, looser meal caps, solo-meal caps dropped.
    #      Reached only when every strict attempt fails.
    #      Macro hard-bands (kcal ± tol, protein/carbs/fat ± macro_tol) are
    #      IDENTICAL in both passes — nutritional accuracy is never traded.
    #
    #    Pass 3 — BEST_EFFORT_LP: same LP, same objective, but the hard
    #      kcal/macro bands are dropped so it is always solvable. This is
    #      what a structurally-infeasible target (diet/recipe-pool
    #      mismatch, etc.) falls into — a real, all-four-macros-considered
    #      LP answer instead of jumping straight to the greedy heuristic.
    #
    #    Pass 4 — greedy fallback (absolute last resort; should be
    #      practically unreachable, since Pass 3 has no hard bounds to
    #      violate and the remaining constraints — serving balance/rules,
    #      meal-type distribution — are satisfiable at minimum servings).
    # ------------------------------------------------------------------
    for strict_culinary in (True, False):
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
                    strict_culinary=strict_culinary,
                    resolved_rules=resolved_rules,
                )
                if result is not None:
                    return result

    for strict_culinary in (False, True):
        for step in (1.0, SERVING_STEP_FINE):
            result = _solve_lp_once(
                all_subs=all_subs,
                recipes_by_meal=recipes_by_meal,
                P_t=P_t,
                C_t=C_t,
                F_t=F_t,
                kcal_t=kcal_t,
                serving_step=step,
                tol=KCAL_TOLERANCES[-1],
                macro_tol=MACRO_TOLERANCES[-1],
                allow_under_kcal=allow_under_kcal,
                strict_culinary=strict_culinary,
                resolved_rules=resolved_rules,
                hard_bounds=False,
            )
            if result is not None:
                return result

    # ------------------------------------------------------------------
    # 4. Even BEST_EFFORT_LP failed (should be exceedingly rare) —
    #    greedy safe fallback as the absolute last resort.
    # ------------------------------------------------------------------
    return _safe_fallback(
        all_subs, recipes_by_meal, P_t, C_t, F_t, kcal_t, allow_under_kcal, resolved_rules
    )


# =============================================================================
# WEEKLY CARRY-OVER BALANCING
# =============================================================================

# Fraction of the accrued cumulative deviation that gets folded into the next
# day's target. Kept modest so a single bad day nudges, rather than forces,
# the following day.
CARRYOVER_FRACTION = 0.5

# Hard cap: an adjusted target may never drift more than this fraction away
# from the original (un-adjusted) target for that day, in either direction.
CARRYOVER_MAX_ADJUST_PCT = 0.25

# Keys this function will adjust if present in macro_target / actual totals.
_CARRYOVER_KEYS = ("protein_g", "carbs_g", "fat_g", "kcal")

# Maps a macro_target key to the corresponding key used in a day's solved
# `day_totals` dict (returned by optimize_subrecipes).
_TARGET_TO_TOTALS_KEY = {
    "protein_g": "protein",
    "carbs_g":   "carbs",
    "fat_g":     "fat",
    "kcal":      "kcal",
}


def apply_weekly_carryover(
    base_target: Dict[str, float],
    cumulative_deviation: Dict[str, float],
    carryover_fraction: float = CARRYOVER_FRACTION,
    max_adjust_pct: float = CARRYOVER_MAX_ADJUST_PCT,
) -> Dict[str, float]:
    """
    Compute an adjusted macro_target for "today", nudging it to compensate
    for the accrued deviation (actual - target) from previous days in the
    same week.

    This is purely a target-shaping step fed INTO optimize_subrecipes — it
    does not touch the LP/tolerance ladder at all, and is fully backward
    compatible: any single-day caller can simply not call this function and
    pass its original macro_target straight into optimize_subrecipes as
    before.

    Parameters
    ----------
    base_target : the day's normal (un-adjusted) macro_target, e.g.
                  { protein_g, carbs_g, fat_g, kcal }
    cumulative_deviation : accrued (actual - target) summed over all
                  previous days this week, using the SAME keys as
                  base_target (protein_g, carbs_g, fat_g, kcal). A positive
                  value means the week is running OVER on that macro so far
                  (today's target gets nudged down); negative means UNDER
                  (today's target gets nudged up).
    carryover_fraction : how much of the cumulative deviation to fold in
                  (0 = no carryover / identical to base_target, 1 = fully
                  compensate in a single day).
    max_adjust_pct : safety cap — the adjusted target is clamped to within
                  +/- this fraction of base_target, so one very bad day
                  cannot wreck the next day's culinary quality.

    Returns
    -------
    A new dict (base_target is not mutated) with the same keys as
    base_target, where each numeric macro key listed in _CARRYOVER_KEYS has
    been adjusted (clamped) and all other keys are passed through unchanged.
    """
    adjusted: Dict[str, float] = dict(base_target)

    for key in _CARRYOVER_KEYS:
        base_val = base_target.get(key)
        if base_val is None:
            continue
        base_val = float(base_val)

        dev = float(cumulative_deviation.get(key) or 0.0)

        # Subtract a fraction of the cumulative deviation: if we've been
        # running OVER (dev > 0), pull today's target down; if UNDER
        # (dev < 0), push today's target up.
        candidate = base_val - carryover_fraction * dev

        # Clamp to +/- max_adjust_pct of the ORIGINAL target for this day.
        lower = base_val * (1.0 - max_adjust_pct)
        upper = base_val * (1.0 + max_adjust_pct)
        if lower > upper:  # guard against negative base_val edge case
            lower, upper = upper, lower
        candidate = max(lower, min(upper, candidate))

        adjusted[key] = candidate

    return adjusted


def update_cumulative_deviation(
    cumulative_deviation: Dict[str, float],
    day_target: Dict[str, float],
    day_totals: Dict[str, Any],
) -> Dict[str, float]:
    """
    Helper for callers running a week-long loop: fold one more solved day
    into the running cumulative_deviation dict (actual - target, summed
    across days so far), returning a NEW dict.

    `day_target` should be the target that was actually fed into
    optimize_subrecipes for that day (i.e. the adjusted_target if carryover
    was applied), and `day_totals` is the third tuple element returned by
    optimize_subrecipes (contains protein/carbs/fat/kcal actuals).

    Skips updating a key if day_totals' tolerance_used indicates the greedy
    fallback path with no numeric totals, but in practice protein/carbs/fat/
    kcal are always present and numeric in day_totals, so this is mainly a
    defensive guard.
    """
    updated = dict(cumulative_deviation)

    for key in _CARRYOVER_KEYS:
        target_val = day_target.get(key)
        if target_val is None:
            continue
        totals_key = _TARGET_TO_TOTALS_KEY[key]
        actual_val = day_totals.get(totals_key)
        if actual_val is None:
            continue
        prev = float(updated.get(key) or 0.0)
        updated[key] = prev + (float(actual_val) - float(target_val))

    return updated
