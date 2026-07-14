"""Compra de pontos com CARTÃO de crédito (MercadoPago Checkout API).

Fluxo (síncrono, diferente do PIX):
  1) Frontend tokeniza o cartão com o SDK JS do MP (public key) — PCI SAQ-A:
     PAN/CVV nunca chegam a este backend, só o card_token single-use.
  2) POST /card/charge cria CardCharge (PENDING) e chama POST /v1/payments.
  3) approved  → credita pontos na hora (idempotente) e retorna.
     in_process → aguarda webhook payment.updated (mesmo webhook do PIX,
                  roteado pelo external_reference "card-<id>").
     rejected  → grava status_detail e retorna erro mapeado em PT-BR.
  4) refunded/charged_back (webhook) → DEBITA os pontos creditados.

Gates compartilhados com o PIX (mesmos valores): sanctions, mínimo R$ 10,
teto R$ 100k não-VIP, limite mensal em pontos — que soma PIX + cartão
naturalmente, pois ambos creditam Transaction(type=PURCHASE).
"""

from __future__ import annotations

from datetime import datetime, timezone

from flask import current_app
from sqlalchemy.exc import IntegrityError

from ..config import Config
from ..extensions import db
from ..models import (
    CardCharge,
    CardChargeStatus,
    TxType,
    User,
)
from ..pix.provider import CardChargeRequest, PixProvider
from . import aml as aml_svc
from . import metrics as metrics_svc
from . import wallet as wallet_svc


class CardError(Exception):
    """Erro de negócio no fluxo de cartão — vira HTTP 400 amigável."""


# Motivos de recusa do MP → mensagem PT-BR pro usuário.
# https://www.mercadopago.com.br/developers/pt/docs/checkout-api/response-handling/collection-results
STATUS_DETAIL_PTBR = {
    "cc_rejected_insufficient_amount": "Cartão sem limite disponível para esta compra.",
    "cc_rejected_bad_filled_card_number": "Número do cartão incorreto — confira e tente de novo.",
    "cc_rejected_bad_filled_date": "Data de validade incorreta — confira e tente de novo.",
    "cc_rejected_bad_filled_security_code": "Código de segurança (CVV) incorreto.",
    "cc_rejected_bad_filled_other": "Algum dado do cartão está incorreto — confira e tente de novo.",
    "cc_rejected_call_for_authorize": "O emissor pediu autorização — ligue para o banco e tente de novo.",
    "cc_rejected_card_disabled": "Cartão inativo — ligue para o banco para ativá-lo.",
    "cc_rejected_duplicated_payment": "Você já fez um pagamento com esse valor há pouco. Aguarde alguns minutos.",
    "cc_rejected_high_risk": "Pagamento recusado pela análise antifraude. Tente outro meio de pagamento.",
    "cc_rejected_max_attempts": "Muitas tentativas seguidas — aguarde alguns minutos e tente de novo.",
    "cc_rejected_blacklist": "Pagamento não autorizado pelo emissor.",
    "cc_rejected_other_reason": "O emissor do cartão recusou o pagamento.",
}


def rejection_message(status_detail: str) -> str:
    return STATUS_DETAIL_PTBR.get(
        status_detail,
        "Pagamento recusado pelo emissor do cartão. Tente outro cartão ou PIX.",
    )


def _provider() -> PixProvider:
    return current_app.extensions["pix_provider"]


def create_card_charge(
    user: User,
    *,
    package_key: str | None = None,
    amount_brl: float | None = None,
    card_token: str,
    payment_method_id: str,
    installments: int = 1,
    issuer_id: str = "",
    idempotency_key: str | None = None,
) -> CardCharge:
    """Cria e processa uma compra de pontos no cartão. Fluxo síncrono."""
    if not card_token or not str(card_token).strip():
        raise CardError("token do cartão ausente — recarregue a página e tente de novo")
    if not payment_method_id:
        raise CardError("bandeira do cartão não identificada")

    installments = max(1, int(installments or 1))
    if installments > Config.CARD_MAX_INSTALLMENTS:
        raise CardError(
            f"parcelamento máximo: {Config.CARD_MAX_INSTALLMENTS}x"
        )

    if package_key and amount_brl is not None:
        raise CardError("informe package_key OU amount_brl, não os dois")
    if not package_key and amount_brl is None:
        raise CardError("informe package_key ou amount_brl")

    # Mesmos gates AML da compra PIX.
    try:
        aml_svc.check_sanctions_or_raise(user)
    except aml_svc.SanctionsBlock as exc:
        metrics_svc.inc_purchase("blocked_sanctions", "card")
        raise CardError(str(exc)) from exc

    if package_key:
        pkg = Config.POINT_PACKAGES.get(package_key)
        if pkg is None:
            raise CardError(f"pacote desconhecido: {package_key}")
        amount_cents = int(round(pkg["price_brl"] * 100))
        points_to_credit = pkg["points"]
        description = f"BlaXx — pacote {pkg['label']}"
        stored_key = package_key
    else:
        try:
            amount_brl = float(amount_brl)
        except (TypeError, ValueError):
            raise CardError("amount_brl inválido")
        if amount_brl < 10:
            raise CardError("valor mínimo R$ 10,00")
        if not getattr(user, "is_vip", False) and amount_brl > 100_000:
            raise CardError("valor máximo R$ 100.000 por compra (VIP não tem limite)")
        amount_cents = int(round(amount_brl * 100))
        points_to_credit = Config.cents_to_pts(amount_cents)
        description = f"BlaXx — R$ {amount_brl:.2f}"
        stored_key = "custom"

    # Limite mensal em pontos — credited_this_month soma TODAS as compras
    # (PIX e cartão creditam TxType.PURCHASE), então o teto é compartilhado.
    if not getattr(user, "is_vip", False):
        purchased_month = wallet_svc.credited_this_month(user.id, TxType.PURCHASE)
        if purchased_month + points_to_credit > Config.PURCHASE_MAX_POINTS_PER_MONTH:
            remaining = Config.PURCHASE_MAX_POINTS_PER_MONTH - purchased_month
            raise CardError(
                f"limite mensal de compra excedido — restam {max(remaining,0)} pts este mes"
            )

    # Idempotência do cliente: retry com a mesma key devolve a charge
    # original SEM cobrar o cartão de novo (o fluxo é síncrono — timeout
    # de rede + retry seria dupla cobrança).
    idem = (idempotency_key or "").strip()[:64] or None
    if idem:
        existing = (
            db.session.query(CardCharge)
            .filter_by(user_id=user.id, idempotency_key=idem)
            .one_or_none()
        )
        if existing is not None:
            return existing

    charge = CardCharge(
        user_id=user.id,
        package_key=stored_key,
        amount_cents=amount_cents,
        points_to_credit=points_to_credit,
        installments=installments,
        idempotency_key=idem,
    )
    db.session.add(charge)
    try:
        db.session.flush()
    except IntegrityError as exc:
        db.session.rollback()
        if idem:
            existing = (
                db.session.query(CardCharge)
                .filter_by(user_id=user.id, idempotency_key=idem)
                .one_or_none()
            )
            if existing is not None:
                return existing
        raise CardError("compra duplicada detectada") from exc

    from ..pix.mercadopago import PayerDataError
    try:
        resp = _provider().create_card_payment(
            CardChargeRequest(
                external_reference=charge.external_reference,
                amount_cents=amount_cents,
                description=description[:255],
                card_token=str(card_token).strip(),
                payment_method_id=str(payment_method_id).strip().lower(),
                installments=installments,
                payer_name=user.name or "",
                payer_cpf=user.cpf or "",
                payer_email=user.email or "",
                issuer_id=str(issuer_id or "").strip(),
                statement_descriptor=Config.MP_STATEMENT_DESCRIPTOR,
            )
        )
    except PayerDataError as exc:
        db.session.rollback()
        raise CardError(str(exc)) from exc
    except NotImplementedError as exc:
        db.session.rollback()
        raise CardError(
            "pagamento com cartão indisponível neste ambiente"
        ) from exc
    except Exception:
        # Erro de rede/gateway com pagamento possivelmente criado no MP:
        # NÃO retentamos aqui (X-Idempotency-Key protege retries do client);
        # a charge fica PENDING e o webhook reconcilia se o MP processou.
        db.session.commit()
        current_app.logger.exception(
            "card charge %s: erro no gateway — aguardando reconciliação", charge.id
        )
        raise CardError(
            "não foi possível concluir o pagamento agora — se o valor for "
            "cobrado, os pontos serão creditados automaticamente"
        )

    charge.mp_payment_id = resp.mp_payment_id or None
    charge.card_brand = (resp.card_brand or payment_method_id)[:20]
    charge.card_last4 = (resp.card_last4 or "")[:4] or None
    charge.status_detail = (resp.status_detail or "")[:80] or None

    if resp.status == "approved":
        _credit_charge(charge)
    elif resp.status in ("in_process", "pending", "authorized"):
        charge.status = CardChargeStatus.IN_PROCESS
    elif resp.status == "cancelled":
        charge.status = CardChargeStatus.CANCELLED
    else:  # rejected e afins
        charge.status = CardChargeStatus.REJECTED

    db.session.commit()
    metrics_svc.inc_purchase(charge.status.value, "card")

    # Threshold check pós-commit (não bloqueia, só registra alerta) —
    # mesma política da compra PIX.
    try:
        aml_svc.check_transaction_threshold(
            user, points_to_credit, kind="purchase",
            monthly_limit_pts=Config.PURCHASE_MAX_POINTS_PER_MONTH
            if not getattr(user, "is_vip", False) else None,
        )
        db.session.commit()
    except Exception:
        db.session.rollback()

    if charge.status == CardChargeStatus.REJECTED:
        raise CardError(rejection_message(charge.status_detail or ""))
    return charge


def _credit_charge(charge: CardCharge, *, idem_key: str | None = None) -> None:
    """Marca approved e credita pontos (idempotente). NÃO comita.

    idem_key permite re-crédito legítimo após uma disputa vencida
    (CHARGED_BACK/REFUNDED → approved): nesse caso a key card-charge:{id} já
    foi consumida pelo crédito original E pelo débito do estorno, então um
    novo crédito com a MESMA key seria no-op. Passar uma key de 2ª geração
    (card-recredit:{id}) restaura os pontos corretamente.
    """
    charge.status = CardChargeStatus.APPROVED
    charge.paid_at = datetime.now(timezone.utc)
    wallet_svc.credit(
        user_id=charge.user_id,
        amount_pts=charge.points_to_credit,
        tx_type=TxType.PURCHASE,
        description=f"Compra de pontos no cartão — {charge.package_key}",
        reference=charge.id,
        idempotency_key=idem_key or f"card-charge:{charge.id}",
    )


def apply_webhook_status(charge: CardCharge, mp_status: str,
                         status_detail: str = "") -> CardCharge:
    """Aplica um status vindo do webhook payment.updated (já verificado
    server-side via get_payment). Idempotente por transição."""
    if status_detail:
        charge.status_detail = status_detail[:80]

    if mp_status == "approved":
        if charge.status != CardChargeStatus.APPROVED:
            # Disputa vencida: se a charge tinha sido estornada
            # (CHARGED_BACK/REFUNDED), a key original já foi usada no crédito
            # e no débito do estorno — usa key de 2ª geração pra re-creditar.
            reversal = charge.status in (
                CardChargeStatus.CHARGED_BACK, CardChargeStatus.REFUNDED)
            _credit_charge(
                charge,
                idem_key=(f"card-recredit:{charge.id}" if reversal else None),
            )
            db.session.commit()
            metrics_svc.inc_purchase("paid", "card")
            try:
                from . import push as push_svc
                push_svc.send_to_user(
                    charge.user_id, "Pagamento aprovado",
                    f"+{charge.points_to_credit} pts creditados na sua carteira.",
                    data={"charge_id": charge.id,
                          "amount_brl": charge.amount_cents / 100},
                )
            except Exception:
                pass
        return charge

    if mp_status in ("refunded", "charged_back"):
        new_status = (CardChargeStatus.REFUNDED if mp_status == "refunded"
                      else CardChargeStatus.CHARGED_BACK)
        already_final = charge.status in (
            CardChargeStatus.REFUNDED, CardChargeStatus.CHARGED_BACK)
        was_credited = charge.status == CardChargeStatus.APPROVED
        charge.status = new_status
        if was_credited and not already_final:
            # Estorno/chargeback: retira os pontos creditados. Se o user já
            # gastou (saldo insuficiente), não deixamos o ledger negativo —
            # registramos pra tratamento manual (suporte/admin).
            try:
                wallet_svc.debit(
                    user_id=charge.user_id,
                    amount_pts=charge.points_to_credit,
                    tx_type=TxType.REFUND,
                    description=f"Estorno de compra no cartão ({mp_status})",
                    reference=charge.id,
                    idempotency_key=f"card-refund:{charge.id}",
                )
            except wallet_svc.InsufficientBalance:
                current_app.logger.error(
                    "card charge %s: %s mas usuário %s não tem saldo pra "
                    "estornar %s pts — tratar via admin",
                    charge.id, mp_status, charge.user_id, charge.points_to_credit,
                )
                from ..models import Notification, User as _User
                admins = db.session.query(_User).filter_by(role="admin").all()
                for admin in admins:
                    db.session.add(Notification(
                        user_id=admin.id, type="system",
                        title=f"Cartão: {mp_status} sem saldo pra estorno",
                        body=(f"Charge {charge.id}: estornar "
                              f"{charge.points_to_credit} pts do usuário "
                              f"{charge.user_id} manualmente."),
                        icon="⚠",
                        reference=charge.id,
                    ))
        db.session.commit()
        return charge

    if mp_status in ("rejected", "cancelled"):
        if charge.status not in (CardChargeStatus.APPROVED,
                                 CardChargeStatus.REFUNDED,
                                 CardChargeStatus.CHARGED_BACK):
            charge.status = (CardChargeStatus.REJECTED if mp_status == "rejected"
                             else CardChargeStatus.CANCELLED)
        db.session.commit()
        return charge

    # pending/in_process/etc: só persiste o detail
    db.session.commit()
    return charge
