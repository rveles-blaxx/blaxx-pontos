"""Endpoints de resgate via PIX (cashback).

Sprint 1-2 (P0): idempotencia em DB via `pix_payouts.idempotency_key`
(UNIQUE por user_id+key). Substitui o cache in-memory anterior (perdia
estado em restart e nao funcionava com 2+ workers / multi-instancia).
"""

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
@login_required
@limiter.limit("10 per hour", key_func=lambda: g.current_user.id if hasattr(g, "current_user") else "anon")
@email_verified_required
def request_redeem():
    """POST /redeem
    {
      "points": 5000,
      "pix_key": "ricardo.veles@gmail.com",
      "password": "..."
    }

    Header opcional: `Idempotency-Key: <uuid v4 ou hash do request>`. Se enviado,
    retries com a mesma key dentro de 10min retornam a MESMA resposta sem debitar
    pontos de novo. Cliente deve gerar UUID na 1ª tentativa e repetir nos retries.
    """
    data = request.get_json(silent=True) or {}

    # 1) Idempotency lookup em DB: se ja existe payout com (user_id, key),
    # devolve a mesma resposta SEM debitar de novo. Cobre retry de cliente,
    # multi-worker e multi-instancia (cache in-memory anterior nao cobria).
    idem_raw = (request.headers.get("Idempotency-Key") or data.get("request_id") or "").strip()
    if idem_raw:
        if len(idem_raw) > 64:
            return jsonify({"error": "Idempotency-Key máx 64 chars"}), 400
        existing = (
            db.session.query(PixPayout)
            .filter_by(user_id=g.current_user.id, idempotency_key=idem_raw)
            .one_or_none()
        )
        if existing is not None:
            # Status 200 (em vez de 201) sinaliza "ja processada" mas a
            # resposta e' identica ao 1o sucesso pro cliente. Idempotente.
            return jsonify(existing.to_dict()), 200

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
            idempotency_key=idem_raw or None,
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
