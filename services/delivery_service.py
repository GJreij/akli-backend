# services/delivery_service.py

from urllib.parse import quote

from utils.supabase_client import supabase
from utils.supabase_paging import fetch_all

# frontend/src/components/AddressForm.tsx DEFAULT_MAP_CENTER — the pin the map
# picker opens on before the client drags it or geolocates. Some clients save
# without ever moving it, so this exact coordinate in the DB is a placeholder,
# not a real location, even though it looks like ordinary pin data.
_DEFAULT_MAP_CENTER = (33.8938, 35.5018)


def get_delivery_overview(start_date, end_date):

    # --- 1. Deliveries between dates ---
    # Excludes cancelled/cancellation-pending — same rule the cooking and
    # procurement panels apply. Without this, a client whose day was
    # modified/cancelled and re-created shows up TWICE on the same date (the
    # orphaned cancelled row plus the current one) with the same name and
    # address, which is what made this panel look "wrong".
    #
    # Also excludes meal_plan_day_id IS NULL: order_service._create_deliveries_
    # and_increment_counts inserts these rows BEFORE _store_meal_plan_bundle
    # links them a few calls later (not one transaction) — if that later step
    # throws (timeout, solver error, etc.) the deliveries row is already
    # committed and stays permanently unlinked: a "pending" delivery with no
    # meal plan, recipes, or payment behind it at all. Confirmed 3 separate
    # real occurrences of this in the data as of 2026-09-17. These aren't
    # real deliveries and must not be shown or counted here.
    deliveries = fetch_all(lambda lo, hi: (
        supabase.table("deliveries")
        .select("id, delivery_date, delivery_slot_id, user_id, delivery_address, status, meal_plan_day_id, needs_cutlery")
        .gte("delivery_date", start_date)
        .lte("delivery_date", end_date)
        .not_.in_("status", ["cancellation_pending", "cancelled"])
        .not_.is_("meal_plan_day_id", None)
        .range(lo, hi)
    ))

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

    # --- 4. Saved addresses (for label + lat/lng -> maps link) ---
    addresses_by_user = {}
    if user_ids:
        addr_res = (
            supabase.table("user_delivery_address")
            .select("user_id, label, address_text, lat, lng, is_default")
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

    def _matched_address(user_id, address_text):
        # Only ever match a saved address by this EXACT address text — falling
        # back to the client's default/first saved address silently pointed
        # drivers at a different location than the one actually being delivered
        # to whenever that specific address had no pin of its own (~83% of
        # deliveries, confirmed 2026-09-22). No match means no pin and no label.
        candidates = addresses_by_user.get(user_id, [])
        return next(
            (a for a in candidates
             if (a.get("address_text") or "").strip().lower() == (address_text or "").strip().lower()),
            None,
        )

    def _map_info(matched_addr, address_text):
        lat, lng = (matched_addr or {}).get("lat"), (matched_addr or {}).get("lng")
        has_pin = lat is not None and lng is not None
        # A pin sitting exactly on the map picker's untouched default center
        # means the client saved without ever moving it — not a real location.
        is_placeholder_pin = has_pin and round(lat, 4) == _DEFAULT_MAP_CENTER[0] and round(lng, 4) == _DEFAULT_MAP_CENTER[1]
        if has_pin and not is_placeholder_pin:
            return f"https://www.google.com/maps?q={lat},{lng}", True
        # No usable pin — fall back to a text search on the address itself
        # so there's always SOME map action, never a dead end.
        if address_text:
            return f"https://www.google.com/maps/search/?api=1&query={quote(address_text)}", False
        return None, False

    # --- 6. Assemble ---
    output = []
    for d in deliveries:
        user = users_by_id.get(d.get("user_id"))
        slot = slots_by_id.get(d.get("delivery_slot_id"))
        payment = payments_by_mpd.get(d.get("meal_plan_day_id"))
        matched_addr = _matched_address(d.get("user_id"), d.get("delivery_address"))
        maps_link, maps_is_precise = _map_info(matched_addr, d.get("delivery_address"))

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
            # The client's own free-text label on this saved address (e.g. a
            # landmark/street note) — shown on their profile but previously
            # dropped entirely on this panel, even when it held real delivery
            # detail the formal geocoded address_text didn't capture.
            "address_label": matched_addr.get("label") if matched_addr else None,
            "maps_link": maps_link,
            # False means maps_link is a text-search fallback (no real saved
            # pin, or the pin was never moved off the map's default center) —
            # the frontend labels this differently so it's never mistaken for
            # a precise, verified location.
            "maps_is_precise": maps_is_precise,
            "needs_cutlery": bool(d.get("needs_cutlery")),
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
