# services/day_edit_service.py
#
# "Edit day" — full-day replan, post-checkout. Separate from meal swap: an
# edit can exclude/re-add/replace MULTIPLE meals in one operation, and unlike
# swap it's BLOCKING — one full-quality solve, no price ceiling, no culinary
# relaxation fallback. If the wallet can't cover a price increase, the whole
# operation hard-stops with a clear top-up amount.

import logging
from datetime import datetime, timezone
from utils.supabase_client import supabase

logger = logging.getLogger(__name__)
from utils.dates import beirut_iso_date
from services.mealplan_service import optimize_subrecipes
from services.pricing_service import fetch_latest_prices
from services.day_pricing_helpers import day_price_and_macros, wallet_balance as get_wallet_balance
from services.weekly_menu_service import WeeklyMenuService

weekly_menu_service = WeeklyMenuService()

NON_EDITABLE_DAY_STATUSES = ("cancellation_pending", "cancelled")
MEAL_TYPE_FLAG = {
    "breakfast": "could_be_breakfast",
    "lunch":     "could_be_lunch",
    "dinner":    "could_be_dinner",
    "snack":     "could_be_snack",
}
# Mirrors the pre-checkout reduction table in mealplan_update_dynamic_service
# (not imported from there — that version is entangled with an in-memory
# plan blob, weekly carry-over, and an include_macros_in_rest option that
# has no post-checkout equivalent here).
REDUCE_PCT = {"breakfast": 0.30, "snack": 0.20, "lunch": 0.40, "dinner": 0.40}
VALID_ACTIONS = ("delete", "replace", "add")


class DayEditService:
    def __init__(self):
        self.sb = supabase

    # ---------- shared setup for preview + confirm ----------

    def _load_context(self, user_id, meal_plan_day_id, changes):
        """
        Ownership + eligibility + the day's current state, re-derived fresh
        every call — same money-relevant discipline as MealSwapService.
        `changes`: [{action: "delete"|"replace"|"add", meal_plan_day_recipe_id?,
                     new_recipe_id?, meal_type?}]
        """
        day_res = (
            self.sb.table("meal_plan_day")
            .select("id, meal_plan_id, date, status, daily_macro_order_id, meal_plan(user_id)")
            .eq("id", meal_plan_day_id)
            .maybe_single()
            .execute()
        )
        day = day_res.data if day_res else None
        if not day:
            return ({"error": "Order day not found."}, 404), None

        owner_id = (day.get("meal_plan") or {}).get("user_id")
        if not owner_id or str(owner_id) != str(user_id):
            return ({"error": "This order does not belong to this user."}, 403), None

        if day["status"] in NON_EDITABLE_DAY_STATUSES:
            return ({"error": "This day has been cancelled and can't be edited."}, 409), None

        # Same two-full-days cutoff as Swap.
        cutoff = beirut_iso_date(3)
        if day["date"] < cutoff:
            return ({
                "error": "Too late to edit — this delivery is too soon.",
                "reason": "too_late",
            }, 400), None

        if not changes:
            return ({"error": "No changes given."}, 400), None

        recipes_res = (
            self.sb.table("meal_plan_day_recipe")
            .select("id, recipe_id, meal_type, is_swapped, original_recipe_id")
            .eq("meal_plan_day_id", meal_plan_day_id)
            .execute()
        )
        day_recipes = recipes_res.data or []
        if not day_recipes:
            return ({"error": "No meals found for this day."}, 404), None
        by_id = {r["id"]: r for r in day_recipes}

        new_recipe_ids = set()
        for change in changes:
            action = change.get("action")
            if action not in VALID_ACTIONS:
                return ({"error": f"Unknown action: {action}"}, 400), None
            if action in ("delete", "replace"):
                mpdr_id = change.get("meal_plan_day_recipe_id")
                if mpdr_id not in by_id:
                    return ({"error": "That meal was not found on this order day."}, 404), None
            if action in ("replace", "add"):
                if not change.get("new_recipe_id"):
                    return ({"error": "Missing new_recipe_id."}, 400), None
                new_recipe_ids.add(change["new_recipe_id"])
            if action == "add" and not change.get("meal_type"):
                return ({"error": "Missing meal_type for an added meal."}, 400), None

        existing_recipe_ids = {r["recipe_id"] for r in day_recipes}
        recipes_res2 = (
            self.sb.table("recipe")
            .select("id, name, could_be_breakfast, could_be_lunch, could_be_dinner, could_be_snack")
            .in_("id", list(existing_recipe_ids | new_recipe_ids))
            .execute()
        )
        recipe_by_id = {r["id"]: r for r in (recipes_res2.data or [])}

        # Same server-side menu guard as Swap. Recipes the day was originally
        # ordered with stay allowed (e.g. re-adding an excluded meal).
        try:
            menu_ids = weekly_menu_service.menu_recipe_ids_for_date(day["date"])
        except Exception:
            logger.exception("Menu lookup failed for day edit on %s", day["date"])
            return ({"error": "Could not check the menu for this date. Please try again."}, 503), None
        originally_ordered = {r.get("original_recipe_id") or r["recipe_id"] for r in day_recipes}

        for change in changes:
            if change.get("action") in ("replace", "add"):
                meal_type = change.get("meal_type") or by_id.get(change.get("meal_plan_day_recipe_id"), {}).get("meal_type")
                recipe = recipe_by_id.get(change["new_recipe_id"])
                if not recipe:
                    return ({"error": "Selected recipe not found."}, 404), None
                if (menu_ids is not None and recipe["id"] not in menu_ids
                        and recipe["id"] not in originally_ordered):
                    return ({
                        "error": f"\"{recipe['name']}\" isn't on the menu for this date.",
                        "reason": "not_on_menu",
                    }, 400), None
                flag = MEAL_TYPE_FLAG.get(meal_type)
                if flag and not recipe.get(flag):
                    return ({"error": f"\"{recipe['name']}\" isn't offered for {meal_type}."}, 400), None

        # The STABLE 100% reference for the eating-out reduction math is the
        # client's own standing diet goal (daily_macro_target) — never
        # daily_macro_order, which gets overwritten with the day's actual
        # SOLVED totals after every edit (see confirm() below). Reading the
        # reduction base from daily_macro_order would mean re-adding a
        # previously-excluded meal computes its "no reduction" case against
        # an already-reduced number, permanently compounding the cut instead
        # of restoring the original goal — confirmed as a real bug in testing.
        goal_row = (
            self.sb.table("daily_macro_target")
            .select("kcal_target, protein_g, carbs_g, fat_g")
            .eq("user_id", owner_id)
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        )
        goal_data = (goal_row.data or [{}])[0] if goal_row else {}
        stable_target = {
            "protein_g": float(goal_data.get("protein_g") or 0),
            "carbs_g":   float(goal_data.get("carbs_g") or 0),
            "fat_g":     float(goal_data.get("fat_g") or 0),
            "kcal":      float(goal_data.get("kcal_target") or 0),
        }
        if stable_target["kcal"] <= 0:
            # No standing goal on file (shouldn't normally happen) — fall
            # back to the day's own currently-ordered target rather than
            # producing a target of zero.
            target_row = (
                self.sb.table("daily_macro_order")
                .select("protein_ordered, carbs_ordered, fat_ordered, kcal_ordered")
                .eq("id", day["daily_macro_order_id"])
                .maybe_single()
                .execute()
            )
            target_data = target_row.data if target_row else None
            stable_target = {
                "protein_g": float((target_data or {}).get("protein_ordered") or 0),
                "carbs_g":   float((target_data or {}).get("carbs_ordered") or 0),
                "fat_g":     float((target_data or {}).get("fat_ordered") or 0),
                "kcal":      float((target_data or {}).get("kcal_ordered") or 0),
            }

        serving_rows_res = (
            self.sb.table("meal_plan_day_recipe_serving")
            .select(
                "id, meal_plan_day_recipe_id, subrecipe_id, "
                "recipe_subrecipe_serving_calculated, kcal_calculated, "
                "protein_calculated, carbs_calculated, fat_calculated"
            )
            .in_("meal_plan_day_recipe_id", [r["id"] for r in day_recipes])
            .execute()
        )
        serving_rows = serving_rows_res.data or []

        return None, {
            "day": day,
            "day_recipes": day_recipes,
            "by_id": by_id,
            "recipe_by_id": recipe_by_id,
            "stable_target": stable_target,
            "serving_rows": serving_rows,
        }

    # ---------- change application ----------

    def _apply_changes(self, day_recipes, changes):
        """
        Returns (recipes_by_meal, eating_out_set) — recipes_by_meal keyed by
        a stable meal_key ("<mpdr_id>" for existing rows, "new_<i>" for
        added ones), reflecting `changes` applied on top of `day_recipes`.
        eating_out_set is recomputed fresh from what the day ALREADY had —
        a meal type the day never included (e.g. a 3-meals-a-day plan with
        no snack slot at all) must never count as "excluded"; only a meal
        type that was present before this operation and is now gone does.
        """
        deleted_ids = {c["meal_plan_day_recipe_id"] for c in changes if c["action"] == "delete"}
        replace_map = {c["meal_plan_day_recipe_id"]: c["new_recipe_id"] for c in changes if c["action"] == "replace"}

        original_meal_types = {r["meal_type"] for r in day_recipes}

        recipes_by_meal = {}
        for r in day_recipes:
            if r["id"] in deleted_ids:
                continue
            recipes_by_meal[str(r["id"])] = {
                "recipe_id": replace_map.get(r["id"], r["recipe_id"]),
                "meal_type": r["meal_type"],
            }

        for i, c in enumerate(changes):
            if c["action"] == "add":
                recipes_by_meal[f"new_{i}"] = {"recipe_id": c["new_recipe_id"], "meal_type": c["meal_type"]}

        final_meal_types = {m["meal_type"] for m in recipes_by_meal.values()}
        eating_out_set = original_meal_types - final_meal_types
        return recipes_by_meal, eating_out_set

    def _build_summary(self, changes, by_id, recipe_by_id):
        """Plain-language summary of a day edit for the client-facing
        Activity log — e.g. "Excluded snack, replaced lunch with Chicken
        Caesar Salad, added dinner (Beef Stir Fry)"."""
        parts = []
        for c in changes:
            action = c.get("action")
            if action == "delete":
                meal_type = (by_id.get(c.get("meal_plan_day_recipe_id")) or {}).get("meal_type", "meal")
                parts.append(f"Excluded {meal_type}")
            elif action == "replace":
                meal_type = (by_id.get(c.get("meal_plan_day_recipe_id")) or {}).get("meal_type", "meal")
                new_name = (recipe_by_id.get(c.get("new_recipe_id")) or {}).get("name", "a new recipe")
                parts.append(f"Replaced {meal_type} with {new_name}")
            elif action == "add":
                meal_type = c.get("meal_type", "meal")
                new_name = (recipe_by_id.get(c.get("new_recipe_id")) or {}).get("name", "a new recipe")
                parts.append(f"Added {meal_type} ({new_name})")
        return ", ".join(parts) if parts else "Day edited"

    def _adjusted_target(self, base_target, eating_out_set):
        reduce_pct = min(sum(REDUCE_PCT.get(mt, 0) for mt in eating_out_set), 1.0)
        if reduce_pct <= 0:
            return dict(base_target)
        return {
            "protein_g": round(base_target["protein_g"] * (1 - reduce_pct), 2),
            "carbs_g":   round(base_target["carbs_g"] * (1 - reduce_pct), 2),
            "fat_g":     round(base_target["fat_g"] * (1 - reduce_pct), 2),
            "kcal":      round(base_target["kcal"] * (1 - reduce_pct), 2),
        }

    # ---------- preview / confirm ----------

    def _preview_or_confirm(self, user_id, meal_plan_day_id, changes):
        """Single, blocking solve — no lock, no price ceiling, no fallback
        tier. If the wallet can't cover the price increase, this hard-stops
        rather than offering a compromise (unlike Swap)."""
        err, ctx = self._load_context(user_id, meal_plan_day_id, changes)
        if err:
            return err[0], err[1], None

        prices = fetch_latest_prices()
        before_price, before_day_macros = day_price_and_macros(
            ctx["serving_rows"], ctx["day_recipes"], ctx["stable_target"], prices
        )

        recipes_by_meal, eating_out_set = self._apply_changes(ctx["day_recipes"], changes)
        if not recipes_by_meal:
            return {"error": "A day can't have every meal removed."}, 400, None

        adjusted_target = self._adjusted_target(ctx["stable_target"], eating_out_set)

        optimized_subs, _loss, day_totals = optimize_subrecipes(
            recipes_by_meal,
            adjusted_target,
            allow_under_kcal=len(eating_out_set) > 0,
            prices=prices,
            flat_fee=prices.get("edit_fee_price", 0.0),
        )
        if day_totals.get("tolerance_used") == "SAFE_FALLBACK" or day_totals.get("price") is None:
            return {"error": "Couldn't find a valid configuration for this edit."}, 409, None

        wallet_balance = get_wallet_balance(self.sb, user_id)
        price_delta = round(day_totals["price"] - before_price, 2)
        wallet_after = round(wallet_balance - price_delta, 2)

        eligible = price_delta <= wallet_balance
        required_topup = None if eligible else round(price_delta - wallet_balance, 2)

        subs_by_meal = {}
        for s in optimized_subs:
            subs_by_meal.setdefault(s["meal_name"], []).append(s)

        after_meals = []
        for meal_key, info in recipes_by_meal.items():
            rows = subs_by_meal.get(meal_key, [])
            macros = {
                "protein": sum(r["macros"]["protein"] for r in rows),
                "carbs":   sum(r["macros"]["carbs"] for r in rows),
                "fat":     sum(r["macros"]["fat"] for r in rows),
                "kcal":    sum(r["macros"]["kcal"] for r in rows),
            }
            recipe = ctx["recipe_by_id"].get(info["recipe_id"], {})
            after_meals.append({
                "meal_plan_day_recipe_id": int(meal_key) if meal_key.isdigit() else None,
                "meal_type": info["meal_type"],
                "recipe_id": info["recipe_id"],
                "recipe_name": recipe.get("name"),
                "macros": macros,
            })

        response = {
            "eligible": eligible,
            "reason": None if eligible else "insufficient_wallet",
            "required_topup": required_topup,
            # The client's standing diet goal — shown alongside before/after
            # so "vs goal" stays visible regardless of any exclusions active
            # on this specific day.
            "goal": {
                "protein": round(ctx["stable_target"]["protein_g"]),
                "carbs":   round(ctx["stable_target"]["carbs_g"]),
                "fat":     round(ctx["stable_target"]["fat_g"]),
                "kcal":    round(ctx["stable_target"]["kcal"]),
            },
            "before": {"day_totals": {
                "protein": round(before_day_macros["protein"]),
                "carbs":   round(before_day_macros["carbs"]),
                "fat":     round(before_day_macros["fat"]),
                "kcal":    round(before_day_macros["kcal"]),
                "price":   before_price,
            }},
            "after": {"day_totals": day_totals, "meals": after_meals},
            "price_delta": price_delta,
            "wallet": {
                "balance_before": wallet_balance,
                "delta": -price_delta,
                "balance_after": wallet_after,
                "sufficient": eligible,
            },
            "edit_id": None,
        }
        return response, 200, {
            **ctx,
            "changes": changes,
            "prices": prices,
            "before_price": before_price,
            "recipes_by_meal": recipes_by_meal,
            "optimized_subs": optimized_subs,
            "day_totals": day_totals,
            "price_delta": price_delta,
            "eligible": eligible,
        }

    # ---------- CLIENT-FACING: preview ----------

    def preview(self, user_id, meal_plan_day_id, changes):
        result, status_code, _ctx = self._preview_or_confirm(user_id, meal_plan_day_id, changes)
        return result, status_code

    # ---------- CLIENT-FACING: confirm ----------

    def confirm(self, user_id, meal_plan_day_id, changes):
        result, status_code, ctx = self._preview_or_confirm(user_id, meal_plan_day_id, changes)
        if status_code != 200:
            return result, status_code

        # Blocking design: no compromise tier — refuse outright rather than
        # ever writing a change the client can't actually afford.
        if not result.get("eligible"):
            return {
                "error": "This edit isn't affordable — add funds to your wallet to proceed.",
                "reason": result.get("reason"),
                "required_topup": result.get("required_topup"),
            }, 409

        price_delta = ctx["price_delta"]
        now = datetime.now(timezone.utc).isoformat()

        if price_delta != 0:
            try:
                self.sb.rpc("apply_meal_swap_wallet_delta", {
                    "p_user_id": user_id,
                    "p_price_delta": price_delta,
                    "p_related_order_id": ctx["day"]["meal_plan_id"],
                    "p_note": f"Day edit on {ctx['day']['date']}",
                    "p_type": "day_edit",
                }).execute()
            except Exception as e:
                return {
                    "error": f"Could not apply wallet adjustment: {e}",
                }, 409

        # Everything from here on writes the actual day change — the wallet
        # has already been settled above. If any of these throws (network
        # blip, DB error), the client must not end up charged/credited for
        # an edit that never actually landed: reverse the wallet leg before
        # returning, rather than leaving money moved with no corresponding
        # meal change.
        try:
            deleted_ids = {c["meal_plan_day_recipe_id"] for c in changes if c["action"] == "delete"}
            replace_map = {c["meal_plan_day_recipe_id"]: c["new_recipe_id"] for c in changes if c["action"] == "replace"}

            if deleted_ids:
                self.sb.table("meal_plan_day_recipe_serving").delete().in_(
                    "meal_plan_day_recipe_id", list(deleted_ids)
                ).execute()
                self.sb.table("meal_plan_day_recipe").delete().in_("id", list(deleted_ids)).execute()

            for mpdr_id, new_recipe_id in replace_map.items():
                existing = ctx["by_id"][mpdr_id]
                original_recipe_id = existing.get("original_recipe_id") or existing["recipe_id"]
                self.sb.table("meal_plan_day_recipe").update({
                    "recipe_id": new_recipe_id,
                    "is_swapped": True,
                    "original_recipe_id": original_recipe_id,
                    "updated_at": now,
                }).eq("id", mpdr_id).execute()

            new_id_by_meal_key = {}
            for i, c in enumerate(changes):
                if c["action"] == "add":
                    ins = self.sb.table("meal_plan_day_recipe").insert({
                        "meal_plan_day_id": meal_plan_day_id,
                        "recipe_id": c["new_recipe_id"],
                        "meal_type": c["meal_type"],
                        "cooking_status": "pending",
                        "packaging_status": "pending",
                        "created_at": now,
                    }).execute()
                    new_id_by_meal_key[f"new_{i}"] = ins.data[0]["id"]

            subs_by_meal = {}
            for s in ctx["optimized_subs"]:
                subs_by_meal.setdefault(s["meal_name"], []).append(s)

            for meal_key in ctx["recipes_by_meal"].keys():
                mpdr_id = new_id_by_meal_key.get(meal_key, int(meal_key) if meal_key.isdigit() else None)
                if mpdr_id is None:
                    continue
                new_servings = subs_by_meal.get(meal_key, [])
                self.sb.table("meal_plan_day_recipe_serving").delete().eq("meal_plan_day_recipe_id", mpdr_id).execute()
                for s in new_servings:
                    macros = s["macros"]
                    self.sb.table("meal_plan_day_recipe_serving").insert({
                        "meal_plan_day_recipe_id": mpdr_id,
                        "subrecipe_id": s["subrecipe_id"],
                        "recipe_subrecipe_serving_calculated": s["servings"],
                        "kcal_calculated": macros.get("kcal"),
                        "protein_calculated": macros.get("protein"),
                        "carbs_calculated": macros.get("carbs"),
                        "fat_calculated": macros.get("fat"),
                        "cooking_status": "pending",
                        "portioning_status": "pending",
                        "created_at": now,
                    }).execute()

            # Keep daily_macro_order in sync with the day's new solved state —
            # a correctness prerequisite for Swap, which reads this same row as
            # its own target on any later swap for this day.
            day_totals = ctx["day_totals"]
            self.sb.table("daily_macro_order").update({
                "protein_ordered": day_totals["protein"],
                "carbs_ordered": day_totals["carbs"],
                "fat_ordered": day_totals["fat"],
                "kcal_ordered": day_totals["kcal"],
            }).eq("id", ctx["day"]["daily_macro_order_id"]).execute()

            log_res = self.sb.table("day_edit_log").insert({
                "meal_plan_day_id": meal_plan_day_id,
                "meal_plan_id": ctx["day"]["meal_plan_id"],
                "user_id": user_id,
                "changes": changes,
                "price_delta": price_delta,
                "summary": self._build_summary(changes, ctx["by_id"], ctx["recipe_by_id"]),
                "created_at": now,
            }).execute()
        except Exception as e:
            return self._fail_after_wallet_settled(user_id, ctx, price_delta, e)

        result["edit_id"] = log_res.data[0]["id"] if log_res.data else None
        return result, 200

    def _fail_after_wallet_settled(self, user_id, ctx, price_delta, error):
        """The wallet leg of an edit already landed but writing the actual
        day change then failed — reverse the wallet delta so the client is
        never left charged/credited for an edit that didn't happen. If the
        reversal itself fails, this is a real money/state mismatch that
        needs a human, so say so plainly instead of quietly losing it."""
        if price_delta == 0:
            logger.error("Day edit failed after wallet no-op for user %s: %s", user_id, error)
            return {"error": f"Could not complete this edit: {error}"}, 500
        try:
            self.sb.rpc("apply_meal_swap_wallet_delta", {
                "p_user_id": user_id,
                "p_price_delta": -price_delta,
                "p_related_order_id": ctx["day"]["meal_plan_id"],
                "p_note": f"Reversal: day edit on {ctx['day']['date']} failed after wallet settlement ({error})",
                "p_type": "day_edit",
            }).execute()
        except Exception as reversal_error:
            logger.critical(
                "Day edit wallet reversal FAILED for user %s, meal_plan_id %s, price_delta %s: "
                "original error=%s, reversal error=%s",
                user_id, ctx["day"]["meal_plan_id"], price_delta, error, reversal_error,
            )
            return {
                "error": (
                    "Something went wrong confirming this edit, and we could not automatically undo the "
                    "wallet charge. Please contact us on WhatsApp right away so we can fix your balance."
                ),
                "critical": True,
            }, 500
        logger.error("Day edit failed after wallet settlement for user %s, reversed cleanly: %s", user_id, error)
        return {
            "error": "Something went wrong confirming this edit — your wallet was not charged. Please try again.",
        }, 500
