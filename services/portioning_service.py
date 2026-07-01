# services/portioning_service.py

from utils.supabase_client import supabase


def normalize_filter_value(value):
    if value is None:
        return None
    if isinstance(value, str):
        if value.strip() in ("", "null", "Null", "NULL"):
            return None
    return value


def parse_int_list(raw_value, field_name):
    if raw_value is None:
        raise ValueError(f"{field_name} is required")

    if isinstance(raw_value, str):
        parts = [p.strip() for p in raw_value.split(",") if p.strip() != ""]
        try:
            return [int(p) for p in parts]
        except ValueError:
            raise ValueError(f"{field_name} must be a comma-separated list of integers")

    if isinstance(raw_value, int):
        return [raw_value]

    if isinstance(raw_value, (list, tuple)):
        out = []
        for x in raw_value:
            try:
                out.append(int(x))
            except (TypeError, ValueError):
                raise ValueError(f"{field_name} contains non-integer")
        return out

    raise ValueError(f"Unsupported format for {field_name}")


def get_portioning_summary(subrecipe_id, meal_plan_day_recipe_ids, cooking_status="completed"):

    # --- 1. Fetch servings ---
    servings_res = (
        supabase.table("meal_plan_day_recipe_serving")
        .select(
            "id, meal_plan_day_recipe_id, subrecipe_id, "
            "recipe_subrecipe_serving_calculated, weight_after_cooking, cooking_status,portioning_status "
        )
        .eq("subrecipe_id", subrecipe_id)
        .in_("meal_plan_day_recipe_id", meal_plan_day_recipe_ids)
        .eq("cooking_status", cooking_status)
        .execute()
    )
    servings = servings_res.data or []

    if not servings:
        any_status_res = (
            supabase.table("meal_plan_day_recipe_serving")
            .select("id", count="exact")
            .eq("subrecipe_id", subrecipe_id)
            .in_("meal_plan_day_recipe_id", meal_plan_day_recipe_ids)
            .execute()
        )
        if (any_status_res.count or 0) > 0:
            return None, "This subrecipe hasn't been marked as cooked yet — mark it cooked before portioning"
        return None, "No servings found"

    # Make sure subrecipe appears in *all* input meal_plan_day_recipe_ids
    found_ids = {row["meal_plan_day_recipe_id"] for row in servings}
    expected_ids = set(meal_plan_day_recipe_ids)

    if found_ids != expected_ids:
        missing = list(expected_ids - found_ids)
        extra = list(found_ids - expected_ids)
        return None, {
            "error": "Subrecipe missing in some MPDRs",
            "missing": missing,
            "extra_found": extra
        }

    total_subrecipe_servings = sum(
        row.get("recipe_subrecipe_serving_calculated") or 0 for row in servings
    )

    # --- 2. meal_plan_day_recipe → meal_plan_day_id ---
    mpdr_res = (
        supabase.table("meal_plan_day_recipe")
        .select("id, meal_plan_day_id")
        .in_("id", list(found_ids))
        .execute()
    )
    mpdr_rows = mpdr_res.data or []
    mpdr_by_id = {row["id"]: row for row in mpdr_rows}

    mpd_ids = {
        row["meal_plan_day_id"]
        for row in mpdr_rows
        if row.get("meal_plan_day_id") is not None
    }

    # --- 3. meal_plan_day ---
    mpd_res = (
        supabase.table("meal_plan_day")
        .select("id, date, delivery_id")
        .in_("id", list(mpd_ids))
        .execute()
    )
    mpd_by_id = {row["id"]: row for row in (mpd_res.data or [])}

    # --- 4. deliveries ---
    delivery_ids = {
        row["delivery_id"]
        for row in mpd_by_id.values()
        if row.get("delivery_id")
    }
    deliveries_by_id = {}
    if delivery_ids:
        deliv_res = (
            supabase.table("deliveries")
            .select("id, delivery_date, delivery_slot_id, user_id")
            .in_("id", list(delivery_ids))
            .execute()
        )
        deliveries_by_id = {r["id"]: r for r in (deliv_res.data or [])}

    # --- 5. users ---
    user_ids = {
        d["user_id"] for d in deliveries_by_id.values() if d.get("user_id")
    }
    users_by_id = {}
    if user_ids:
        users_res = (
            supabase.table("user")
            .select("id, name, last_name")
            .in_("id", list(user_ids))
            .execute()
        )
        users_by_id = {u["id"]: u for u in (users_res.data or [])}

    # --- 6. delivery slots ---
    slot_ids = {
        d["delivery_slot_id"] for d in deliveries_by_id.values() if d.get("delivery_slot_id")
    }
    slots_by_id = {}
    if slot_ids:
        slots_res = (
            supabase.table("delivery_slots")
            .select("id, start_time, end_time")
            .in_("id", list(slot_ids))
            .execute()
        )
        slots_by_id = {s["id"]: s for s in (slots_res.data or [])}

    # --- 7. Subrecipe info ---
    subrecipe_res = (
        supabase.table("subrecipe")
        .select("*")
        .eq("id", subrecipe_id)
        .execute()
    )
    if not subrecipe_res.data:
        return None, f"Subrecipe {subrecipe_id} not found"

    subrecipe_info = subrecipe_res.data[0]

    # --- 8. Subrecipe ingredients ---
    sub_ingred_res = (
        supabase.table("subrec_ingred")
        .select("id, subrecipe_id, ingredient_id, quantity, optional")
        .eq("subrecipe_id", subrecipe_id)
        .execute()
    )
    sub_ingred = sub_ingred_res.data or []

    ingredient_ids = [r["ingredient_id"] for r in sub_ingred if r.get("ingredient_id")]
    ingredients_by_id = {}
    if ingredient_ids:
        ing_res = (
            supabase.table("ingredient")
            .select("id, name, unit, serving_per_unit")
            .in_("id", ingredient_ids)
            .execute()
        )
        ingredients_by_id = {i["id"]: i for i in (ing_res.data or [])}

    # --- Build result lines per client ---
    clients = []
    for r in servings:
        mpdr = mpdr_by_id.get(r["meal_plan_day_recipe_id"])
        mpd = mpd_by_id.get(mpdr["meal_plan_day_id"])
        deliv = deliveries_by_id.get(mpd.get("delivery_id"))
        user = users_by_id.get(deliv.get("user_id")) if deliv else None
        slot = slots_by_id.get(deliv.get("delivery_slot_id")) if deliv else None

        clients.append({
            "meal_plan_day_recipe_serving_id": r["id"],
            "delivery_date": deliv.get("delivery_date") if deliv else None,
            "delivery_slot": slot,
            "client": user,

            # servings
            "servings_for_client": r.get("recipe_subrecipe_serving_calculated"),

            # cooking / portioning status
            "cooking_status": r.get("cooking_status"),
            "portioning_status": r.get("portioning_status"),

            # weight handling
            "weight_after_cooking": r.get("weight_after_cooking") or 0,
            "has_weight_after_cooking": r.get("weight_after_cooking") is not None
        })


    # --- Ingredient summary ---
    ingredients_summary = []
    for rel in sub_ingred:
        ing = ingredients_by_id.get(rel["ingredient_id"])
        if not ing:
            continue

        qty = rel.get("quantity") or 0
        spu = ing.get("serving_per_unit") or 0

        total_servings_equivalent = total_subrecipe_servings * qty * spu
        total_units = total_subrecipe_servings * qty

        ingredients_summary.append({
            "ingredient_id": ing["id"],
            "name": ing.get("name"),
            "unit": ing.get("unit"),
            "quantity_per_subrecipe": qty,
            "serving_per_unit": spu,
            "total_units_for_batch": total_units,
            "total_servings_equivalent": total_servings_equivalent,
            "optional": rel.get("optional")
        })

    # Final response dictionary
    return {
        "subrecipe": subrecipe_info,
        "summary": {
            "total_subrecipe_servings_for_batch": total_subrecipe_servings,
            "ingredients": ingredients_summary
        },
        "clients": clients
    }, None
