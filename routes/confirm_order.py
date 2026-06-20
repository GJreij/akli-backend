# routes/confirm_order.py

from flask import Blueprint, request, jsonify
from services.order_service import OrderService
from utils.event_logger import log_event
import traceback
import logging

logger = logging.getLogger(__name__)

confirm_order_bp = Blueprint("confirm_order", __name__)
order_service = OrderService()


order_service = OrderService()


@confirm_order_bp.route("/confirm_order", methods=["POST"])
def confirm_order():
    """
    Confirm a user's meal plan order.
    Required JSON body:
        - user_id
        - meal_plan
        - checkout_summary
        - delivery_slot_id
    """
    try:
        payload = request.get_json(silent=True) or {}

        user_id = payload.get("user_id")
        meal_plan = payload.get("meal_plan")
        checkout_summary = payload.get("checkout_summary")
        delivery_slot_id = payload.get("delivery_slot_id")
        payment_method      = payload.get("payment_method")       # "cash" | "whish" | "neo"
        delivery_address    = payload.get("delivery_address")     # free-text override from frontend
        delivery_address_id = payload.get("delivery_address_id")  # id from user_delivery_address

        # ---- Input validation ----
        missing = []
        if not user_id:
            missing.append("user_id")
        if not meal_plan:
            missing.append("meal_plan")
        if not checkout_summary:
            missing.append("checkout_summary")
        if not delivery_slot_id:
            missing.append("delivery_slot_id")
        if not delivery_address_id and not delivery_address:
            missing.append("delivery_address_id")

        if missing:
            log_event(user_id, "api_error", {"route": "/confirm_order", "status_code": 400, "reason": "missing_fields", "missing_fields": missing})
            return jsonify({
                "error": "Missing required fields",
                "missing_fields": missing
            }), 400

        # ---- Orchestration ----
        result, status_code = order_service.confirm_order(
            user_id=user_id,
            meal_plan=meal_plan,
            checkout_summary=checkout_summary,
            delivery_slot_id=delivery_slot_id,
            payment_method=payment_method,
            delivery_address=delivery_address,
            delivery_address_id=delivery_address_id,
        )

        if status_code == 200:
            log_event(user_id, "order_created", {
                "delivery_slot_id": delivery_slot_id,
                "order_id": result.get("order_id") or result.get("id"),
            })
        else:
            log_event(user_id, "api_error", {"route": "/confirm_order", "status_code": status_code, "error": result.get("error")})

        return jsonify(result), status_code

    except Exception as e:
        logger.error("confirm_order failed: %s\n%s", str(e), traceback.format_exc())
        log_event(None, "api_error", {"route": "/confirm_order", "status_code": 500, "error": str(e)})
        return jsonify({
            "error": "An unexpected error occurred during order confirmation.",
            "details": str(e)
        }), 500
