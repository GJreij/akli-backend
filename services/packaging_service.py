# services/packaging_service.py

from utils.supabase_client import supabase
from utils.supabase_paging import fetch_all

# Canonical meal-of-day order, matching the convention used across the
# client-facing app (labels.ts, OrderHistory.tsx, OrderFlow.tsx) and the
# admin packaging/portioning boards.
MEAL_TYPE_ORDER = {"breakfast": 0, "lunch": 1, "snack": 2, "dinner": 3}


def _client_sort_key(client):
    return f"{client.get('name') or ''} {client.get('last_name') or ''}".strip().lower()


def _recipe_sort_key(recipe):
    meal_rank = MEAL_TYPE_ORDER.get((recipe.get("meal_type") or "").lower(), 99)
    return (recipe.get("eat_date") or "", meal_rank, (recipe.get("recipe_name") or "").lower())


def _salad_status(default_ids, actual_ids, subrecipes_by_id):
    """How this meal's side salad differs from the recipe's standard one.

    None       -> the dish has no salad and none was added (nothing to flag)
    "standard" -> packed with the recipe's usual salad
    "removed"  -> the dish normally has a salad but this client gets none
    "swapped"  -> a different salad than usual (e.g. House Salad fallback)
    "added"    -> a salad on a dish that normally has none
    """
    if not default_ids and not actual_ids:
        return None

    def names(ids):
        return ", ".join(sorted((subrecipes_by_id.get(i, {}).get("name") or "?") for i in ids)) or None

    if not actual_ids:
        status = "removed"
    elif not default_ids:
        status = "added"
    elif actual_ids == default_ids:
        status = "standard"
    else:
        status = "swapped"
    return {"status": status, "usual": names(default_ids), "packed": names(actual_ids)}


def get_packaging_view(start_date, end_date):

    # --- 1. Deliveries between dates ---
    deliveries_res = (
        supabase.table("deliveries")
        .select("id, delivery_date, delivery_slot_id, user_id, needs_cutlery")
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
        .select("id, delivery_id, status, date")
        .in_("delivery_id", list(deliveries_by_id.keys()))
        .execute()
    )

    # Cancelled/cancellation-pending orders must not appear on the packaging
    # board — don't cook/package something that might not ship.
    mpd = [x for x in (mpd_res.data or []) if x.get("status") not in ("cancellation_pending", "cancelled")]
    mpd_by_delivery = {}
    for row in mpd:
        mpd_by_delivery.setdefault(row["delivery_id"], []).append(row)

    # --- 5. meal_plan_day_recipe ---
    mpd_ids = [m["id"] for m in mpd]
    mpdr_res = (
        supabase.table("meal_plan_day_recipe")
        .select("id, meal_plan_day_id, recipe_id, meal_type, packaging_status, is_swapped")
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
    # Paged: several components per meal means a multi-day range can pass
    # PostgREST's 1000-row cap, which would silently drop sides off the board.
    mpdr_ids = [r["id"] for r in mpdr]
    servings = fetch_all(lambda lo, hi: (
        supabase.table("meal_plan_day_recipe_serving")
        .select("id, meal_plan_day_recipe_id, subrecipe_id, recipe_subrecipe_serving_calculated")
        .in_("meal_plan_day_recipe_id", mpdr_ids)
        .order("id")
        .range(lo, hi)
    )) if mpdr_ids else []

    servings_by_mpdr = {}
    for s in servings:
        servings_by_mpdr.setdefault(s["meal_plan_day_recipe_id"], []).append(s)

    # --- 7b. each recipe's standard composition, to spot salads that were
    # removed or swapped for this client (packers pack from memory of the
    # standard dish, so the difference has to be shouted, not implied).
    unique_recipe_ids = list(set(recipe_ids))
    recipe_subs = fetch_all(lambda lo, hi: (
        supabase.table("recipe_subrecipe")
        .select("id, recipe_id, subrecipe_id")
        .in_("recipe_id", unique_recipe_ids)
        .order("id")
        .range(lo, hi)
    )) if unique_recipe_ids else []

    # --- 8. subrecipes ---
    subrecipe_ids = list(
        {s["subrecipe_id"] for s in servings if s.get("subrecipe_id")}
        | {rs["subrecipe_id"] for rs in recipe_subs if rs.get("subrecipe_id")}
    )
    subrecipes_by_id = {}
    if subrecipe_ids:
        subrecipes_res = (
            supabase.table("subrecipe")
            .select("id, name, is_side_salad")
            .in_("id", subrecipe_ids)
            .execute()
        )
        subrecipes_by_id = {s["id"]: s for s in (subrecipes_res.data or [])}

    def _is_salad(sub_id):
        return bool(subrecipes_by_id.get(sub_id, {}).get("is_side_salad"))

    default_salads_by_recipe = {}
    for rs in recipe_subs:
        if _is_salad(rs["subrecipe_id"]):
            default_salads_by_recipe.setdefault(rs["recipe_id"], set()).add(rs["subrecipe_id"])

    # -------------------------------------------------------------------------
    # ------------------------- GROUPING STARTS HERE ---------------------------
    # -------------------------------------------------------------------------

    packaging_output = {}

    for delivery in deliveries:
        # A delivery with no active (non-cancelled) meal_plan_day has nothing
        # to package — skip it entirely rather than showing an empty client
        # card, which would otherwise confuse the packaging team into
        # thinking there's a forgotten order for that slot.
        mpd_entries = mpd_by_delivery.get(delivery["id"], [])
        if not mpd_entries:
            continue

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
                "needs_cutlery": False,
                "recipes": []
            }

        # A client can only have one delivery per date+slot, but OR it in
        # case of duplicates so a cutlery request is never hidden.
        if delivery.get("needs_cutlery"):
            packaging_output[d_date][slot_id]["clients"][client_key]["needs_cutlery"] = True

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
                        "serving_size": serv["recipe_subrecipe_serving_calculated"],
                        "is_side_salad": _is_salad(serv["subrecipe_id"]),
                    })

                packaging_output[d_date][slot_id]["clients"][client_key]["recipes"].append({
                    "meal_plan_day_recipe_id": mpdr_entry["id"],
                    # The day the client EATS this meal. Usually delivery_date+1,
                    # but a client can get several days in one bag (several
                    # deliveries rows on the same night) — the board splits the
                    # card by this so packers know which box is for which day.
                    "eat_date": mpd_entry.get("date"),
                    "meal_type": mpdr_entry.get("meal_type"),
                    "recipe_name": recipe_info.get("name") if recipe_info else None,
                    "packaging_status": mpdr_entry.get("packaging_status") or "pending",
                    "is_swapped": bool(mpdr_entry.get("is_swapped")),
                    "subrecipes": subs,
                    "salad": _salad_status(
                        default_salads_by_recipe.get(mpdr_entry["recipe_id"], set()),
                        {s["subrecipe_id"] for s in subs if s["is_side_salad"]},
                        subrecipes_by_id,
                    ),
                })

    # -------------------------------------------------------------------------
    # ------------------------ TRANSFORM INTO ARRAY ---------------------------
    # -------------------------------------------------------------------------

    final_output = []
    for d_date, slots in sorted(packaging_output.items()):
        slot_list = []
        for slot_id, slot_data in sorted(slots.items(), key=lambda x: x[1]["start_time"] or ""):
            clients_list = sorted(slot_data["clients"].values(), key=_client_sort_key)
            for client in clients_list:
                client["recipes"].sort(key=_recipe_sort_key)
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
