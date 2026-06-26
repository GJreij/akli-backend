# routes/delivery.py

from flask import Blueprint, request, jsonify
from services.delivery_service import get_delivery_overview

delivery_bp = Blueprint("delivery", __name__)


@delivery_bp.route("/deliveries/overview", methods=["POST"])
def deliveries_overview():
    body = request.get_json(silent=True) or {}

    start_date = body.get("start_date")
    end_date = body.get("end_date")

    if not start_date or not end_date:
        return jsonify({"error": "start_date and end_date are required"}), 400

    result = get_delivery_overview(start_date, end_date)
    return jsonify(result)
