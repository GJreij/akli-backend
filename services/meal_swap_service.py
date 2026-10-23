# services/meal_swap_service.py

import logging
from datetime import datetime, timezone
from utils.supabase_client import supabase

logger = logging.getLogger(__name__)
from utils.dates import beirut_iso_date
from services.mealplan_service import (
    optimize_subrecipes_with_swap,
    get_recipe_subrecipes,
    SWAP_MAX_MOVEMENT_PCT,
)
from services.pricing_service import fetch_latest_prices, compute_macro_cost, compute_packaging_cost, single_meal_fee
from services.day_pricing_helpers import (
    day_price_and_macros, sum_macros, wallet_balance as get_wallet_balance,
    paid_share, charged_delta, list_price_budget,
)
from services.weekly_menu_service import WeeklyMenuService

weekly_menu_service = WeeklyMenuService()

NON_SWAPPABLE_DAY_STATUSES = ("cancellation_pending", "cancelled")
MEAL_TYPE_FLAG = {
    "breakfast": "could_be_breakfast",
    "lunch":     "could_be_lunch",
    "dinner":    "could_be_dinner",
    "snack":     "could_be_snack",
}
MODES = ("meal_only", "rebalance_day")


class MealSwapService:
    def __init__(self):
        self.sb = supabase

    # ---------- shared setup for preview + confirm ----------

    def _load_context(self, user_id, meal_plan_day_id, meal_plan_day_recipe_id, new_recipe_id):
        """
        Ownership + eligibility + the day's current state, re-derived fresh
        every call (never cached/trusted from a prior preview — this is a
        money-relevant operation). Returns (error_response, context);
        error_response is None on success.
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

        if day["status"] in NON_SWAPPABLE_DAY_STATUSES:
            return ({"error": "This day has been cancelled and can't be modified."}, 409), None

        # Two full days' notice: today Wednesday -> earliest modifiable
        # delivery is Saturday (today+3), Thursday/Friday are blocked.
        cutoff = beirut_iso_date(3)
        if day["date"] < cutoff:
            return ({
                "error": "Too late to modify — this delivery is too soon.",
                "reason": "too_late",
            }, 400), None

        recipes_res = (
            self.sb.table("meal_plan_day_recipe")
            .select("id, recipe_id, meal_type, is_swapped, original_recipe_id")
            .eq("meal_plan_day_id", meal_plan_day_id)
            .execute()
        )
        day_recipes = recipes_res.data or []
        if not day_recipes:
            return ({"error": "No meals found for this day."}, 404), None

        target = next((r for r in day_recipes if r["id"] == meal_plan_day_recipe_id), None)
        if not target:
            return ({"error": "That meal was not found on this order day."}, 404), None

        new_recipe_res = (
            self.sb.table("recipe")
            .select("id, name, could_be_breakfast, could_be_lunch, could_be_dinner, could_be_snack")
            .eq("id", new_recipe_id)
            .maybe_single()
            .execute()
        )
        new_recipe = new_recipe_res.data if new_recipe_res else None
        if not new_recipe:
            return ({"error": "Selected recipe not found."}, 404), None

        # Never trust the client's picker for menu availability — it shows an
        # unfiltered list while the menu is still loading. Swapping back to
        # the meal's originally-ordered recipe stays allowed.
        try:
            menu_ids = weekly_menu_service.menu_recipe_ids_for_date(day["date"])
        except Exception:
            logger.exception("Menu lookup failed for swap on %s", day["date"])
            return ({"error": "Could not check the menu for this date. Please try again."}, 503), None
        original_id = target.get("original_recipe_id") or target["recipe_id"]
        if menu_ids is not None and new_recipe_id not in menu_ids and new_recipe_id != original_id:
            return ({
                "error": f"\"{new_recipe['name']}\" isn't on the menu for this date.",
                "reason": "not_on_menu",
            }, 400), None

        flag = MEAL_TYPE_FLAG.get(target["meal_type"])
        if flag and not new_recipe.get(flag):
            return ({
                "error": f"\"{new_recipe['name']}\" isn't offered for {target['meal_type']}.",
            }, 400), None

        # Names for every meal on the day (not just old/new) — needed to show
        # the client what happens to the OTHER meals under "rebalance the day".
        recipes_res2 = (
            self.sb.table("recipe")
            .select("id, name")
            .in_("id", list({r["recipe_id"] for r in day_recipes}))
            .execute()
        )
        recipe_by_id = {r["id"]: r for r in (recipes_res2.data or [])}
        old_recipe = recipe_by_id.get(target["recipe_id"]) or {"id": target["recipe_id"], "name": None}

        target_row = (
            self.sb.table("daily_macro_order")
            .select("protein_ordered, carbs_ordered, fat_ordered, kcal_ordered")
            .eq("id", day["daily_macro_order_id"])
            .maybe_single()
            .execute()
        )
        target_data = target_row.data if target_row else None
        macro_target = {
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
            "target": target,
            "new_recipe": new_recipe,
            "old_recipe": old_recipe,
            "recipe_by_id": recipe_by_id,
            "macro_target": macro_target,
            "serving_rows": serving_rows,
        }

    def _day_price_and_macros(self, serving_rows, meal_plan_day_recipe_id, day_recipes, macro_target, prices):
        """Day-level price/macros via the shared helper, plus this swap's
        target meal's own macros in isolation (swap-specific)."""
        day_price, day_macros = day_price_and_macros(serving_rows, day_recipes, macro_target, prices)
        meal_macros = sum_macros([r for r in serving_rows if r["meal_plan_day_recipe_id"] == meal_plan_day_recipe_id])
        return day_price, day_macros, meal_macros

    def _other_meals_breakdown(self, ctx, meal_plan_day_recipe_id, after_subs_by_meal=None):
        """Before/after macros for every meal OTHER than the one being
        swapped — `after_subs_by_meal` is the rebalance solve's grouped
        output, or None for "just this meal" (unchanged by definition)."""
        result = []
        for r in ctx["day_recipes"]:
            if r["id"] == meal_plan_day_recipe_id:
                continue
            before_macros = sum_macros([s for s in ctx["serving_rows"] if s["meal_plan_day_recipe_id"] == r["id"]])
            if after_subs_by_meal is not None:
                rows = after_subs_by_meal.get(str(r["id"]), [])
                after_macros = {
                    "protein": sum(x["macros"]["protein"] for x in rows),
                    "carbs":   sum(x["macros"]["carbs"] for x in rows),
                    "fat":     sum(x["macros"]["fat"] for x in rows),
                    "kcal":    sum(x["macros"]["kcal"] for x in rows),
                }
            else:
                after_macros = before_macros
            result.append({
                "meal_plan_day_recipe_id": r["id"],
                "meal_type": r["meal_type"],
                "recipe_name": (ctx["recipe_by_id"].get(r["recipe_id"]) or {}).get("name"),
                "before_macros": before_macros,
                "after_macros": after_macros,
            })
        return result

    def _run_swap_solve(self, day_recipes, meal_plan_day_recipe_id, new_recipe_id, macro_target, prices, price_ceiling, reference_servings=None):
        recipes_by_meal = {
            str(r["id"]): {
                "recipe_id": new_recipe_id if r["id"] == meal_plan_day_recipe_id else r["recipe_id"],
                "meal_type": r["meal_type"],
            }
            for r in day_recipes
        }
        optimized_subs, _loss, day_totals = optimize_subrecipes_with_swap(
            recipes_by_meal,
            macro_target,
            locked_meal_key=str(meal_plan_day_recipe_id),
            locked_recipe_id=new_recipe_id,
            prices=prices,
            price_ceiling=price_ceiling,
            flat_fee=prices.get("swap_fee_price", 0.0),
            reference_servings=reference_servings,
            max_movement_pct=SWAP_MAX_MOVEMENT_PCT if reference_servings else None,
        )
        # SAFE_FALLBACK doesn't know about locked_meal_key/price_ceiling/the
        # movement bound at all (see mealplan_service.optimize_subrecipes) —
        # reaching it here means no valid solution was found, not that one
        # was found. Never accept its output for a swap.
        if day_totals.get("tolerance_used") == "SAFE_FALLBACK" or day_totals.get("price") is None:
            return None, None
        return optimized_subs, day_totals

    def _meal_snapshot(self, optimized_subs, meal_plan_day_recipe_id):
        rows = [s for s in optimized_subs if s["meal_name"] == str(meal_plan_day_recipe_id)]
        macros = {
            "protein": sum(r["macros"]["protein"] for r in rows),
            "carbs":   sum(r["macros"]["carbs"] for r in rows),
            "fat":     sum(r["macros"]["fat"] for r in rows),
            "kcal":    sum(r["macros"]["kcal"] for r in rows),
        }
        return rows, macros

    # ---------- Option 1: swap just this meal, touch nothing else ----------

    def _meal_only_option(self, ctx, meal_plan_day_recipe_id, new_recipe_id, prices,
                           before_price, before_day_macros, before_meal_macros, wallet_balance):
        """Pure arithmetic, no solver: every other meal keeps its exact
        current servings, so this is always computable and never infeasible."""
        locked_subs = get_recipe_subrecipes(new_recipe_id)
        after_meal_macros = {
            "protein": sum(s["macros"]["protein"] for s in locked_subs),
            "carbs":   sum(s["macros"]["carbs"] for s in locked_subs),
            "fat":     sum(s["macros"]["fat"] for s in locked_subs),
            "kcal":    sum(s["macros"]["kcal"] for s in locked_subs),
        }
        after_day_macros = {
            k: before_day_macros[k] - before_meal_macros[k] + after_meal_macros[k]
            for k in ("protein", "carbs", "fat", "kcal")
        }

        old_subrecipe_count = len([s for s in ctx["serving_rows"] if s["meal_plan_day_recipe_id"] == meal_plan_day_recipe_id])
        total_subrecipe_count = len(ctx["serving_rows"]) - old_subrecipe_count + len(locked_subs)

        kcal_t = ctx["macro_target"].get("kcal") or after_day_macros["kcal"]
        macro_cost = compute_macro_cost(
            protein_g=after_day_macros["protein"], carbs_g=after_day_macros["carbs"], fat_g=after_day_macros["fat"],
            kcal=kcal_t, prices=prices, apply_kcal_discount=True,
        )
        packaging_cost = compute_packaging_cost(
            meals_count=len(ctx["day_recipes"]), subrecipes_count=total_subrecipe_count, prices=prices,
        )
        # A swap never changes the meal count, so the single-meal fee is on
        # both sides (before_price via day_price_and_macros) and nets out —
        # but it must be here too, or a one-meal day's swap credits $fee.
        after_price = round(
            prices["day_packaging_price"] + macro_cost["macro_cost_after_discount"] + packaging_cost
            + single_meal_fee(len(ctx["day_recipes"]), prices)
            + prices.get("swap_fee_price", 0.0), 2
        )

        # List-price difference scaled to what the client actually paid for
        # this day (see day_pricing_helpers.paid_share).
        price_delta = charged_delta(after_price - before_price, ctx["paid_share"])
        eligible = price_delta <= wallet_balance
        required_topup = None if eligible else round(price_delta - wallet_balance, 2)
        wallet_after = round(wallet_balance - price_delta, 2)

        response_option = {
            "eligible": eligible,
            "reason": None if eligible else "insufficient_wallet",
            "required_topup": required_topup,
            "after": {
                "meal": {"recipe_id": new_recipe_id, "name": ctx["new_recipe"].get("name"), "macros": after_meal_macros},
                "day_totals": {
                    "protein": round(after_day_macros["protein"]),
                    "carbs":   round(after_day_macros["carbs"]),
                    "fat":     round(after_day_macros["fat"]),
                    "kcal":    round(after_day_macros["kcal"]),
                    "price":   after_price,
                },
                "other_meals": self._other_meals_breakdown(ctx, meal_plan_day_recipe_id, after_subs_by_meal=None),
            },
            "price_delta": price_delta,
            "wallet": {
                "balance_before": wallet_balance, "delta": -price_delta,
                "balance_after": wallet_after, "sufficient": eligible,
            },
        }
        confirm_data = {"mode": "meal_only", "locked_subs": locked_subs, "price_delta": price_delta, "eligible": eligible}
        return response_option, confirm_data

    # ---------- Option 2: rebalance the day, other meals bounded ----------

    def _rebalance_option(self, ctx, meal_plan_day_recipe_id, new_recipe_id, prices, before_price, wallet_balance):
        reference_servings = {
            (str(r["meal_plan_day_recipe_id"]), r["subrecipe_id"]): r["recipe_subrecipe_serving_calculated"] or 0
            for r in ctx["serving_rows"]
        }

        floor_subs, floor_totals = self._run_swap_solve(
            ctx["day_recipes"], meal_plan_day_recipe_id, new_recipe_id, ctx["macro_target"], prices,
            price_ceiling=None, reference_servings=reference_servings,
        )
        if floor_subs is None:
            # Even the best-fit, movement-bounded rebalance found nothing —
            # this option isn't available at all for this swap (rare).
            return None, None

        floor_delta = charged_delta(floor_totals["price"] - before_price, ctx["paid_share"])
        eligible = True
        reason = None
        required_topup = None

        if floor_delta <= wallet_balance:
            optimized_subs, day_totals = floor_subs, floor_totals
        else:
            # The LP caps LIST price, so convert the wallet into the list-price
            # increase it covers at this day's paid share.
            price_ceiling = round(before_price + list_price_budget(wallet_balance, ctx["paid_share"]), 2)
            capped_subs, capped_totals = self._run_swap_solve(
                ctx["day_recipes"], meal_plan_day_recipe_id, new_recipe_id, ctx["macro_target"], prices,
                price_ceiling, reference_servings=reference_servings,
            )
            if capped_subs is not None:
                optimized_subs, day_totals = capped_subs, capped_totals
            else:
                optimized_subs, day_totals = floor_subs, floor_totals
                eligible = False
                reason = "insufficient_wallet"
                required_topup = round(floor_delta - wallet_balance, 2)

        price_delta = charged_delta(day_totals["price"] - before_price, ctx["paid_share"])
        wallet_after = round(wallet_balance - price_delta, 2)
        _after_rows, after_meal_macros = self._meal_snapshot(optimized_subs, meal_plan_day_recipe_id)

        subs_by_meal = {}
        for s in optimized_subs:
            subs_by_meal.setdefault(s["meal_name"], []).append(s)

        response_option = {
            "eligible": eligible,
            "reason": reason,
            "required_topup": required_topup,
            "after": {
                "meal": {"recipe_id": new_recipe_id, "name": ctx["new_recipe"].get("name"), "macros": after_meal_macros},
                "day_totals": day_totals,
                "other_meals": self._other_meals_breakdown(ctx, meal_plan_day_recipe_id, after_subs_by_meal=subs_by_meal),
            },
            "price_delta": price_delta,
            "wallet": {
                "balance_before": wallet_balance, "delta": -price_delta,
                "balance_after": wallet_after, "sufficient": eligible,
            },
        }
        confirm_data = {"mode": "rebalance_day", "optimized_subs": optimized_subs, "price_delta": price_delta, "eligible": eligible}
        return response_option, confirm_data

    # ---------- shared pipeline ----------

    def _preview_or_confirm(self, user_id, meal_plan_day_id, meal_plan_day_recipe_id, new_recipe_id):
        """Computes BOTH options fresh every call — never cached/trusted
        from a prior preview, since this settles real money."""
        err, ctx = self._load_context(user_id, meal_plan_day_id, meal_plan_day_recipe_id, new_recipe_id)
        if err:
            return err[0], err[1], None

        prices = fetch_latest_prices()
        before_price, before_day_macros, before_meal_macros = self._day_price_and_macros(
            ctx["serving_rows"], meal_plan_day_recipe_id, ctx["day_recipes"], ctx["macro_target"], prices
        )
        wallet_balance = get_wallet_balance(self.sb, user_id)
        ctx["paid_share"] = paid_share(self.sb, ctx["day"]["id"])

        meal_only_response, meal_only_confirm = self._meal_only_option(
            ctx, meal_plan_day_recipe_id, new_recipe_id, prices,
            before_price, before_day_macros, before_meal_macros, wallet_balance,
        )
        rebalance_response, rebalance_confirm = self._rebalance_option(
            ctx, meal_plan_day_recipe_id, new_recipe_id, prices, before_price, wallet_balance,
        )

        response = {
            "before": {
                "meal": {
                    "recipe_id": ctx["old_recipe"]["id"],
                    "name": ctx["old_recipe"].get("name"),
                    "macros": before_meal_macros,
                },
                "day_totals": {
                    "protein": round(before_day_macros["protein"]),
                    "carbs":   round(before_day_macros["carbs"]),
                    "fat":     round(before_day_macros["fat"]),
                    "kcal":    round(before_day_macros["kcal"]),
                    "price":   before_price,
                },
            },
            "options": {
                "meal_only": meal_only_response,
                "rebalance_day": rebalance_response,
            },
            "swap_id": None,
        }
        return response, 200, {
            **ctx,
            "prices": prices,
            "before_price": before_price,
            "confirm_data": {"meal_only": meal_only_confirm, "rebalance_day": rebalance_confirm},
        }

    # ---------- CLIENT-FACING: preview ----------

    def preview(self, user_id, meal_plan_day_id, meal_plan_day_recipe_id, new_recipe_id):
        result, status_code, _solve_ctx = self._preview_or_confirm(
            user_id, meal_plan_day_id, meal_plan_day_recipe_id, new_recipe_id
        )
        return result, status_code

    # ---------- CLIENT-FACING: confirm ----------

    def confirm(self, user_id, meal_plan_day_id, meal_plan_day_recipe_id, new_recipe_id, mode):
        if mode not in MODES:
            return {"error": f"Unknown mode: {mode}"}, 400

        result, status_code, solve_ctx = self._preview_or_confirm(
            user_id, meal_plan_day_id, meal_plan_day_recipe_id, new_recipe_id
        )
        if status_code != 200:
            return result, status_code

        chosen = solve_ctx["confirm_data"].get(mode)
        if chosen is None:
            return {"error": "That option isn't available for this swap."}, 409

        # Defense-in-depth: a blocked option is still HTTP 200 from preview()
        # so the UI can render it, but it must never actually be confirmed —
        # even if the client's UI has stale state from an earlier preview.
        if not chosen["eligible"]:
            return {
                "error": "This option isn't affordable — add funds to your wallet to proceed.",
                "reason": "insufficient_wallet",
                "required_topup": result["options"][mode]["required_topup"],
            }, 409

        ctx = solve_ctx
        price_delta = chosen["price_delta"]
        now = datetime.now(timezone.utc).isoformat()

        if price_delta != 0:
            try:
                self.sb.rpc("apply_meal_swap_wallet_delta", {
                    "p_user_id": user_id,
                    "p_price_delta": price_delta,
                    "p_related_order_id": ctx["day"]["meal_plan_id"],
                    "p_note": f"Meal swap on {ctx['day']['date']}: {ctx['old_recipe'].get('name')} -> {ctx['new_recipe'].get('name')} ({mode})",
                }).execute()
            except Exception as e:
                return {
                    "error": f"Could not apply wallet adjustment: {e}",
                    "suggestion": "cancel_and_replan",
                }, 409

        # Everything from here on writes the actual meal/serving change — the
        # wallet has already been settled above. If any of these throws
        # (network blip, DB error), the client must not end up charged/
        # credited for a swap that never actually landed: reverse the wallet
        # leg before returning, rather than leaving money moved with no
        # corresponding meal change.
        try:
            target = ctx["target"]
            original_recipe_id = target.get("original_recipe_id") or target["recipe_id"]
            self.sb.table("meal_plan_day_recipe").update({
                "recipe_id": new_recipe_id,
                "is_swapped": True,
                "original_recipe_id": original_recipe_id,
                "updated_at": now,
            }).eq("id", meal_plan_day_recipe_id).execute()

            if mode == "meal_only":
                # Only the swapped meal's servings change — every other meal on
                # the day is left completely untouched.
                self.sb.table("meal_plan_day_recipe_serving").delete().eq(
                    "meal_plan_day_recipe_id", meal_plan_day_recipe_id
                ).execute()
                for s in chosen["locked_subs"]:
                    macros = s["macros"]
                    self.sb.table("meal_plan_day_recipe_serving").insert({
                        "meal_plan_day_recipe_id": meal_plan_day_recipe_id,
                        "subrecipe_id": s["id"],
                        "recipe_subrecipe_serving_calculated": 1.0,
                        "kcal_calculated": macros.get("kcal"),
                        "protein_calculated": macros.get("protein"),
                        "carbs_calculated": macros.get("carbs"),
                        "fat_calculated": macros.get("fat"),
                        "cooking_status": "pending",
                        "portioning_status": "pending",
                        "created_at": now,
                    }).execute()
            else:  # rebalance_day
                subs_by_meal = {}
                for s in chosen["optimized_subs"]:
                    subs_by_meal.setdefault(s["meal_name"], []).append(s)

                for day_recipe in ctx["day_recipes"]:
                    mpdr_id = day_recipe["id"]
                    new_servings = subs_by_meal.get(str(mpdr_id), [])
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

            log_res = self.sb.table("meal_swap_log").insert({
                "meal_plan_day_recipe_id": meal_plan_day_recipe_id,
                "meal_plan_id": ctx["day"]["meal_plan_id"],
                "user_id": user_id,
                "old_recipe_id": ctx["old_recipe"]["id"],
                "new_recipe_id": new_recipe_id,
                "price_delta": price_delta,
                "summary": f"Swapped {ctx['old_recipe'].get('name')} for {ctx['new_recipe'].get('name')}",
                "created_at": now,
            }).execute()
        except Exception as e:
            return self._fail_after_wallet_settled(user_id, ctx, price_delta, mode, e)

        result["swap_id"] = log_res.data[0]["id"] if log_res.data else None
        result["confirmed_mode"] = mode
        return result, 200

    def _fail_after_wallet_settled(self, user_id, ctx, price_delta, mode, error):
        """The wallet leg of a swap already landed but writing the actual
        meal/serving change then failed — reverse the wallet delta so the
        client is never left charged/credited for a swap that didn't
        happen. If the reversal itself fails, this is a real money/state
        mismatch that needs a human, so say so plainly instead of quietly
        losing it."""
        if price_delta == 0:
            logger.error("Meal swap failed after wallet no-op for user %s: %s", user_id, error)
            return {"error": f"Could not complete the swap: {error}"}, 500
        try:
            self.sb.rpc("apply_meal_swap_wallet_delta", {
                "p_user_id": user_id,
                "p_price_delta": -price_delta,
                "p_related_order_id": ctx["day"]["meal_plan_id"],
                "p_note": f"Reversal: meal swap on {ctx['day']['date']} ({mode}) failed after wallet settlement ({error})",
            }).execute()
        except Exception as reversal_error:
            logger.critical(
                "Meal swap wallet reversal FAILED for user %s, meal_plan_id %s, price_delta %s: "
                "original error=%s, reversal error=%s",
                user_id, ctx["day"]["meal_plan_id"], price_delta, error, reversal_error,
            )
            return {
                "error": (
                    "Something went wrong confirming this swap, and we could not automatically undo the "
                    "wallet charge. Please contact us on WhatsApp right away so we can fix your balance."
                ),
                "critical": True,
            }, 500
        logger.error("Meal swap failed after wallet settlement for user %s, reversed cleanly: %s", user_id, error)
        return {
            "error": "Something went wrong confirming this swap — your wallet was not charged. Please try again.",
        }, 500
