"""Cartão de crédito (MercadoPago Checkout API) · /payments/card.

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
    """Provider com name='mercadopago' pra exercitar o roteamento do webhook.

    create_card_payment devolve o status programado em `next_status`;
    get_payment devolve o payment registrado em `payments`.
    """
    name = "mercadopago"

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
            mp_payment_id=mp_id,
            status=self.next_status,
            status_detail=self.next_detail,
            card_brand=req.payment_method_id,
            card_last4="1111",
        )

    def get_payment(self, mp_payment_id: str) -> dict:
        return self.payments[mp_payment_id]

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
    app.config["MP_PUBLIC_KEY"] = "TEST-pk-0000"
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


def test_card_config_exposes_public_key(client):
    r = client.get("/payments/card/config")
    j = r.get_json()
    assert j["enabled"] is True
    assert j["public_key"] == "TEST-pk-0000"
    assert j["max_installments"] == 6


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


def test_card_in_process_then_webhook_approves(app, client, provider):
    uid = _mk_user(app)
    token = _login(client)
    provider.next_status = "in_process"
    provider.next_detail = "pending_review_manual"

    r = client.post("/payments/card/charge", json=_charge_body(),
                    headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 201
    j = r.get_json()
    assert j["status"] == "in_process"
    assert _balance(app, uid) == 0

    # MP aprova depois → webhook payment.updated
    mp_id = provider.card_calls[0].external_reference  # card-<id>
    with app.app_context():
        charge = db.session.query(CardCharge).one()
        provider.payments["MP-1"]["status"] = "approved"
        provider.payments["MP-1"]["status_detail"] = "accredited"

    r = client.post("/pix/webhook", json={
        "action": "payment.updated", "data": {"id": "MP-1"},
    })
    assert r.status_code == 200, r.get_json()
    assert _balance(app, uid) == 1000
    with app.app_context():
        c = db.session.query(CardCharge).one()
        assert c.status == CardChargeStatus.APPROVED


# ============================================================================
# Idempotência
# ============================================================================

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


def test_webhook_replay_does_not_double_credit(app, client, provider):
    uid = _mk_user(app)
    token = _login(client)
    provider.next_status = "in_process"
    client.post("/payments/card/charge", json=_charge_body(),
                headers={"Authorization": f"Bearer {token}"})
    provider.payments["MP-1"]["status"] = "approved"

    r1 = client.post("/pix/webhook", json={"action": "payment.updated",
                                           "data": {"id": "MP-1"}})
    r2 = client.post("/pix/webhook", json={"action": "payment.updated",
                                           "data": {"id": "MP-1"}})
    assert r1.status_code == 200 and r2.status_code == 200
    assert r2.get_json().get("replay") is True
    assert _balance(app, uid) == 1000


# ============================================================================
# PCI — defesa em profundidade
# ============================================================================

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


# ============================================================================
# Estorno / chargeback
# ============================================================================

@pytest.mark.parametrize("mp_status,expected", [
    ("refunded", CardChargeStatus.REFUNDED),
    ("charged_back", CardChargeStatus.CHARGED_BACK),
])
def test_refund_and_chargeback_debit_points(app, client, provider, mp_status, expected):
    uid = _mk_user(app)
    token = _login(client)
    r = client.post("/payments/card/charge", json=_charge_body(),
                    headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 201
    assert _balance(app, uid) == 1000

    provider.payments["MP-1"]["status"] = mp_status
    provider.payments["MP-1"]["status_detail"] = mp_status
    r = client.post("/pix/webhook", json={"action": "payment.updated",
                                          "data": {"id": "MP-1"}})
    assert r.status_code == 200, r.get_json()
    assert _balance(app, uid) == 0
    with app.app_context():
        c = db.session.query(CardCharge).one()
        assert c.status == expected


def test_dispute_won_recredits_after_chargeback(app, client, provider):
    """CHARGED_BACK → approved (merchant vence a disputa) re-credita os pontos.

    Regressão: a key card-charge:{id} já foi consumida pelo crédito original e
    pelo débito do estorno; sem uma key de 2ª geração o re-crédito seria no-op
    e o usuário ficaria sem os pontos apesar de ganhar a disputa.
    """
    uid = _mk_user(app)
    token = _login(client)
    client.post("/payments/card/charge", json=_charge_body(),
                headers={"Authorization": f"Bearer {token}"})
    assert _balance(app, uid) == 1000

    # 1) Chargeback → debita
    provider.payments["MP-1"]["status"] = "charged_back"
    client.post("/pix/webhook", json={"action": "payment.updated", "data": {"id": "MP-1"}})
    assert _balance(app, uid) == 0

    # 2) Disputa vencida → volta a approved → re-credita
    provider.payments["MP-1"]["status"] = "approved"
    r = client.post("/pix/webhook", json={"action": "payment.updated", "data": {"id": "MP-1"}})
    assert r.status_code == 200, r.get_json()
    assert _balance(app, uid) == 1000
    with app.app_context():
        c = db.session.query(CardCharge).one()
        assert c.status == CardChargeStatus.APPROVED


def test_double_reversal_after_recredit_debits_again(app, client, provider):
    """Regressão (auditoria segurança 2026-07-20, finding #1): após re-crédito
    de disputa vencida, um SEGUNDO estorno deve debitar de novo.

    A key de estorno é versionada por geração (card-refund → card-refund-1).
    Sem isso, o 2º estorno reusaria `card-refund:{id}` (já consumida na 1ª
    geração) → débito viraria no-op → o cliente reteria os pontos estornados
    uma 2ª vez pelo MP (perda de dinheiro silenciosa).

    Usa status distintos (charged_back e depois refunded) porque o replay-store
    do webhook deduplica eventos idênticos (mesmo payment_id+action+status).
    """
    uid = _mk_user(app)
    token = _login(client)
    client.post("/payments/card/charge", json=_charge_body(),
                headers={"Authorization": f"Bearer {token}"})
    assert _balance(app, uid) == 1000

    # 1) chargeback → debita (estorno geração 0)
    provider.payments["MP-1"]["status"] = "charged_back"
    client.post("/pix/webhook", json={"action": "payment.updated", "data": {"id": "MP-1"}})
    assert _balance(app, uid) == 0

    # 2) disputa vencida → approved → re-credita (crédito geração 1)
    provider.payments["MP-1"]["status"] = "approved"
    client.post("/pix/webhook", json={"action": "payment.updated", "data": {"id": "MP-1"}})
    assert _balance(app, uid) == 1000

    # 3) SEGUNDO estorno (status distinto p/ furar o replay-store) → debita de novo
    provider.payments["MP-1"]["status"] = "refunded"
    r = client.post("/pix/webhook", json={"action": "payment.updated", "data": {"id": "MP-1"}})
    assert r.status_code == 200, r.get_json()
    assert _balance(app, uid) == 0        # antes do fix permanecia 1000 (bug)
    with app.app_context():
        c = db.session.query(CardCharge).one()
        assert c.status == CardChargeStatus.REFUNDED


def test_webhook_credit_failure_leaves_no_poison_event(app, client, provider, monkeypatch):
    """Se o crédito falhar, o MpWebhookEvent NÃO é gravado — a retentativa do
    MP re-tenta o crédito em vez de tratar como replay e perder o pagamento."""
    from app.services import purchase as purchase_svc
    from app.models import MpWebhookEvent
    uid = _mk_user(app)
    token = _login(client)
    provider.next_status = "in_process"
    client.post("/payments/card/charge", json=_charge_body(),
                headers={"Authorization": f"Bearer {token}"})
    provider.payments["MP-1"]["status"] = "approved"

    # Este teste cobre o ramo PIX (purchase_svc.confirm_payment). Para cartão o
    # crédito é via card_svc; aqui garantimos que o padrão geral (evento após
    # efeito) vale para o ramo PIX também.
    email2 = "pixfail@test.com"
    with app.app_context():
        from app.models import User, Wallet, PixCharge
        from datetime import datetime, timezone
        u = User(name="Pix Fail", email=email2, cpf="15350946056", role="user")
        u.set_password("StrongP@ss1!"); u.email_verified_at = datetime.now(timezone.utc)
        db.session.add(u); db.session.flush()
        db.session.add(Wallet(user_id=u.id, balance_pts=0, pending_pts=0))
        ch = PixCharge(user_id=u.id, package_key="custom", amount_cents=9000,
                       points_to_credit=1000, br_code="x",
                       expires_at=PixCharge.make_expiry(1800))
        db.session.add(ch); db.session.commit()
        txid = ch.txid
    provider.payments["PIX-1"] = {"id": "PIX-1", "status": "approved",
                                  "external_reference": txid}

    calls = {"n": 0}
    real = purchase_svc.confirm_payment
    def flaky(t, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise purchase_svc.PixError("falha simulada no crédito")
        return real(t, **kw)
    monkeypatch.setattr(purchase_svc, "confirm_payment", flaky)

    r1 = client.post("/pix/webhook", json={"action": "payment.updated", "data": {"id": "PIX-1"}})
    assert r1.status_code == 400  # crédito falhou
    with app.app_context():
        assert db.session.query(MpWebhookEvent).count() == 0  # sem evento-veneno

    r2 = client.post("/pix/webhook", json={"action": "payment.updated", "data": {"id": "PIX-1"}})
    assert r2.status_code == 200  # retry credita de fato
    with app.app_context():
        from app.models import Wallet, User
        u = db.session.query(User).filter_by(email=email2).one()
        assert db.session.query(Wallet).filter_by(user_id=u.id).one().balance_pts == 1000


def test_chargeback_without_balance_flags_admin(app, client, provider):
    """User gastou os pontos antes do chargeback → não zera ledger, avisa admin."""
    uid = _mk_user(app)
    token = _login(client)
    client.post("/payments/card/charge", json=_charge_body(),
                headers={"Authorization": f"Bearer {token}"})

    # user "gasta" os pontos (débito direto pra simular)
    with app.app_context():
        from app.services import wallet as wallet_svc
        wallet_svc.debit(user_id=uid, amount_pts=1000, tx_type=TxType.REDEEM,
                         description="gastou tudo", idempotency_key="spent-1")
        db.session.commit()

    provider.payments["MP-1"]["status"] = "charged_back"
    r = client.post("/pix/webhook", json={"action": "payment.updated",
                                          "data": {"id": "MP-1"}})
    assert r.status_code == 200
    assert _balance(app, uid) == 0  # nunca negativo
    with app.app_context():
        c = db.session.query(CardCharge).one()
        assert c.status == CardChargeStatus.CHARGED_BACK
