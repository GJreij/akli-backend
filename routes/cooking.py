from flask import Blueprint, request, jsonify
from services.cooking_service import get_cooking_overview
from datetime import datetime, timedelta


cooking_bp = Blueprint('cooking', __name__)

@cooking_bp.route("/cooking/overview", methods=["POST"])
def cooking_overview():
    data = request.json or {}

    start_date = data.get("start_date")
    end_date   = data.get("end_date")

    if not start_date or not end_date:
        return jsonify({"error": "start_date and end_date are required"}), 400

    # 🟦 Convert COOKING date → EATING date (+1 day)
    start_date = (datetime.fromisoformat(start_date) + timedelta(days=1)).date().isoformat()
    end_date   = (datetime.fromisoformat(end_date) + timedelta(days=1)).date().isoformat()


    # print("HEADERS:", dict(request.headers))
    # print("RAW:", request.data.decode("utf-8"))
    # print("FORM:", request.form.to_dict())
    # print("JSON:", request.get_json(silent=True))
    if not start_date or not end_date:
        return jsonify({"error": "start_date and end_date are required"}), 400

    def clean(v):
        if v in (None, "", " ", "null", "Null", "NULL"):
            return None
        return v


    filters = {
        "client_id":     clean(data.get("client_id")),
        "delivery_slot_id": clean(data.get("delivery_slot_id")),
        "recipe_id":     clean(data.get("recipe_id")),
        "subrecipe_id":  clean(data.get("subrecipe_id")),
        "ingredient_id": clean(data.get("ingredient_id")),
        "cooking_status":        clean(data.get("cooking_status")),
    }

    result = get_cooking_overview(start_date, end_date, filters)
    return jsonify(result)
