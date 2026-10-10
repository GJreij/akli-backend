# services/weekly_menu_service.py

from utils.supabase_client import supabase
from datetime import datetime


class WeeklyMenuService:
    def __init__(self):
        self.sb = supabase

    def get_available_recipe_ids_for_date(self, date_str: str, tenant_id=None):
        """
        Returns recipe_ids available for a given date.
        Date filtering is done ONLY on weekly_menu.
        """

        # 1) Validate date
        try:
            datetime.strptime(date_str, "%Y-%m-%d")
        except ValueError:
            return {
                "error": "Invalid date format. Use YYYY-MM-DD.",
                "date_received": date_str
            }, 400

        # 2) Fetch weekly_menu ids matching the date
        q = (
            self.sb.table("weekly_menu")
            .select("id")
            .lte("week_start_date", date_str)
            .gte("week_end_date", date_str)
        )

        if tenant_id is not None:
            q = q.eq("tenant_id", tenant_id)

        menu_res = q.execute()
        menus = menu_res.data or []

        if not menus:
            return {
                "date": date_str,
                "tenant_id": tenant_id,
                "recipe_ids": [],
                "count": 0,
                "message": "No weekly menu found for this date."
            }, 200

        weekly_menu_ids = [m["id"] for m in menus if m.get("id")]

        # 3) Fetch recipes linked to those menus
        recipe_res = (
            self.sb.table("weekly_menu_recipe")
            .select("recipe_id")
            .in_("weekly_menu_id", weekly_menu_ids)
            .execute()
        )

        rows = recipe_res.data or []

        recipe_ids = sorted({
            r["recipe_id"]
            for r in rows
            if r.get("recipe_id") is not None
        })

        return {
            "date": date_str,
            "tenant_id": tenant_id,
            "weekly_menu_ids": weekly_menu_ids,
            "recipe_ids": recipe_ids,
            "count": len(recipe_ids),
        }, 200

    def menu_recipe_ids_for_date(self, date_str: str, tenant_id=None):
        """
        Server-side menu guard for post-checkout swaps/edits. Returns the set
        of recipe_ids on the weekly menu covering `date_str`, or None when no
        weekly menu is configured for that date (no restriction — same
        semantics as the frontend's empty-list fallback).
        """
        result, status_code = self.get_available_recipe_ids_for_date(date_str, tenant_id)
        if status_code != 200:
            raise ValueError(result.get("error") or "Could not load the menu for this date.")
        if not result.get("weekly_menu_ids"):
            return None
        return set(result.get("recipe_ids") or [])
