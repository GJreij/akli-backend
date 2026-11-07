"""
Kitchen Day: what to cook on a given cook date, net of what's already cooked.

Demand = every non-cancelled meal_plan_day_recipe_serving delivered from
`cook_date` up to HORIZON_DAYS ahead. Stock = active kitchen_batch rows.

A serving counts as covered when it's linked to a batch (batch_id) or was
already portioned (portioning_status 'done', from before batches existed).
Open servings are then allocated to active batches virtually, earliest
delivery first, picking the batch that expires soonest but still covers that
date — so a fridge batch is used before a freezer batch. Whatever is left
open becomes the cook list:

  must_cook  = open servings delivered on cook_date itself
  suggested  = open servings the batch would still be good for
               (cook_date .. cook_date + fridge_days)
  freeze     = open servings after that, up to freezer_days ahead —
               only for subrecipes that freeze

Orders have a 48h lead time (utils/dates.beirut_iso_date), so cook_date and
the day after are final; later days can still grow.

kitchen_settings shapes the cook-ahead window: with cook days set (e.g.
Sun/Tue/Thu) a batch only needs to last until the day before the next cook
day, and late_order_buffer_pct adds a margin on days still open to orders.
"""

from collections import defaultdict
from datetime import date, timedelta

from utils.dates import beirut_iso_date
from utils.supabase_client import supabase
from utils.supabase_paging import fetch_all

HORIZON_DAYS = 7
MAX_LOOKBACK_DAYS = 45  # longest freezer_days is 45
IN_CHUNK = 200  # ids per .in_() — keeps request URLs short


def _chunks(ids, size=IN_CHUNK):
    ids = list(ids)
    for i in range(0, len(ids), size):
        yield ids[i:i + size]


def _fetch_in(table, select, column, ids, extra=None):
    """fetch_all over chunked .in_(column, ids)."""
    rows = []
    for chunk in _chunks(ids):
        def build(lo, hi, chunk=chunk):
            q = supabase.table(table).select(select).in_(column, chunk)
            if extra:
                q = extra(q)
            return q.order("id").range(lo, hi)
        rows.extend(fetch_all(build))
    return rows


def _d(s):
    return date.fromisoformat(s[:10])


def _dow(d):
    """0 = Sunday … 6 = Saturday (kitchen_settings.cook_days)."""
    return (d.weekday() + 1) % 7


def get_kitchen_settings():
    rows = supabase.table("kitchen_settings").select("cook_days, late_order_buffer_pct").eq("tenant_id", 1).execute().data or []
    row = rows[0] if rows else {}
    return {
        "cook_days": sorted({int(x) for x in (row.get("cook_days") or [])}),
        "late_order_buffer_pct": int(row.get("late_order_buffer_pct") or 0),
    }


def _next_cook_day(after, cook_days):
    if not cook_days:
        return None
    for i in range(1, 15):
        d = after + timedelta(days=i)
        if _dow(d) in cook_days:
            return d
    return None


def _batch_use_by(batch, sub):
    if batch.get("use_by"):
        return _d(batch["use_by"])
    days = sub.get("freezer_days") if batch["storage"] == "freezer" else sub.get("fridge_days")
    return _d(batch["cooked_on"]) + timedelta(days=days or 0)


def _scaled_ingredients(sub_ingreds, ingredient_map, servings, digits=1):
    out = []
    for ing in sub_ingreds:
        ing_def = ingredient_map.get(ing["ingredient_id"])
        if not ing_def:
            continue
        qty = (ing.get("quantity") or 0) * servings * (ing_def.get("serving_per_unit") or 1.0)
        out.append({
            "ingredient_id": ing_def["id"],
            "name": ing_def["name"],
            "unit": ing_def.get("unit"),
            "quantity": round(qty, digits),
            "is_staple": bool(ing_def.get("is_staple")),
            "stored_frozen": bool(ing_def.get("stored_frozen")),
            "thaw_hours": ing_def.get("thaw_hours"),
        })
    return sorted(out, key=lambda x: x["name"].lower())


def get_kitchen_day(cook_date_str):
    cook_date = _d(cook_date_str)
    horizon_end = cook_date + timedelta(days=HORIZON_DAYS)

    batches = fetch_all(lambda lo, hi: (
        supabase.table("kitchen_batch").select("*").eq("status", "active").order("id").range(lo, hi)
    ))
    settings = get_kitchen_settings()
    next_planned = _next_cook_day(cook_date, settings["cook_days"])
    # Last day today's batch has to cover: the day before the next cook day.
    window_end = next_planned - timedelta(days=1) if next_planned else horizon_end
    open_from = _d(beirut_iso_date(2))  # first date that can still take orders
    buffer_pct = settings["late_order_buffer_pct"]

    # Most portions are weighed by eye, not through the portioning panel, so
    # a batch can't rely on batch_id links alone to be used up. Demand is
    # also read from the oldest active batch's cook date: those past
    # deliveries are allocated against it below (consuming it) but never
    # show up on the cook list.
    demand_start = min([cook_date] + [_d(b["cooked_on"]) for b in batches])
    demand_start = max(demand_start, cook_date - timedelta(days=MAX_LOOKBACK_DAYS))

    # ---------------------------------------------------------------
    # 1. Demand: deliveries → meal_plan_day → recipes → servings
    # ---------------------------------------------------------------
    deliveries = fetch_all(lambda lo, hi: (
        supabase.table("deliveries")
        .select("id, meal_plan_day_id, user_id, delivery_date")
        .gte("delivery_date", demand_start.isoformat())
        .lte("delivery_date", horizon_end.isoformat())
        .order("id")
        .range(lo, hi)
    ))
    delivery_by_mpd = {}
    for d in deliveries:
        mid = d.get("meal_plan_day_id")
        if mid and (mid not in delivery_by_mpd or d["delivery_date"] < delivery_by_mpd[mid]["delivery_date"]):
            delivery_by_mpd[mid] = d

    mpds = _fetch_in("meal_plan_day", "id, status", "id", delivery_by_mpd.keys())
    live_mpd_ids = [m["id"] for m in mpds if m.get("status") not in ("cancellation_pending", "cancelled")]

    mpdrs = _fetch_in("meal_plan_day_recipe", "id, meal_plan_day_id, recipe_id, meal_type", "meal_plan_day_id", live_mpd_ids)
    mpdr_map = {r["id"]: r for r in mpdrs}

    servings = _fetch_in(
        "meal_plan_day_recipe_serving",
        "id, meal_plan_day_recipe_id, subrecipe_id, recipe_subrecipe_serving_calculated, portioning_status, batch_id",
        "meal_plan_day_recipe_id", mpdr_map.keys(),
    )
    servings = [s for s in servings if s.get("subrecipe_id") and (s.get("recipe_subrecipe_serving_calculated") or 0) > 0]

    # ---------------------------------------------------------------
    # 2. How much of each active batch is already linked
    # ---------------------------------------------------------------
    linked_by_batch = defaultdict(float)
    for s in _fetch_in("meal_plan_day_recipe_serving", "id, batch_id, recipe_subrecipe_serving_calculated",
                       "batch_id", [b["id"] for b in batches]):
        linked_by_batch[s["batch_id"]] += s.get("recipe_subrecipe_serving_calculated") or 0

    sub_ids = {s["subrecipe_id"] for s in servings} | {b["subrecipe_id"] for b in batches}
    if not sub_ids:
        return {"cook_date": cook_date.isoformat(), "horizon_end": horizon_end.isoformat(),
                "next_cook_date": None, "settings": settings,
                "is_cook_day": (_dow(cook_date) in settings["cook_days"]) if settings["cook_days"] else None,
                "next_planned_cook_day": next_planned.isoformat() if next_planned else None,
                "subrecipes": []}

    subs = {s["id"]: s for s in _fetch_in(
        "subrecipe",
        "id, name, instructions, prep_type, skill, equipment, fridge_days, freezer_days, is_side_salad, active_minutes, total_minutes, prep_ahead",
        "id", sub_ids,
    )}
    sub_ingreds = defaultdict(list)
    for si in _fetch_in("subrec_ingred", "subrecipe_id, ingredient_id, quantity", "subrecipe_id", sub_ids):
        sub_ingreds[si["subrecipe_id"]].append(si)
    ingredient_map = {i["id"]: i for i in _fetch_in(
        "ingredient", "id, name, unit, serving_per_unit, is_staple, stored_frozen, thaw_hours",
        "id", {si["ingredient_id"] for rows in sub_ingreds.values() for si in rows},
    )}

    # ---------------------------------------------------------------
    # 3. Names + comments for the clients in this window
    # ---------------------------------------------------------------
    user_ids = list({d["user_id"] for d in delivery_by_mpd.values() if d.get("user_id")})
    users = {u["id"]: u for u in _fetch_in("user", "id, name, last_name", "id", user_ids)}

    def _name(uid):
        u = users.get(uid) or {}
        return f"{(u.get('name') or '').strip()} {(u.get('last_name') or '').strip()}".strip() or "Unknown"

    recipe_ids = {r["recipe_id"] for r in mpdrs if r.get("recipe_id")}
    recipes = {r["id"]: r for r in _fetch_in("recipe", "id, name", "id", recipe_ids)}
    recipe_comments = {}
    for p in _fetch_in("user_recipe_preferences", "recipe_id, user_id, comment, updated_at", "user_id", user_ids,
                       extra=lambda q: q.not_.is_("comment", None)):
        if (p.get("comment") or "").strip() and p.get("recipe_id") in recipe_ids:
            key = (p["recipe_id"], p["user_id"])
            if key not in recipe_comments or (p.get("updated_at") or "") > (recipe_comments[key].get("updated_at") or ""):
                recipe_comments[key] = p
    handling_by_key = {}
    for h in _fetch_in("kitchen_comment_handling",
                       "id, user_id, recipe_id, subrecipe_id, comment, handling, instruction, decided_by",
                       "user_id", user_ids):
        handling_by_key[(h["user_id"], h.get("recipe_id") or 0, h["subrecipe_id"], (h["comment"] or "").strip())] = h

    salad_comments = {}
    salad_ids = [sid for sid, s in subs.items() if s.get("is_side_salad")]
    if salad_ids:
        for p in _fetch_in("user_side_salad_preference", "subrecipe_id, user_id, comment", "user_id", user_ids,
                           extra=lambda q: q.not_.is_("comment", None)):
            if (p.get("comment") or "").strip() and p["subrecipe_id"] in salad_ids:
                salad_comments[(p["subrecipe_id"], p["user_id"])] = p["comment"]

    # ---------------------------------------------------------------
    # 4. Serving lines, by subrecipe, in delivery order
    # ---------------------------------------------------------------
    lines_by_sub = defaultdict(list)
    for s in servings:
        mpdr = mpdr_map[s["meal_plan_day_recipe_id"]]
        deliv = delivery_by_mpd.get(mpdr["meal_plan_day_id"])
        if not deliv:
            continue
        lines_by_sub[s["subrecipe_id"]].append({
            "serving_id": s["id"],
            "mpdr_id": mpdr["id"],
            "recipe_id": mpdr.get("recipe_id"),
            "user_id": deliv.get("user_id"),
            "date": _d(deliv["delivery_date"]),
            "servings": s["recipe_subrecipe_serving_calculated"],
            "covered": bool(s.get("batch_id")) or s.get("portioning_status") == "done",
            "batch_id": s.get("batch_id"),
        })
    for lines in lines_by_sub.values():
        lines.sort(key=lambda x: (x["date"], x["serving_id"]))

    batches_by_sub = defaultdict(list)
    for b in batches:
        batches_by_sub[b["subrecipe_id"]].append(b)

    # ---------------------------------------------------------------
    # 5. Per subrecipe: allocate open lines to stock, then build the cook plan
    # ---------------------------------------------------------------
    out = []
    for sub_id, sub in subs.items():
        lines = lines_by_sub.get(sub_id, [])
        sub_batches = batches_by_sub.get(sub_id, [])
        if not sub_batches:
            # No stock to consume: past deliveries were only read for batches.
            lines = [ln for ln in lines if ln["date"] >= cook_date]
        if not lines and not sub_batches:
            continue

        stock = []
        for b in sub_batches:
            use_by = _batch_use_by(b, sub)
            remaining = (b["servings_cooked"] or 0) - (b.get("discarded_servings") or 0) - linked_by_batch.get(b["id"], 0)
            stock.append({
                "batch_id": b["id"], "cooked_on": b["cooked_on"], "storage": b["storage"],
                "use_by": use_by.isoformat(), "expired": use_by < cook_date,
                "servings_cooked": b["servings_cooked"], "remaining": round(remaining, 2),
                "variant_note": b.get("variant_note"), "notes": b.get("notes"),
                "_left": max(remaining, 0),
                "_use_by": use_by, "_cooked_on": _d(b["cooked_on"]),
                "allocated_servings": 0.0, "allocated_today_servings": 0.0,
                "allocated_mpdr_ids": set(), "allocated_today_mpdr_ids": set(),
                "_by_date": defaultdict(lambda: {"servings": 0.0, "mpdr_ids": set()}),
            })
        stock.sort(key=lambda x: x["_use_by"])

        open_lines = []
        past_done = False
        for ln in lines:
            if not past_done and ln["date"] >= cook_date:
                # Everything delivered before cook_date has been served:
                # what's left now is what's physically in the fridge/freezer.
                for st in stock:
                    st["remaining"] = round(st["_left"], 2)
                past_done = True
            if ln["covered"]:
                continue
            batch = next((st for st in stock
                          if st["_cooked_on"] <= ln["date"] <= st["_use_by"]
                          and st["_left"] >= ln["servings"] - 0.01), None)
            if batch:
                batch["_left"] -= ln["servings"]
                if ln["date"] >= cook_date:
                    batch["allocated_servings"] += ln["servings"]
                    batch["allocated_mpdr_ids"].add(ln["mpdr_id"])
                    day = batch["_by_date"][ln["date"].isoformat()]
                    day["servings"] += ln["servings"]
                    day["mpdr_ids"].add(ln["mpdr_id"])
                    if ln["date"] == cook_date:
                        batch["allocated_today_servings"] += ln["servings"]
                        batch["allocated_today_mpdr_ids"].add(ln["mpdr_id"])
            elif ln["date"] >= cook_date:
                open_lines.append(ln)
        if not past_done:
            for st in stock:
                st["remaining"] = round(st["_left"], 2)

        fridge_end = min(cook_date + timedelta(days=sub.get("fridge_days") or 0), window_end)
        freezer_days = sub.get("freezer_days")
        freeze_end = min(horizon_end, window_end, cook_date + timedelta(days=freezer_days)) if freezer_days else None

        today_lines = [ln for ln in lines if ln["date"] == cook_date]
        must = [ln for ln in open_lines if ln["date"] == cook_date]
        suggested = [ln for ln in open_lines if ln["date"] <= fridge_end]
        freeze = [ln for ln in open_lines if freeze_end and fridge_end < ln["date"] <= freeze_end]

        def _sum(ls):
            return round(sum(ln["servings"] for ln in ls), 2)

        def _by_date(ls):
            acc = defaultdict(float)
            ids = defaultdict(set)
            for ln in ls:
                acc[ln["date"].isoformat()] += ln["servings"]
                ids[ln["date"].isoformat()].add(ln["mpdr_id"])
            return [{"date": k, "servings": round(v, 2), "mpdr_ids": sorted(ids[k])} for k, v in sorted(acc.items())]

        comment_lines = today_lines + suggested
        comments, seen = [], {}
        for ln in comment_lines:
            uid = ln["user_id"]
            is_salad = bool(sub.get("is_side_salad"))
            text = salad_comments.get((sub_id, uid)) if is_salad else (
                (recipe_comments.get((ln["recipe_id"], uid)) or {}).get("comment"))
            if not text:
                continue
            text = text.strip()
            rid = None if is_salad else ln["recipe_id"]
            key = (uid, rid or 0, text)
            if key not in seen:
                h = handling_by_key.get((uid, rid or 0, sub_id, text)) or {}
                entry = {"user_id": uid, "name": _name(uid), "comment": text,
                         "recipe_id": rid, "recipe_name": (recipes.get(rid) or {}).get("name") if rid else None,
                         "date": ln["date"].isoformat(), "servings": ln["servings"],
                         "handling": h.get("handling"), "instruction": h.get("instruction"),
                         "decided_by": h.get("decided_by"),
                         # this client's open servings, for a separate pot
                         "servings_must": 0.0, "servings_suggested": 0.0}
                seen[key] = entry
                comments.append(entry)
        for ln in open_lines:
            for entry in comments:
                if entry["user_id"] == ln["user_id"] and entry["recipe_id"] in (None, ln["recipe_id"]):
                    if ln["date"] == cook_date:
                        entry["servings_must"] += ln["servings"]
                    if ln["date"] <= fridge_end:
                        entry["servings_suggested"] += ln["servings"]
        for entry in comments:
            entry["servings_must"] = round(entry["servings_must"], 2)
            entry["servings_suggested"] = round(entry["servings_suggested"], 2)

        def _buffer(ls):
            open_s = sum(ln["servings"] for ln in ls if ln["date"] >= open_from)
            return round(open_s * buffer_pct / 100, 2) if buffer_pct else 0.0

        for st in stock:
            st["allocated_servings"] = round(st["allocated_servings"], 2)
            st["allocated_today_servings"] = round(st["allocated_today_servings"], 2)
            st["allocated_mpdr_ids"] = sorted(st["allocated_mpdr_ids"])
            st["allocated_today_mpdr_ids"] = sorted(st["allocated_today_mpdr_ids"])
            # Per delivery date, for "portion several days" on Kitchen Day.
            st["allocated_by_date"] = [{"date": k, "servings": round(v["servings"], 2), "mpdr_ids": sorted(v["mpdr_ids"])}
                                       for k, v in sorted(st.pop("_by_date").items())]
            st["unallocated"] = round(st.pop("_left"), 2)  # left over after covering the next days
            st.pop("_use_by")
            st.pop("_cooked_on")

        sugg_buffer, freeze_buffer = _buffer(suggested), _buffer(freeze)
        must_s = _sum(must)
        sugg_s = round(_sum(suggested) + sugg_buffer, 2)
        freeze_s = round(_sum(freeze) + freeze_buffer, 2)
        out.append({
            "subrecipe_id": sub_id,
            "name": sub["name"],
            "instructions": sub.get("instructions"),
            "prep_type": sub.get("prep_type") or "cook",
            "is_side_salad": bool(sub.get("is_side_salad")),
            "skill": sub.get("skill") or "chef",
            "equipment": sub.get("equipment"),
            "fridge_days": sub.get("fridge_days"),
            "freezer_days": freezer_days,
            "active_minutes": sub.get("active_minutes"),
            "total_minutes": sub.get("total_minutes"),
            "prep_ahead": sub.get("prep_ahead"),
            "recipes": sorted({recipes[ln["recipe_id"]]["name"] for ln in today_lines + suggested
                               if ln["recipe_id"] in recipes}),
            "today": {
                "servings": _sum(today_lines),
                "covered": _sum([ln for ln in today_lines if ln not in must]),
                "mpdr_ids": sorted({ln["mpdr_id"] for ln in today_lines}),
            },
            "stock": stock,
            "must_cook": {"servings": must_s, "mpdr_ids": sorted({ln["mpdr_id"] for ln in must}),
                          "ingredients": _scaled_ingredients(sub_ingreds[sub_id], ingredient_map, must_s)},
            "suggested": {"servings": sugg_s, "buffer": sugg_buffer, "through": fridge_end.isoformat(), "by_date": _by_date(suggested),
                          "mpdr_ids": sorted({ln["mpdr_id"] for ln in suggested}),
                          "ingredients": _scaled_ingredients(sub_ingreds[sub_id], ingredient_map, sugg_s)},
            "freeze": ({"servings": freeze_s, "buffer": freeze_buffer, "through": freeze_end.isoformat(), "by_date": _by_date(freeze),
                        "mpdr_ids": sorted({ln["mpdr_id"] for ln in freeze}),
                        "ingredients": _scaled_ingredients(sub_ingreds[sub_id], ingredient_map, freeze_s)}
                       if freeze else None),
            "comments": comments,
            # For a hand-typed batch size: multiply by any number of servings.
            "ingredients_per_serving": _scaled_ingredients(sub_ingreds[sub_id], ingredient_map, 1, digits=4),
        })

    # Next date with live deliveries after cook_date (Kitchen Day > Tomorrow).
    live_mpd_set = set(live_mpd_ids)
    next_dates = sorted({d["delivery_date"][:10] for mid, d in delivery_by_mpd.items()
                         if mid in live_mpd_set and _d(d["delivery_date"]) > cook_date})

    prep_rank = {"cook": 0, "assemble": 1, "slice": 2, "ready": 3}
    out.sort(key=lambda x: (prep_rank.get(x["prep_type"], 9), -x["must_cook"]["servings"], x["name"].lower()))
    return {"cook_date": cook_date.isoformat(), "horizon_end": horizon_end.isoformat(),
            "next_cook_date": next_dates[0] if next_dates else None,
            "settings": settings,
            "is_cook_day": (_dow(cook_date) in settings["cook_days"]) if settings["cook_days"] else None,
            "next_planned_cook_day": next_planned.isoformat() if next_planned else None,
            "subrecipes": out}


def get_kitchen_simple(cook_date_str):
    """Kitchen (simple screen): ordered servings per subrecipe for the 7 days
    from cook_date, plus what the cook needs for cook_date itself — the
    meal_plan_day_recipe ids to portion, ingredients per serving, recipe
    instructions and client comments. No stock or batches: the cook types
    what they have (kitchen_plan_item in Supabase)."""
    start = _d(cook_date_str)
    end = start + timedelta(days=HORIZON_DAYS - 1)
    dates = [(start + timedelta(days=i)).isoformat() for i in range(HORIZON_DAYS)]

    deliveries = fetch_all(lambda lo, hi: (
        supabase.table("deliveries")
        .select("id, meal_plan_day_id, user_id, delivery_date")
        .gte("delivery_date", start.isoformat())
        .lte("delivery_date", end.isoformat())
        .order("id")
        .range(lo, hi)
    ))
    delivery_by_mpd = {}
    for d in deliveries:
        mid = d.get("meal_plan_day_id")
        if mid and (mid not in delivery_by_mpd or d["delivery_date"] < delivery_by_mpd[mid]["delivery_date"]):
            delivery_by_mpd[mid] = d
    mpds = _fetch_in("meal_plan_day", "id, status", "id", delivery_by_mpd.keys())
    live = [m["id"] for m in mpds if m.get("status") not in ("cancellation_pending", "cancelled")]
    mpdrs = {r["id"]: r for r in _fetch_in("meal_plan_day_recipe", "id, meal_plan_day_id, recipe_id", "meal_plan_day_id", live)}
    servings = _fetch_in("meal_plan_day_recipe_serving",
                         "id, meal_plan_day_recipe_id, subrecipe_id, recipe_subrecipe_serving_calculated",
                         "meal_plan_day_recipe_id", mpdrs.keys())

    today = start.isoformat()
    by_sub = defaultdict(lambda: defaultdict(float))
    # Clients and meal boxes per day: servings alone don't say how many
    # people eat it (a side salad of 0.5 is still one box).
    people_by_sub = defaultdict(lambda: defaultdict(set))
    boxes_by_sub = defaultdict(lambda: defaultdict(set))
    today_mpdr = defaultdict(set)
    # (subrecipe, user, recipe) delivered today: who to show comments for.
    today_lines = defaultdict(set)
    for sv in servings:
        n = sv.get("recipe_subrecipe_serving_calculated") or 0
        sid = sv.get("subrecipe_id")
        if not sid or n <= 0:
            continue
        mpdr = mpdrs[sv["meal_plan_day_recipe_id"]]
        deliv = delivery_by_mpd.get(mpdr["meal_plan_day_id"])
        if not deliv:
            continue
        day = deliv["delivery_date"][:10]
        by_sub[sid][day] += n
        people_by_sub[sid][day].add(deliv.get("user_id"))
        boxes_by_sub[sid][day].add(mpdr["id"])
        if day == today:
            today_mpdr[sid].add(mpdr["id"])
            today_lines[sid].add((deliv.get("user_id"), mpdr.get("recipe_id")))

    sub_ids = list(by_sub.keys())
    subs = {x["id"]: x for x in _fetch_in(
        "subrecipe", "id, name, instructions, prep_type, is_side_salad", "id", sub_ids)}
    sub_ingreds = defaultdict(list)
    for si in _fetch_in("subrec_ingred", "id, subrecipe_id, ingredient_id, quantity", "subrecipe_id", sub_ids):
        sub_ingreds[si["subrecipe_id"]].append(si)
    ingredient_map = {i["id"]: i for i in _fetch_in(
        "ingredient", "id, name, unit, serving_per_unit, is_staple, stored_frozen, thaw_hours",
        "id", {si["ingredient_id"] for rows in sub_ingreds.values() for si in rows},
    )}

    # Comments, only for clients delivered today.
    user_ids = list({u for lines in today_lines.values() for u, _ in lines if u})
    users = {u["id"]: u for u in _fetch_in("user", "id, name, last_name", "id", user_ids)}

    def _name(uid):
        u = users.get(uid) or {}
        return f"{(u.get('name') or '').strip()} {(u.get('last_name') or '').strip()}".strip() or "Unknown"

    recipe_ids = {r for lines in today_lines.values() for _, r in lines if r}
    recipes = {r["id"]: r for r in _fetch_in("recipe", "id, name", "id", recipe_ids)}
    recipe_comments = {}
    for p in _fetch_in("user_recipe_preferences", "id, recipe_id, user_id, comment, updated_at", "user_id", user_ids,
                       extra=lambda q: q.not_.is_("comment", None)):
        if (p.get("comment") or "").strip() and p.get("recipe_id") in recipe_ids:
            key = (p["recipe_id"], p["user_id"])
            if key not in recipe_comments or (p.get("updated_at") or "") > (recipe_comments[key].get("updated_at") or ""):
                recipe_comments[key] = p
    salad_comments = {}
    for p in _fetch_in("user_side_salad_preference", "id, subrecipe_id, user_id, comment", "user_id", user_ids,
                       extra=lambda q: q.not_.is_("comment", None)):
        if (p.get("comment") or "").strip():
            salad_comments[(p["subrecipe_id"], p["user_id"])] = p["comment"].strip()
    handling = {}
    for h in _fetch_in("kitchen_comment_handling", "id, user_id, recipe_id, subrecipe_id, comment, instruction",
                       "user_id", user_ids):
        handling[(h["user_id"], h.get("recipe_id") or 0, h["subrecipe_id"], (h["comment"] or "").strip())] = h.get("instruction")

    rows = []
    for sid, per_day in by_sub.items():
        sub = subs.get(sid)
        if not sub:
            continue
        is_salad = bool(sub.get("is_side_salad"))
        comments, seen = [], set()
        for uid, rid in sorted(today_lines.get(sid, ()), key=lambda x: (str(x[0]), x[1] or 0)):
            text = salad_comments.get((sid, uid)) if is_salad else (recipe_comments.get((rid, uid)) or {}).get("comment")
            if not text:
                continue
            text = text.strip()
            key = (uid, None if is_salad else rid, text)
            if key in seen:
                continue
            seen.add(key)
            comments.append({
                "user_id": uid, "name": _name(uid), "comment": text,
                "recipe_id": None if is_salad else rid,
                "recipe_name": None if is_salad else (recipes.get(rid) or {}).get("name"),
                "instruction": handling.get((uid, 0 if is_salad else (rid or 0), sid, text)),
            })
        rows.append({
            "subrecipe_id": sid,
            "name": sub["name"],
            "prep_type": sub.get("prep_type") or "cook",
            "is_side_salad": is_salad,
            "instructions": sub.get("instructions"),
            "by_date": {d: round(per_day.get(d, 0), 2) for d in dates},
            "people_by_date": {d: len(people_by_sub[sid].get(d, ())) for d in dates},
            "boxes_by_date": {d: len(boxes_by_sub[sid].get(d, ())) for d in dates},
            "today_mpdr_ids": sorted(today_mpdr.get(sid, ())),
            "comments": comments,
            "ingredients_per_serving": _scaled_ingredients(sub_ingreds[sid], ingredient_map, 1, digits=4),
        })
    prep_rank = {"cook": 0, "assemble": 1, "slice": 2, "ready": 3}
    rows.sort(key=lambda r: (prep_rank.get(r["prep_type"], 9), -r["by_date"][today], r["name"].lower()))
    return {"cook_date": today, "dates": dates, "subrecipes": rows}


def get_kitchen_week(start_str):
    """Ordered servings per subrecipe per delivery date for 7 days from
    start — the raw demand the Week view and the assistant plan cook days
    from (stock is per day, see get_kitchen_day)."""
    start = _d(start_str)
    end = start + timedelta(days=6)
    deliveries = fetch_all(lambda lo, hi: (
        supabase.table("deliveries")
        .select("id, meal_plan_day_id, delivery_date")
        .gte("delivery_date", start.isoformat())
        .lte("delivery_date", end.isoformat())
        .order("id")
        .range(lo, hi)
    ))
    date_by_mpd = {}
    for d in deliveries:
        mid = d.get("meal_plan_day_id")
        if mid and (mid not in date_by_mpd or d["delivery_date"] < date_by_mpd[mid]):
            date_by_mpd[mid] = d["delivery_date"][:10]
    mpds = _fetch_in("meal_plan_day", "id, status", "id", date_by_mpd.keys())
    live = [m["id"] for m in mpds if m.get("status") not in ("cancellation_pending", "cancelled")]
    mpdrs = {r["id"]: r for r in _fetch_in("meal_plan_day_recipe", "id, meal_plan_day_id", "meal_plan_day_id", live)}
    servings = _fetch_in("meal_plan_day_recipe_serving",
                         "meal_plan_day_recipe_id, subrecipe_id, recipe_subrecipe_serving_calculated",
                         "meal_plan_day_recipe_id", mpdrs.keys())

    by_sub = defaultdict(lambda: defaultdict(float))
    for sv in servings:
        n = sv.get("recipe_subrecipe_serving_calculated") or 0
        if not sv.get("subrecipe_id") or n <= 0:
            continue
        day = date_by_mpd.get(mpdrs[sv["meal_plan_day_recipe_id"]]["meal_plan_day_id"])
        if day:
            by_sub[sv["subrecipe_id"]][day] += n

    subs = {x["id"]: x for x in _fetch_in(
        "subrecipe", "id, name, prep_type, skill, equipment, fridge_days, freezer_days, active_minutes, total_minutes",
        "id", by_sub.keys())}
    dates = [(start + timedelta(days=i)).isoformat() for i in range(7)]
    rows = []
    for sid, per_day in by_sub.items():
        sub = subs.get(sid)
        if not sub:
            continue
        rows.append({
            "subrecipe_id": sid, "name": sub["name"], "prep_type": sub.get("prep_type") or "cook",
            "skill": sub.get("skill") or "chef", "equipment": sub.get("equipment"),
            "fridge_days": sub.get("fridge_days"), "freezer_days": sub.get("freezer_days"),
            "active_minutes": sub.get("active_minutes"), "total_minutes": sub.get("total_minutes"),
            "by_date": {d: round(per_day.get(d, 0), 2) for d in dates},
            "total": round(sum(per_day.values()), 2),
        })
    prep_rank = {"cook": 0, "assemble": 1, "slice": 2, "ready": 3}
    rows.sort(key=lambda r: (prep_rank.get(r["prep_type"], 9), -r["total"], r["name"].lower()))
    return {"start": start.isoformat(), "end": end.isoformat(), "dates": dates,
            "open_from": beirut_iso_date(2), "settings": get_kitchen_settings(), "subrecipes": rows}
