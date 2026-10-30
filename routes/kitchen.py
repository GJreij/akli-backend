from flask import Blueprint, request, jsonify
from services.kitchen_service import get_kitchen_day, get_kitchen_week


kitchen_bp = Blueprint('kitchen', __name__)


@kitchen_bp.route("/kitchen/day", methods=["POST"])
def kitchen_day():
    data = request.json or {}
    cook_date = data.get("date")
    if not cook_date:
        return jsonify({"error": "date is required"}), 400
    try:
        return jsonify(get_kitchen_day(cook_date))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400


@kitchen_bp.route("/kitchen/week", methods=["POST"])
def kitchen_week():
    data = request.json or {}
    start = data.get("start")
    if not start:
        return jsonify({"error": "start is required"}), 400
    try:
        return jsonify(get_kitchen_week(start))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
