"""
services/daily_menu_service.py — production port of the kitchen-batching +
hard-macro-fit-bar redesign validated in scripts/solver_study/menu_lab.py
and scripts/solver_study/kitchen_batching_study.py. Replaces
get_or_create_daily_template / build_day_candidate / composite_score /
score_day previously in routes/mealplan_routes.py.

CRITICAL SCOPING RULE (see project plan's Audit section — this is exactly
where the prior rewrite broke in production): every function here takes
`meals_map` explicitly and filters any `daily_menu` row set against it
BEFORE touching multi-meal-type data. A 1-meal request must never read,
reason about, or return meal types it didn't ask for, even when a date
already has a full daily_menu template from someone else's earlier
full-meal request.

Three layers, in order:
  1. build_week_templates()  - whole-range SHARED template (kitchen
     batching + quality-ranked, cooldown-spaced scheduling). No personal
     like/dislike bias here — this is shared across every client on the
     same kitchen date. Reuses/extends existing daily_menu rows within
     TEMPLATE_TTL_DAYS; only generates for dates missing a valid one.
  2. apply_client_overrides() - per-CLIENT swap of disliked/excluded/
     missing/self-colliding slots. This is where personal preference
     enters.
  3. repair_day_if_needed()  - single bounded repair step + substitution
     advisory, called from the route's day loop (after optimize_subrecipes,
     since carryover-adjusted targets are only known there).
"""

import heapq
import math
from collections import defaultdict
from datetime import date as date_cls, datetime, timedelta
from typing import Any, Dict, List, Tuple

from utils.supabase_client import supabase
from services.mealplan_service import optimize_subrecipes

MEAL_TYPES = ("breakfast", "lunch", "dinner", "snack")

# =============================================================================
# CONFIG — mirrors scripts/solver_study/menu_lab.py 1:1 where applicable,
# so the validated study numbers transfer directly to production.
# =============================================================================

FLEX_SUB_COUNT_WEIGHT      = 0.5
FLEX_SUM_MAX_WEIGHT        = 0.1
WEEKDAY_POPULARITY_WEIGHT  = 1.5
MACRO_COMPAT_WEIGHT        = 3.0
MACRO_COMPAT_HARD_FILTER   = -2.5
SUBRECIPE_SIMILARITY_WEIGHT = 8.0  # unused at template-build time (no
                                    # yesterday/day-to-day recency walk in
                                    # the holistic allocator) — kept for
                                    # parity/reference with menu_lab.py.

ASSUMED_MAIN_HEADROOM     = 6
ASSUMED_NON_MAIN_HEADROOM = 3

MIN_SHARE_OF_EVEN_SPLIT    = 0.5
INFEASIBLE_CEILING_PENALTY = 10.0

POPULARITY_CAP = 50

TEMPLATE_TTL_DAYS   = 7
HIGH_KCAL_THRESHOLD = 2800

# Kept for reference/logging only — not used as the repair trigger.
REPAIRABLE_TIERS = ("BEST_EFFORT_LP", "SAFE_FALLBACK")

# Repair triggers on ACTUAL relative deviation from target, not on which
# LP tier/pass produced the result — see needs_repair() below for why.
REPAIR_KCAL_DEVIATION_THRESHOLD  = 0.20
REPAIR_MACRO_DEVIATION_THRESHOLD = 0.30


def needs_repair(day_totals: dict, macro_target: dict) -> bool:
    """Whether this day's ACTUAL macros are far enough off target to be
    worth a repair attempt.

    Went through two wrong versions before this one: first checked
    `tolerance_used in ("BEST_EFFORT_LP", "SAFE_FALLBACK")` — broke the
    moment the safety-net tier (services/mealplan_service.py's
    optimize_subrecipes, pass 3b) started catching most of what used to
    land in those two labels, since it resolves to a plain numeric label
    instead, silently stopping repair (and the substitution advisory)
    from ever firing again. The next version checked `macro_hard_bounds`
    instead — closer, but still wrong: a day can land at the safety-net
    tier (macro_hard_bounds=False) and still be genuinely close to target
    (confirmed via real testing — one day landed within 0.8% of its kcal
    target at that tier), so checking the boolean alone triggered a real,
    costly extra solve on days that didn't need one at all. Checking the
    actual deviation is the only version that means what it says: "is
    this day genuinely off," independent of which pass produced it.
    """
    kcal_t = macro_target.get("kcal", 0)
    if kcal_t > 0:
        kcal_dev = abs(day_totals.get("kcal", 0) - kcal_t) / kcal_t
        if kcal_dev > REPAIR_KCAL_DEVIATION_THRESHOLD:
            return True

    for macro, target_key in (("protein", "protein_g"), ("carbs", "carbs_g"), ("fat", "fat_g")):
        t = macro_target.get(target_key, 0)
        if t > 0:
            dev = abs(day_totals.get(macro, 0) - t) / t
            if dev > REPAIR_MACRO_DEVIATION_THRESHOLD:
                return True

    return False


# =============================================================================
# BATCH PREFETCHERS — one DB round-trip per data type, called once before
# generation, same pattern as routes/mealplan_routes.py's existing
# prefetch_flex_stats / prefetch_recipe_macros.
# =============================================================================

def prefetch_flex_stats(recipe_ids: list) -> dict:
    if not recipe_ids:
        return {}
    resp = (
        supabase.table("recipe_subrecipe")
        .select("recipe_id, max_serving, is_main, subrecipe(max_serving)")
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
        override = rs.get("max_serving")
        base = sub.get("max_serving")
        resolved = override if override is not None else base
        if resolved is None:
            resolved = ASSUMED_MAIN_HEADROOM if rs.get("is_main") else ASSUMED_NON_MAIN_HEADROOM
        maxes[rid]  += int(resolved)
        counts[rid] += 1
    return {
        rid: {"sub_count": max(counts.get(rid, 0), 1), "sum_max": max(maxes.get(rid, 0), 3)}
        for rid in recipe_ids
    }


def prefetch_recipe_macros(recipe_ids: list) -> dict:
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


def prefetch_subrecipe_sets(recipe_ids: list) -> dict:
    """{recipe_id: frozenset(subrecipe_id)} — real replacement for the
    dead recipe_category signal (that table doesn't exist on the live
    schema). Used only for potential future recency-similarity scoring;
    the whole-range holistic allocator itself doesn't need day-to-day
    similarity since it has no "yesterday" concept."""
    if not recipe_ids:
        return {}
    resp = (
        supabase.table("recipe_subrecipe")
        .select("recipe_id, subrecipe_id")
        .in_("recipe_id", recipe_ids)
        .execute()
    )
    out: dict = defaultdict(set)
    for row in (resp.data or []):
        rid = row.get("recipe_id")
        sid = row.get("subrecipe_id")
        if rid is not None and sid is not None:
            out[rid].add(sid)
    return {rid: frozenset(s) for rid, s in out.items()}


def prefetch_weekday_popularity(recipe_ids: list, weekdays: list) -> dict:
    """Real fix for the dead production signal: prod's old query selected
    meal_plan_day.recipe_id/.weekday, neither of which exists on that
    table (recipe_id lives on meal_plan_day_recipe; weekday must be
    derived from meal_plan_day.date). This joins correctly."""
    if not recipe_ids or not weekdays:
        return {}
    try:
        resp = (
            supabase.table("meal_plan_day_recipe")
            .select("recipe_id, meal_plan_day(date)")
            .in_("recipe_id", recipe_ids)
            .execute()
        )
    except Exception:
        return {(rid, wd): 0.5 for rid in recipe_ids for wd in weekdays}

    counts: dict = defaultdict(int)
    for row in (resp.data or []):
        rid = row.get("recipe_id")
        mpd = row.get("meal_plan_day") or {}
        date_str = mpd.get("date")
        if rid is None or not date_str:
            continue
        try:
            wd = datetime.strptime(date_str[:10], "%Y-%m-%d").date().weekday()
        except Exception:
            continue
        counts[(rid, wd)] += 1

    return {
        (rid, wd): min(counts.get((rid, wd), 0) / POPULARITY_CAP, 1.0)
        for rid in recipe_ids for wd in weekdays
    }


def fetch_last_eaten(user_id: str, recipe_ids: list) -> Dict[int, date_cls]:
    """{recipe_id: most_recent_date_this_client_ate_it}. Live query over
    meal_plan -> meal_plan_day -> meal_plan_day_recipe, scoped to
    user_id. No new table: /update_meal_plan never persists (preview
    only), /confirm_order is the only write path, so this is always
    correct even if a kitchen admin edits an order via the FlutterFlow
    admin app instead of this backend."""
    if not recipe_ids:
        return {}
    try:
        resp = (
            supabase.table("meal_plan_day_recipe")
            .select("recipe_id, meal_plan_day(date, meal_plan(user_id))")
            .in_("recipe_id", recipe_ids)
            .execute()
        )
    except Exception:
        return {}

    last_eaten: Dict[int, date_cls] = {}
    for row in (resp.data or []):
        rid = row.get("recipe_id")
        mpd = row.get("meal_plan_day") or {}
        mp  = mpd.get("meal_plan") or {}
        if mp.get("user_id") != user_id:
            continue
        date_str = mpd.get("date")
        if rid is None or not date_str:
            continue
        try:
            d = datetime.strptime(date_str[:10], "%Y-%m-%d").date()
        except Exception:
            continue
        if rid not in last_eaten or d > last_eaten[rid]:
            last_eaten[rid] = d
    return last_eaten


def fetch_other_orders_by_date(
    date_range: List[date_cls], exclude_user_id: str,
) -> Dict[date_cls, Dict[str, Dict[int, int]]]:
    """{date: {meal_type: {recipe_id: count}}} — CONFIRMED orders from
    OTHER clients for these dates (ground truth for kitchen batching: what
    is actually being cooked, not just what the unconfirmed shared
    daily_menu template projects). No new table: /confirm_order is the
    only write path into meal_plan_day_recipe, same reasoning as
    fetch_last_eaten. Two-step query (meal_plan_day first, filtered by the
    real `date` column, then meal_plan_day_recipe by day id) since
    PostgREST doesn't reliably filter on a nested embed's column here."""
    if not date_range:
        return {}
    date_strs = [str(d) for d in date_range]
    try:
        days_resp = (
            supabase.table("meal_plan_day")
            .select("id, date, meal_plan(user_id)")
            .in_("date", date_strs)
            .execute()
        )
    except Exception:
        return {}

    day_id_to_date_user: Dict[int, Tuple[date_cls, str]] = {}
    for row in (days_resp.data or []):
        mp = row.get("meal_plan") or {}
        uid = mp.get("user_id")
        if uid == exclude_user_id or not uid:
            continue
        date_str = row.get("date")
        if row.get("id") is None or not date_str:
            continue
        try:
            d = datetime.strptime(date_str[:10], "%Y-%m-%d").date()
        except Exception:
            continue
        day_id_to_date_user[row["id"]] = (d, uid)

    if not day_id_to_date_user:
        return {}

    try:
        recipes_resp = (
            supabase.table("meal_plan_day_recipe")
            .select("meal_plan_day_id, recipe_id, meal_type")
            .in_("meal_plan_day_id", list(day_id_to_date_user.keys()))
            .execute()
        )
    except Exception:
        return {}

    # Count DISTINCT clients per (date, meal_type, recipe), not raw rows —
    # a single client with multiple confirmed orders overlapping the same
    # date (edit/reorder) must not inflate "most ordered" on its own. This
    # is the concrete fix for "I can't copy that" (a lone client's own
    # repeat shouldn't look like kitchen consensus).
    voters: Dict[Tuple[date_cls, str, int], set] = defaultdict(set)
    for row in (recipes_resp.data or []):
        entry = day_id_to_date_user.get(row.get("meal_plan_day_id"))
        rid = row.get("recipe_id")
        mt = row.get("meal_type")
        if entry is None or rid is None or not mt:
            continue
        d, uid = entry
        voters[(d, mt, rid)].add(uid)

    result: Dict[date_cls, Dict[str, Dict[int, int]]] = defaultdict(lambda: defaultdict(dict))
    for (d, mt, rid), uids in voters.items():
        result[d][mt][rid] = len(uids)

    return {d: {mt: dict(counts) for mt, counts in mts.items()} for d, mts in result.items()}


def population_reference_target() -> dict:
    """Median protein/carbs/fat/kcal across active daily_macro_target
    rows — gates whether a SHARED (cross-client) template choice is
    broadly macro-reasonable. Deliberately NOT any one client's real
    target: a shared template can't be simultaneously optimal for every
    client's distinct target. Falls back to a flat even 3-way split if no
    population data exists yet (e.g. a brand-new market)."""
    try:
        resp = (
            supabase.table("daily_macro_target")
            .select("protein_g, carbs_g, fat_g, kcal_target")
            .execute()
        )
        rows = resp.data or []
    except Exception:
        rows = []

    if not rows:
        return {"protein_g": 100.0, "carbs_g": 150.0, "fat_g": 55.0, "kcal": 1800.0}

    def _median(key, fallback_kcal_frac=None):
        vals = sorted(float(r[key]) for r in rows if r.get(key) is not None)
        n = len(vals)
        if n == 0:
            return 0.0
        mid = n // 2
        return vals[mid] if n % 2 else (vals[mid - 1] + vals[mid]) / 2.0

    protein_g = _median("protein_g")
    carbs_g   = _median("carbs_g")
    fat_g     = _median("fat_g")
    kcal_vals = [float(r["kcal_target"]) for r in rows if r.get("kcal_target") is not None]
    kcal = (sorted(kcal_vals)[len(kcal_vals) // 2] if kcal_vals
            else 4 * (protein_g + carbs_g) + 9 * fat_g)
    return {"protein_g": protein_g, "carbs_g": carbs_g, "fat_g": fat_g, "kcal": kcal}


# =============================================================================
# SCORING — pure in-memory, mirrors menu_lab.py
# =============================================================================

def macro_compat_score(recipe_id: int, recipe_macros: dict, macro_target: dict) -> float:
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
    diff = abs(rec_p - tgt_p) + abs(rec_c - tgt_c) + abs(rec_f - tgt_f)
    return MACRO_COMPAT_WEIGHT * (0.5 - diff)


def _feasibility_penalty(rid, flex_stats, recipe_macros, macro_target, num_meal_types=4) -> float:
    """Penalizes a recipe whose own kcal ceiling (all subrecipes at max
    serving) can't plausibly reach a workable share of the target —
    catches catalog data-quality issues like a single-subrecipe snack
    capped at ~168kcal being scheduled for a 1800kcal client."""
    flex = flex_stats.get(rid, {"sub_count": 1, "sum_max": 3})
    avg_max = flex["sum_max"] / max(flex["sub_count"], 1)
    est_max_kcal = recipe_macros.get(rid, {}).get("kcal", 0.0) * avg_max
    even_split = macro_target.get("kcal", 0.0) / max(num_meal_types, 1)
    floor = MIN_SHARE_OF_EVEN_SPLIT * even_split
    if floor <= 0 or est_max_kcal >= floor:
        return 0.0
    shortfall_frac = 1.0 - (est_max_kcal / floor)
    return INFEASIBLE_CEILING_PENALTY * shortfall_frac


def _quality_score(rid, flex_stats, popularity, recipe_macros, macro_target, weekdays: List[int]) -> float:
    """Day-invariant quality score for the whole-range allocator. No
    personal like/dislike bias — that's applied only at the per-client
    override layer, so one anonymous first-mover's taste can't silently
    bias the SHARED template everyone else inherits."""
    flex = flex_stats.get(rid, {"sub_count": 1, "sum_max": 3})
    q = FLEX_SUB_COUNT_WEIGHT * flex["sub_count"] + FLEX_SUM_MAX_WEIGHT * flex["sum_max"]
    q += macro_compat_score(rid, recipe_macros, macro_target)
    avg_pop = sum(popularity.get((rid, wd), 0.5) for wd in weekdays) / len(weekdays)
    q += WEEKDAY_POPULARITY_WEIGHT * avg_pop
    q -= _feasibility_penalty(rid, flex_stats, recipe_macros, macro_target)
    return q


def _fair_counts(ranked_ids: List[int], num_days: int) -> Dict[int, int]:
    pool = len(ranked_ids)
    if pool >= num_days:
        return {rid: 1 for rid in ranked_ids[:num_days]}
    weights = [pool - i for i in range(pool)]
    total_w = sum(weights)
    raw = [num_days * w / total_w for w in weights]
    counts = [max(1, math.floor(x)) for x in raw]
    remainder = num_days - sum(counts)
    fractional = sorted(range(pool), key=lambda i: (raw[i] - math.floor(raw[i]), -i), reverse=True)
    i = 0
    while remainder > 0 and i < len(fractional):
        counts[fractional[i]] += 1
        remainder -= 1
        i += 1
    return {ranked_ids[i]: counts[i] for i in range(pool)}


# =============================================================================
# WHOLE-RANGE HOLISTIC ALLOCATOR (point-1 substrate) — direct port of
# scripts/solver_study/menu_lab.py::run_holistic_week, generalized to any
# date range and seeded with already-committed dates so a partial re-run
# (some dates already have a valid daily_menu template) stays consistent
# with them instead of ignoring them.
# =============================================================================

def _rank_and_schedule(
    meals_map: dict, eligible_by_meal_type: dict, flex_stats: dict, popularity: dict,
    recipe_macros: dict, reference_target: dict, dates: List[date_cls],
    preexisting: Dict[date_cls, Dict[str, int]],
) -> Dict[date_cls, Dict[str, int]]:
    """Returns {date: {meal_key: recipe_id}} for every date in `dates`
    (both pre-existing, passed through unchanged, and newly scheduled).
    `preexisting` seeds quota/cooldown state and same-day/week collision
    checks so newly-scheduled dates stay consistent with what's already
    committed for other dates in the same range."""
    weekdays = [d.weekday() for d in dates]
    num_days = len(dates)
    meal_types_by_pool_size = sorted(
        set(meals_map.values()), key=lambda mt: len(eligible_by_meal_type.get(mt, []))
    )
    meal_key_by_type = {mt: mk for mk, mt in meals_map.items()}

    state: Dict[str, dict] = {}
    used_this_week: Dict[str, set] = {mt: set() for mt in meal_types_by_pool_size}
    for day in dates:
        for mt in meal_types_by_pool_size:
            rid = preexisting.get(day, {}).get(meal_key_by_type[mt])
            if rid is not None:
                used_this_week[mt].add(rid)

    for meal_type in meal_types_by_pool_size:
        pool = eligible_by_meal_type.get(meal_type, [])
        ranked = sorted(
            pool,
            key=lambda rid: _quality_score(rid, flex_stats, popularity, recipe_macros, reference_target, weekdays),
            reverse=True,
        )
        # Pre-existing picks for this meal type reduce the quota needed
        # for the remaining (missing) days only.
        missing_days = [d for d in dates if meal_key_by_type[meal_type] not in preexisting.get(d, {})]
        counts = _fair_counts(ranked, max(len(missing_days), 1)) if missing_days else {}
        distinct = len(counts)
        cooldown = max(0, math.ceil(max(len(missing_days), 1) / distinct) - 1) if distinct else 0
        heap = [(-c, rid) for rid, c in counts.items()]
        heapq.heapify(heap)
        state[meal_type] = {"heap": heap, "cooldown_q": [], "cooldown": cooldown, "ranked": ranked}

    def _select_for_meal_type(st: dict, day_idx: int, exclude: set, week_used: set) -> int | None:
        """Picks one recipe id, respecting `exclude` (today's other slots
        plus, on the first attempt, yesterday's pick for this same slot).
        Cooldown-respecting heap first, then due cooldown_q entries, then
        ranked-list fallbacks. Only mutates heap/cooldown_q state when it
        actually commits to a returned choice, so a failed attempt here is
        safe to retry with a relaxed `exclude` set."""
        skipped, chosen_rid = [], None
        while st["heap"]:
            neg_c, rid = heapq.heappop(st["heap"])
            if rid not in exclude:
                chosen_rid = rid
                remaining = -neg_c - 1
                if remaining > 0:
                    heapq.heappush(st["cooldown_q"], (day_idx + st["cooldown"] + 1, -remaining, rid))
                break
            skipped.append((neg_c, rid))
        for item in skipped:
            heapq.heappush(st["heap"], item)
        if chosen_rid is not None:
            return chosen_rid

        candidates = [e for e in st["cooldown_q"] if e[2] not in exclude]
        if candidates:
            candidates.sort()
            entry = candidates[0]
            st["cooldown_q"].remove(entry)
            heapq.heapify(st["cooldown_q"])
            _, neg_c, chosen_rid = entry
            remaining = -neg_c - 1
            if remaining > 0:
                heapq.heappush(st["cooldown_q"], (day_idx + st["cooldown"] + 1, -remaining, chosen_rid))
            return chosen_rid

        for rid in st["ranked"]:
            if rid not in exclude and rid not in week_used:
                return rid
        for rid in st["ranked"]:
            if rid not in exclude:
                return rid
        return None

    result: Dict[date_cls, Dict[str, int]] = {}
    for day_idx, day in enumerate(dates):
        existing_today = preexisting.get(day, {})
        used_today: set = set(existing_today.values())
        chosen_by_meal: dict = dict(existing_today)
        prev_day_map = result[dates[day_idx - 1]] if day_idx > 0 else {}

        for meal_type in meal_types_by_pool_size:
            mk = meal_key_by_type[meal_type]
            if mk in chosen_by_meal:
                continue  # already committed for this date - never touched

            st = state[meal_type]
            while st["cooldown_q"] and st["cooldown_q"][0][0] <= day_idx:
                _, neg_c, rid = heapq.heappop(st["cooldown_q"])
                heapq.heappush(st["heap"], (neg_c, rid))

            week_used = used_this_week[meal_type]
            prev_rid = prev_day_map.get(mk)

            # Prefer avoiding an immediate day-to-day repeat of THIS slot.
            # Falls back to allowing it only when the pool is too small to
            # avoid (e.g. a 1-2 recipe pool) — without this, a starved
            # cooldown state degenerates into repeating the same recipe
            # for the rest of the range (confirmed via fresh testing on
            # 2026-08-18: pool=2/30-day range collapsed to 10 straight
            # identical days without this check).
            chosen_rid = None
            if prev_rid is not None:
                chosen_rid = _select_for_meal_type(st, day_idx, used_today | {prev_rid}, week_used)
            if chosen_rid is None:
                chosen_rid = _select_for_meal_type(st, day_idx, used_today, week_used)

            chosen_by_meal[mk] = chosen_rid
            if chosen_rid is not None:
                used_today.add(chosen_rid)
                used_this_week[meal_type].add(chosen_rid)

        result[day] = chosen_by_meal

    return result


def _apply_kitchen_batching(
    schedule: Dict[date_cls, Dict[str, int]], meals_map: dict, eligible_by_meal_type: dict,
    recipe_macros: dict, reference_target: dict, mutable_dates: set,
    allowed_recipe_ids_by_date: Dict[date_cls, set] | None = None,
    locked_slots: Dict[date_cls, set] | None = None,
) -> Dict[date_cls, Dict[str, int]]:
    """Direct port of menu_lab.apply_kitchen_batching, restricted to
    `mutable_dates` (freshly-scheduled dates only — never touches a date
    that already had a committed daily_menu template, since that's shared
    with other clients and stable within its TTL).

    `allowed_recipe_ids_by_date`, when given, additionally restricts a
    merge candidate to recipes actually available on THAT SPECIFIC date
    (e.g. in that week's weekly_menu roster) — without this, a same-day
    merge could pull in a recipe that's could_be_{mt}-eligible in general
    but belongs to a different weekly_menu period than the date being
    batched.

    `locked_slots`, when given, marks specific meal_key slots on a
    mutable date that are still off-limits for a SWAP (they're a partial
    day's already-committed rows, shared with other clients) even though
    the date itself is `mutable` because some OTHER slot on it is being
    freshly generated. A locked slot may still be offered as a merge
    DONOR to other slots — only being overwritten is forbidden."""
    num_days = len(schedule)
    meal_key_by_type = {mt: mk for mk, mt in meals_map.items()}
    meal_types_by_pool_size = sorted(
        set(meals_map.values()), key=lambda mt: len(eligible_by_meal_type.get(mt, []))
    )

    original_by_type: Dict[str, Dict[date_cls, int]] = {mt: {} for mt in meal_types_by_pool_size}
    for day, chosen in schedule.items():
        for mt in meal_types_by_pool_size:
            rid = chosen.get(meal_key_by_type[mt])
            if rid is not None:
                original_by_type[mt][day] = rid

    result: Dict[date_cls, Dict[str, int]] = {}
    for day, chosen in schedule.items():
        if day not in mutable_dates:
            result[day] = dict(chosen)
            continue

        allowed_today = allowed_recipe_ids_by_date.get(day) if allowed_recipe_ids_by_date else None
        locked_today = locked_slots.get(day, set()) if locked_slots else set()

        chosen = dict(chosen)
        decided_types: List[str] = []
        for mt in meal_types_by_pool_size:
            mk = meal_key_by_type[mt]
            current_rid = chosen.get(mk)

            if not decided_types or mk in locked_today:
                decided_types.append(mt)
                continue

            pool_mt = eligible_by_meal_type.get(mt, [])
            repeats_normally_avoidable = len(pool_mt) >= num_days

            for other_mt in decided_types:
                candidate_rid = chosen.get(meal_key_by_type[other_mt])
                if candidate_rid is None or candidate_rid == current_rid:
                    continue
                if candidate_rid not in pool_mt:
                    continue
                if allowed_today and candidate_rid not in allowed_today:
                    continue
                if macro_compat_score(candidate_rid, recipe_macros, reference_target) < MACRO_COMPAT_HARD_FILTER:
                    continue
                if repeats_normally_avoidable:
                    other_days_for_mt = [
                        d for d, rid in original_by_type[mt].items()
                        if d != day and rid == candidate_rid
                    ]
                    if other_days_for_mt:
                        continue
                chosen[mk] = candidate_rid
                break

            decided_types.append(mt)

        result[day] = chosen

    return result


# =============================================================================
# PUBLIC ENTRY POINT — layer 1: whole-range shared template
# =============================================================================

def build_week_templates(
    meals_map: dict,
    available_dates: List[date_cls],
    allowed_recipe_ids_by_date: Dict[date_cls, set],
    all_recipes: List[dict],
    flex_stats: dict,
    popularity: dict,
    recipe_macros: dict,
    reference_target: dict,
    other_orders: Dict[date_cls, Dict[str, Dict[int, int]]] | None = None,
) -> Dict[date_cls, Dict[str, dict]]:
    """Builds/reuses the SHARED (cross-client) daily_menu template for
    every date in available_dates, scoped strictly to `meals_map`.

    Returns {date: {meal_key: {recipe_id, meal_type, recipe_name, photo}}}.
    Dates with an existing valid (non-TTL-expired) template for ALL
    requested meal types are read straight from `daily_menu`. Dates
    missing some or all requested meal types are filled in via the
    whole-range holistic allocator + kitchen-batching pass, then
    persisted.

    `other_orders` (from fetch_other_orders_by_date — CONFIRMED orders
    from other clients, ground truth) takes priority over the allocator's
    own independent quality ranking for a missing slot: if other clients
    already have a clear leader recipe for that date/meal_type and it's
    still eligible and macro-reasonable for the population, reuse it —
    real kitchen batching, not a projection nobody's actually ordered.
    """
    recipe_lookup = {r["id"]: r for r in all_recipes}
    meal_key_by_type = {mt: mk for mk, mt in meals_map.items()}
    requested_meal_types = set(meals_map.values())

    # ---- Read existing daily_menu rows, scoped to requested meal types
    #      and TTL, for the whole range in one query. ----
    date_strs = [str(d) for d in available_dates]
    resp = (
        supabase.table("daily_menu")
        .select("date, meal_type, recipe_id, created_at")
        .in_("date", date_strs)
        .in_("meal_type", list(requested_meal_types))  # scoping fix: never read meal types this request didn't ask for
        .execute()
    )
    rows_by_date: Dict[date_cls, Dict[str, dict]] = defaultdict(dict)
    for row in (resp.data or []):
        try:
            d = datetime.strptime(row["date"][:10], "%Y-%m-%d").date()
        except Exception:
            continue
        rows_by_date[d][row["meal_type"]] = row

    preexisting: Dict[date_cls, Dict[str, int]] = {}
    for d in available_dates:
        rows = rows_by_date.get(d, {})
        # TTL check per date: if the OLDEST requested-meal-type row for
        # this date is stale, treat the whole date as missing so it gets
        # regenerated (avoids a half-stale/half-fresh template).
        valid = {mt: r for mt, r in rows.items() if mt in requested_meal_types}
        if valid:
            try:
                oldest = min(
                    datetime.strptime(r["created_at"][:19], "%Y-%m-%dT%H:%M:%S")
                    for r in valid.values() if r.get("created_at")
                )
                if (datetime.utcnow() - oldest).days > TEMPLATE_TTL_DAYS:
                    valid = {}
            except Exception:
                pass
        # Record whatever valid SUBSET exists, not only a complete match —
        # confirmed bug (2026-08-18): requiring `valid.keys() >=
        # requested_meal_types` treated a date with e.g. lunch already
        # committed (by an earlier, narrower-scoped client) but no
        # breakfast yet as fully blank, silently re-picking a possibly
        # DIFFERENT lunch recipe than what's already committed and
        # already shown to that earlier client.
        if valid:
            preexisting[d] = {meal_key_by_type[mt]: r["recipe_id"] for mt, r in valid.items()}

    requested_meal_keys = set(meals_map.keys())
    missing_dates = [
        d for d in available_dates
        if set(preexisting.get(d, {}).keys()) != requested_meal_keys
    ]
    # Meal-key slots already committed to daily_menu for a date that's
    # otherwise still `missing_dates` (a partial day) — must never be
    # reassigned by the allocator or by kitchen batching, since they're
    # already shared with, and returned to, other clients.
    locked_slots: Dict[date_cls, set] = {
        d: set(preexisting.get(d, {}).keys()) for d in available_dates if preexisting.get(d)
    }

    eligible_by_meal_type: Dict[str, list] = defaultdict(list)
    for rid, r in recipe_lookup.items():
        for mt in requested_meal_types:
            if r.get(f"could_be_{mt}"):
                eligible_by_meal_type[mt].append(rid)

    if missing_dates:
        # Seed with preexisting picks (as immutable context) so the
        # allocator's spacing/quota/collision logic across the whole
        # range stays consistent with dates already committed.
        seed = {d: dict(preexisting.get(d, {})) for d in available_dates}

        # Kitchen signal: prefer a clear other-clients'-orders leader over
        # an independent quality-ranked pick for a still-open slot. Also
        # must be in THAT DATE's own allowed pool (weekly_menu roster),
        # not just could_be_{mt}-eligible in general, or a leader from a
        # different week could get seeded onto this date.
        other_orders = other_orders or {}
        for d in missing_dates:
            allowed_today = allowed_recipe_ids_by_date.get(d)
            for mt in requested_meal_types:
                mk = meal_key_by_type[mt]
                if mk in seed[d]:
                    continue  # already committed via preexisting
                counts = other_orders.get(d, {}).get(mt, {})
                if not counts:
                    continue
                leader = max(counts.items(), key=lambda kv: (kv[1], -kv[0]))[0]
                if leader not in eligible_by_meal_type.get(mt, []):
                    continue
                if allowed_today and leader not in allowed_today:
                    continue
                if macro_compat_score(leader, recipe_macros, reference_target) < MACRO_COMPAT_HARD_FILTER:
                    continue
                seed[d][mk] = leader

        # Segment the range into maximal contiguous runs that share an
        # IDENTICAL allowed-recipe pool. weekly_menu periods are
        # contiguous and non-overlapping in practice, so a range spanning
        # N weekly_menu rows becomes N segments here. Each segment is
        # scheduled with ONLY its own pool (intersected with could_be_mt
        # eligibility) — without this, could_be_{mt} alone can't tell
        # which week a recipe actually belongs to, so a >2-week order
        # could get a week-N recipe assigned to a week-N+1 date. Confirmed
        # via real data on 2026-08-18: weekly_menu 27 and 28 (adjacent,
        # both currently active) share only 2 of 18 recipes.
        def _pool_signature(d: date_cls) -> frozenset:
            allowed = allowed_recipe_ids_by_date.get(d)
            return frozenset(allowed) if allowed else frozenset()

        segments: List[List[date_cls]] = []
        for d in available_dates:
            if segments and _pool_signature(segments[-1][-1]) == _pool_signature(d):
                segments[-1].append(d)
            else:
                segments.append([d])

        for seg_dates in segments:
            if not any(d in missing_dates for d in seg_dates):
                continue
            allowed_seg = allowed_recipe_ids_by_date.get(seg_dates[0])
            if allowed_seg:
                eligible_seg = {
                    mt: [rid for rid in ids if rid in allowed_seg]
                    for mt, ids in eligible_by_meal_type.items()
                }
            else:
                eligible_seg = dict(eligible_by_meal_type)
            seg_result = _rank_and_schedule(
                meals_map, eligible_seg, flex_stats, popularity,
                recipe_macros, reference_target, seg_dates, seed,
            )
            for d in seg_dates:
                seed[d] = seg_result[d]

        schedule = _apply_kitchen_batching(
            seed, meals_map, dict(eligible_by_meal_type), recipe_macros,
            reference_target, mutable_dates=set(missing_dates),
            allowed_recipe_ids_by_date=allowed_recipe_ids_by_date,
            locked_slots=locked_slots,
        )

        # Persist only newly-decided dates (never touch preexisting ones).
        upsert_rows = []
        for d in missing_dates:
            for mk, mt in meals_map.items():
                rid = schedule[d].get(mk)
                if rid is not None:
                    upsert_rows.append({"date": str(d), "meal_type": mt, "recipe_id": rid})
        if upsert_rows:
            supabase.table("daily_menu").upsert(
                upsert_rows, on_conflict="date,meal_type", ignore_duplicates=True,
            ).execute()
    else:
        schedule = {d: dict(preexisting.get(d, {})) for d in available_dates}

    result: Dict[date_cls, Dict[str, dict]] = {}
    for d in available_dates:
        day_map = {}
        for mk, mt in meals_map.items():
            rid = schedule.get(d, {}).get(mk) or preexisting.get(d, {}).get(mk)
            if rid is None:
                continue
            recipe = recipe_lookup.get(rid, {})
            day_map[mk] = {
                "recipe_id":   rid,
                "meal_key":    mk,
                "meal_type":   mt,
                "recipe_name": recipe.get("name"),
                "photo":       recipe.get("photo"),
            }
        result[d] = day_map

    return result


# =============================================================================
# LAYER 2: per-client override (dislikes/exclusions/missing/self-collision)
# =============================================================================

def apply_client_overrides(
    template_for_date: Dict[str, dict],
    meals_map: dict,
    allowed_ids_today: set,
    user_prefs: dict,
    recipe_lookup: dict,
    recipe_macros: dict,
    macro_target: dict,
    week_used_by_type: Dict[str, set] | None = None,
    previous_day_recipes: set | None = None,
) -> Dict[str, dict] | None:
    """Swaps out slots that don't work for THIS client: disliked/excluded/
    missing recipes (existing correct pattern), and same-day self-
    collision (this client requested two meal_keys that the shared
    template happens to point at the same batched recipe — allowed and
    desired across DIFFERENT clients, never for one client's own day).
    Personal like/dislike bias enters HERE, never in build_week_templates.
    Returns None if a slot cannot be filled at all (mirrors prior
    behavior: caller returns a 404 for that date).

    Swap candidates are filtered through the same three-tier fallback as
    _best_alternative: prefer a recipe this client hasn't had anywhere
    else this week; if none qualify, allow a within-week repeat as long
    as it isn't the immediately preceding day's pick; only if even that's
    exhausted (tiny pool) does a same-week-and-adjacent-day repeat become
    acceptable. week_used_by_type/previous_day_recipes are optional so
    this still works standalone (e.g. for a high-kcal client's day-1
    call, where there's no "week so far" yet).
    """
    week_used_by_type = week_used_by_type or {}
    previous_day_recipes = previous_day_recipes or set()

    recipes_by_meal: Dict[str, dict] = {}
    used_today: set = set()
    needs_swap: List[Tuple[str, str]] = []

    for meal_key, meal_type in meals_map.items():
        info = template_for_date.get(meal_key)
        rid = info["recipe_id"] if info else None
        pref = user_prefs.get(rid, {})

        if (
            not rid
            or pref.get("dont_include")
            or pref.get("dislike")
            or rid not in allowed_ids_today
            or rid in used_today       # self-collision with an earlier slot this same date
            or rid in previous_day_recipes  # adjacent-day repeat for THIS client
        ):
            needs_swap.append((meal_key, meal_type))
            continue

        recipes_by_meal[meal_key] = dict(info)
        used_today.add(rid)

    for meal_key, meal_type in needs_swap:
        week_used = week_used_by_type.get(meal_type, set())

        def score_candidates(exclude: set) -> list:
            pairs = []
            for rid in allowed_ids_today:
                recipe = recipe_lookup.get(rid)
                if not recipe or rid in used_today or rid in exclude:
                    continue
                if not recipe.get(f"could_be_{meal_type}", False):
                    continue
                if user_prefs.get(rid, {}).get("dont_include"):
                    continue
                if macro_compat_score(rid, recipe_macros, macro_target) < MACRO_COMPAT_HARD_FILTER:
                    continue
                score = macro_compat_score(rid, recipe_macros, macro_target)
                if user_prefs.get(rid, {}).get("like"):
                    score += 2.0
                if user_prefs.get(rid, {}).get("dislike"):
                    score -= 5.0
                pairs.append((rid, score))
            return pairs

        # Tier 1: no repeat anywhere in this client's week, INCLUDING
        # yesterday. Bug found via fresh testing (2026-08-18): this used
        # to exclude only `week_used`, not `previous_day_recipes` -- so a
        # recipe flagged for swap specifically BECAUSE it repeated
        # yesterday could immediately re-select itself as its own
        # replacement (week_used starts empty on any client's first swap
        # of the day, so tier 1 always had candidates and tier 2 -- the
        # one that actually excludes yesterday -- never got a chance to
        # run).
        pairs = score_candidates(week_used | previous_day_recipes)
        # Tier 2: allow a within-week repeat, but never yesterday's pick.
        if not pairs:
            pairs = score_candidates(previous_day_recipes)
        # Tier 3: true last resort - only today's own self-collision excluded.
        if not pairs:
            pairs = score_candidates(set())

        if not pairs:
            return None

        chosen_rid = max(pairs, key=lambda p: p[1])[0]
        recipe = recipe_lookup.get(chosen_rid, {})
        recipes_by_meal[meal_key] = {
            "recipe_id":   chosen_rid,
            "meal_key":    meal_key,
            "meal_type":   meal_type,
            "recipe_name": recipe.get("name"),
            "photo":       recipe.get("photo"),
        }
        used_today.add(chosen_rid)

    return recipes_by_meal


# =============================================================================
# LAYER 3: single bounded repair step + substitution advisory
# =============================================================================


REPAIR_FLEX_SUB_COUNT_WEIGHT = 1.0
REPAIR_FLEX_SUM_MAX_WEIGHT   = 0.15


def _flexibility_score(rid: int, flex_stats: dict) -> float:
    """How much room the LP has to rebalance servings within this recipe
    — more subrecipes and more combined serving headroom both mean more
    levers the solver can pull to hit a macro target. Used to rank REPAIR
    candidates (where the goal is solver success, not just a
    macro-plausible pick) — deliberately separate from _quality_score's
    blended weights, since here flexibility should dominate, not just
    contribute one term among several."""
    f = flex_stats.get(rid, {"sub_count": 1, "sum_max": 3})
    return REPAIR_FLEX_SUB_COUNT_WEIGHT * f["sub_count"] + REPAIR_FLEX_SUM_MAX_WEIGHT * f["sum_max"]


def _best_alternative(
    meal_type: str, today_used: set, week_used: set, previous_day_recipes: set,
    allowed_ids_today: set, eligible_by_meal_type: dict, recipe_macros: dict,
    macro_target: dict, last_eaten: Dict[int, date_cls], flex_stats: dict,
) -> int | None:
    """Three-tier fallback, in order — always excluding today's own picks
    (self-collision):
      1. No repeat anywhere in this client's week. Among that pool, the
         MOST FLEXIBLE candidate wins (most subrecipes + serving
         headroom), macro fit as tiebreak — not just best macro fit alone.
         This is a repair step, so the point isn't "closest match on
         paper," it's "give the LP room to actually hit the target";
         macro-similar but rigid (e.g. single-subrecipe) recipes were
         repeatedly observed re-failing the very deviation check that
         triggered the repair in the first place.
      2. Whole-week-clean wasn't achievable — allow a within-week repeat,
         but NEVER the immediately preceding day's pick (the adjacent-day
         rule). Prefer whichever recipe this client ate longest ago.
      3. Even that's exhausted (tiny pool) — true last resort, only
         self-collision excluded.
    """
    pool = [
        rid for rid in eligible_by_meal_type.get(meal_type, [])
        if rid not in today_used and rid not in week_used and rid in allowed_ids_today
    ]
    if pool:
        return max(
            pool,
            key=lambda rid: (
                _flexibility_score(rid, flex_stats),
                macro_compat_score(rid, recipe_macros, macro_target),
            ),
        )

    candidates = [
        rid for rid in eligible_by_meal_type.get(meal_type, [])
        if rid not in today_used and rid not in previous_day_recipes and rid in allowed_ids_today
    ]
    if candidates:
        return min(candidates, key=lambda rid: last_eaten.get(rid, date_cls.min))

    final_candidates = [
        rid for rid in eligible_by_meal_type.get(meal_type, [])
        if rid not in today_used and rid in allowed_ids_today
    ]
    if not final_candidates:
        return None
    return min(final_candidates, key=lambda rid: last_eaten.get(rid, date_cls.min))


def repair_day_if_needed(
    recipes_by_meal: Dict[str, dict],
    meals_map: dict,
    day_totals: dict,
    macro_target: dict,
    allowed_ids_today: set,
    eligible_by_meal_type: dict,
    recipe_macros: dict,
    week_used_by_type: Dict[str, set],
    previous_day_recipes: set,
    last_eaten: Dict[int, date_cls],
    flex_stats: dict,
    recipe_lookup: Dict[int, dict] | None = None,
    trial_meal_types: Tuple[str, ...] = ("dinner", "lunch"),
    user_id: str | None = None,
) -> Tuple[Dict[str, dict], dict, str | None, List[Dict] | None, float | None]:
    """Fixed dinner-then-lunch repair (point 3): at most `len(trial_meal_
    types)` extra optimize_subrecipes calls (2 by default — never a third
    solve on top for the caller, since the winning trial's own subrecipe
    breakdown and error are returned directly). Returns
    (final_recipes_by_meal, final_day_totals, swapped_meal_key_or_None,
    optimized_subs_or_None, macro_error_or_None — the last two are only
    set when a swap actually happened; otherwise the caller already has
    its own optimized_subs/loss from before repair ran).
    Called from the route's day loop, right after the normal
    optimize_subrecipes call, using the carryover-adjusted target that's
    only known there.

    `trial_meal_types` lets the caller cap the escalation for long ranges:
    a real 10-day request against the hardest test client measured
    35.93s/32.45s with both trials enabled — over Heroku's 30s hard
    timeout. The route passes `("dinner",)` only once the range exceeds 7
    days, keeping the worst case bounded to 1 extra solve/day for long
    requests while still getting the improved dinner-only repair.

    Replaced the earlier "swap whichever slot has the worst macro-compat
    score" version — validated via scripts/solver_study/kitchen_batching_
    study.py against real data that targeting dinner specifically (usually
    the most flexible pool) then lunch beats a generic worst-slot search:
    12.6% of repairable days resolved vs 3.4% (11/87 vs 3/87 on the same
    200 real client-days), because "worst macro-compat score" often landed
    on breakfast/snack — small, rigid pools with little room to help.
    Snack is never a repair target (almost always single-subrecipe, can't
    meaningfully rebalance a day's macros).

    Each trial works from the ORIGINAL recipes_by_meal (not stacked on the
    previous trial's swap) and uses _best_alternative's three-tier
    fallback, now ranking the no-repeat tier by flexibility first (most
    subrecipes/serving headroom, i.e. most room for the LP to rebalance)
    rather than macro fit alone — a macro-similar but rigid recipe was
    repeatedly observed just re-failing the same deviation check. Stops
    after the last configured trial regardless of outcome — no further
    escalation.
    """
    if not needs_repair(day_totals, macro_target):
        return recipes_by_meal, day_totals, None, None, None

    meal_key_by_type = {info["meal_type"]: mk for mk, info in recipes_by_meal.items()}
    today_used = {info["recipe_id"] for info in recipes_by_meal.values()}

    for target_type in trial_meal_types:
        target_mk = meal_key_by_type.get(target_type)
        if target_mk is None:
            continue  # this meal type wasn't requested today
        week_used = week_used_by_type.get(target_type, set())
        alt = _best_alternative(
            target_type, today_used, week_used, previous_day_recipes, allowed_ids_today,
            eligible_by_meal_type, recipe_macros, macro_target, last_eaten, flex_stats,
        )
        if alt is None:
            continue

        # Confirmed bug (2026-08-18): overwriting only recipe_id left
        # recipe_name/photo frozen on whatever was in this slot BEFORE
        # the swap — real data showed two different real recipes (Meat
        # Burger Platter, Pesto Meat & Rice Bowl) both displayed as
        # "Lasagne" on different days, because Lasagne happened to be the
        # ORIGINAL pick repair swapped away from each time. recipe_id and
        # the solved macros were always correct; only the displayed name/
        # photo were stale.
        alt_info = (recipe_lookup or {}).get(alt)
        candidate = dict(recipes_by_meal)
        candidate[target_mk] = {
            **recipes_by_meal[target_mk],
            "recipe_id": alt,
            **({"recipe_name": alt_info.get("name"), "photo": alt_info.get("photo")} if alt_info else {}),
        }
        candidate_subs, candidate_error, candidate_totals = optimize_subrecipes(candidate, macro_target, user_id=user_id)

        if not needs_repair(candidate_totals, macro_target):
            # Return the subrecipe breakdown (and error) from THIS solve
            # too — the caller used to re-run optimize_subrecipes a
            # second time on the exact same candidate/target just to get
            # these, doubling the LP cost of every successful repair for
            # no reason (confirmed 2026-08-18, flagged as a real time
            # cost).
            return candidate, candidate_totals, target_mk, candidate_subs, candidate_error

    return recipes_by_meal, day_totals, None, None, None
