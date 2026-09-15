from utils.supabase_client import supabase

# PostgREST caps rows-per-request (this project's default is 1000) regardless
# of how many actually match — a wide procurement date range can easily have
# more meal_plan_day_recipe_serving rows than that, and the extra rows were
# silently dropped rather than erroring, which is why the shopping list could
# be short without ever showing up as an error anywhere (analytics_event
# included). Page through with .range() so nothing gets truncated.
_PAGE_SIZE = 1000


def _fetch_all(build_query):
    """build_query(query) -> query with .range() applied; loops until a page
    comes back short of _PAGE_SIZE."""
    rows = []
    offset = 0
    while True:
        page = build_query(supabase, offset, offset + _PAGE_SIZE - 1).execute().data or []
        rows.extend(page)
        if len(page) < _PAGE_SIZE:
            break
        offset += _PAGE_SIZE
    return rows


def get_ingredients_to_buy(start_date, end_date, recipe=None, client=None, delivery_slot=None):

    # ---------------------------------------------------------
    # 1. Fetch deliveries within date range
    # ---------------------------------------------------------
    def build_deliveries(sb, lo, hi):
        q = (
            sb.table("deliveries")
            .select("id, meal_plan_day_id")
            .gte("delivery_date", start_date)
            .lte("delivery_date", end_date)
        )
        if client:
            q = q.eq("user_id", client)
        if delivery_slot:
            q = q.eq("delivery_slot_id", delivery_slot)
        return q.range(lo, hi)

    deliveries = _fetch_all(build_deliveries)
    if not deliveries:
        return []

    meal_plan_day_ids = [d["meal_plan_day_id"] for d in deliveries if d["meal_plan_day_id"]]
    if not meal_plan_day_ids:
        return []

    # ---------------------------------------------------------
    # 1.5. Drop cancelled/cancellation-pending meal_plan_days — don't buy
    # ingredients for something that might not ship. Same rule the cooking
    # board applies (see cooking_service.get_cooking_overview).
    # ---------------------------------------------------------
    mpd = _fetch_all(lambda sb, lo, hi: (
        sb.table("meal_plan_day")
        .select("id, status")
        .in_("id", meal_plan_day_ids)
        .range(lo, hi)
    ))

    meal_plan_day_ids = [
        x["id"] for x in mpd
        if x.get("status") not in ("cancellation_pending", "cancelled")
    ]

    if not meal_plan_day_ids:
        return []

    # ---------------------------------------------------------
    # 2. Fetch meal_plan_day_recipe rows for these days
    # ---------------------------------------------------------
    mprd = _fetch_all(lambda sb, lo, hi: (
        sb.table("meal_plan_day_recipe")
        .select("id, recipe_id, meal_plan_day_id")
        .in_("meal_plan_day_id", meal_plan_day_ids)
        .range(lo, hi)
    ))

    if recipe:
        mprd = [r for r in mprd if str(r["recipe_id"]) == str(recipe)]

    if not mprd:
        return []

    meal_plan_day_recipe_ids = [r["id"] for r in mprd]

    # ---------------------------------------------------------
    # 3. Fetch servings (meal_plan_day_recipe_serving)
    # ---------------------------------------------------------
    servings = _fetch_all(lambda sb, lo, hi: (
        sb.table("meal_plan_day_recipe_serving")
        .select("id, subrecipe_id, recipe_subrecipe_serving_calculated, meal_plan_day_recipe_id")
        .in_("meal_plan_day_recipe_id", meal_plan_day_recipe_ids)
        .range(lo, hi)
    ))

    if not servings:
        return []

    subrecipe_ids = list({s["subrecipe_id"] for s in servings})

    # ---------------------------------------------------------
    # 4. Fetch ingredients for these subrecipes
    # ---------------------------------------------------------
    ingred_rows = _fetch_all(lambda sb, lo, hi: (
        sb.table("subrec_ingred")
        .select("""
            subrecipe_id,
            ingredient_id,
            quantity,
            ingredient:ingredient_id (
                name,
                unit,
                serving_per_unit
            )
        """)
        .in_("subrecipe_id", subrecipe_ids)
        .range(lo, hi)
    ))


    ingred_map = {}
    for row in ingred_rows:
        ingred_map.setdefault(row["subrecipe_id"], []).append(row)

    # ---------------------------------------------------------
    # 5. Multiply servings × ingredient quantities
    # ---------------------------------------------------------
    # ---------------------------------------------
    # 5. Multiply servings × ingredient quantities
    # ---------------------------------------------
    totals = {}

    for s in servings:
        sub_id = s["subrecipe_id"]
        servings_count = s["recipe_subrecipe_serving_calculated"]

        if sub_id not in ingred_map:
            continue

        for ing in ingred_map[sub_id]:
            ing_id = ing["ingredient_id"]

            base_qty = ing["quantity"] * servings_count
            serving_per_unit = ing["ingredient"]["serving_per_unit"] or 1

            final_qty = base_qty * serving_per_unit

            if ing_id not in totals:
                totals[ing_id] = {
                    "ingredient_id": ing_id,
                    "name": ing["ingredient"]["name"],
                    "unit": ing["ingredient"]["unit"],
                    "total_quantity": 0,
                }

            totals[ing_id]["total_quantity"] += final_qty

    # ---------------------------------------------
    # 6. Sort ingredients alphabetically
    # ---------------------------------------------
    result = list(totals.values())
    result.sort(key=lambda x: x["name"].lower())

    return result

