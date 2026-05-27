"""Endpoints de envio de pontos P2P (entre clientes inscritos)."""

from __future__ import annotations

from flask import Blueprint, g, jsonify, request

from ..extensions import limiter
from ..services import transfer as transfer_svc
from .auth import login_required, email_verified_required

bp = Blueprint("transfer", __name__)


@bp.post("/")
@limiter.limit("20 per hour", key_func=lambda: g.current_user.id if hasattr(g, "current_user") else "anon")
@login_required
@email_verified_required
def send():
    """POST /transfer
    {
      "to": "lucas@example.com" | "12345678900",
      "amount_pts": 2000,
      "password": "...",
      "message": "obrigado!"
    }
    """
    data = request.get_json(silent=True) or {}
    try:
        amount = int(data.get("amount_pts") or 0)
    except (TypeError, ValueError):
        return jsonify({"error": "amount_pts deve ser inteiro"}), 400

    try:
        transfer = transfer_svc.send(
            g.current_user,
            recipient_identifier=data.get("to") or "",
            amount_pts=amount,
            password=data.get("password") or "",
            message=data.get("message"),
        )
    except transfer_svc.TransferError as exc:
        return jsonify({"error": str(exc)}), 400

    return jsonify(transfer.to_dict()), 201
