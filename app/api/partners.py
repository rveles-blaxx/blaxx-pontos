"""Endpoints de parceiros."""

from __future__ import annotations

from flask import Blueprint, jsonify, request

from ..extensions import db
from ..models import Partner

bp = Blueprint("partners", __name__)


@bp.get("/")
def list_partners():
    """Lista parceiros ativos. Filtros opcionais: ?category=...&q=..."""
    q = db.session.query(Partner).filter_by(is_active=True)

    category = request.args.get("category", "").strip()
    if category:
        q = q.filter(Partner.category == category)

    search = request.args.get("q", "").strip().lower()
    items = q.order_by(Partner.name.asc()).all()
    if search:
        items = [p for p in items if search in p.name.lower()
                 or search in (p.description or "").lower()]
    return jsonify({"items": [p.to_dict() for p in items]})


@bp.get("/<partner_id>")
def get_partner(partner_id: str):
    partner = db.session.get(Partner, partner_id)
    if partner is None or not partner.is_active:
        return jsonify({"error": "Parceiro não encontrado"}), 404

    # Inclui também benefícios desse parceiro
    from ..models import Benefit
    benefits = (
        db.session.query(Benefit)
        .filter_by(partner_id=partner.id, is_active=True)
        .order_by(Benefit.cost_pts.asc())
        .all()
    )
    return jsonify({
        **partner.to_dict(),
        "benefits": [b.to_dict() for b in benefits],
    })


@bp.get("/categories")
def list_categories():
    rows = (
        db.session.query(Partner.category)
        .filter(Partner.is_active == True)
        .distinct()
        .order_by(Partner.category.asc())
        .all()
    )
    return jsonify({"items": [r[0] for r in rows]})
