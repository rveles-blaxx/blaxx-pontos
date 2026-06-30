"""Compra de pontos via PIX.

Fluxo:
  1) Usuário escolhe um pacote → POST /pix/charge
  2) Sistema cria PixCharge (PENDING), pede BR Code ao provider, devolve QR.
  3) Usuário paga no banco. Provedor envia webhook → POST /pix/webhook
  4) Webhook marca PixCharge como PAID e credita os pontos na carteira.
     A operação é idempotente (mesmo webhook duas vezes não dobra pontos).
"""

from __future__ import annotations

from datetime import datetime, timezone

from flask import current_app

from ..config import Config
from ..extensions import db
from ..models import (
    PixCharge,
    PixChargeStatus,
    TxType,
    User,
)
from ..pix.provider import PixChargeRequest, PixProvider
from . import aml as aml_svc
from . import metrics as metrics_svc
from . import wallet as wallet_svc


class PixError(Exception):
    pass


def list_packages() -> dict:
    return Config.POINT_PACKAGES


def _provider() -> PixProvider:
    return current_app.extensions["pix_provider"]


def create_charge(
    user: User,
    package_key: str | None = None,
    amount_brl: float | None = None,
) -> PixCharge:
    """Cria charge PIX via MP. Aceita pacote pré-definido OU valor livre.

    Exatamente UMA das duas opções deve ser fornecida:
      - package_key: chave de Config.POINT_PACKAGES (start/plus/prime/black/...)
      - amount_brl: valor livre em reais (mínimo R$ 10, máximo R$ 100k não-VIP)

    Em ambos os casos a charge resultante usa o provider PIX configurado
    (Mercado Pago em prod), gerando QR Code real do MP.
    """
    if package_key and amount_brl is not None:
        raise PixError("informe package_key OU amount_brl, não os dois")
    if not package_key and amount_brl is None:
        raise PixError("informe package_key ou amount_brl")

    # Sprint 4 (S4-AML) — sanctions bloqueia, threshold/velocity registram.
    try:
        aml_svc.check_sanctions_or_raise(user)
    except aml_svc.SanctionsBlock as exc:
        metrics_svc.inc_purchase("blocked_sanctions", _provider().name)
        raise PixError(str(exc)) from exc

    if package_key:
        pkg = Config.POINT_PACKAGES.get(package_key)
        if pkg is None:
            raise PixError(f"pacote desconhecido: {package_key}")
        amount_cents = int(round(pkg["price_brl"] * 100))
        points_to_credit = pkg["points"]
        description = f"BlaXx — pacote {pkg['label']}"
        stored_key = package_key
    else:
        # Valor livre — validação de faixa
        try:
            amount_brl = float(amount_brl)
        except (TypeError, ValueError):
            raise PixError("amount_brl inválido")
        if amount_brl < 10:
            raise PixError("valor mínimo R$ 10,00")
        if not getattr(user, "is_vip", False) and amount_brl > 100_000:
            raise PixError("valor máximo R$ 100.000 por compra (VIP não tem limite)")
        amount_cents = int(round(amount_brl * 100))
        # Conversao via Config.CENTS_PER_POINT (default: 1 pt = 9 cents = R$ 0,09)
        points_to_credit = Config.cents_to_pts(amount_cents)
        description = f"BlaXx — R$ {amount_brl:.2f}"
        stored_key = "custom"

    # Sprint 1-2 (P0): limite MENSAL acumulado de compra (em pontos creditados).
    # Checado na CRIACAO da charge (forecast), nao na confirmacao — evita o
    # caso "cliente paga e depois nao pode creditar". VIP fica isento.
    if not getattr(user, "is_vip", False):
        purchased_month = wallet_svc.credited_this_month(user.id, TxType.PURCHASE)
        if purchased_month + points_to_credit > Config.PURCHASE_MAX_POINTS_PER_MONTH:
            remaining = Config.PURCHASE_MAX_POINTS_PER_MONTH - purchased_month
            raise PixError(
                f"limite mensal de compra excedido — restam {max(remaining,0)} pts este mes"
            )

    charge = PixCharge(
        user_id=user.id,
        package_key=stored_key,
        amount_cents=amount_cents,
        points_to_credit=points_to_credit,
        br_code="",  # será preenchido a seguir
        expires_at=PixCharge.make_expiry(Config.PIX_CHARGE_TTL_SECONDS),
    )
    db.session.add(charge)
    db.session.flush()  # garante txid

    resp = _provider().create_charge(
        PixChargeRequest(
            txid=charge.txid,
            amount_cents=amount_cents,
            description=description[:255],
            payer_name=user.name,
            payer_cpf=user.cpf,
            payer_email=user.email,    # MP exige email válido
            expires_in_seconds=Config.PIX_CHARGE_TTL_SECONDS,
        )
    )
    charge.br_code = resp.br_code
    charge.qr_code_image = resp.qr_code_image or None
    db.session.commit()
    metrics_svc.inc_purchase("created", _provider().name)
    # Threshold check pós-commit (não bloqueia, só registra alerta)
    try:
        aml_svc.check_transaction_threshold(
            user, points_to_credit, kind="purchase",
            monthly_limit_pts=Config.PURCHASE_MAX_POINTS_PER_MONTH
            if not getattr(user, "is_vip", False) else None,
        )
        # Commit isolado do alerta
        db.session.commit()
    except Exception:
        db.session.rollback()
    return charge


def confirm_payment(txid: str) -> PixCharge:
    """Chamado pelo webhook do provedor PIX quando o pagamento é confirmado.

    Idempotente: chamadas repetidas com o mesmo txid não creditam de novo.
    """
    charge = db.session.query(PixCharge).filter_by(txid=txid).one_or_none()
    if charge is None:
        raise PixError(f"charge não encontrada: txid={txid}")

    if charge.status == PixChargeStatus.PAID:
        return charge  # já foi processada

    if charge.status == PixChargeStatus.EXPIRED or charge.is_expired():
        charge.status = PixChargeStatus.EXPIRED
        db.session.commit()
        raise PixError("charge expirada")

    charge.status = PixChargeStatus.PAID
    charge.paid_at = datetime.now(timezone.utc)

    wallet_svc.credit(
        user_id=charge.user_id,
        amount_pts=charge.points_to_credit,
        tx_type=TxType.PURCHASE,
        description=f"Compra de pontos — pacote {charge.package_key}",
        reference=charge.id,
        idempotency_key=f"charge:{charge.id}",  # blinda contra webhook duplicado
    )
    db.session.commit()
    metrics_svc.inc_purchase("paid", _provider().name)
    # Sprint 7 — push pro user confirmando crédito
    try:
        from . import push as push_svc
        push_svc.send_to_user(
            charge.user_id,
            "Pagamento confirmado",
            f"+{charge.points_to_credit} pts creditados na sua carteira.",
            data={"charge_id": charge.id, "amount_brl": charge.amount_cents/100},
        )
    except Exception:
        pass
    return charge


def expire_if_needed(charge: PixCharge) -> PixCharge:
    if charge.status == PixChargeStatus.PENDING and charge.is_expired():
        charge.status = PixChargeStatus.EXPIRED
        db.session.commit()
    return charge
