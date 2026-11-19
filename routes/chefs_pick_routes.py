# routes/chefs_pick_routes.py
"""
"Chef's pick" = the most-ordered dish of a day so far, per meal group
(breakfast / lunch+dinner / snack), from confirmed, non-cancelled orders.
Shown as a badge on the Menu tab and in the swap / day-edit sheets so
clients lean toward what the kitchen already cooks in volume.

Only recipe ids and counts are returned — never who ordered.
"""

from datetime import datetime, timedelta

from flask import Blueprint, jsonify, request

from services.daily_menu_service import chefs_picks_by_date

chefs_pick_bp = Blueprint("chefs_pick", __name__)

MAX_RANGE_DAYS = 31


@chefs_pick_bp.route("/chefs_picks", methods=["POST"])
def chefs_picks():
    """
    JSON body: { "start_date": "YYYY-MM-DD", "end_date": "YYYY-MM-DD" }
    (end_date optional, defaults to start_date)

    Returns: { "picks": { "YYYY-MM-DD": { "breakfast": 12, "main": 34, "snack": 56 } } }
    A group is missing when no dish there has reached the minimum orders yet.
    """
    payload = request.get_json(silent=True) or {}
    try:
        start = datetime.strptime(payload.get("start_date") or "", "%Y-%m-%d").date()
        end = datetime.strptime(payload.get("end_date") or payload.get("start_date"), "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return jsonify({"error": "start_date (and optional end_date) must be YYYY-MM-DD"}), 400
    if end < start or (end - start).days > MAX_RANGE_DAYS:
        return jsonify({"error": f"Range must be 0-{MAX_RANGE_DAYS} days"}), 400

    dates = [start + timedelta(days=i) for i in range((end - start).days + 1)]
    picks = chefs_picks_by_date(dates)
    return jsonify({"picks": {str(d): groups for d, groups in picks.items()}}), 200
