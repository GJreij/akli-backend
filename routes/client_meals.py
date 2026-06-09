from flask import Blueprint, request, jsonify
from datetime import date, timedelta
from services.client_meals_service import ClientMealsService
from utils.event_logger import log_event

client_meals_bp = Blueprint("client_meals", __name__)
service = ClientMealsService()


@client_meals_bp.route("/client/upcoming_recipes", methods=["GET"])
def upcoming_recipes():
    user_id = request.args.get("user_id")
    from_date = request.args.get("from")
    to_date = request.args.get("to")

    if not user_id:
        return jsonify({"error": "user_id is required"}), 400

    today = date.today()

    # Default window: past 3 days → next 7 days
    if not from_date:
        from_date = (today - timedelta(days=3)).isoformat()
    if not to_date:
        to_date = (today + timedelta(days=7)).isoformat()

    result = service.get_upcoming_recipes(
        user_id=user_id,
        from_date=from_date,
        to_date=to_date
    )

    log_event(user_id, "meal_plan_viewed", {"from_date": from_date, "to_date": to_date})
    return jsonify(result), 200
