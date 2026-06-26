# services/delivery_service.py

from utils.supabase_client import supabase


def get_delivery_overview(start_date, end_date):

    # --- 1. Deliveries between dates ---
    deliveries_res = (
        supabase.table("deliveries")
        .select("id, delivery_date, delivery_slot_id, user_id, delivery_address, status, meal_plan_day_id")
        .gte("delivery_date", start_date)
        .lte("delivery_date", end_date)
        .execute()
    )

    deliveries = deliveries_res.data or []
    if not deliveries:
        return []

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
            .select("id, name, last_name, phone_number")
            .in_("id", user_ids)
            .execute()
        )
        users_by_id = {u["id"]: u for u in (users_res.data or [])}

    # --- 4. Saved addresses (for lat/lng -> maps link) ---
    addresses_by_user = {}
    if user_ids:
        addr_res = (
            supabase.table("user_delivery_address")
            .select("user_id, address_text, lat, lng, is_default")
            .in_("user_id", user_ids)
            .execute()
        )
        for a in addr_res.data or []:
            addresses_by_user.setdefault(a["user_id"], []).append(a)

    # --- 5. Payments (keyed by meal_plan_day_id) ---
    mpd_ids = list({d["meal_plan_day_id"] for d in deliveries if d.get("meal_plan_day_id")})
    payments_by_mpd = {}
    if mpd_ids:
        payments_res = (
            supabase.table("payment")
            .select("meal_plan_day_id, amount, currency, provider, status")
            .in_("meal_plan_day_id", mpd_ids)
            .execute()
        )
        for p in payments_res.data or []:
            payments_by_mpd[p["meal_plan_day_id"]] = p

    def _maps_link(user_id, address_text):
        candidates = addresses_by_user.get(user_id, [])
        match = None
        for a in candidates:
            if (a.get("address_text") or "").strip().lower() == (address_text or "").strip().lower():
                match = a
                break
        if not match:
            match = next((a for a in candidates if a.get("is_default")), None) or (candidates[0] if candidates else None)

        if match and match.get("lat") is not None and match.get("lng") is not None:
            return f"https://www.google.com/maps?q={match['lat']},{match['lng']}"
        return None

    # --- 6. Assemble ---
    output = []
    for d in deliveries:
        user = users_by_id.get(d.get("user_id"))
        slot = slots_by_id.get(d.get("delivery_slot_id"))
        payment = payments_by_mpd.get(d.get("meal_plan_day_id"))

        output.append({
            "id": d["id"],
            "delivery_date": d.get("delivery_date"),
            "status": d.get("status"),
            "delivery_slot": {
                "id": slot["id"],
                "start_time": slot.get("start_time"),
                "end_time": slot.get("end_time"),
            } if slot else None,
            "client": {
                "id": d.get("user_id"),
                "name": user.get("name") if user else None,
                "last_name": user.get("last_name") if user else None,
                "phone_number": user.get("phone_number") if user else None,
            } if user else None,
            "address": d.get("delivery_address"),
            "maps_link": _maps_link(d.get("user_id"), d.get("delivery_address")),
            "payment": {
                "amount": payment.get("amount"),
                "currency": payment.get("currency"),
                "provider": payment.get("provider"),
                "status": payment.get("status"),
                "collect_cash": (payment.get("provider") == "cash" and payment.get("status") != "paid"),
            } if payment else None,
        })

    def _sort_key(row):
        slot_start = (row["delivery_slot"] or {}).get("start_time") or ""
        client_name = ((row["client"] or {}).get("name") or "")
        return (row["delivery_date"] or "", slot_start, client_name.lower())

    output.sort(key=_sort_key)
    return output
