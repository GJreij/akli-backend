import functools
import math
from typing import Dict, Any, List, Tuple
from collections import defaultdict

from pulp import (
    LpProblem, LpMinimize, LpVariable, lpSum, LpInteger, LpBinary, value,
    PULP_CBC_CMD, LpStatus
)
from utils.supabase_client import supabase
from services.pricing_service import get_kcal_discount


# =============================================================================
# CONFIG
# =============================================================================

# Tolerance ladder: solver tries each in order, first feasible wins.
# Capped at 20% max — anything looser than that is no longer treated as
# "solved"; BEST_EFFORT_LP (see below) takes over instead of stretching
# the band further.
KCAL_TOLERANCES  = [0.08, 0.10, 0.15, 0.20]
# Base macro tolerance ladder — these are no longer used directly as the
# hard-band %. They feed `macro_tolerance()` below, which rescales each one
# per-macro, per-client, based on how big a kcal-share that macro represents
# in THIS client's diet. A flat 20% band on a 30g fat target and an 80g fat
# target are very different things in absolute grams; share-scaling keeps
# the absolute slack proportionate instead of letting small targets blow up
# or large targets get an unrealistically wide band.
BASE_MACRO_TOLERANCES = [0.15, 0.18, 0.22, 0.25]

# BASE kcal band for the safety-net pass tried right before BEST_EFFORT_LP
# drops the kcal bound too (see optimize_subrecipes, pass 3b) — always
# passed through kcal_tolerance() before use, same proportional scaling
# as the rest of the ladder, NOT used flat. (First version used this
# value as a flat % directly, which for a large-kcal client gave a much
# wider absolute band than intended — e.g. ~[1732, 4041] kcal for a
# ~2887 target — wide enough that the solver could still ride the band
# down near its floor under protein pressure. Found via real testing:
# a day landed at 1781 kcal, barely above that floor, "hard-bounded" in
# name only.) Still wider than the normal ladder's loosest rung (20%),
# but properly scaled down for large targets like every other tier.
# Macros are deliberately left soft-only at this tier (see that pass's
# comment for why: hard-bounding both kcal and macros here was still
# often infeasible, and leaving BOTH soft let kcal collapse by ~50% to
# protect a macro instead).
SAFETY_NET_KCAL_TOL = 0.40

# kcal-per-gram for each macro, used to convert a gram target into its
# share of total daily kcal.
KCAL_PER_G = {"protein": 4.0, "carbs": 4.0, "fat": 9.0}

# A macro at this kcal-share gets exactly BASE_TOL (no rescaling). Below this
# share it gets a wider %; above it, a tighter %. 0.25 ~ "an even three-way
# split of P/C/F" as the reference point.
REFERENCE_KCAL_SHARE = 0.25

# Clamp so the rescaling never degenerates (share -> 0 blowing the tolerance
# to infinity, or share -> 1 squeezing it to nothing).
MIN_MACRO_TOLERANCE = 0.10
MAX_MACRO_TOLERANCE = 0.75


def macro_tolerance(macro: str, grams_target: float, kcal_t: float, base_tol: float) -> float:
    """Share-scaled tolerance for one macro, for one client/day.

    share = this macro's kcal contribution / total daily kcal.
    A macro that's a small slice of the diet (e.g. fat on a low-fat plan)
    gets a wider relative band; a macro that dominates the diet (e.g. carbs
    on a high-carb plan) gets a tighter one — because moving it by the same
    % moves total kcal much more.
    """
    if kcal_t <= 0 or grams_target <= 0:
        return MAX_MACRO_TOLERANCE
    share = (grams_target * KCAL_PER_G[macro]) / kcal_t
    tol = base_tol * (REFERENCE_KCAL_SHARE / max(share, 0.01))
    return max(MIN_MACRO_TOLERANCE, min(MAX_MACRO_TOLERANCE, tol))


# Reference point for proportional kcal tolerance: below this target size,
# every tier stays exactly its flat % (unchanged behavior). Above it, tiers
# looser than KCAL_TOLERANCE_PROPORTIONAL_ABOVE tighten so the ABSOLUTE kcal
# slack plateaus at a constant (base_tol * REFERENCE_KCAL) instead of
# growing linearly with the target — e.g. base_tol=0.20: a 1500kcal target
# keeps 300kcal of absolute slack; a 3000kcal target tightens to 10% = 300kcal,
# not the 600kcal a flat 20% would allow. The 8%/10% tiers are exempted
# entirely (KCAL_TOLERANCE_PROPORTIONAL_ABOVE = 0.10) — they're already
# tight enough that scaling them down further just makes the strict tier
# harder to hit for big-kcal clients, working against the goal.
REFERENCE_KCAL = 1500.0
MIN_KCAL_TOLERANCE = 0.04
KCAL_TOLERANCE_PROPORTIONAL_ABOVE = 0.10  # rungs at or below this stay flat, always


def kcal_tolerance(kcal_t: float, base_tol: float) -> float:
    """Proportional kcal tolerance — see the module comment above."""
    if base_tol <= KCAL_TOLERANCE_PROPORTIONAL_ABOVE:
        return base_tol
    if kcal_t <= 0 or kcal_t <= REFERENCE_KCAL:
        return base_tol
    return max(MIN_KCAL_TOLERANCE, base_tol * (REFERENCE_KCAL / kcal_t))


# Half-step granularity tried after integer step fails for each tolerance.
SERVING_STEP_FINE = 0.5

# Minimum servings per step size.
SERVING_MIN_BY_STEP = {
    1.0: 1.0,
    0.5: 0.5,
}

# Objective weights — all expressed as fractions of their macro targets,
# so a 10 g overshoot on protein is equally bad as 10 g on carbs (percentage-wise).
WEIGHT_PROTEIN   = 1.0
WEIGHT_CARBS     = 1.0
WEIGHT_FAT       = 1.0
WEIGHT_KCAL_SOFT = 0.30

# Soft penalty (always active, every pass) for snack outweighing lunch/
# dinner, or breakfast outweighing both — see the meal-shape deviation
# terms in _solve_lp_once. Weighted higher than WEIGHT_KCAL_SOFT: this is
# a genuine culinary-sanity rule the product explicitly wants respected
# whenever possible, not a minor tiebreaker, but it must still be able to
# lose to kcal/macro accuracy at BEST_EFFORT_LP rather than force a wildly
# wrong calorie total (the HARD version of this rule, applied only when
# hard_bounds=True, is what actually guarantees zero violations whenever
# the ladder can find a feasible answer at all).
WEIGHT_MEAL_SHAPE_SOFT = 0.6

# Maximum factor by which max_serving may be auto-scaled when the day's
# recipe combination structurally cannot reach the calorie target at max servings.
# Prevents LP infeasibility for high-calorie users (athletes, etc.).
MAX_SERVING_SCALE_FACTOR = 3.0

# Swap's "rebalance the day" option only: how far any OTHER (non-swapped)
# meal's servings may move from their current value. Keeps the day's
# rebalance to a real, executable plate instead of an unbounded subrecipe
# scaling to whatever multiple papers over the swapped meal's macro shift.
SWAP_MAX_MOVEMENT_PCT = 0.3

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

# Intra-meal serving balance, driven by recipe_subrecipe.is_main:
#   - every main's servings >= every non-main's servings in that meal
#     ("the main is the biggest of them all")
#   - any two mains in the same meal are bounded against each other by this
#     ratio, in both directions, so multiple mains can each scale up but
#     can't run away from one another unboundedly.
# recipe_subrecipe.max_serving (or, failing that, subrecipe.max_serving) is
# now a genuinely OPTIONAL per-subrecipe ceiling layered on top of this —
# most subrecipes have none and are bounded only by the is_main relationship.
MAIN_RATIO = 2.5


# =============================================================================
# DATA FETCHING
# =============================================================================

def get_recipe_subrecipes(recipe_id: int) -> List[Dict[str, Any]]:
    """Return subrecipes linked to a recipe, enriched with per-serving macros.

    max_serving resolution: a per-recipe override on the recipe_subrecipe
    join row (set via the recipe editor) wins when present; otherwise falls
    back to the subrecipe's own global max_serving; otherwise None (no cap —
    the is_main/MAIN_RATIO relationship is what governs balance instead).

    is_main comes off the join row itself — it's a per-recipe role, not a
    global property of the subrecipe (the same subrecipe can be the main in
    one recipe and a side in another).
    """
    resp = (
        supabase.table("recipe_subrecipe")
        .select("max_serving, is_main, subrecipe(id, name, max_serving, kcal, protein, carbs, fat)")
        .eq("recipe_id", recipe_id)
        .execute()
    )

    subrecipes = []
    for rs in resp.data or []:
        sub = rs.get("subrecipe") or {}
        override = rs.get("max_serving")
        base = sub.get("max_serving")
        resolved_max = override if override is not None else base
        subrecipes.append({
            "id":          sub.get("id"),
            "name":        sub.get("name"),
            "max_serving": resolved_max,
            "is_main":     bool(rs.get("is_main")),
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


def _compute_fine_step_eligibility(all_subs: List[Dict]) -> set:
    """Returns the set of all_subs indices allowed to use the 0.25-serving
    step in the single-meal path's mixed-granularity attempt: every
    subrecipe if the day is built entirely from single-subrecipe meals
    (the exception — there's no main/non-main distinction to preserve when
    a meal has only one item), otherwise only is_main subrecipes in meals
    that have 2+ subrecipes."""
    meal_sub_indices: Dict[str, List[int]] = defaultdict(list)
    for idx, s in enumerate(all_subs):
        meal_sub_indices[s["meal"]].append(idx)

    if all(len(idxs) == 1 for idxs in meal_sub_indices.values()):
        return set(range(len(all_subs)))

    eligible = set()
    for idxs in meal_sub_indices.values():
        if len(idxs) >= 2:
            for i in idxs:
                if all_subs[i].get("is_main"):
                    eligible.add(i)
    return eligible


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

    Every candidate serving bump is checked against the same RELAXED culinary
    caps the LP's Pass 2 enforces (meal-type kcal share, dinner/lunch balance)
    plus the is_main/MAIN_RATIO balance relationship — see
    `_respects_main_balance` below. Without this, a day made of single-subrecipe
    meals (the LP's hardest case, since each meal is then just "N copies of
    one fixed-macro block") could hit this fallback and have the greedy
    kcal-fill phase dump almost the entire day's calories into whichever
    single recipe has the highest kcal/serving — producing a lopsided plate
    (e.g. a 1800 kcal lunch next to a 250 kcal dinner) even though the day's
    macro *totals* look perfectly on target.
    """
    servings = {i: 1 for i in range(len(all_subs))}

    meal_of:      Dict[int, str] = {i: s["meal"] for i, s in enumerate(all_subs)}
    meal_type_of: Dict[int, Any] = {
        i: recipes_by_meal.get(s["meal"], {}).get("meal_type")
        for i, s in enumerate(all_subs)
    }

    mains_by_meal: Dict[str, List[int]] = defaultdict(list)
    for i, s in enumerate(all_subs):
        if s["is_main"]:
            mains_by_meal[s["meal"]].append(i)

    present_types = {info.get("meal_type") for info in recipes_by_meal.values()}

    def _kcal_by_meal_type(servs: Dict[int, float]) -> Dict[str, float]:
        out: Dict[str, float] = defaultdict(float)
        for i, s in enumerate(all_subs):
            mt = meal_type_of[i]
            if mt:
                out[mt] += servs[i] * s["macros"]["kcal"]
        return out

    def _respects_main_balance(idx: int, trial: Dict[int, float]) -> bool:
        """Would bumping idx to trial[idx] break the is_main ordering (every
        main's servings >= every non-main's, in the same meal) or the
        MAIN_RATIO bound between two mains in the same meal?"""
        meal_mains = mains_by_meal[meal_of[idx]]
        if all_subs[idx]["is_main"]:
            for m in meal_mains:
                if m != idx and trial[idx] > MAIN_RATIO * trial[m]:
                    return False
        else:
            for m in meal_mains:
                if trial[idx] > trial[m]:
                    return False
        return True

    def _respects_balance_caps(idx: int, servs: Dict[int, float]) -> bool:
        """Would bumping idx's serving by one violate the RELAXED meal-type
        kcal caps or the is_main balance relationship?"""
        trial = dict(servs)
        trial[idx] += 1

        if not _respects_main_balance(idx, trial):
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

        # Same relative meal-size sanity rules as _solve_lp_once - the
        # greedy fallback must not reintroduce what the LP passes forbid.
        if mt == "snack":
            if "lunch" in present_types and by_type["snack"] > by_type["lunch"]:
                return False
            if "dinner" in present_types and by_type["snack"] > by_type["dinner"]:
                return False
        if mt == "breakfast":
            if "lunch" in present_types and "dinner" in present_types:
                if by_type["breakfast"] > by_type["lunch"] and by_type["breakfast"] > by_type["dinner"]:
                    return False
            elif "lunch" in present_types and by_type["breakfast"] > by_type["lunch"]:
                return False
            elif "dinner" in present_types and by_type["breakfast"] > by_type["dinner"]:
                return False
        return True

    def _under_ceiling(i: int) -> bool:
        ceiling = all_subs[i]["max_serving"]
        return ceiling is None or servings[i] < ceiling

    def best_protein_per_kcal() -> int | None:
        candidates = [
            i for i in range(len(all_subs))
            if _under_ceiling(i) and _respects_balance_caps(i, servings)
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
            if _under_ceiling(i) and _respects_balance_caps(i, servings)
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
    serving_step,   # 1.0, 0.5, or the sentinel string "mixed_quarter"
    tol: float,
    macro_tols: Dict[str, float],
    allow_under_kcal: bool,
    strict_culinary: bool = True,
    hard_bounds: bool = True,
    macro_hard_bounds: bool = True,
    protein_hard_bound: bool = False,
    skip_balance: bool = False,
    fine_eligible: set | None = None,
    locked_meal_key: str | None = None,
    locked_units: Dict[int, float] | None = None,
    prices: Dict[str, float] | None = None,
    price_ceiling: float | None = None,
    flat_fee: float = 0.0,
    reference_servings: Dict[int, float] | None = None,
    max_movement_pct: float | None = None,
) -> Tuple[List[Dict], float, Dict] | None:
    """
    Build and solve one LP instance.

    Key design decisions
    --------------------
    1. Objective is PERCENTAGE-normalised to prevent the solver from trading
       one macro for another based on absolute gram differences.

    2. Kcal deviation is soft-penalised in the objective AND hard-bounded by
       the tolerance band (independently of macro_hard_bounds — see below).

    3. Protein, carbs, and fat each have their own hard band (macro_tol)
       when macro_hard_bounds=True (the multi-meal path, always). On the
       single-meal path (macro_hard_bounds=False), macro accuracy is
       "best-fit" via the objective only, UNLESS protein_hard_bound=True —
       in that case protein alone gets a hard band while carbs/fat stay
       best-fit, so kcal + protein both land within tolerance and only
       carbs/fat float freely.

    4. Meal-type distribution caps are relative to total_K (not fixed kcal_t).

    5. strict_culinary=True  → STRICT culinary constraint set (Pass 1).
       strict_culinary=False → RELAXED culinary constraint set (Pass 2).
       Macro hard-bands are identical in both passes.

    6. skip_balance=True drops the main-vs-non-main ordering constraint
       (used on single-meal days, where that ordering is what causes
       structural infeasibility) but NEVER the main-vs-main MAIN_RATIO
       bound between two mains sharing a meal — that check stops one main
       from dominating another and has nothing to do with the single-meal
       infeasibility problem skip_balance exists to solve.

    7. locked_meal_key/locked_units (post-confirmation meal swap only):
       when set, the swapped meal's servings are fixed at locked_units
       instead of being solved for — its macros/price still count toward
       every constraint and the objective, it just can't be scaled to an
       odd fraction. Every other meal stays exactly as free as normal.

    8. prices/price_ceiling (same feature): caps total day price at a hard
       ceiling, expressed as a LINEAR constraint on the same decision
       variables the macro totals use. The one subtlety: the real pricing
       model's kcal-discount is a NON-linear function of the solved kcal
       total, which can't go into an LP directly — so the discount used
       here is computed from kcal_t (the target, a constant) instead of
       the solved total_K. kcal_t is already hard-bounded close to
       whatever total_K resolves to, so this is a safe linearization, not
       a different rule.

    9. flat_fee: a flat, swap/edit-specific platform fee (0 by default —
       ordinary generate/update solves never set this) added as a constant
       term onto total_price, so it counts toward price_ceiling/affordability
       checks like any other cost rather than being a surprise tacked on
       after the fact.

    Returns None if the LP is infeasible or non-optimal.
    """
    culinary_tag = "strict" if strict_culinary else "relaxed"
    step_tag = serving_step if isinstance(serving_step, float) else "mixedQ"
    label = f"MealPlan_tol{int(tol * 100)}_step{step_tag}_{culinary_tag}"

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
    # Decision variables. "mixed_quarter": per-subrecipe unit step — 0.25
    # for fine_eligible indices, 0.5 for everyone else, in the SAME LP
    # simultaneously (not two separate solves).
    # ------------------------------------------------------------------
    if serving_step == "mixed_quarter":
        _fine = fine_eligible or set()
        step_by_index = {i: (0.25 if i in _fine else 0.5) for i in range(len(all_subs))}
        y = {
            i: LpVariable(
                f"y_{i}", lowBound=1,  # 1 unit = one step_i -- min is one 0.25 or 0.5 step
                upBound=(
                    int(round(float(all_subs[i]["max_serving"]) / step_by_index[i]))
                    if all_subs[i]["max_serving"] is not None else None
                ),
                cat=LpInteger,
            )
            for i in range(len(all_subs))
        }
        servings_expr = {i: step_by_index[i] * y[i] for i in range(len(all_subs))}
    elif serving_step == 1.0:
        x = {
            i: LpVariable(
                f"x_{i}",
                lowBound=1,
                upBound=(int(s["max_serving"]) if s["max_serving"] is not None else None),
                cat=LpInteger,
            )
            for i, s in enumerate(all_subs)
        }
        servings_expr = x
    else:
        # Half-step: encode as integer multiples of serving_step
        serving_min = SERVING_MIN_BY_STEP.get(serving_step, 1.0)
        min_units = int(round(serving_min / serving_step))
        y = {
            i: LpVariable(
                f"y_{i}",
                lowBound=min_units,
                upBound=(
                    int(round(float(all_subs[i]["max_serving"]) / serving_step))
                    if all_subs[i]["max_serving"] is not None else None
                ),
                cat=LpInteger,
            )
            for i in range(len(all_subs))
        }
        servings_expr = {i: serving_step * y[i] for i in range(len(all_subs))}

    # ------------------------------------------------------------------
    # Lock a meal's servings (post-confirmation swap only). The locked
    # meal's indices are fixed to the caller-supplied constants instead of
    # being left as free variables — everything downstream (macro totals,
    # price, every constraint) still counts them, they just can't move.
    # ------------------------------------------------------------------
    if locked_meal_key is not None and locked_units:
        for i, s in enumerate(all_subs):
            if s["meal"] == locked_meal_key and i in locked_units:
                prob += servings_expr[i] == locked_units[i]

    # ------------------------------------------------------------------
    # Bound how far OTHER (non-locked) meals may move from their current
    # servings (post-confirmation swap only). Without this, a subrecipe
    # with no explicit max_serving is free to scale to whatever multiple
    # the LP needs to paper over the locked meal's macro shortfall — found
    # in testing to produce genuinely absurd plates (6x a single subrecipe)
    # in exchange for a day-level macro total that merely looks correct on
    # paper. This never applies to the locked meal itself (already exactly
    # fixed above) or to a normal generate/update solve (reference_servings
    # is only ever passed for a swap's "rebalance the day" option).
    # ------------------------------------------------------------------
    if reference_servings and max_movement_pct is not None:
        for i, s in enumerate(all_subs):
            if s["meal"] == locked_meal_key:
                continue
            ref = reference_servings.get(i)
            if ref is None:
                continue
            prob += servings_expr[i] <= ref * (1 + max_movement_pct)
            prob += servings_expr[i] >= ref * (1 - max_movement_pct)

    # ------------------------------------------------------------------
    # Aggregate macro expressions
    # ------------------------------------------------------------------
    total_P = lpSum(servings_expr[i] * s["macros"]["protein"] for i, s in enumerate(all_subs))
    total_C = lpSum(servings_expr[i] * s["macros"]["carbs"]   for i, s in enumerate(all_subs))
    total_F = lpSum(servings_expr[i] * s["macros"]["fat"]     for i, s in enumerate(all_subs))
    total_K = lpSum(servings_expr[i] * s["macros"]["kcal"]    for i, s in enumerate(all_subs))

    # Price expression (post-confirmation swap only — see point 8 above).
    # discount_pct is deliberately computed from kcal_t (constant), not
    # total_K (solved), since the real discount curve is non-linear in the
    # solved kcal and can't go into an LP directly.
    total_price = None
    if prices is not None:
        discount_pct = get_kcal_discount(kcal_t)
        total_price = (
            prices["day_packaging_price"]
            + (1 - discount_pct) * (
                total_P * prices["protein_price_per_g"]
                + total_C * prices["carbs_price_per_g"]
                + total_F * prices["fat_price_per_g"]
            )
            + len(recipes_by_meal) * prices["recipe_packaging_price"]
            + len(all_subs) * prices["subrecipe_packaging_price"]
            + (prices.get("single_meal_fee", 0) or 0) * (1 if len(recipes_by_meal) == 1 else 0)
            + flat_fee
        )

    # Per-meal-type kcal, needed both for the hard distribution constraints
    # further down AND for the soft meal-shape penalty folded into the
    # objective below (computed here, early, so both can use it).
    kcal_by_type: Dict[str, Any] = defaultdict(int)
    for i, s in enumerate(all_subs):
        meal_key  = s["meal"]
        meal_type = recipes_by_meal.get(meal_key, {}).get("meal_type")
        if meal_type:
            kcal_by_type[meal_type] = kcal_by_type[meal_type] + servings_expr[i] * s["macros"]["kcal"]
    types         = set(kcal_by_type.keys())
    has_breakfast = "breakfast" in types
    has_lunch     = "lunch"     in types
    has_dinner    = "dinner"    in types
    has_snack     = "snack"     in types

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
    # Meal-shape SOFT deviation terms — how much snack outweighs lunch/
    # dinner, and how much breakfast outweighs BOTH lunch and dinner.
    # Always folded into the objective (all passes, including
    # BEST_EFFORT_LP), separately from the HARD versions of these same
    # rules added further below (which only apply when hard_bounds=True).
    # This is deliberate: BEST_EFFORT_LP exists specifically to always
    # find an answer close to the kcal/macro target by dropping every
    # other hard bound — hard-walling these shape rules there too can
    # make hitting the target itself infeasible (found via real testing:
    # a 3-meal day where satisfying "snack <= lunch" left no way to reach
    # anywhere near kcal target, so the solver settled ~50% under target
    # instead). Soft-only at that tier means shape is still strongly
    # discouraged from being violated, but never at the cost of a wildly
    # wrong calorie total.
    # ------------------------------------------------------------------
    dev_snack_v_lunch  = LpVariable("dev_snack_v_lunch",  lowBound=0)
    dev_snack_v_dinner = LpVariable("dev_snack_v_dinner", lowBound=0)
    dev_breakfast_v_bigger = LpVariable("dev_breakfast_v_bigger", lowBound=0)

    if has_snack and has_lunch:
        prob += kcal_by_type["snack"] - kcal_by_type["lunch"] <= dev_snack_v_lunch
    if has_snack and has_dinner:
        prob += kcal_by_type["snack"] - kcal_by_type["dinner"] <= dev_snack_v_dinner

    if has_breakfast and has_lunch and has_dinner:
        _shape_big_m = 3.0 * max(kcal_t, 1.0)
        _bigger_meal_soft = LpVariable("bigger_meal_is_dinner_soft", cat=LpBinary)
        prob += kcal_by_type["breakfast"] - kcal_by_type["lunch"]  <= dev_breakfast_v_bigger + _shape_big_m * (1 - _bigger_meal_soft)
        prob += kcal_by_type["breakfast"] - kcal_by_type["dinner"] <= dev_breakfast_v_bigger + _shape_big_m * _bigger_meal_soft
    elif has_breakfast and has_lunch and not has_dinner:
        prob += kcal_by_type["breakfast"] - kcal_by_type["lunch"] <= dev_breakfast_v_bigger
    elif has_breakfast and has_dinner and not has_lunch:
        prob += kcal_by_type["breakfast"] - kcal_by_type["dinner"] <= dev_breakfast_v_bigger

    # ------------------------------------------------------------------
    # Objective: percentage-normalised macro deviations + soft kcal
    # penalty + soft meal-shape penalty
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
        + WEIGHT_MEAL_SHAPE_SOFT * (dev_snack_v_lunch / safe_K)
        + WEIGHT_MEAL_SHAPE_SOFT * (dev_snack_v_dinner / safe_K)
        + WEIGHT_MEAL_SHAPE_SOFT * (dev_breakfast_v_bigger / safe_K)
    )

    # ------------------------------------------------------------------
    # Hard kcal band — skippable via hard_bounds. When hard_bounds=False
    # this becomes a BEST-EFFORT pass: no band can make it infeasible, so
    # it always returns an answer, and the objective above already weighs
    # all four macros simultaneously, so that answer is the mathematically
    # closest achievable point to every target at once. Used as the step
    # between the tolerance ladder and the greedy SAFE_FALLBACK so a
    # structurally-infeasible target still gets a real LP answer instead
    # of the heuristic's uncontrolled macro behaviour.
    #
    # Macro hard bands are independent of the kcal band: macro_hard_bounds
    # bounds all three (the multi-meal path, always); protein_hard_bound
    # bounds protein alone when macro_hard_bounds=False (the single-meal
    # path's protein-first attempt) — carbs/fat then stay best-fit only,
    # pulled toward target by the objective but never hard-bounded.
    # ------------------------------------------------------------------
    if hard_bounds:
        prob += total_K <= (1.0 + tol) * kcal_t
        if not allow_under_kcal:
            prob += total_K >= (1.0 - tol) * kcal_t

    if macro_hard_bounds:
        if P_t > 0:
            prob += total_P >= (1.0 - macro_tols["protein"]) * P_t
            prob += total_P <= (1.0 + macro_tols["protein"]) * P_t
        if C_t > 0:
            prob += total_C >= (1.0 - macro_tols["carbs"]) * C_t
            prob += total_C <= (1.0 + macro_tols["carbs"]) * C_t
        if F_t > 0:
            prob += total_F >= (1.0 - macro_tols["fat"]) * F_t
            prob += total_F <= (1.0 + macro_tols["fat"]) * F_t
    elif protein_hard_bound and P_t > 0:
        prob += total_P >= (1.0 - macro_tols["protein"]) * P_t
        prob += total_P <= (1.0 + macro_tols["protein"]) * P_t

    # ------------------------------------------------------------------
    # Price ceiling (post-confirmation swap only — see point 8 above).
    # A hard wall, never relaxed across the tolerance ladder: the whole
    # point is that a swap can never cost more than the client's wallet
    # can cover, no matter how loose the macro bands get elsewhere.
    # ------------------------------------------------------------------
    if total_price is not None and price_ceiling is not None:
        prob += total_price <= price_ceiling

    # ------------------------------------------------------------------
    # Intra-meal serving balance, driven by is_main:
    #   - the main-vs-main MAIN_RATIO bound between two mains sharing a
    #     meal is ALWAYS active, regardless of skip_balance — it stops one
    #     main dominating another and is unrelated to what skip_balance
    #     exists to relax.
    #   - every main's servings >= every non-main's servings in that meal
    #     is the ordering skip_balance drops (single-meal days only).
    # Every recipe with >1 subrecipe is expected to have >=1 main (enforced
    # at data-entry time in the admin UI); a meal with no mains at all would
    # leave this block a no-op for that meal, so it's still worth guarding
    # against upstream, not relied on here.
    # ------------------------------------------------------------------
    meal_sub_indices: Dict[str, List[int]] = defaultdict(list)
    for _idx, _s in enumerate(all_subs):
        meal_sub_indices[_s["meal"]].append(_idx)

    for _meal_key, _indices in meal_sub_indices.items():
        if len(_indices) < 2:
            continue
        _mains = [i for i in _indices if all_subs[i]["is_main"]]
        _non_mains = [i for i in _indices if not all_subs[i]["is_main"]]

        for _m1 in _mains:
            for _m2 in _mains:
                if _m1 != _m2:
                    prob += servings_expr[_m1] <= MAIN_RATIO * servings_expr[_m2]

        if not skip_balance:
            for _m in _mains:
                for _n in _non_mains:
                    prob += servings_expr[_m] >= servings_expr[_n]

    # ------------------------------------------------------------------
    # Meal-type kcal distribution constraints
    # Caps are relative to total_K (not the fixed kcal_t) so they stay
    # proportionally meaningful when the solver drifts within the band.
    # Uses a single elif chain to avoid multiple conflicting blocks.
    # (kcal_by_type/types/has_* were computed earlier, alongside the
    # objective's soft meal-shape terms — reused here as-is.)
    # ------------------------------------------------------------------
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
    # Relative meal-size sanity rules (explicit product direction) — HARD
    # versions. Only applied when hard_bounds=True (the strict/relaxed
    # tolerance ladder), matching how the kcal/macro hard bands above are
    # already gated. NOT applied at BEST_EFFORT_LP (hard_bounds=False):
    # that tier exists specifically to always find an answer close to the
    # kcal/macro target by dropping every other hard bound, and hard-
    # walling these shape rules there too can make hitting the target
    # itself infeasible (confirmed via real testing — a 3-meal day where
    # "snack <= lunch" left no way to reach anywhere near kcal target,
    # solver settled ~50% under target instead of respecting a shape
    # rule). BEST_EFFORT_LP still respects these rules via the SOFT
    # deviation terms folded into the objective above (always active,
    # every pass) — just not as an absolute wall.
    #   1. Snack must never outweigh lunch OR dinner individually. The
    #      %-of-day caps above aren't enough on their own — a single-
    #      subrecipe snack with no serving ceiling can still land under
    #      its %-of-day cap while numerically exceeding a modest lunch or
    #      dinner (e.g. snack at 34% of a day beats a lunch that's only
    #      24% of it).
    #   2. Breakfast must never outweigh BOTH lunch and dinner — one of
    #      lunch/dinner should always be the day's biggest meal. "OR" has
    #      no direct linear form, so it's modeled as a disjunction via one
    #      binary indicator (standard big-M encoding). M must be a plain
    #      CONSTANT (PuLP can't multiply two variable expressions
    #      together) — kcal_t scaled up is a safe, well-scaled bound since
    #      no single meal type's solved kcal should ever approach 3x the
    #      day's own target.
    # ------------------------------------------------------------------
    if hard_bounds:
        if has_snack and has_lunch:
            prob += kcal_by_type["snack"] <= kcal_by_type["lunch"]
        if has_snack and has_dinner:
            prob += kcal_by_type["snack"] <= kcal_by_type["dinner"]

        if has_breakfast and has_lunch and has_dinner:
            _BIG_M = 3.0 * max(kcal_t, 1.0)
            _bigger_meal = LpVariable("bigger_meal_is_dinner", cat=LpBinary)
            prob += kcal_by_type["breakfast"] <= kcal_by_type["lunch"]  + _BIG_M * (1 - _bigger_meal)
            prob += kcal_by_type["breakfast"] <= kcal_by_type["dinner"] + _BIG_M * _bigger_meal
        elif has_breakfast and has_lunch and not has_dinner:
            prob += kcal_by_type["breakfast"] <= kcal_by_type["lunch"]
        elif has_breakfast and has_dinner and not has_lunch:
            prob += kcal_by_type["breakfast"] <= kcal_by_type["dinner"]

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
        "protein":            int(round(value(total_P))),
        "carbs":              int(round(value(total_C))),
        "fat":                int(round(value(total_F))),
        "kcal":               int(round(value(total_K))),
        "tolerance_used":     tol if hard_bounds else "BEST_EFFORT_LP",
        "serving_step_used":  serving_step,
        "culinary_pass":      "strict" if strict_culinary else "relaxed",
        "macro_hard_bounds":  macro_hard_bounds,
        "protein_hard_bound": protein_hard_bound,
        "skip_balance":       skip_balance,
        "price":              round(float(value(total_price)), 2) if total_price is not None else None,
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
    locked_meal_key: str | None = None,
    locked_subrecipe_servings: Dict[int, float] | None = None,
    prices: Dict[str, float] | None = None,
    price_ceiling: float | None = None,
    flat_fee: float = 0.0,
    reference_servings: Dict[Tuple[str, int], float] | None = None,
    max_movement_pct: float | None = None,
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
    locked_meal_key / locked_subrecipe_servings / prices / price_ceiling:
        post-confirmation meal-swap support (see _solve_lp_once points 7-8).
        locked_subrecipe_servings is keyed by subrecipe_id (not the internal
        all_subs index, which doesn't exist until this function builds it) —
        translated to an index-keyed dict below before solving. Every other
        meal in the day stays exactly as free as a normal solve.
    reference_servings / max_movement_pct: swap's "rebalance the day" option
        only — bounds every OTHER (non-locked) meal's servings to within
        +/- max_movement_pct of its CURRENT stored value, keyed by
        (meal_key, subrecipe_id) since the same subrecipe_id can appear in
        more than one meal. Without this, a subrecipe with no max_serving
        cap is free to scale to whatever multiple papers over the locked
        meal's macro shortfall (confirmed in testing: a real swap drove one
        subrecipe to 6x its normal serving). Never applies to a normal
        generate/update solve or to Day Edit, both of which intentionally
        leave other meals fully free.

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
            max_serving = s.get("max_serving")
            all_subs.append({
                "meal":         meal_key,
                "subrecipe_id": s["id"],
                "name":         s["name"],
                "macros":       s["macros"],
                "is_main":      bool(s.get("is_main")),
                "max_serving":  (float(int(max_serving)) if max_serving is not None else None),
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
    #     Subrecipes with no explicit ceiling (max_serving is None) are
    #     already structurally unbounded — any day containing at least one
    #     of those can always reach kcal_t by scaling that one up, so the
    #     guard is a no-op whenever an unbounded subrecipe is present, and
    #     only subrecipes that DO have an explicit ceiling ever get scaled.
    # ------------------------------------------------------------------
    if kcal_t > 0 and all(s["max_serving"] is not None for s in all_subs):
        max_achievable_kcal = sum(s["max_serving"] * s["macros"]["kcal"] for s in all_subs)
        min_needed_kcal     = (1.0 - KCAL_TOLERANCES[-1]) * kcal_t
        if max_achievable_kcal < min_needed_kcal:
            scale = min(
                (kcal_t / max(max_achievable_kcal, 1.0)) * 1.05,
                MAX_SERVING_SCALE_FACTOR,
            )
            for s in all_subs:
                s["max_serving"] = math.ceil(s["max_serving"] * scale)

    # Guard: if all targets are zero we have nothing to optimise.
    if kcal_t <= 0:
        return _safe_fallback(
            all_subs, recipes_by_meal, P_t, C_t, F_t, kcal_t, allow_under_kcal
        )

    fine_eligible = _compute_fine_step_eligibility(all_subs)
    is_single_meal = len(recipes_by_meal) == 1
    STEPS = (1.0, SERVING_STEP_FINE, "mixed_quarter")

    # Translate locked_subrecipe_servings (keyed by subrecipe_id — the only
    # thing a caller can know before all_subs exists) into index-keyed
    # locked_units (keyed by position in all_subs, which is what
    # _solve_lp_once actually operates on).
    locked_units: Dict[int, float] | None = None
    if locked_meal_key is not None and locked_subrecipe_servings:
        locked_units = {
            i: locked_subrecipe_servings[s["subrecipe_id"]]
            for i, s in enumerate(all_subs)
            if s["meal"] == locked_meal_key and s["subrecipe_id"] in locked_subrecipe_servings
        }

    # Translate reference_servings (keyed by (meal_key, subrecipe_id), the
    # only thing a caller can know before all_subs exists) into index-keyed,
    # same reasoning as locked_subrecipe_servings above.
    reference_units: Dict[int, float] | None = None
    if reference_servings:
        reference_units = {
            i: reference_servings[(s["meal"], s["subrecipe_id"])]
            for i, s in enumerate(all_subs)
            if (s["meal"], s["subrecipe_id"]) in reference_servings
        }

    # Bind the swap-only params once so every rung of the tolerance ladder
    # below gets them uniformly, without touching each of its call sites.
    _solve = functools.partial(
        _solve_lp_once,
        locked_meal_key=locked_meal_key,
        locked_units=locked_units,
        prices=prices,
        price_ceiling=price_ceiling,
        flat_fee=flat_fee,
        reference_servings=reference_units,
        max_movement_pct=max_movement_pct,
    )

    # ------------------------------------------------------------------
    # 3a. Single-meal days: kcal + protein bounded, carbs/fat best-fit only.
    #
    #    A day made of exactly one meal has no other meal to balance
    #    against, so the multi-meal balance/culinary machinery below
    #    doesn't apply the same way — dropping macro hard bounds entirely
    #    (protein/carbs/fat all best-fit) lets the LP always find a kcal-
    #    tight answer instead of going infeasible on a target/recipe
    #    mismatch. Protein is brought back as a hard bound first — same
    #    share-scaled tolerance the multi-meal path uses — so it lands as
    #    close to target as the recipe allows; carbs/fat stay best-fit
    #    only. If protein's bound is infeasible at a tier (most often a
    #    single-subrecipe meal, where one serving count can't satisfy
    #    kcal + protein simultaneously — though this can also happen with
    #    a 2-subrecipe meal that simply lacks the protein density), fall
    #    straight back to a fully-unbounded attempt at the same tier
    #    before moving to the next one.
    # ------------------------------------------------------------------
    if is_single_meal:
        zero_macro_tols = {"protein": 0.0, "carbs": 0.0, "fat": 0.0}
        for base_kcal_tol, base_macro_tol in zip(KCAL_TOLERANCES, BASE_MACRO_TOLERANCES):
            tol = kcal_tolerance(kcal_t, base_kcal_tol)
            protein_tol = macro_tolerance("protein", P_t, kcal_t, base_macro_tol)
            protein_macro_tols = {"protein": protein_tol, "carbs": 0.0, "fat": 0.0}

            for step in STEPS:
                result = _solve(
                    all_subs=all_subs, recipes_by_meal=recipes_by_meal,
                    P_t=P_t, C_t=C_t, F_t=F_t, kcal_t=kcal_t,
                    serving_step=step, tol=tol, macro_tols=protein_macro_tols,
                    allow_under_kcal=allow_under_kcal, strict_culinary=True,
                    hard_bounds=True, macro_hard_bounds=False, protein_hard_bound=True,
                    skip_balance=True, fine_eligible=fine_eligible,
                )
                if result is not None:
                    return result

            for step in STEPS:
                result = _solve(
                    all_subs=all_subs, recipes_by_meal=recipes_by_meal,
                    P_t=P_t, C_t=C_t, F_t=F_t, kcal_t=kcal_t,
                    serving_step=step, tol=tol, macro_tols=zero_macro_tols,
                    allow_under_kcal=allow_under_kcal, strict_culinary=True,
                    hard_bounds=True, macro_hard_bounds=False, protein_hard_bound=False,
                    skip_balance=True, fine_eligible=fine_eligible,
                )
                if result is not None:
                    return result
        return _safe_fallback(
            all_subs, recipes_by_meal, P_t, C_t, F_t, kcal_t, allow_under_kcal
        )

    # ------------------------------------------------------------------
    # 3b. Multi-meal days: two-pass tolerance ladder.
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
    #
    #    Every rung tries all three granularities (1.0, 0.5, mixed_quarter)
    #    before moving to the next — mixed_quarter drops eligible
    #    subrecipes (mains in a multi-subrecipe meal) to a 0.25 step while
    #    everything else stays on 0.5, in the same LP.
    # ------------------------------------------------------------------
    for strict_culinary in (True, False):
        for base_kcal_tol, base_macro_tol in zip(KCAL_TOLERANCES, BASE_MACRO_TOLERANCES):
            tol = kcal_tolerance(kcal_t, base_kcal_tol)
            macro_tols = {
                "protein": macro_tolerance("protein", P_t, kcal_t, base_macro_tol),
                "carbs":   macro_tolerance("carbs",   C_t, kcal_t, base_macro_tol),
                "fat":     macro_tolerance("fat",      F_t, kcal_t, base_macro_tol),
            }
            for step in STEPS:
                result = _solve(
                    all_subs=all_subs,
                    recipes_by_meal=recipes_by_meal,
                    P_t=P_t,
                    C_t=C_t,
                    F_t=F_t,
                    kcal_t=kcal_t,
                    serving_step=step,
                    tol=tol,
                    macro_tols=macro_tols,
                    allow_under_kcal=allow_under_kcal,
                    strict_culinary=strict_culinary,
                    macro_hard_bounds=True,
                    fine_eligible=fine_eligible,
                )
                if result is not None:
                    return result

    # ------------------------------------------------------------------
    # 3b. Safety-net pass: KCAL stays hard-bounded (wide, but real);
    #     macros go soft-only (objective-guided, no hard band) — before
    #     dropping the kcal bound too. Found via real testing: hard-
    #     bounding BOTH kcal and macros here (even at a wide %) was often
    #     still infeasible when a recipe combination's fixed protein-per-
    #     kcal ratio structurally exceeds what the target implies (e.g.
    #     mains with no serving ceiling at ~0.14g protein/kcal, against a
    #     weekly-carryover-slashed target wanting ~0.04g/kcal — no
    #     tolerance band accommodates that gap). And a fully-unbounded
    #     objective (both kcal AND macros soft) will happily sacrifice
    #     kcal by ~50% to avoid overshooting the already-slashed protein
    #     target instead — a real, reproduced case (1372 kcal against a
    #     2784 target). Keeping kcal hard-bounded here, alone, fixes both:
    #     the solver is FORCED to stay close to the calorie target no
    #     matter what, and lets protein/carbs/fat land wherever the
    #     recipes' fixed ratios put them — confirmed via direct testing to
    #     resolve the reproduced case (1372 -> 2508 kcal, against the same
    #     2784 target, same recipes).
    # ------------------------------------------------------------------
    for strict_culinary in (False, True):
        for step in STEPS:
            result = _solve(
                all_subs=all_subs,
                recipes_by_meal=recipes_by_meal,
                P_t=P_t,
                C_t=C_t,
                F_t=F_t,
                kcal_t=kcal_t,
                serving_step=step,
                tol=kcal_tolerance(kcal_t, SAFETY_NET_KCAL_TOL),
                macro_tols={"protein": 0.0, "carbs": 0.0, "fat": 0.0},
                allow_under_kcal=allow_under_kcal,
                strict_culinary=strict_culinary,
                hard_bounds=True,
                macro_hard_bounds=False,
                fine_eligible=fine_eligible,
            )
            if result is not None:
                return result

    # ------------------------------------------------------------------
    # 3c. True best-effort: same LP, same objective, hard bounds dropped
    #     entirely. Reached only if even the wide safety-net band above
    #     is infeasible — a genuinely structural mismatch between this
    #     day's specific recipes and the target.
    # ------------------------------------------------------------------
    final_macro_tols = {
        "protein": macro_tolerance("protein", P_t, kcal_t, BASE_MACRO_TOLERANCES[-1]),
        "carbs":   macro_tolerance("carbs",   C_t, kcal_t, BASE_MACRO_TOLERANCES[-1]),
        "fat":     macro_tolerance("fat",      F_t, kcal_t, BASE_MACRO_TOLERANCES[-1]),
    }
    final_tol = kcal_tolerance(kcal_t, KCAL_TOLERANCES[-1])
    for strict_culinary in (False, True):
        for step in STEPS:
            result = _solve(
                all_subs=all_subs,
                recipes_by_meal=recipes_by_meal,
                P_t=P_t,
                C_t=C_t,
                F_t=F_t,
                kcal_t=kcal_t,
                serving_step=step,
                tol=final_tol,
                macro_tols=final_macro_tols,
                allow_under_kcal=allow_under_kcal,
                strict_culinary=strict_culinary,
                hard_bounds=False,
                macro_hard_bounds=False,
                fine_eligible=fine_eligible,
            )
            if result is not None:
                return result

    # ------------------------------------------------------------------
    # 4. Even BEST_EFFORT_LP failed (should be exceedingly rare) —
    #    greedy safe fallback as the absolute last resort.
    # ------------------------------------------------------------------
    return _safe_fallback(
        all_subs, recipes_by_meal, P_t, C_t, F_t, kcal_t, allow_under_kcal
    )


# =============================================================================
# MEAL SWAP (post-confirmation modify)
# =============================================================================

def optimize_subrecipes_with_swap(
    recipes_by_meal: Dict[str, Dict[str, Any]],
    macro_target: Dict[str, float],
    locked_meal_key: str,
    locked_recipe_id: int,
    prices: Dict[str, float] | None = None,
    price_ceiling: float | None = None,
    allow_under_kcal: bool = False,
    flat_fee: float = 0.0,
    reference_servings: Dict[Tuple[str, int], float] | None = None,
    max_movement_pct: float | None = None,
) -> Tuple[List[Dict[str, Any]], float | None, Dict[str, Any]]:
    """
    Re-solve a day with one meal swapped to `locked_recipe_id` and locked at
    its standard single serving (1x every one of its subrecipes — never
    scaled to an odd fraction; a rigid dish like lasagna can't be served at
    0.85 servings). Every other meal in `recipes_by_meal` stays exactly as
    free to rebalance as a normal generate/update solve.

    Caller contract: `recipes_by_meal[locked_meal_key]["recipe_id"]` must
    already equal `locked_recipe_id` — this only derives the locked-servings
    map and forwards it into optimize_subrecipes, it doesn't mutate
    recipes_by_meal itself.
    """
    locked_subs = get_recipe_subrecipes(locked_recipe_id)
    locked_subrecipe_servings = {s["id"]: 1.0 for s in locked_subs}

    return optimize_subrecipes(
        recipes_by_meal,
        macro_target,
        allow_under_kcal=allow_under_kcal,
        locked_meal_key=locked_meal_key,
        locked_subrecipe_servings=locked_subrecipe_servings,
        prices=prices,
        price_ceiling=price_ceiling,
        flat_fee=flat_fee,
        reference_servings=reference_servings,
        max_movement_pct=max_movement_pct,
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
