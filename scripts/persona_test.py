"""
PERSONA-based test of the production solver (services.mealplan_service),
post the latest fixes (20% tolerance cap, subrecipe rules + 2.5x default
fallback, BEST_EFFORT_LP pass).

Every number is derived the EXACT same way the product does it (ported 1:1
from FrontEnd/akli-web/src/lib/macros.ts and src/components/DietWizard.tsx):
  - Mifflin-St Jeor BMR -> TDEE (activity multipliers: 1.2/1.3/1.45/1.6)
  - goal adjustment (lose -500, build +300, maintain/health +0), floored at
    KCAL_FLOOR=900
  - macros via byWeight() (g/kg protein+fat by diet type, carbs = remainder,
    0.75 g/kg carb floor)
  - diet type restricted to the FOUR real DIET_OPTIONS (high-protein,
    balanced, low-carb, low-fat) -- no invented "0 carb"/"carnivore" targets

On top of the estimate, this also models the NEW wizard rule just shipped:
post-simulation adjustment capped at +/-15% of the estimate, floored at
KCAL_FLOOR. Each persona is tested at three points: low (-15%), estimate
(0%), high (+15%) -- exactly the only three values a real client could now
produce through the wizard.

For each (persona, variant), real recipes are sampled from the live
Supabase catalog (all 4 meal types) and run through optimize_subrecipes.

Run with:
    python scripts/persona_test.py
"""

import os
import random
import sys
import time
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import services.mealplan_service as svc
from utils.supabase_client import supabase

N_SAMPLES = 4
RANDOM_SEED = 7
KCAL_FLOOR = 900
WIZARD_ADJUST_PCT = 0.15

PER_KG = {
    "high-protein": {"pk": 2.0, "fk": 0.8}, "low-fat": {"pk": 1.8, "fk": 0.5},
    "low-carb": {"pk": 1.9, "fk": 1.2}, "balanced": {"pk": 1.6, "fk": 1.0},
}
CARB_FLOOR_G_PER_KG = 0.75
DEFAULT_DIET = {"lose": "high-protein", "build": "high-protein", "maintain": "balanced", "health": "balanced"}
ACTIVITY = {"sedentary": 1.2, "lightly_active": 1.3, "moderately_active": 1.45, "very_active": 1.6}


def by_weight(kcal, weight, diet):
    pk, fk = PER_KG[diet]["pk"], PER_KG[diet]["fk"]
    p, f = weight * pk, weight * fk
    carb_floor_g = weight * CARB_FLOOR_G_PER_KG
    max_pf_kcal = kcal - carb_floor_g * 4
    pf_kcal = p * 4 + f * 9
    if max_pf_kcal <= 0:
        p, f = 0, 0
    elif pf_kcal > max_pf_kcal:
        scale = max_pf_kcal / pf_kcal
        p *= scale
        f *= scale
    carbs = max(carb_floor_g, (kcal - p * 4 - f * 9) / 4)
    return {"protein_g": round(p, 1), "fat_g": round(f, 1), "carbs_g": round(carbs, 1)}


def mifflin_kcal(sex, weight, height, age, activity_key, goal):
    bmr = (10 * weight + 6.25 * height - 5 * age + 5) if sex == "male" else (10 * weight + 6.25 * height - 5 * age - 161)
    tdee = bmr * ACTIVITY[activity_key]
    kcal = tdee
    if goal == "lose":
        kcal -= 500
    if goal == "build":
        kcal += 300
    return max(KCAL_FLOOR, round(kcal / 10) * 10)


def wizard_variants(estimate_kcal):
    low = max(KCAL_FLOOR, round(estimate_kcal * (1 - WIZARD_ADJUST_PCT) / 10) * 10)
    high = round(estimate_kcal * (1 + WIZARD_ADJUST_PCT) / 10) * 10
    return {"low(-15%)": low, "estimate": estimate_kcal, "high(+15%)": high}


PERSONAS = [
    ("Office worker, losing weight",          "female", 34, 163, 72, "sedentary",          "lose",     None),
    ("Young male building muscle",            "male",   24, 178, 70, "very_active",        "build",    None),
    ("Middle-aged male, low-carb maintenance", "male",  45, 175, 88, "lightly_active",     "maintain", "low-carb"),
    ("Female athlete, building",              "female", 27, 168, 60, "very_active",        "build",    None),
    ("Older client, gentle weight loss",      "female", 56, 160, 78, "sedentary",          "lose",     "low-fat"),
    ("Young female, general health",          "female", 22, 165, 58, "moderately_active",  "health",   None),
    ("Male office worker, losing weight",     "male",   38, 180, 95, "sedentary",          "lose",     None),
    ("Postpartum client, low-fat maintenance", "female", 31, 162, 68, "lightly_active",    "maintain", "low-fat"),
    ("Active young male, building",           "male",   19, 175, 65, "very_active",        "build",    None),
    ("Heavier-set male, sensible loss",       "male",   50, 172, 110, "sedentary",         "lose",     None),
]

MEAL_COMBO = ["breakfast", "lunch", "snack", "dinner"]


def fetch_catalog():
    recipes_resp = supabase.table("recipe").select(
        "id, name, could_be_breakfast, could_be_lunch, could_be_dinner, could_be_snack"
    ).execute()
    recipes = recipes_resp.data or []
    links_resp = supabase.table("recipe_subrecipe").select(
        "recipe_id, subrecipe(id, name, max_serving, kcal, protein, carbs, fat)"
    ).execute()
    subs_by_recipe = defaultdict(list)
    for row in links_resp.data or []:
        sub = row.get("subrecipe") or {}
        if not sub.get("id"):
            continue
        subs_by_recipe[row["recipe_id"]].append({
            "id": sub["id"], "name": sub.get("name"), "max_serving": sub.get("max_serving") or 3,
            "macros": {"kcal": float(sub.get("kcal") or 0.0), "protein": float(sub.get("protein") or 0.0),
                       "carbs": float(sub.get("carbs") or 0.0), "fat": float(sub.get("fat") or 0.0)},
        })
    eligible_by_meal_type = defaultdict(list)
    for r in recipes:
        rid = r["id"]
        if not subs_by_recipe.get(rid):
            continue
        for mt, flag_key in (("breakfast", "could_be_breakfast"), ("lunch", "could_be_lunch"),
                              ("dinner", "could_be_dinner"), ("snack", "could_be_snack")):
            if r.get(flag_key):
                eligible_by_meal_type[mt].append(rid)
    return dict(subs_by_recipe), dict(eligible_by_meal_type), recipes


def run_one(subs_by_recipe, recipe_id_by_meal, target):
    def _fake(recipe_id):
        return [dict(s) for s in subs_by_recipe.get(recipe_id, [])]
    svc.get_recipe_subrecipes = _fake
    rbm = {mt: {"recipe_id": rid, "meal_type": mt} for mt, rid in recipe_id_by_meal.items()}
    _, _, totals = svc.optimize_subrecipes(rbm, target)
    return totals


def classify(tol):
    if tol == "SAFE_FALLBACK":
        return "fallback"
    if tol == "BEST_EFFORT_LP":
        return "best_effort"
    return "solved"


def dev(actual, target):
    if target == 0:
        return 0.0 if actual == 0 else float("inf")
    return abs((actual - target) / target * 100)


def main():
    print("Fetching real recipe/subrecipe catalog from Supabase...")
    subs_by_recipe, eligible_by_meal_type, recipes = fetch_catalog()
    name_by_id = {r["id"]: r["name"] for r in recipes}
    random.seed(RANDOM_SEED)

    t0 = time.time()
    rows = []
    mode_counts = defaultdict(int)

    for persona_name, sex, age, height, weight, activity_key, goal, diet_override in PERSONAS:
        diet = diet_override or DEFAULT_DIET[goal]
        estimate = mifflin_kcal(sex, weight, height, age, activity_key, goal)
        variants = wizard_variants(estimate)

        for variant_name, kcal in variants.items():
            macros = by_weight(kcal, weight, diet)
            target = {**macros, "kcal": kcal}

            per_variant_dev = []
            per_variant_modes = []
            example_recipes = None
            example_totals = None
            for sample_idx in range(N_SAMPLES):
                recipe_id_by_meal = {mt: random.choice(eligible_by_meal_type[mt]) for mt in MEAL_COMBO}
                if sample_idx == 0:
                    example_recipes = ", ".join(f"{mt}={name_by_id.get(rid)}" for mt, rid in recipe_id_by_meal.items())
                totals = run_one(subs_by_recipe, recipe_id_by_meal, target)
                if sample_idx == 0:
                    example_totals = totals
                mode = classify(totals.get("tolerance_used"))
                per_variant_modes.append(mode)
                mode_counts[mode] += 1
                ds = [dev(totals.get(mkey, 0) or 0, target[tkey]) for mkey, tkey in
                      (("kcal", "kcal"), ("protein", "protein_g"), ("carbs", "carbs_g"), ("fat", "fat_g"))]
                ds = [d for d in ds if d != float("inf")]
                if ds:
                    per_variant_dev.append(max(ds))

            avg_dev = sum(per_variant_dev) / len(per_variant_dev) if per_variant_dev else 0
            rows.append({
                "persona": persona_name, "goal": goal, "diet": diet, "variant": variant_name,
                "target_kcal": target["kcal"], "target_protein_g": target["protein_g"],
                "target_carbs_g": target["carbs_g"], "target_fat_g": target["fat_g"],
                "example_totals": example_totals, "example_recipes": example_recipes,
                "modes": per_variant_modes, "avg_max_dev": avg_dev,
            })

        print(f"  [{persona_name}] diet={diet} estimate={estimate}kcal variants={variants}")

    elapsed = round(time.time() - t0, 2)
    total_calls = sum(len(r["modes"]) for r in rows)
    print(f"\nDone in {elapsed}s ({total_calls} solver calls across {len(rows)} persona/variant points)\n")

    print("=" * 100)
    print(f"{'Persona':<32}{'Diet':<13}{'Variant':<13}{'TargetKcal':>10}  {'Modes (4 samples)':<28}{'AvgMaxDev%':>11}")
    print("=" * 100)
    for r in rows:
        modes_str = ",".join(m[0].upper() for m in r["modes"])  # S/B/F shorthand
        print(f"{r['persona'][:31]:<32}{r['diet']:<13}{r['variant']:<13}{r['target_kcal']:>10}  "
              f"{modes_str:<28}{r['avg_max_dev']:>10.1f}%")

    print("\n(Modes: S=solved within tolerance, B=BEST_EFFORT_LP, F=SAFE_FALLBACK)")

    total = sum(mode_counts.values())
    print(f"\n=== OVERALL MODE DISTRIBUTION ({total} calls) ===")
    for k in ("solved", "best_effort", "fallback"):
        print(f"  {k:<12}: {mode_counts[k]:>4}  ({mode_counts[k]/total:.1%})")

    overall_avg_dev = sum(r["avg_max_dev"] for r in rows) / len(rows)
    print(f"\nOverall avg max-macro deviation across all persona/variant points: {overall_avg_dev:.1f}%")

    print("\n=== DETAIL: target vs actual (first sample per persona/variant) ===")
    for r in rows:
        t = r["example_totals"]
        print(f"\n[{r['persona']} / {r['variant']}] diet={r['diet']}")
        print(f"  Target : P={r['target_protein_g']}g C={r['target_carbs_g']}g F={r['target_fat_g']}g kcal={r['target_kcal']}")
        print(f"  Actual : P={t['protein']}g C={t['carbs']}g F={t['fat']}g kcal={t['kcal']}  mode={t['tolerance_used']}")
        print(f"  Recipes: {r['example_recipes']}")


if __name__ == "__main__":
    main()
