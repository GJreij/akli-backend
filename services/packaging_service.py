# services/packaging_service.py

from utils.supabase_client import supabase


def get_packaging_view(start_date, end_date):

    # --- 1. Deliveries between dates ---
    deliveries_res = (
        supabase.table("deliveries")
        .select("id, delivery_date, delivery_slot_id, user_id")
        .gte("delivery_date", start_date)
        .lte("delivery_date", end_date)
        .execute()
    )

    deliveries = deliveries_res.data or []
    if not deliveries:
        return []

    deliveries_by_id = {d["id"]: d for d in deliveries}

    # --- 2. Delivery slots ---
    slot_ids = list({d["delivery_slot_id"] for d in deliveries if d.get("delivery_slot_id")})
    slots_by_id = {}

    if slot_ids:
        slots_res = (
            supabase.table("delivery_slots")
            .select("id, start_time, end_time")
            .in_("id", slot_ids)
            .execute()
        )
        slots_by_id = {s["id"]: s for s in (slots_res.data or [])}

    # --- 3. Users ---
    user_ids = list({d["user_id"] for d in deliveries if d.get("user_id")})
    users_by_id = {}

    if user_ids:
        users_res = (
            supabase.table("user")
            .select("id, name, last_name")
            .in_("id", user_ids)
            .execute()
        )
        users_by_id = {u["id"]: u for u in (users_res.data or [])}

    # --- 4. meal_plan_day ---
    mpd_res = (
        supabase.table("meal_plan_day")
        .select("id, delivery_id")
        .in_("delivery_id", list(deliveries_by_id.keys()))
        .execute()
    )

    mpd = mpd_res.data or []
    mpd_by_delivery = {}
    for row in mpd:
        mpd_by_delivery.setdefault(row["delivery_id"], []).append(row)

    # --- 5. meal_plan_day_recipe ---
    mpd_ids = [m["id"] for m in mpd]
    mpdr_res = (
        supabase.table("meal_plan_day_recipe")
        .select("id, meal_plan_day_id, recipe_id, meal_type, packaging_status")
        .in_("meal_plan_day_id", mpd_ids)
        .execute()
    )
    mpdr = mpdr_res.data or []
    mpdr_by_mpd = {}
    for r in mpdr:
        mpdr_by_mpd.setdefault(r["meal_plan_day_id"], []).append(r)

    # --- 6. recipes ---
    recipe_ids = [r["recipe_id"] for r in mpdr if r.get("recipe_id")]
    recipes_by_id = {}
    if recipe_ids:
        recipes_res = (
            supabase.table("recipe")
            .select("id, name")
            .in_("id", recipe_ids)
            .execute()
        )
        recipes_by_id = {r["id"]: r for r in (recipes_res.data or [])}

    # --- 7. servings (subrecipes) ---
    mpdr_ids = [r["id"] for r in mpdr]
    servings_res = (
        supabase.table("meal_plan_day_recipe_serving")
        .select("meal_plan_day_recipe_id, subrecipe_id, recipe_subrecipe_serving_calculated")
        .in_("meal_plan_day_recipe_id", mpdr_ids)
        .execute()
    )
    servings = servings_res.data or []

    servings_by_mpdr = {}
    for s in servings:
        servings_by_mpdr.setdefault(s["meal_plan_day_recipe_id"], []).append(s)

    # --- 8. subrecipes ---
    subrecipe_ids = list({s["subrecipe_id"] for s in servings if s.get("subrecipe_id")})
    subrecipes_by_id = {}
    if subrecipe_ids:
        subrecipes_res = (
            supabase.table("subrecipe")
            .select("id, name")
            .in_("id", subrecipe_ids)
            .execute()
        )
        subrecipes_by_id = {s["id"]: s for s in (subrecipes_res.data or [])}

    # -------------------------------------------------------------------------
    # ------------------------- GROUPING STARTS HERE ---------------------------
    # -------------------------------------------------------------------------

    packaging_output = {}

    for delivery in deliveries:
        d_date = delivery["delivery_date"]
        slot_id = delivery["delivery_slot_id"]
        user = users_by_id.get(delivery["user_id"])
        slot = slots_by_id.get(slot_id)

        # Initialize date group
        if d_date not in packaging_output:
            packaging_output[d_date] = {}

        # Initialize slot group
        if slot_id not in packaging_output[d_date]:
            packaging_output[d_date][slot_id] = {
                "slot_id": slot_id,
                "start_time": slot["start_time"] if slot else None,
                "end_time": slot["end_time"] if slot else None,
                "clients": {}
            }

        client_key = delivery["user_id"]

        if client_key not in packaging_output[d_date][slot_id]["clients"]:
            packaging_output[d_date][slot_id]["clients"][client_key] = {
                "name": user.get("name") if user else None,
                "last_name": user.get("last_name") if user else None,
                "recipes": []
            }

        # Which mpd entries belong to this delivery?
        mpd_entries = mpd_by_delivery.get(delivery["id"], [])

        for mpd_entry in mpd_entries:
            mpdr_list = mpdr_by_mpd.get(mpd_entry["id"], [])

            for mpdr_entry in mpdr_list:
                recipe_info = recipes_by_id.get(mpdr_entry["recipe_id"])

                # Subrecipes
                subs = []
                for serv in servings_by_mpdr.get(mpdr_entry["id"], []):
                    subs.append({
                        "subrecipe_id": serv["subrecipe_id"],
                        "subrecipe_name": subrecipes_by_id.get(serv["subrecipe_id"], {}).get("name"),
                        "serving_size": serv["recipe_subrecipe_serving_calculated"]
                    })

                packaging_output[d_date][slot_id]["clients"][client_key]["recipes"].append({
                    "meal_plan_day_recipe_id": mpdr_entry["id"],
                    "meal_type": mpdr_entry.get("meal_type"),
                    "recipe_name": recipe_info.get("name") if recipe_info else None,
                    "packaging_status": mpdr_entry.get("packaging_status") or "pending",
                    "subrecipes": subs
                })

    # -------------------------------------------------------------------------
    # ------------------------ TRANSFORM INTO ARRAY ---------------------------
    # -------------------------------------------------------------------------

    final_output = []
    for d_date, slots in sorted(packaging_output.items()):
        slot_list = []
        for slot_id, slot_data in sorted(slots.items(), key=lambda x: x[1]["start_time"] or ""):
            clients_list = list(slot_data["clients"].values())
            slot_data_out = {
                "slot_id": slot_id,
                "start_time": slot_data["start_time"],
                "end_time": slot_data["end_time"],
                "clients": clients_list
            }
            slot_list.append(slot_data_out)

        final_output.append({
            "delivery_date": d_date,
            "slots": slot_list
        })

    return final_output
