from utils.supabase_client import supabase
from collections import defaultdict
from services.portioning_service import ALLERGEN_KEYS


# ---------------------------------------------------------
#   Helper: Apply NULL, NOT NULL, or normal filter
# ---------------------------------------------------------
def apply_null_filter(query, column, value):
    if value is None:
        return query

    if value == "null":
        return query.is_(column, None)

    if value == "not_null":
        return query.not_.is_(column, None)

    return query.eq(column, value)


# ---------------------------------------------------------
#   Main service: full cooking overview
# ---------------------------------------------------------
def get_cooking_overview(start_date, end_date, filters):

    # =====================================================
    # 1️⃣ Fetch deliveries in range (delivery_date — same
    #     semantics as procurement/packaging/deliveries) and
    #     resolve their meal_plan_days from there. Cooking
    #     always happens on delivery_date, regardless of
    #     AM/PM slot (see order_service._get_slot_period).
    # =====================================================
    deliveries_in_range = (
        supabase.table("deliveries")
        .select("id, meal_plan_day_id, user_id, delivery_slot_id, delivery_date")
        .gte("delivery_date", start_date)
        .lte("delivery_date", end_date)
        .execute()
        .data
    ) or []

    if not deliveries_in_range:
        return []

    mpd_ids = list({d["meal_plan_day_id"] for d in deliveries_in_range if d.get("meal_plan_day_id")})
    if not mpd_ids:
        return []

    mpd = (
        supabase.table("meal_plan_day")
        .select("id, date, delivery_id, status")
        .in_("id", mpd_ids)
        .execute()
        .data
    )

    if not mpd:
        return []

    # Cancelled/cancellation-pending orders must not appear on the cooking
    # board — don't cook/package something that might not ship.
    mpd = [x for x in mpd if x.get("status") not in ("cancellation_pending", "cancelled")]
    if not mpd:
        return []

    mpd_map = {x["id"]: x for x in mpd}
    mpd_ids = [x["id"] for x in mpd]

    # Earliest delivery_date per meal_plan_day, for display — a meal_plan_day
    # could in theory have more than one delivery row.
    delivery_date_by_mpd = {}
    for d in deliveries_in_range:
        mid = d.get("meal_plan_day_id")
        if not mid:
            continue
        if mid not in delivery_date_by_mpd or d["delivery_date"] < delivery_date_by_mpd[mid]:
            delivery_date_by_mpd[mid] = d["delivery_date"]

    # meal_plan_day_id -> user_id, for the allergen-conflict check below —
    # same 1:1 relationship delivery_date_by_mpd relies on.
    user_id_by_mpd = {
        d["meal_plan_day_id"]: d["user_id"]
        for d in deliveries_in_range
        if d.get("meal_plan_day_id") and d.get("user_id")
    }

    # =====================================================
    # 2️⃣ Deliveries filtering
    # =====================================================
    if filters["client_id"] or filters["delivery_slot_id"]:

        deliveries = deliveries_in_range

        client_filter = filters["client_id"]
        if client_filter:
            if client_filter == "null":
                mpd_ids = [d["meal_plan_day_id"] for d in deliveries if d["user_id"] is None]
            elif client_filter == "not_null":
                mpd_ids = [d["meal_plan_day_id"] for d in deliveries if d["user_id"] is not None]
            else:
                mpd_ids = [
                    d["meal_plan_day_id"]
                    for d in deliveries
                    if str(d["user_id"]) == str(client_filter)
                ]

        slot_filter = filters["delivery_slot_id"]
        if slot_filter:
            if slot_filter == "null":
                mpd_ids = [d["meal_plan_day_id"] for d in deliveries if d["delivery_slot_id"] is None]
            elif slot_filter == "not_null":
                mpd_ids = [d["meal_plan_day_id"] for d in deliveries if d["delivery_slot_id"] is not None]
            else:
                mpd_ids = [
                    d["meal_plan_day_id"]
                    for d in deliveries
                    if str(d["delivery_slot_id"]) == str(slot_filter)
                ]

        if not mpd_ids:
            return []

    # =====================================================
    # 3️⃣ Fetch meal_plan_day_recipe
    # =====================================================
    mpdr_query = (
    supabase.table("meal_plan_day_recipe")
    .select("id, meal_plan_day_id, recipe_id, packaging_status, is_swapped")
    .in_("meal_plan_day_id", mpd_ids)
    )

    mpdr_query = apply_null_filter(mpdr_query, "recipe_id", filters["recipe_id"])

    mpdr = mpdr_query.execute().data
    if not mpdr:
        return []

    mpdr_ids = [x["id"] for x in mpdr]
    recipe_ids = list({x["recipe_id"] for x in mpdr})
    # =====================================================
    # 3.5️⃣ Which users are in this (filtered) date range
    # =====================================================
    mpd_id_set = set(mpd_ids)
    user_ids = sorted({
        d["user_id"] for d in deliveries_in_range
        if d.get("user_id") and d.get("meal_plan_day_id") in mpd_id_set
    })
    # If there are no users (e.g., internal/testing days), comments will be empty.
    # =====================================================
    # 3.6️⃣ Fetch recipe comments from user_recipe_preferences
    # =====================================================
    prefs = []
    if user_ids and recipe_ids:
        prefs = (
            supabase.table("user_recipe_preferences")
            .select("recipe_id, user_id, comment, updated_at, created_at")
            .in_("recipe_id", recipe_ids)
            .in_("user_id", user_ids)
            .not_.is_("comment", None)
            .execute()
            .data
        ) or []

    # (Optional) keep only latest comment per (recipe_id, user_id)
    def _ts(p):
        return p.get("updated_at") or p.get("created_at") or ""

    prefs.sort(key=_ts, reverse=True)

    latest_pref_by_pair = {}
    for p in prefs:
        key = (p.get("recipe_id"), p.get("user_id"))
        if key not in latest_pref_by_pair:
            latest_pref_by_pair[key] = p

    prefs = list(latest_pref_by_pair.values())

    # =====================================================
        # =====================================================
    # 3.7️⃣ Fetch user display names from "user"
    # =====================================================
    users = []
    if user_ids:
        users = (
            supabase.table("user")
            .select("id, name, last_name, " + ", ".join(ALLERGEN_KEYS))
            .in_("id", user_ids)
            .execute()
            .data
        ) or []

    def _display_name(u):
        if not u:
            return "Unknown"
        fn = (u.get("name") or "").strip()
        ln = (u.get("last_name") or "").strip()
        full = (fn + " " + ln).strip()
        return full or fn or ln or "Unknown"

    user_name_map = {u["id"]: _display_name(u) for u in users if u.get("id")}
    user_map = {u["id"]: u for u in users if u.get("id")}

    # =====================================================
    # 3.8️⃣ Recipe-level allergen rollup — same recipe_allergen view every
    # customer-facing surface (My Tastes, menu, order review) uses, so a
    # "this client is allergic" flag here can never disagree with what the
    # client themselves was shown. Alert-only: never filters/reorders recipes.
    # =====================================================
    recipe_allergen_rows = (
        supabase.table("recipe_allergen")
        .select("*")
        .in_("recipe_id", recipe_ids)
        .execute()
        .data
    ) if recipe_ids else []
    recipe_allergen_keys_by_recipe = {
        row["recipe_id"]: {k for k in ALLERGEN_KEYS if row.get(k)}
        for row in (recipe_allergen_rows or [])
    }


    # Group comments by recipe_id for fast lookup later
    comments_by_recipe = defaultdict(list)
    for p in prefs:
        rid = p.get("recipe_id")
        uid = p.get("user_id")
        comments_by_recipe[rid].append(
            {
                "user_id": uid,
                "name": user_name_map.get(uid, "Unknown"),
                "comment": p.get("comment"),
                "updated_at": p.get("updated_at"),
                "created_at": p.get("created_at"),
            }
        )


    # =====================================================
    # 4️⃣ Recipes
    # =====================================================
    recipes = (
        supabase.table("recipe")
        .select("*")
        .in_("id", recipe_ids)
        .execute()
        .data
    )
    recipe_map = {r["id"]: r for r in recipes}

    # =====================================================
    # 5️⃣ Servings (meal_plan_day_recipe_serving)
    # =====================================================
    servings_query = (
        supabase.table("meal_plan_day_recipe_serving")
        .select("*")
        .in_("meal_plan_day_recipe_id", mpdr_ids)
    )

    servings_query = apply_null_filter(servings_query, "subrecipe_id", filters["subrecipe_id"])


    servings = servings_query.execute().data
    if not servings:
        return []

    # SORT SERVINGS TO ENSURE DETERMINISTIC ORDER
    servings.sort(key=lambda s: (s.get("subrecipe_id") or 0, s.get("id")))

    subrecipe_ids = list({s["subrecipe_id"] for s in servings if s["subrecipe_id"]})

    # =====================================================
    # 6️⃣ Subrecipes
    # =====================================================
    subrecipes = (
        supabase.table("subrecipe")
        .select("*")
        .in_("id", subrecipe_ids)
        .execute()
        .data
    )
    subrecipe_map = {s["id"]: s for s in subrecipes}

    # =====================================================
    # 7️⃣ Subrecipe ingredients
    # =====================================================
    subrec_ingred = (
        supabase.table("subrec_ingred")
        .select("*")
        .in_("subrecipe_id", subrecipe_ids)
        .execute()
        .data
    )

    ingredient_ids = list({i["ingredient_id"] for i in subrec_ingred})

    ingredients = (
        supabase.table("ingredient")
        .select("*")
        .in_("id", ingredient_ids)
        .execute()
        .data
    )
    ingredient_map = {i["id"]: i for i in ingredients}

    subrec_ing_map = defaultdict(list)
    for ing in subrec_ingred:
        subrec_ing_map[ing["subrecipe_id"]].append(ing)

    # =====================================================
    # 8️⃣ Build final output
    # =====================================================
    output = []

    # earliest_date shown to the admin is the cook/delivery date (what they
    # filtered by), not the eating date — fall back to eating date only if a
    # meal_plan_day somehow has no delivery row.
    def _cook_date(meal_plan_day_id):
        return delivery_date_by_mpd.get(meal_plan_day_id) or mpd_map[meal_plan_day_id]["date"]

    # SORT RECIPES BY EARLIEST DATE
    recipe_ids_sorted = sorted(
        recipe_ids,
        key=lambda rid: min(
            _cook_date(r["meal_plan_day_id"])
            for r in mpdr
            if r["recipe_id"] == rid
        )
    )

    for recipe_id in recipe_ids_sorted:
        recipe = recipe_map.get(recipe_id)
        if not recipe:
            continue

        mpdr_for_recipe = [r for r in mpdr if r["recipe_id"] == recipe_id]
        mpdr_ids_for_recipe = [r["id"] for r in mpdr_for_recipe]

        recipe_servings = [
            s for s in servings if s["meal_plan_day_recipe_id"] in mpdr_ids_for_recipe
        ]
        if not recipe_servings:
            continue

        dates = [_cook_date(r["meal_plan_day_id"]) for r in mpdr_for_recipe]
        earliest_date = min(dates)

        # ------------------------------------------
        # 🟨 ALLERGEN CONFLICTS — which clients on this recipe declared an
        # allergen this recipe actually contains. Alert-only; doesn't affect
        # sorting/filtering above.
        # ------------------------------------------
        # Users actually eating THIS recipe on the filtered dates — comments
        # and allergen conflicts must both be scoped to this set, not to
        # every user with a delivery somewhere in the date range (that was
        # showing e.g. a client's tawouk comment on tawouk cards even on
        # days that client wasn't having tawouk).
        recipe_user_ids = {
            user_id_by_mpd[r["meal_plan_day_id"]]
            for r in mpdr_for_recipe
            if user_id_by_mpd.get(r["meal_plan_day_id"])
        }

        allergen_conflicts = []
        recipe_allergen_keys = recipe_allergen_keys_by_recipe.get(recipe_id)
        if recipe_allergen_keys:
            for uid in recipe_user_ids:
                user = user_map.get(uid)
                if not user:
                    continue
                hit = sorted(k for k in recipe_allergen_keys if user.get(k))
                if hit:
                    allergen_conflicts.append({
                        "user_id": uid,
                        "name": user_name_map.get(uid, "Unknown"),
                        "allergens": hit,
                    })

        # ------------------------------------------
        # 🟦 RECIPE-LEVEL INGREDIENTS (sorted alphabetically)
        # ------------------------------------------
        recipe_ing_totals = defaultdict(float)

        for s in recipe_servings:
            sub_id = s["subrecipe_id"]
            if not sub_id:
                continue

            multiplier = s["recipe_subrecipe_serving_calculated"] or 0

            for ing in subrec_ing_map[sub_id]:
                ing_id = ing["ingredient_id"]
                base_qty = ing["quantity"] or 0

                ing_def = ingredient_map.get(ing_id, {})
                serving_per_unit = ing_def.get("serving_per_unit") or 1.0

                recipe_ing_totals[ing_id] += base_qty * multiplier * serving_per_unit

        ingredient_list = sorted(
            [
                {
                    "ingredient_id": ing_id,
                    "name": ingredient_map[ing_id]["name"],
                    "unit": ingredient_map[ing_id]["unit"],
                    "total_quantity": round(qty, 1),
                }
                for ing_id, qty in recipe_ing_totals.items()
            ],
            key=lambda x: x["name"].lower(),
        )

        # ------------------------------------------
        # 🟧 SUBRECIPES (sorted alphabetically)
        # ------------------------------------------
        servings_by_sub = defaultdict(list)
        for s in recipe_servings:
            if s["subrecipe_id"]:
                servings_by_sub[s["subrecipe_id"]].append(s)

        subrecipe_list = []

        for sub_id, sub_servings in servings_by_sub.items():
            sub = subrecipe_map.get(sub_id)
            if not sub:
                continue

            total_servings = sum(
                (s["recipe_subrecipe_serving_calculated"] or 0) for s in sub_servings
            )

            # SUBRECIPE INGREDIENTS (sorted alphabetically)
            sub_ing_totals = defaultdict(float)
            for ing in subrec_ing_map[sub_id]:
                ing_id = ing["ingredient_id"]
                base_qty = ing["quantity"] or 0

                ing_def = ingredient_map.get(ing_id, {})
                serving_per_unit = ing_def.get("serving_per_unit") or 1.0

                sub_ing_totals[ing_id] += base_qty * total_servings * serving_per_unit

            sub_ing_list = sorted(
                [
                    {
                        "ingredient_id": ing_id,
                        "name": ingredient_map[ing_id]["name"],
                        "unit": ingredient_map[ing_id]["unit"],
                        "quantity": round(qty, 1),
                    }
                    for ing_id, qty in sub_ing_totals.items()
                ],
                key=lambda x: x["name"].lower(),
            )

            subrecipe_list.append(
                {
                    "subrecipe_id": sub_id,
                    "name": sub["name"],
                    "description": sub["description"],
                    "instructions": sub["instructions"],
                    "total_servings": total_servings,
                    "selected_meal_plan_day_recipe_serving_id": [s["id"] for s in sub_servings],
                    # Only the meal_plan_day_recipe ids that actually HAVE a
                    # serving row for this subrecipe — NOT every mpdr of the
                    # parent recipe. A recipe's subrecipe composition can vary
                    # across its own instances (e.g. a rotating side), so
                    # r["meal_plan_day_recipe_ids"] (all mpdrs for the recipe)
                    # is the wrong list to hand /portioning/summary for one
                    # specific subrecipe: it would include mpdrs that were
                    # never served with this subrecipe, tripping the "Subrecipe
                    # missing in some MPDRs" guard there.
                    "meal_plan_day_recipe_ids": sorted({s["meal_plan_day_recipe_id"] for s in sub_servings}),
                    "ingredients_needed": sub_ing_list,
                }
            )

        # SORT SUBRECIPES ALPHABETICALLY
        subrecipe_list = sorted(subrecipe_list, key=lambda x: x["name"].lower())

        # final assembled recipe item
        output.append(
            {
                "recipe_id": recipe_id,
                "name": recipe["name"],
                "description": recipe["description"],
                "instructions": recipe["instructions"],
                "meal_plan_day_recipe_ids": mpdr_ids_for_recipe,
                "earliest_date": earliest_date,
                "ingredients_needed": ingredient_list,
                "subrecipes": subrecipe_list,
                "comments": [
                    c for c in comments_by_recipe.get(recipe_id, [])
                    if c["user_id"] in recipe_user_ids
                ],
                # True if this recipe was swapped in on ANY of the underlying
                # days/clients being grouped here — staff must not trust an
                # already-printed label showing pre-swap macros in that case.
                "any_swapped": any(r.get("is_swapped") for r in mpdr_for_recipe),
                "allergen_conflicts": allergen_conflicts,
            }
        )

    # FINAL SORT: recipes by earliest date
    output = sorted(output, key=lambda r: r["earliest_date"])

    return output
