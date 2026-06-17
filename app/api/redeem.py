"""Endpoints de resgate via PIX (cashback)."""

from __future__ import annotations

from flask import Blueprint, g, jsonify, request

from ..extensions import db, limiter
from ..models import PixPayout
from ..security import MfaStepUpRequired
from ..services import redeem as redeem_svc
from .auth import login_required, email_verified_required

bp = Blueprint("redeem", __name__)


@bp.get("/quote")
@login_required
def quote():
    try:
        points = int(request.args.get("points") or 0)
    except (TypeError, ValueError):
        return jsonify({"error": "points deve ser inteiro"}), 400
    try:
        return jsonify(redeem_svc.quote(points))
    except redeem_svc.RedeemError as exc:
        return jsonify({"error": str(exc)}), 400


@bp.post("/")
@limiter.limit("10 per hour", key_func=lambda: g.current_user.id if hasattr(g, "current_user") else "anon")
@login_required
@email_verified_required
def request_redeem():
    """POST /redeem
    {
      "points": 5000,
      "pix_key": "ricardo.veles@gmail.com",
      "password": "..."
    }
    """
    data = request.get_json(silent=True) or {}
    try:
        points = int(data.get("points") or 0)
    except (TypeError, ValueError):
        return jsonify({"error": "points deve ser inteiro"}), 400

    try:
        payout = redeem_svc.request_redeem(
            g.current_user,
            points=points,
            pix_key=(data.get("pix_key") or "").strip(),
            password=data.get("password") or "",
            mfa_code=data.get("mfa_code"),
        )
    except MfaStepUpRequired as exc:
        return jsonify({"error": str(exc), "mfa_required": True}), 401
    except redeem_svc.RedeemError as exc:
        return jsonify({"error": str(exc)}), 400

    return jsonify(payout.to_dict()), 201


@bp.get("/<payout_id>")
@login_required
def get_payout(payout_id: str):
    payout = db.session.get(PixPayout, payout_id)
    if payout is None or payout.user_id != g.current_user.id:
        return jsonify({"error": "not found"}), 404
    return jsonify(payout.to_dict())
