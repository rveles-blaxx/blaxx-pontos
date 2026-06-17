"""Níveis de cliente (loyalty tiers) por pontos acumulados (lifetime).

Regra de negócio (decisão de produto):
  * O nível é definido pela SOMA DE TODOS OS CRÉDITOS confirmados no ledger
    (compra, transferência recebida, bônus, estorno). Débitos (resgate,
    transferência enviada, expiração) NÃO reduzem o lifetime — o nível nunca
    cai por gastar.
  * 4 categorias: Bronze (0+) · Prata (5.000+) · Ouro (20.000+) · Black (50.000+).
    Faixas definidas em Config.tiers().
"""

from __future__ import annotations

from sqlalchemy import func

from ..config import Config
from ..extensions import db
from ..models import Transaction, TxStatus, Wallet


def lifetime_points(wallet_id: str) -> int:
    """Soma de todos os créditos (amount_pts > 0) confirmados da carteira."""
    total = (
        db.session.query(func.coalesce(func.sum(Transaction.amount_pts), 0))
        .filter(
            Transaction.wallet_id == wallet_id,
            Transaction.amount_pts > 0,
            Transaction.status == TxStatus.CONFIRMED,
        )
        .scalar()
    )
    return int(total or 0)


def tier_state(user) -> dict:
    """Estado completo de fidelidade do usuário: saldo, lifetime, nível e
    progresso até o próximo nível. Pronto pra serializar no /card.
    """
    wallet = db.session.query(Wallet).filter_by(user_id=user.id).one()
    lifetime = lifetime_points(wallet.id)
    progress = Config.tier_progress(lifetime)

    # BlaXx VIP é por convite (admin seta is_vip): sobrepõe a escala por
    # pontos. É a categoria máxima — sem "próximo nível".
    if getattr(user, "is_vip", False):
        return {
            "balance_pts": wallet.balance_pts,
            "lifetime_points": lifetime,
            "tier": Config.VIP_TIER,
            "next_tier": None,
            "points_to_next": 0,
            "progress_pct": 100,
        }

    return {
        "balance_pts": wallet.balance_pts,
        "lifetime_points": lifetime,
        "tier": progress["current"],
        "next_tier": progress["next"],
        "points_to_next": progress["points_to_next"],
        "progress_pct": progress["progress_pct"],
    }
