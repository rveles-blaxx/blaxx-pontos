"""Resgate de pontos via PIX (cashback).

Fluxo:
  1) Usuário pede resgate informando pontos + chave PIX.
  2) Sistema valida (mínimo 2.500 pts, múltiplo da conversão, limite diário).
  3) Cria PixPayout (REQUESTED), DEBITA pontos da wallet imediatamente.
  4) Chama provider.request_payout (PROCESSING).
  5) Se sucesso → PAID; se falha → FAILED + REFUND dos pontos (estorno).

Conversão: Config.CENTS_PER_POINT (default 9 cents = 1 ponto = R$ 0,09).
"""

from __future__ import annotations

from datetime import datetime, timezone

from flask import current_app

from ..config import Config
from ..extensions import db
from ..models import (
    PixPayout,
    PixPayoutStatus,
    TxType,
    User,
)
from ..pix.provider import PixPayoutRequest, PixProvider
from . import wallet as wallet_svc


class RedeemError(Exception):
    pass


def _provider() -> PixProvider:
    return current_app.extensions["pix_provider"]


def quote(points: int) -> dict:
    """Cota o resgate: quanto o usuário recebe em R$ por X pontos."""
    if points <= 0:
        raise RedeemError("informe um valor de pontos positivo")
    cents = Config.pts_to_cents(points)  # pts * CENTS_PER_POINT
    return {
        "points": points,
        "amount_cents": cents,
        "amount_brl": round(cents / 100, 2),
        "rate": Config.rate_label(),
        "cents_per_point": Config.CENTS_PER_POINT,
    }


def request_redeem(
    user: User,
    *,
    points: int,
    pix_key: str,
    password: str,
) -> PixPayout:
    if not user.check_password(password):
        raise RedeemError("senha incorreta")

    # Sprint 3 (S3-10) · Gate de CPF real antes de payout PIX.
    # Usuarios que entraram so via Google recebem um placeholder do tipo
    # "G:xxxx12chars" como CPF. Esse CPF nao existe na Receita Federal
    # e o PSP vai recusar (DICT do BACEN exige titularidade). Em vez de
    # deixar quebrar la na frente, recusamos cedo com mensagem util.
    if not user.cpf or user.cpf.startswith("G:") or ":" in (user.cpf or ""):
        raise RedeemError(
            "Para resgatar via PIX, complete seu CPF real no perfil. "
            "Sua conta foi criada via Google sem CPF informado."
        )

    if points < Config.REDEEM_MIN_POINTS:
        raise RedeemError(
            f"resgate mínimo é {Config.REDEEM_MIN_POINTS} pts"
        )

    # Nao ha restricao de "multiplo" porque CENTS_PER_POINT=9 ja produz
    # valores inteiros em centavos para qualquer numero inteiro de pontos.

    if not pix_key or len(pix_key) > 180:
        raise RedeemError("chave PIX inválida")

    # Limite diario: usuarios VIP nao tem teto.
    # Demais: REDEEM_MAX_POINTS_PER_DAY (default = R$ 100.000 convertidos em pts).
    if not user.is_vip:
        redeemed_today = wallet_svc.debited_today(user.id, TxType.REDEEM)
        if redeemed_today + points > Config.REDEEM_MAX_POINTS_PER_DAY:
            remaining = Config.REDEEM_MAX_POINTS_PER_DAY - redeemed_today
            raise RedeemError(
                f"limite diário de resgate excedido — restam {max(remaining,0)} pts hoje"
            )

    quoted = quote(points)
    payout = PixPayout(
        user_id=user.id,
        points_debited=points,
        amount_cents=quoted["amount_cents"],
        pix_key=pix_key,
    )
    db.session.add(payout)
    db.session.flush()

    # 1) Debita pontos ANTES de chamar o provedor — protege contra duplicidade
    try:
        wallet_svc.debit(
            user_id=user.id,
            amount_pts=points,
            tx_type=TxType.REDEEM,
            description=f"Resgate via PIX → {pix_key}",
            reference=payout.id,
            idempotency_key=f"redeem-debit:{payout.id}",
        )
    except wallet_svc.InsufficientBalance as exc:
        db.session.rollback()
        raise RedeemError(str(exc)) from exc

    # 2) Pede o pagamento ao provedor
    payout.status = PixPayoutStatus.PROCESSING
    db.session.flush()

    resp = _provider().request_payout(
        PixPayoutRequest(
            txid=payout.txid,
            amount_cents=payout.amount_cents,
            pix_key=pix_key,
            description="Blaxx Pontos — resgate",
        )
    )

    if resp.status == "paid":
        payout.status = PixPayoutStatus.PAID
        payout.end_to_end_id = resp.end_to_end_id
        payout.paid_at = datetime.now(timezone.utc)
    elif resp.status == "processing":
        # Provedor confirma depois via callback — débito permanece
        payout.end_to_end_id = resp.end_to_end_id or None
    else:
        # Falha no provedor → estorna pontos (refund)
        payout.status = PixPayoutStatus.FAILED
        payout.failure_reason = resp.failure_reason or "falha no provedor PIX"
        wallet_svc.credit(
            user_id=user.id,
            amount_pts=points,
            tx_type=TxType.REFUND,
            description=f"Estorno de resgate falho — {payout.failure_reason}",
            reference=payout.id,
            idempotency_key=f"redeem-refund:{payout.id}",
        )

    db.session.commit()
    return payout


def confirm_payout(txid: str, *, end_to_end_id: str | None = None) -> PixPayout:
    """Para provedores que confirmam por callback (status assíncrono)."""
    payout = db.session.query(PixPayout).filter_by(txid=txid).one_or_none()
    if payout is None:
        raise RedeemError(f"payout não encontrado: txid={txid}")

    if payout.status == PixPayoutStatus.PAID:
        return payout

    payout.status = PixPayoutStatus.PAID
    payout.paid_at = datetime.now(timezone.utc)
    if end_to_end_id:
        payout.end_to_end_id = end_to_end_id
    db.session.commit()
    return payout
