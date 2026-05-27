"""Endpoints de benefícios (marketplace) e vouchers."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from flask import Blueprint, g, jsonify, request

from ..extensions import db
from ..models import Benefit, Voucher, Notification, TxType
from ..services import wallet as wallet_svc
from ..services.wallet import InsufficientBalance
from .auth import login_required

bp_benefits = Blueprint("benefits", __name__)
bp_vouchers = Blueprint("vouchers", __name__)


# --------------------- Benefícios (catálogo) ---------------------- #

@bp_benefits.get("/")
def list_benefits():
    """Cards do marketplace. Filtros: ?category=, ?max_pts=, ?partner_id=."""
    q = db.session.query(Benefit).filter_by(is_active=True)

    if (category := request.args.get("category", "").strip()):
        q = q.filter(Benefit.category == category)
    if (partner_id := request.args.get("partner_id", "").strip()):
        q = q.filter(Benefit.partner_id == partner_id)
    if (max_pts := request.args.get("max_pts", "").strip()):
        try: q = q.filter(Benefit.cost_pts <= int(max_pts))
        except ValueError: pass

    items = q.order_by(Benefit.cost_pts.asc()).all()
    return jsonify({"items": [b.to_dict() for b in items]})


@bp_benefits.get("/<benefit_id>")
def get_benefit(benefit_id: str):
    b = db.session.get(Benefit, benefit_id)
    if b is None or not b.is_active:
        return jsonify({"error": "Benefício não encontrado"}), 404
    return jsonify(b.to_dict())


@bp_benefits.post("/<benefit_id>/redeem")
@login_required
def redeem(benefit_id: str):
    """Resgata um benefício: debita pontos e emite um Voucher."""
    benefit = db.session.get(Benefit, benefit_id)
    if benefit is None or not benefit.is_active:
        return jsonify({"error": "Benefício não encontrado"}), 404

    # Estoque
    if benefit.stock == 0:
        return jsonify({"error": "Sem estoque disponível"}), 409
    if benefit.stock > 0:
        benefit.stock -= 1

    user = g.current_user
    try:
        wallet_svc.debit(
            user_id=user.id,
            amount_pts=benefit.cost_pts,
            tx_type=TxType.REDEEM,
            description=f"Resgate: {benefit.name}",
            reference=f"benefit:{benefit.id}",
            idempotency_key=f"benefit:{benefit.id}:{datetime.now(timezone.utc).isoformat()}",
        )
    except InsufficientBalance as e:
        return jsonify({"error": "Saldo insuficiente"}), 402

    voucher = Voucher(
        user_id=user.id,
        benefit_id=benefit.id,
        code=Voucher.make_code(),
        points_spent=benefit.cost_pts,
        expires_at=datetime.now(timezone.utc) + timedelta(days=benefit.expires_in_days),
    )
    db.session.add(voucher)

    db.session.add(Notification(
        user_id=user.id, type="voucher",
        title="Voucher emitido",
        body=f"Seu voucher {voucher.code} está disponível. Use até {voucher.expires_at.strftime('%d/%m/%Y')}.",
        icon="✦",
        reference=voucher.id,
    ))
    db.session.commit()
    return jsonify(voucher.to_dict()), 201


# --------------------- Vouchers (do usuário) ---------------------- #

@bp_vouchers.get("/")
@login_required
def list_my_vouchers():
    user_id = g.current_user.id
    items = (
        db.session.query(Voucher)
        .filter_by(user_id=user_id)
        .order_by(Voucher.created_at.desc())
        .all()
    )
    return jsonify({"items": [v.to_dict() for v in items]})


@bp_vouchers.get("/<voucher_id>")
@login_required
def get_voucher(voucher_id: str):
    v = db.session.get(Voucher, voucher_id)
    if v is None or v.user_id != g.current_user.id:
        return jsonify({"error": "Voucher não encontrado"}), 404
    return jsonify(v.to_dict())
