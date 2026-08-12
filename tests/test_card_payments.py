"""Cartão de crédito · /payments/card.

Cobertura:
  - flag CARD_ENABLED off → /config enabled:false e /charge 503
  - approved síncrono (mock) credita pontos na hora, idempotente
  - rejected → 400 com mensagem PT-BR, sem crédito
  - in_process → webhook approved credita (roteado por external_reference)
  - Idempotency-Key: retry não cobra nem credita 2x
  - dados brutos de cartão (PAN/CVV) recusados na entrada
  - limite mensal compartilhado entre PIX e cartão (TxType.PURCHASE)
  - refund/chargeback via webhook debita os pontos creditados
  - replay do webhook approved não credita 2x

Roda com:
    pytest -v tests/test_card_payments.py
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
    CardCharge, CardChargeStatus, Transaction, TxType, User, Wallet,
)
from app.pix.provider import CardChargeRequest, CardChargeResponse


VALID_CPF = "52998224725"


class FakeMPProvider:
    """Provider falso para exercitar o fluxo de cartão sem rede.

    create_card_payment devolve o status programado em `next_status`;
    get_payment devolve o payment registrado em `payments`.
    """
    name = "fake-card"

    def __init__(self):
        self.payments: dict[str, dict] = {}
        self.next_status = "approved"
        self.next_detail = "accredited"
        self.card_calls: list[CardChargeRequest] = []

    def create_card_payment(self, req: CardChargeRequest) -> CardChargeResponse:
        self.card_calls.append(req)
        mp_id = f"MP-{len(self.card_calls)}"
        self.payments[mp_id] = {
            "id": mp_id,
            "status": self.next_status,
            "status_detail": self.next_detail,
            "external_reference": req.external_reference,
        }
        return CardChargeResponse(
            provider_payment_id=mp_id,
            status=self.next_status,
            status_detail=self.next_detail,
            card_brand=req.payment_method_id,
            card_last4="1111",
        )

    def get_payment(self, provider_payment_id: str) -> dict:
        return self.payments[provider_payment_id]

    # PixProvider nominal — não usados nestes testes
    def create_charge(self, req):  # pragma: no cover
        raise AssertionError("não deveria criar charge PIX aqui")

    def request_payout(self, req):  # pragma: no cover
        raise AssertionError("não deveria fazer payout aqui")

    def get_charge_status(self, txid):  # pragma: no cover
        return "unknown"


@pytest.fixture
def provider():
    return FakeMPProvider()


@pytest.fixture
def app(provider):
    app = create_app(TestConfig, pix_provider=provider)
    app.config["CARD_ENABLED"] = True
    app.config["CARD_MAX_INSTALLMENTS"] = 6
    with app.app_context():
        db.create_all()
        yield app
        db.session.remove()
        db.drop_all()


@pytest.fixture
def client(app):
    return app.test_client()


def _mk_user(app, email="card@test.com", balance_pts=0) -> str:
    with app.app_context():
        u = User(name="Card User", email=email, cpf=VALID_CPF, role="user")
        u.set_password("StrongP@ss1!")
        u.email_verified_at = datetime.now(timezone.utc)
        db.session.add(u)
        db.session.flush()
        db.session.add(Wallet(user_id=u.id, balance_pts=balance_pts, pending_pts=0))
        db.session.commit()
        return u.id


def _login(client, email="card@test.com"):
    r = client.post("/auth/login", json={"email": email, "password": "StrongP@ss1!"})
    assert r.status_code == 200, r.get_json()
    return r.get_json()["access_token"]


def _balance(app, user_id) -> int:
    with app.app_context():
        return db.session.query(Wallet).filter_by(user_id=user_id).one().balance_pts


def _charge_body(**over):
    body = {
        "amount_brl": 90.0,          # 9000 cents → 1000 pts (9 cents/pt)
        "card_token": "tok_ok_123",
        "payment_method_id": "visa",
        "installments": 1,
    }
    body.update(over)
    return body


# ============================================================================
# Flag CARD_ENABLED
# ============================================================================

def test_card_disabled_config_and_503(app, client):
    app.config["CARD_ENABLED"] = False
    _mk_user(app)
    token = _login(client)

    r = client.get("/payments/card/config")
    assert r.status_code == 200
    assert r.get_json()["enabled"] is False
    assert r.get_json()["public_key"] == ""

    r = client.post("/payments/card/charge", json=_charge_body(),
                    headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 503
    assert r.get_json()["code"] == "CARD_DISABLED"


def test_card_config_without_stripe_is_disabled(client):
    """Sem STRIPE_API_KEY não há checkout de cartão possível.

    O config precisa dizer isso de forma explícita (enabled=False), e nunca
    devolver uma chave publicável vazia com enabled=True — o frontend tentaria
    montar o Elements e falharia em silêncio.
    """
    r = client.get("/payments/card/config")
    j = r.get_json()
    assert j["enabled"] is False
    assert j["provider"] == "none"
    assert j["public_key"] == ""


def test_card_config_exposes_stripe_publishable_key(app, client):
    """Com o Stripe configurado, o config entrega o provider e a chave pk_."""
    from app.pix.stripe_card import StripeCardProvider

    app.extensions["card_intl_provider"] = StripeCardProvider(
        api_key="sk_test_" + "x" * 24, webhook_secret="whsec_" + "s" * 32
    )
    app.config["STRIPE_PUBLISHABLE_KEY"] = "pk_test_" + "y" * 24
    try:
        j = client.get("/payments/card/config").get_json()
        assert j["provider"] == "stripe"
        assert j["public_key"].startswith("pk_test_")
        # Stripe não parcela no Brasil — o front não pode oferecer parcelas.
        assert j["max_installments"] == 1
    finally:
        app.extensions["card_intl_provider"] = None


# ============================================================================
# Fluxo aprovado / recusado / em análise
# ============================================================================

def test_card_approved_credits_points(app, client, provider):
    uid = _mk_user(app)
    token = _login(client)

    r = client.post("/payments/card/charge", json=_charge_body(),
                    headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 201, r.get_json()
    j = r.get_json()
    assert j["status"] == "approved"
    assert j["points_to_credit"] == 1000
    assert j["card_last4"] == "1111"
    assert _balance(app, uid) == 1000


def test_card_rejected_returns_400_no_credit(app, client, provider):
    uid = _mk_user(app)
    token = _login(client)
    provider.next_status = "rejected"
    provider.next_detail = "cc_rejected_insufficient_amount"

    r = client.post("/payments/card/charge", json=_charge_body(),
                    headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 400
    assert "limite" in r.get_json()["error"].lower()
    assert _balance(app, uid) == 0
    with app.app_context():
        c = db.session.query(CardCharge).one()
        assert c.status == CardChargeStatus.REJECTED
        assert c.status_detail == "cc_rejected_insufficient_amount"


def test_idempotency_key_prevents_double_charge(app, client, provider):
    uid = _mk_user(app)
    token = _login(client)
    headers = {"Authorization": f"Bearer {token}", "Idempotency-Key": "same-key-1"}

    r1 = client.post("/payments/card/charge", json=_charge_body(), headers=headers)
    r2 = client.post("/payments/card/charge", json=_charge_body(), headers=headers)
    assert r1.status_code == 201 and r2.status_code == 201
    assert r1.get_json()["id"] == r2.get_json()["id"]
    assert len(provider.card_calls) == 1      # gateway chamado UMA vez
    assert _balance(app, uid) == 1000         # creditado UMA vez


def test_raw_card_data_rejected(app, client):
    _mk_user(app)
    token = _login(client)
    body = _charge_body(card_number="4111111111111111", cvv="123")
    r = client.post("/payments/card/charge", json=body,
                    headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 400
    assert r.get_json()["code"] == "RAW_CARD_DATA"


# ============================================================================
# Limites compartilhados PIX + cartão
# ============================================================================

def test_monthly_limit_shared_with_pix(app, client):
    uid = _mk_user(app)
    token = _login(client)
    # Simula compras (PIX ou cartão — ambas creditam PURCHASE) já feitas no mês
    with app.app_context():
        from app.services import wallet as wallet_svc
        wallet_svc.credit(
            user_id=uid, amount_pts=99_900, tx_type=TxType.PURCHASE,
            description="compra anterior", idempotency_key="prev-1",
        )
        db.session.commit()

    # 1000 pts a mais estouraria o teto de 100k/mês
    r = client.post("/payments/card/charge", json=_charge_body(),
                    headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 400
    assert "limite mensal" in r.get_json()["error"]


# NOTA (2026-08-01): os testes do endpoint POST /pix/webhook foram removidos
# junto com o próprio endpoint — o MercadoPago foi descontinuado. A confirmação
# de pagamento agora tem cobertura própria e mais forte:
#   · Stripe → tests/test_stripe_card.py (assinatura HMAC, replay, adulteração)
#   · Asaas  → tests/test_asaas_payout.py e tests/test_asaas_cobranca.py

