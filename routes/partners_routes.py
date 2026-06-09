from flask import Blueprint, request, jsonify
from services.partner_service import PartnerService
from utils.event_logger import log_event

partner_bp = Blueprint("partner", __name__)
partner_service = PartnerService()

@partner_bp.route("/partner_shares", methods=["GET"])
def get_partner_shares():
    partner_id = request.args.get("partner_id")
    this_month = request.args.get("this_month", "false").lower() == "true"

    if not partner_id:
        log_event(None, "api_error", {"route": "/partner_shares", "status_code": 400, "reason": "missing_partner_id"})
        return jsonify({"error": "Missing partner_id"}), 400

    result = partner_service.get_partner_shares(partner_id, this_month)
    log_event(partner_id, "partner_shares_viewed", {"this_month": this_month})
    return jsonify(result), 200
