# routes/portioning.py

from flask import Blueprint, request, jsonify
from services.portioning_service import (
    normalize_filter_value,
    parse_int_list,
    get_portioning_summary
)
from utils.event_logger import log_event

portioning_bp = Blueprint("portioning", __name__)


@portioning_bp.route("/portioning/summary", methods=["POST"])
def portioning_summary():
    body = request.get_json(silent=True) or {}

    raw_subrecipe_id = normalize_filter_value(body.get("subrecipe_id"))
    raw_mpdr_ids = normalize_filter_value(body.get("meal_plan_day_recipe_ids"))
    raw_status = normalize_filter_value(body.get("cooking_status")) or "completed"

    if raw_subrecipe_id is None:
        return jsonify({"error": "subrecipe_id is required"}), 400

    try:
        subrecipe_id = int(raw_subrecipe_id)
    except:
        return jsonify({"error": "subrecipe_id must be integer"}), 400

    try:
        meal_plan_day_recipe_ids = parse_int_list(raw_mpdr_ids, "meal_plan_day_recipe_ids")
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    result, error = get_portioning_summary(
        subrecipe_id=subrecipe_id,
        meal_plan_day_recipe_ids=meal_plan_day_recipe_ids,
        cooking_status=raw_status
    )

    if error:
        log_event(None, "api_error", {"route": "/portioning/summary", "status_code": 400, "error": error})
        return jsonify({"error": error}), 400

    log_event(None, "portioning_summary_viewed", {
        "subrecipe_id": subrecipe_id,
        "mpdr_count": len(meal_plan_day_recipe_ids) if meal_plan_day_recipe_ids else 0,
    })
    return jsonify(result)
