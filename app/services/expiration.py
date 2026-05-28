"""Sprint 2 · Expiracao de pontos > 24 meses (LGPD + compromisso comercial).

O site Blaxx promete "pontos validos por 24 meses". O backend precisa
cumprir isso emitindo Transaction.EXPIRE no ledger pra zerar pontos
nao gastos apos 24 meses do credito original.

Algoritmo FIFO simplificado:
  1. Pra cada user, pega lista de Transaction.CREDIT (positivas) ordenadas
     por created_at ASC.
  2. Soma debitos pos-FIFO consumidos.
  3. Identifica creditos cuja janela de validade (24m) ja venceu E que
     ainda nao foram totalmente consumidos.
  4. Emite Transaction.EXPIRE com o resto.

Idempotencia:
  - Cada execucao usa idempotency_key=f"expire:{wallet_id}:{YYYY-MM}".
  - Roda no maximo 1 vez por mes por usuario.
  - Se ja existe Transaction.EXPIRE no periodo, pula.

Performance: roda em batch, transacional por usuario. Ate ~50k usuarios
dura segundos. Acima disso paralelize por shards.

Uso:
    # CLI manual
    from app.services.expiration import expire_old_points_all
    expire_old_points_all()

    # Cron mensal (preferivel)
    # No Render: configurar Cron Service rodando uma vez por mes.
    # Endpoint admin POST /admin/expire-points dispara a varredura.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from ..extensions import db
from ..models import (
    Transaction, TxType, TxStatus, Wallet, User,
)

logger = logging.getLogger(__name__)

# Janela de validade dos pontos. 24 meses = 730 dias (aproximacao segura).
# Sobreescrivivel via Config.POINTS_VALIDITY_DAYS.
DEFAULT_VALIDITY_DAYS = 730


def _validity_days() -> int:
    from flask import current_app
    return int(current_app.config.get("POINTS_VALIDITY_DAYS", DEFAULT_VALIDITY_DAYS))


def expire_wallet_points(wallet: Wallet, cutoff: datetime) -> int:
    """Expira pontos da wallet cujos creditos sao anteriores a `cutoff`.

    Retorna quantos pontos foram expirados (positivo = sucesso).
    Usa idempotency_key por (wallet, ano-mes) — chamadas multiplas no
    mesmo mes sao no-op.

    Algoritmo FIFO:
      saldo_a_expirar = max(0, sum(creditos antigos) - sum(debitos))
      onde "creditos antigos" = Transaction.CREDIT com created_at < cutoff
    """
    now = datetime.now(timezone.utc)
    period_key = now.strftime("%Y-%m")
    idem = f"expire:{wallet.id}:{period_key}"

    # Ja rodou esse mes pra essa wallet?
    existing = db.session.query(Transaction).filter_by(
        wallet_id=wallet.id, idempotency_key=idem
    ).first()
    if existing:
        logger.debug("expire_wallet_points: ja rodou em %s para wallet=%s", period_key, wallet.id)
        return 0

    # Soma creditos com created_at < cutoff (positivos, status CONFIRMED)
    from sqlalchemy import func, and_
    q_credits_old = db.session.query(func.coalesce(func.sum(Transaction.amount_pts), 0)).filter(
        Transaction.wallet_id == wallet.id,
        Transaction.status == TxStatus.CONFIRMED,
        Transaction.amount_pts > 0,
        Transaction.created_at < cutoff,
    )
    credits_old = int(q_credits_old.scalar() or 0)

    # Soma TODOS os debitos (negativos) ate hoje — FIFO assume que debitos
    # consomem creditos do mais antigo. Se debitos > creditos_old, nao ha
    # nada pra expirar (tudo que era velho ja foi gasto).
    q_debits = db.session.query(func.coalesce(func.sum(Transaction.amount_pts), 0)).filter(
        Transaction.wallet_id == wallet.id,
        Transaction.status == TxStatus.CONFIRMED,
        Transaction.amount_pts < 0,
    )
    debits = int(q_debits.scalar() or 0)  # ja negativo

    # FIFO: creditos velhos consumidos primeiro
    to_expire = max(0, credits_old + debits)  # debits e' negativo

    # Cap pelo saldo atual (nao expirar mais do que o user tem)
    to_expire = min(to_expire, wallet.balance_pts)

    if to_expire <= 0:
        return 0

    # Cria a Transaction EXPIRE
    tx = Transaction(
        wallet_id=wallet.id,
        type=TxType.EXPIRE if hasattr(TxType, "EXPIRE") else TxType.REDEEM,
        status=TxStatus.CONFIRMED,
        amount_pts=-to_expire,
        description=f"Expiracao automatica · pontos creditados antes de {cutoff.date().isoformat()}",
        idempotency_key=idem,
    )
    db.session.add(tx)
    wallet.balance_pts -= to_expire
    db.session.commit()
    logger.info("expire_wallet_points: wallet=%s expirou %d pts (ref=%s)",
                wallet.id, to_expire, period_key)
    return to_expire


def expire_old_points_all(dry_run: bool = False) -> dict:
    """Varre todas as wallets e expira pontos velhos.

    Args:
        dry_run: se True, calcula mas nao faz commit (preview)

    Returns:
        {
            "wallets_scanned": N,
            "wallets_affected": N,
            "points_expired_total": N,
            "errors": [{"wallet_id": ..., "error": ...}, ...],
        }
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=_validity_days())
    result = {
        "cutoff": cutoff.isoformat(),
        "validity_days": _validity_days(),
        "wallets_scanned": 0,
        "wallets_affected": 0,
        "points_expired_total": 0,
        "dry_run": dry_run,
        "errors": [],
    }

    # So wallets com saldo > 0 (otimizacao — sem saldo, nada a expirar)
    wallets = db.session.query(Wallet).filter(Wallet.balance_pts > 0).all()
    result["wallets_scanned"] = len(wallets)

    for w in wallets:
        try:
            if dry_run:
                # Simula a conta sem commit
                from sqlalchemy import func
                q_cred = db.session.query(func.coalesce(func.sum(Transaction.amount_pts), 0)).filter(
                    Transaction.wallet_id == w.id,
                    Transaction.status == TxStatus.CONFIRMED,
                    Transaction.amount_pts > 0,
                    Transaction.created_at < cutoff,
                )
                q_deb = db.session.query(func.coalesce(func.sum(Transaction.amount_pts), 0)).filter(
                    Transaction.wallet_id == w.id,
                    Transaction.status == TxStatus.CONFIRMED,
                    Transaction.amount_pts < 0,
                )
                preview = max(0, min(
                    (q_cred.scalar() or 0) + (q_deb.scalar() or 0),
                    w.balance_pts,
                ))
                if preview > 0:
                    result["wallets_affected"] += 1
                    result["points_expired_total"] += preview
            else:
                expired = expire_wallet_points(w, cutoff)
                if expired > 0:
                    result["wallets_affected"] += 1
                    result["points_expired_total"] += expired
        except Exception as e:
            db.session.rollback()
            result["errors"].append({"wallet_id": w.id, "error": str(e)})
            logger.exception("expire_old_points_all: erro em wallet=%s", w.id)

    logger.info(
        "expire_old_points_all: cutoff=%s · scanned=%d · affected=%d · expired=%d · dry=%s",
        cutoff.isoformat(),
        result["wallets_scanned"],
        result["wallets_affected"],
        result["points_expired_total"],
        dry_run,
    )
    return result
