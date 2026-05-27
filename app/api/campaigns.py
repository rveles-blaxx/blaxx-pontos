"""Endpoints de campanhas."""

from __future__ import annotations

from datetime import datetime, timezone

from flask import Blueprint, g, jsonify, request

from ..extensions import db
from ..models import Campaign, UserCampaign, Notification, TxType
from ..services import wallet as wallet_svc
from .auth import login_required

bp = Blueprint("campaigns", __name__)


@bp.get("/")
def list_campaigns():
    """Lista campanhas ativas. Se autenticado, marca quais o usuário aderiu."""
    items = (
        db.session.query(Campaign)
        .filter_by(is_active=True)
        .order_by(Campaign.created_at.desc())
        .all()
    )

    # Se houver Bearer válido, marca adesão e progresso
    from .auth import _bearer_user
    user = _bearer_user()
    joined_map: dict[str, UserCampaign] = {}
    if user is not None:
        joined = db.session.query(UserCampaign).filter_by(user_id=user.id).all()
        joined_map = {uc.campaign_id: uc for uc in joined}

    out = []
    for c in items:
        d = c.to_dict()
        uc = joined_map.get(c.id)
        d["joined"] = uc is not None
        d["progress_brl"] = round(uc.progress_cents / 100, 2) if uc else 0.0
        d["progress_pct"] = (
            min(100, int(uc.progress_cents * 100 / c.target_brl))
            if uc and c.target_brl > 0 else 0
        )
        d["completed_at"] = uc.completed_at.isoformat() if uc and uc.completed_at else None
        out.append(d)
    return jsonify({"items": out})


@bp.get("/<campaign_id>")
def get_campaign(campaign_id: str):
    c = db.session.get(Campaign, campaign_id)
    if c is None or not c.is_active:
        return jsonify({"error": "Campanha não encontrada"}), 404
    return jsonify(c.to_dict())


@bp.post("/<campaign_id>/join")
@login_required
def join_campaign(campaign_id: str):
    c = db.session.get(Campaign, campaign_id)
    if c is None or not c.is_active:
        return jsonify({"error": "Campanha não encontrada"}), 404

    user = g.current_user
    existing = (
        db.session.query(UserCampaign)
        .filter_by(user_id=user.id, campaign_id=c.id)
        .one_or_none()
    )
    if existing:
        return jsonify(existing.to_dict()), 200

    uc = UserCampaign(user_id=user.id, campaign_id=c.id, progress_cents=0)
    db.session.add(uc)
    db.session.add(Notification(
        user_id=user.id, type="campaign",
        title=f"Participando: {c.name}",
        body=f"Boa! Você está participando. Meta: R$ {c.target_brl / 100:.0f}. Bônus: {c.reward_pts} pts.",
        icon="★",
        reference=c.id,
    ))
    db.session.commit()
    return jsonify(uc.to_dict()), 201


@bp.post("/<campaign_id>/progress")
@login_required
def add_progress(campaign_id: str):
    """Avanço de progresso (uso interno / demo).

    Em produção: chamado pelo motor de regras quando o usuário compra em
    parceiro elegível. Aqui exposto pra demonstrar a UI.
    Body: {"amount_brl": 100.00}
    """
    c = db.session.get(Campaign, campaign_id)
    if c is None or not c.is_active:
        return jsonify({"error": "Campanha não encontrada"}), 404

    uc = (
        db.session.query(UserCampaign)
        .filter_by(user_id=g.current_user.id, campaign_id=c.id)
        .one_or_none()
    )
    if uc is None:
        return jsonify({"error": "Você ainda não aderiu a esta campanha"}), 409

    data = request.get_json(silent=True) or {}
    try:
        delta_cents = int(round(float(data.get("amount_brl", 0)) * 100))
    except (TypeError, ValueError):
        return jsonify({"error": "amount_brl inválido"}), 400
    if delta_cents <= 0:
        return jsonify({"error": "amount_brl precisa ser > 0"}), 400

    was_completed = uc.completed_at is not None
    uc.progress_cents += delta_cents

    # Atingiu a meta?
    if not was_completed and uc.progress_cents >= c.target_brl:
        uc.completed_at = datetime.now(timezone.utc)
        wallet_svc.credit(
            user_id=g.current_user.id,
            amount_pts=c.reward_pts,
            tx_type=TxType.BONUS,
            description=f"Campanha concluída: {c.name}",
            reference=f"campaign:{c.id}",
            idempotency_key=f"campaign-reward:{c.id}:{g.current_user.id}",
        )
        db.session.add(Notification(
            user_id=g.current_user.id, type="campaign",
            title=f"Você completou: {c.name}",
            body=f"Bônus de {c.reward_pts} pts creditado na sua carteira.",
            icon="✓",
            reference=c.id,
        ))

    db.session.commit()
    return jsonify(uc.to_dict())


@bp.get("/mine")
@login_required
def my_campaigns():
    items = (
        db.session.query(UserCampaign)
        .filter_by(user_id=g.current_user.id)
        .order_by(UserCampaign.joined_at.desc())
        .all()
    )
    return jsonify({"items": [uc.to_dict() for uc in items]})
