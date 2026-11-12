from flask import Blueprint, request, jsonify

from services.advanced_planning_service import AdvancedPlanningError, apply_changes
from utils.event_logger import log_event

advanced_planning_bp = Blueprint("advanced_planning", __name__)


@advanced_planning_bp.route("/advanced_planning/apply", methods=["POST"])
def advanced_planning_apply():
    """
    Input:  { user_id, plan: <same shape as /generate_meal_plan>,
              changes: [{date, meal_key, subrecipe_id, servings}] }
    Output: the plan with those servings applied and every touched day's
            macros recomputed from the DB (plan.advanced_planning = true,
            day.manual_servings = true on edited days).
    """
    data = request.get_json(silent=True) or {}
    user_id = data.get("user_id")
    try:
        updated = apply_changes(user_id, data.get("plan"), data.get("changes") or [])
    except AdvancedPlanningError as e:
        log_event(user_id, "advanced_planning_rejected", {
            "status_code": e.status,
            "reason": str(e),
            "change_count": len(data.get("changes") or []),
        })
        return jsonify({"error": str(e)}), e.status

    details = updated.pop("_log", {}) or {}
    # One row per save: every item changed (from -> to servings) and each
    # touched day's macros before/after vs its kcal target.
    log_event(user_id, "advanced_planning_applied", {
        "change_count": len(details.get("changes") or []),
        "start_date": updated.get("start_date"),
        "end_date": updated.get("end_date"),
        "changes": details.get("changes") or [],
        "days": details.get("days") or [],
    })
    return jsonify(updated), 200
