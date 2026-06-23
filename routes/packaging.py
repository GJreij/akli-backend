# routes/packaging.py

from flask import Blueprint, request, jsonify
from services.packaging_service import get_packaging_view

packaging_bp = Blueprint("packaging", __name__)


@packaging_bp.route("/packaging", methods=["POST"])
def packaging():
    body = request.get_json(silent=True) or {}

    start_date = body.get("start_date")
    end_date = body.get("end_date")

    if not start_date or not end_date:
        return jsonify({"error": "start_date and end_date are required"}), 400

    result = get_packaging_view(start_date, end_date)
    return jsonify(result)
