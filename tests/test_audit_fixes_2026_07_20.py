"""Regressões das correções da auditoria de contrato/ledger (2026-07-20).

Cobre:
  F3 — teto mensal de compra considera charges pendentes (não só o já creditado).
  F9 — teto de resgate é LÍQUIDO de estornos (payout falho + refund não pesa).
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("MAILER", "noop")

from app import create_app
from app.config import TestConfig
from app.extensions import db
from app.models import (
    CardCharge,
    CardChargeStatus,
    PixCharge,
    PixChargeStatus,
    TxType,
    User,
    Wallet,
)
from app.services import purchase as purchase_svc
from app.services import wallet as wallet_svc


VALID_CPF = "52998224725"


@pytest.fixture
def app():
    a = create_app(TestConfig)
    with a.app_context():
        db.create_all()
        yield a
        db.session.remove()
        db.drop_all()


def _mk_user(balance=0, email="af@test.com"):
    u = User(name="Audit Fix", email=email, cpf=VALID_CPF, role="user")
    u.set_password("StrongP@ss1!")
    u.email_verified_at = datetime.now(timezone.utc)
    db.session.add(u)
    db.session.flush()
    db.session.add(Wallet(user_id=u.id, balance_pts=balance))
    db.session.commit()
    return u


# --------------------------------------------------------------------------- #
# F3 — forecast do teto mensal inclui charges abertas (pending/in_process)      #
# --------------------------------------------------------------------------- #
def test_pending_purchase_forecast_counts_open_charges_only(app):
    with app.app_context():
        u = _mk_user()
        # Abertas: devem contar.
        db.session.add(PixCharge(
            user_id=u.id, package_key="start", amount_cents=18000,
            points_to_credit=1000, br_code="x",
            status=PixChargeStatus.PENDING,
            expires_at=PixCharge.make_expiry(3600),
        ))
        db.session.add(CardCharge(
            user_id=u.id, package_key="custom", amount_cents=20000,
            points_to_credit=2000, status=CardChargeStatus.IN_PROCESS,
        ))
        # Finalizadas: NÃO contam (já entram em credited_this_month / descartadas).
        db.session.add(PixCharge(
            user_id=u.id, package_key="start", amount_cents=9000,
            points_to_credit=500, br_code="y",
            status=PixChargeStatus.PAID,
            expires_at=PixCharge.make_expiry(3600),
        ))
        db.session.add(CardCharge(
            user_id=u.id, package_key="custom", amount_cents=3000,
            points_to_credit=300, status=CardChargeStatus.APPROVED,
        ))
        db.session.commit()

        pending = purchase_svc.pending_purchase_points_this_month(u.id)
        assert pending == 3000  # 1000 (PIX pending) + 2000 (card in_process)


def test_pending_purchase_forecast_blocks_over_cap(app):
    """Duas charges pendentes que juntas furam o teto: a 2ª criação é barrada."""
    with app.app_context():
        from app.config import Config
        u = _mk_user()
        cap = Config.PURCHASE_MAX_POINTS_PER_MONTH
        # Charge pendente que já consome quase todo o teto.
        db.session.add(PixCharge(
            user_id=u.id, package_key="custom", amount_cents=1,
            points_to_credit=cap - 100, br_code="z",
            status=PixChargeStatus.PENDING,
            expires_at=PixCharge.make_expiry(3600),
        ))
        db.session.commit()

        # Nova compra de 200 pts: já-creditado(0) + pendente(cap-100) + 200 > cap.
        with pytest.raises(purchase_svc.PixError, match="limite mensal"):
            purchase_svc.create_charge(u, amount_brl=200 * Config.CENTS_PER_POINT / 100)


# --------------------------------------------------------------------------- #
# F9 — teto de resgate LÍQUIDO de estornos                                      #
# --------------------------------------------------------------------------- #
def test_net_redeemed_subtracts_redeem_refunds(app):
    with app.app_context():
        u = _mk_user(balance=10_000)
        # Resgate debita REDEEM.
        wallet_svc.debit(u.id, amount_pts=2500, tx_type=TxType.REDEEM,
                         idempotency_key="redeem-debit:p1")
        assert wallet_svc.debited_today(u.id, TxType.REDEEM) == 2500
        assert wallet_svc.net_redeemed_today(u.id) == 2500

        # Payout falhou → estorno (REFUND com key redeem-refund).
        wallet_svc.credit(u.id, amount_pts=2500, tx_type=TxType.REFUND,
                          idempotency_key="redeem-refund:p1")
        # Bruto continua 2500 (ledger imutável), mas líquido zera.
        assert wallet_svc.debited_today(u.id, TxType.REDEEM) == 2500
        assert wallet_svc.net_redeemed_today(u.id) == 0
        assert wallet_svc.net_redeemed_this_month(u.id) == 0


def test_net_redeemed_ignores_card_refunds(app):
    """Estorno de CARTÃO (key card-refund) não deve abater o teto de resgate."""
    with app.app_context():
        u = _mk_user(balance=10_000)
        wallet_svc.debit(u.id, amount_pts=2500, tx_type=TxType.REDEEM,
                         idempotency_key="redeem-debit:p2")
        # Estorno de cartão — NÃO relacionado a resgate.
        wallet_svc.credit(u.id, amount_pts=2500, tx_type=TxType.REFUND,
                          idempotency_key="card-refund:c9")
        # Líquido de resgate permanece 2500 (card-refund é ignorado).
        assert wallet_svc.net_redeemed_today(u.id) == 2500
