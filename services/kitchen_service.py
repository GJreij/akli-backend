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
"""

from collections import defaultdict
from datetime import date, timedelta

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


def _batch_use_by(batch, sub):
    if batch.get("use_by"):
        return _d(batch["use_by"])
    days = sub.get("freezer_days") if batch["storage"] == "freezer" else sub.get("fridge_days")
    return _d(batch["cooked_on"]) + timedelta(days=days or 0)


def _scaled_ingredients(sub_ingreds, ingredient_map, servings):
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
            "quantity": round(qty, 1),
            "is_staple": bool(ing_def.get("is_staple")),
            "stored_frozen": bool(ing_def.get("stored_frozen")),
        })
    return sorted(out, key=lambda x: x["name"].lower())


def get_kitchen_day(cook_date_str):
    cook_date = _d(cook_date_str)
    horizon_end = cook_date + timedelta(days=HORIZON_DAYS)

    batches = fetch_all(lambda lo, hi: (
        supabase.table("kitchen_batch").select("*").eq("status", "active").order("id").range(lo, hi)
    ))

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
        return {"cook_date": cook_date.isoformat(), "horizon_end": horizon_end.isoformat(), "subrecipes": []}

    subs = {s["id"]: s for s in _fetch_in(
        "subrecipe",
        "id, name, instructions, prep_type, skill, equipment, fridge_days, freezer_days, is_side_salad, active_minutes, total_minutes",
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
                "allocated_servings": 0.0, "allocated_mpdr_ids": set(), "allocated_today_mpdr_ids": set(),
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
                    if ln["date"] == cook_date:
                        batch["allocated_today_mpdr_ids"].add(ln["mpdr_id"])
            elif ln["date"] >= cook_date:
                open_lines.append(ln)
        if not past_done:
            for st in stock:
                st["remaining"] = round(st["_left"], 2)

        fridge_end = cook_date + timedelta(days=sub.get("fridge_days") or 0)
        freezer_days = sub.get("freezer_days")
        freeze_end = min(horizon_end, cook_date + timedelta(days=freezer_days)) if freezer_days else None

        today_lines = [ln for ln in lines if ln["date"] == cook_date]
        must = [ln for ln in open_lines if ln["date"] == cook_date]
        suggested = [ln for ln in open_lines if ln["date"] <= fridge_end]
        freeze = [ln for ln in open_lines if freeze_end and fridge_end < ln["date"] <= freeze_end]

        def _sum(ls):
            return round(sum(ln["servings"] for ln in ls), 2)

        def _by_date(ls):
            acc = defaultdict(float)
            for ln in ls:
                acc[ln["date"].isoformat()] += ln["servings"]
            return [{"date": k, "servings": round(v, 2)} for k, v in sorted(acc.items())]

        comment_lines = today_lines + suggested
        comments, seen = [], set()
        for ln in comment_lines:
            uid = ln["user_id"]
            text = salad_comments.get((sub_id, uid)) if sub.get("is_side_salad") else (
                (recipe_comments.get((ln["recipe_id"], uid)) or {}).get("comment"))
            if text and (uid, text) not in seen:
                seen.add((uid, text))
                comments.append({"user_id": uid, "name": _name(uid), "comment": text.strip(),
                                 "date": ln["date"].isoformat(), "servings": ln["servings"]})

        for st in stock:
            st["allocated_servings"] = round(st["allocated_servings"], 2)
            st["allocated_mpdr_ids"] = sorted(st["allocated_mpdr_ids"])
            st["allocated_today_mpdr_ids"] = sorted(st["allocated_today_mpdr_ids"])
            st["unallocated"] = round(st.pop("_left"), 2)  # left over after covering the next days
            st.pop("_use_by")
            st.pop("_cooked_on")

        must_s, sugg_s, freeze_s = _sum(must), _sum(suggested), _sum(freeze)
        out.append({
            "subrecipe_id": sub_id,
            "name": sub["name"],
            "instructions": sub.get("instructions"),
            "prep_type": sub.get("prep_type") or "cook",
            "skill": sub.get("skill") or "chef",
            "equipment": sub.get("equipment"),
            "fridge_days": sub.get("fridge_days"),
            "freezer_days": freezer_days,
            "active_minutes": sub.get("active_minutes"),
            "total_minutes": sub.get("total_minutes"),
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
            "suggested": {"servings": sugg_s, "through": fridge_end.isoformat(), "by_date": _by_date(suggested),
                          "mpdr_ids": sorted({ln["mpdr_id"] for ln in suggested}),
                          "ingredients": _scaled_ingredients(sub_ingreds[sub_id], ingredient_map, sugg_s)},
            "freeze": ({"servings": freeze_s, "through": freeze_end.isoformat(), "by_date": _by_date(freeze),
                        "mpdr_ids": sorted({ln["mpdr_id"] for ln in freeze}),
                        "ingredients": _scaled_ingredients(sub_ingreds[sub_id], ingredient_map, freeze_s)}
                       if freeze else None),
            "comments": comments,
        })

    prep_rank = {"cook": 0, "assemble": 1, "ready": 2}
    out.sort(key=lambda x: (prep_rank.get(x["prep_type"], 9), -x["must_cook"]["servings"], x["name"].lower()))
    return {"cook_date": cook_date.isoformat(), "horizon_end": horizon_end.isoformat(), "subrecipes": out}
