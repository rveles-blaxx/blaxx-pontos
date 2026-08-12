"""Regressões end-to-end webhook→ledger (Q01/S08 · pré-D06/D07).

Recriam, contra os webhooks NOVOS do corte de gateway (D02), as regressões do
finding #1 da auditoria de segurança de 2026-07-20 que morreram junto com o
endpoint POST /pix/webhook no commit cf1cf8e:

  · crédito idempotente — o mesmo evento entregue 2x não dobra pontos
  · estorno/chargeback debita UMA vez (keys geracionais card-refund-{n})
  · disputa vencida re-credita (card-recredit-{n}) e um SEGUNDO estorno
    debita de novo — sem duplo débito nem duplo crédito
  · autenticação: assinatura Stripe inválida → 401; token Asaas errado → 401
  · evento-veneno: crédito que falha NÃO grava replay-event — o retry do
    provedor reprocessa em vez de cair no dedup e perder o pagamento

Endpoints exercitados (nível HTTP, ledger verificado no banco):
  POST /payments/stripe/webhook   (cartão internacional — corpo ASSINADO)
  POST /payouts/asaas/webhook     (PIX/cobrança — token estático + reconsulta)

Roda com:
    cd backend && python -m pytest tests/test_webhook_ledger_regressions.py -v
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from datetime import datetime, timezone

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("MAILER", "noop")

from app import create_app
from app.config import TestConfig
from app.extensions import db
from app.models import (
    AsaasWebhookEvent,
    CardCharge,
    CardChargeStatus,
    Notification,
    PixCharge,
    PixChargeStatus,
    Transaction,
    TxType,
    User,
    Wallet,
)
from app.pix.provider import CardChargeResponse
from app.pix.stripe_card import StripeCardProvider

VALID_CPF = "52998224725"
ADMIN_CPF = "15350946056"
WEBHOOK_SECRET = "whsec_" + "s" * 32
ASAAS_TOKEN = "tok-webhook-blaxx"


# ─────────────────────────── fakes ─────────────────────────── #

class FakeStripeProvider(StripeCardProvider):
    """Stripe sem rede: `verify_webhook` REAL (HMAC herdado — a autenticação
    do endpoint é testada com criptografia de verdade); pagamentos em memória.

    `payment_status` devolve o status interno programado em `statuses` — é a
    reconsulta que o webhook faz antes de creditar (estado atual manda sobre
    o snapshot do evento)."""

    def __init__(self):
        super().__init__(api_key="sk_test_" + "x" * 24,
                         webhook_secret=WEBHOOK_SECRET)
        self.statuses: dict[str, str] = {}
        self.next_status = "approved"

    def create_card_payment(self, req) -> CardChargeResponse:
        pi_id = f"pi_{len(self.statuses) + 1}"
        self.statuses[pi_id] = self.next_status
        return CardChargeResponse(
            provider_payment_id=pi_id,
            status=self.next_status,
            status_detail="",
            card_brand=req.payment_method_id,
            card_last4="4242",
        )

    def payment_status(self, payment_intent_id: str) -> str:
        return self.statuses[payment_intent_id]


class FakeAsaasProvider:
    """Asaas de mentira: só o que o webhook de cobrança usa (`get_payment`).

    `name = "asaas"` é o que faz `_payout_provider()` do handler devolver esta
    instância — e portanto o teste prova que o crédito sai da RECONSULTA à
    API, não do status que veio no corpo (o webhook do Asaas não é assinado).
    """
    name = "asaas"

    def __init__(self):
        self.payments: dict[str, dict] = {}
        self.get_payment_calls: list[str] = []

    def get_payment(self, payment_id: str) -> dict:
        self.get_payment_calls.append(payment_id)
        return self.payments[payment_id]


# ─────────────────────────── fixtures ─────────────────────────── #

@pytest.fixture
def stripe_provider():
    return FakeStripeProvider()


@pytest.fixture
def asaas_provider():
    return FakeAsaasProvider()


@pytest.fixture
def app(stripe_provider):
    app = create_app(TestConfig)
    app.config["CARD_ENABLED"] = True
    app.extensions["card_intl_provider"] = stripe_provider
    with app.app_context():
        db.create_all()
        yield app
        db.session.remove()
        db.drop_all()


@pytest.fixture
def asaas_app(asaas_provider):
    app = create_app(TestConfig, pix_provider=asaas_provider)
    with app.app_context():
        db.create_all()
        yield app
        db.session.remove()
        db.drop_all()


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def asaas_client(asaas_app):
    return asaas_app.test_client()


# ─────────────────────────── helpers ─────────────────────────── #

def _mk_user(app, email="card@test.com", cpf=VALID_CPF, role="user",
             balance_pts=0) -> str:
    with app.app_context():
        u = User(name="Webhook User", email=email, cpf=cpf, role=role)
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


def _tx_keys(app, reference) -> set[str]:
    with app.app_context():
        return {t.idempotency_key for t in
                db.session.query(Transaction).filter_by(reference=reference)}


def _mk_card_charge(app, client, provider, status="approved") -> tuple[str, str]:
    """Cria uma compra de cartão via API. Retorna (charge_id, pi_id).

    R$ 90,00 = 9000 cents → 1000 pts (9 cents/pt)."""
    provider.next_status = status
    token = _login(client)
    r = client.post(
        "/payments/card/charge",
        json={"amount_brl": 90.0, "card_token": "tok_ok_123",
              "payment_method_id": "visa", "installments": 1},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 201, r.get_json()
    j = r.get_json()
    with app.app_context():
        pi = db.session.get(CardCharge, j["id"]).provider_payment_id
    return j["id"], pi


# --- Stripe: assinatura e eventos --- #

def _sign(body: bytes, secret: str = WEBHOOK_SECRET, timestamp=None) -> str:
    ts = str(int(timestamp or time.time()))
    sig = hmac.new(secret.encode(), f"{ts}.".encode() + body,
                   hashlib.sha256).hexdigest()
    return f"t={ts},v1={sig}"


def _post_stripe(client, event: dict, secret: str = WEBHOOK_SECRET):
    body = json.dumps(event).encode()
    return client.post("/payments/stripe/webhook", data=body,
                       content_type="application/json",
                       headers={"Stripe-Signature": _sign(body, secret)})


def _ev_succeeded(pi_id):
    return {"type": "payment_intent.succeeded",
            "data": {"object": {"object": "payment_intent", "id": pi_id}}}


def _ev_refunded(pi_id):
    return {"type": "charge.refunded",
            "data": {"object": {"object": "charge", "id": "ch_1",
                                "payment_intent": pi_id}}}


def _ev_dispute(pi_id):
    return {"type": "charge.dispute.created",
            "data": {"object": {"object": "dispute", "id": "dp_1",
                                "payment_intent": pi_id}}}


# --- Asaas: eventos de cobrança --- #

def _asaas_event(event_id, event, payment_id, txid, body_status="RECEIVED"):
    return {"id": event_id, "event": event,
            "payment": {"id": payment_id, "externalReference": txid,
                        "status": body_status}}


def _post_asaas(client, payload, token=None):
    headers = {"asaas-access-token": token} if token is not None else {}
    return client.post("/payouts/asaas/webhook", json=payload, headers=headers)


def _mk_pix_charge(app, email="pix@test.com", cpf=VALID_CPF) -> tuple[str, str]:
    """Cria user + PixCharge PENDING direto no banco. Retorna (user_id, txid)."""
    uid = _mk_user(app, email=email, cpf=cpf)
    with app.app_context():
        ch = PixCharge(user_id=uid, package_key="custom", amount_cents=9000,
                       points_to_credit=1000, br_code="brcode-teste",
                       expires_at=PixCharge.make_expiry(1800))
        db.session.add(ch)
        db.session.commit()
        return uid, ch.txid


# ============================================================================
# Stripe — autenticação do endpoint
# ============================================================================

def test_stripe_sem_provider_configurado_404(app, client):
    """Sem STRIPE_API_KEY não existe rota útil — e principalmente não existe
    caminho não autenticado até o ledger."""
    app.extensions["card_intl_provider"] = None
    r = _post_stripe(client, _ev_succeeded("pi_1"))
    assert r.status_code == 404


def test_stripe_assinatura_invalida_401_sem_tocar_ledger(app, client, stripe_provider):
    uid = _mk_user(app)
    charge_id, pi = _mk_card_charge(app, client, stripe_provider, status="in_process")
    stripe_provider.statuses[pi] = "approved"

    # segredo errado
    r = _post_stripe(client, _ev_succeeded(pi), secret="whsec_" + "o" * 32)
    assert r.status_code == 401

    # corpo adulterado (assinado sobre outro corpo)
    body = json.dumps(_ev_succeeded(pi)).encode()
    tampered = json.dumps(_ev_succeeded("pi_de_outro")).encode()
    r = client.post("/payments/stripe/webhook", data=tampered,
                    content_type="application/json",
                    headers={"Stripe-Signature": _sign(body)})
    assert r.status_code == 401

    # sem header nenhum
    r = client.post("/payments/stripe/webhook", data=body,
                    content_type="application/json")
    assert r.status_code == 401

    assert _balance(app, uid) == 0, "evento não autenticado nunca credita"


# ============================================================================
# Stripe — crédito idempotente
# ============================================================================

def test_stripe_succeeded_credita_uma_vez_replay_nao_dobra(app, client, stripe_provider):
    uid = _mk_user(app)
    charge_id, pi = _mk_card_charge(app, client, stripe_provider, status="in_process")
    assert _balance(app, uid) == 0

    # O PI agora está succeeded na Stripe (reconsulta confirma).
    stripe_provider.statuses[pi] = "approved"
    ev = _ev_succeeded(pi)

    r = _post_stripe(client, ev)
    assert r.status_code == 200, r.get_json()
    assert _balance(app, uid) == 1000

    # Entrega "at least once": o MESMO evento chega de novo.
    r = _post_stripe(client, ev)
    assert r.status_code == 200
    assert _balance(app, uid) == 1000, "replay do webhook não pode dobrar pontos"
    assert _tx_keys(app, charge_id) == {f"card-charge:{charge_id}"}


def test_stripe_succeeded_mas_pi_nao_aprovado_nao_credita(app, client, stripe_provider):
    """O corpo do evento é snapshot; a reconsulta é quem manda. Um succeeded
    atrasado/fora de ordem de um PI que regrediu não credita."""
    uid = _mk_user(app)
    _, pi = _mk_card_charge(app, client, stripe_provider, status="in_process")
    stripe_provider.statuses[pi] = "in_process"  # ainda não liquidou

    r = _post_stripe(client, _ev_succeeded(pi))
    assert r.status_code == 200
    assert _balance(app, uid) == 0


# ============================================================================
# Stripe — estorno / chargeback debitam UMA vez
# ============================================================================

@pytest.mark.parametrize("mk_event,expected", [
    (_ev_refunded, CardChargeStatus.REFUNDED),
    (_ev_dispute, CardChargeStatus.CHARGED_BACK),
])
def test_stripe_estorno_debita_uma_vez_replay_nao_dobra(
        app, client, stripe_provider, mk_event, expected):
    uid = _mk_user(app)
    charge_id, pi = _mk_card_charge(app, client, stripe_provider)
    assert _balance(app, uid) == 1000

    r = _post_stripe(client, mk_event(pi))
    assert r.status_code == 200, r.get_json()
    assert _balance(app, uid) == 0
    with app.app_context():
        assert db.session.get(CardCharge, charge_id).status == expected

    # Replay do mesmo evento: não debita de novo (saldo nunca negativa).
    r = _post_stripe(client, mk_event(pi))
    assert r.status_code == 200
    assert _balance(app, uid) == 0
    assert _tx_keys(app, charge_id) == {
        f"card-charge:{charge_id}", f"card-refund:{charge_id}"}


# ============================================================================
# Stripe — disputa vencida: re-crédito e 2º estorno (keys geracionais)
# ============================================================================

def test_stripe_disputa_vencida_recredita_e_segundo_estorno_debita(
        app, client, stripe_provider):
    """Regressão (auditoria 2026-07-20, finding #1): ciclo completo de disputa.

    chargeback (geração 0) → disputa vencida re-credita (card-recredit-1) →
    SEGUNDO estorno debita de novo (card-refund-1). Sem as keys geracionais o
    2º estorno reusaria card-refund:{id}, já consumida, e viraria no-op — o
    cliente reteria pontos estornados uma 2ª vez pelo gateway (perda de
    dinheiro silenciosa). E o re-crédito reusaria card-charge:{id}, deixando o
    cliente sem os pontos apesar de a disputa ter sido ganha."""
    uid = _mk_user(app)
    charge_id, pi = _mk_card_charge(app, client, stripe_provider)
    assert _balance(app, uid) == 1000

    # 1) chargeback → debita (estorno geração 0)
    r = _post_stripe(client, _ev_dispute(pi))
    assert r.status_code == 200
    assert _balance(app, uid) == 0

    # 2) disputa vencida → PI volta a succeeded → re-credita (crédito geração 1)
    stripe_provider.statuses[pi] = "approved"
    r = _post_stripe(client, _ev_succeeded(pi))
    assert r.status_code == 200, r.get_json()
    assert _balance(app, uid) == 1000, \
        "disputa vencida precisa devolver os pontos (card-recredit-1)"

    # 3) SEGUNDO estorno → debita de novo (estorno geração 1)
    r = _post_stripe(client, _ev_refunded(pi))
    assert r.status_code == 200, r.get_json()
    assert _balance(app, uid) == 0, \
        "2º estorno após re-crédito precisa debitar (card-refund-1); " \
        "no-op aqui = cliente fica com pontos estornados"

    # 4) replay do 2º estorno: nada muda
    r = _post_stripe(client, _ev_refunded(pi))
    assert r.status_code == 200
    assert _balance(app, uid) == 0

    with app.app_context():
        assert db.session.get(CardCharge, charge_id).status == CardChargeStatus.REFUNDED
    assert _tx_keys(app, charge_id) == {
        f"card-charge:{charge_id}",
        f"card-refund:{charge_id}",
        f"card-recredit-1:{charge_id}",
        f"card-refund-1:{charge_id}",
    }, "cada geração do ciclo de disputa consome exatamente uma key"


def test_stripe_chargeback_sem_saldo_nao_negativa_e_avisa_admin(
        app, client, stripe_provider):
    """User gastou os pontos antes do chargeback → ledger nunca fica negativo;
    o caso vai pra fila manual (notificação de admin)."""
    admin_id = _mk_user(app, email="admin@test.com", cpf=ADMIN_CPF, role="admin")
    uid = _mk_user(app)
    charge_id, pi = _mk_card_charge(app, client, stripe_provider)

    with app.app_context():
        from app.services import wallet as wallet_svc
        wallet_svc.debit(user_id=uid, amount_pts=1000, tx_type=TxType.REDEEM,
                         description="gastou tudo", idempotency_key="spent-1")
        db.session.commit()

    r = _post_stripe(client, _ev_dispute(pi))
    assert r.status_code == 200
    assert _balance(app, uid) == 0, "estorno sem saldo não pode negativar o ledger"
    with app.app_context():
        assert db.session.get(CardCharge, charge_id).status == CardChargeStatus.CHARGED_BACK
        notif = (db.session.query(Notification)
                 .filter_by(user_id=admin_id, reference=charge_id).one())
        assert "estornar" in notif.body.lower()


# ============================================================================
# Stripe — falha de processamento → 500 → a Stripe reenvia (evento-veneno)
# ============================================================================

def test_stripe_falha_no_credito_devolve_500_e_retry_credita(
        app, client, stripe_provider, monkeypatch):
    """Se o crédito falhar no meio, o endpoint devolve 500 — a Stripe reenvia
    com backoff e o retry credita. O par com o Asaas: lá o dedup é por evento
    e a proteção é não gravar o replay-event; aqui não há replay-store, então
    a proteção é NUNCA responder 2xx para um evento cujo efeito não aconteceu."""
    from app.services import card_purchase as card_svc

    uid = _mk_user(app)
    _, pi = _mk_card_charge(app, client, stripe_provider, status="in_process")
    stripe_provider.statuses[pi] = "approved"

    real = card_svc.apply_webhook_status
    calls = {"n": 0}

    def flaky(charge, status, status_detail=""):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("falha simulada no crédito")
        return real(charge, status, status_detail=status_detail)

    monkeypatch.setattr(card_svc, "apply_webhook_status", flaky)

    r = _post_stripe(client, _ev_succeeded(pi))
    assert r.status_code == 500, "efeito não aplicado não pode virar 2xx"
    assert _balance(app, uid) == 0

    r = _post_stripe(client, _ev_succeeded(pi))
    assert r.status_code == 200
    assert _balance(app, uid) == 1000, "o reenvio da Stripe completa o crédito"


# ============================================================================
# Asaas — autenticação do endpoint
# ============================================================================

def test_asaas_token_errado_401_sem_tocar_ledger(asaas_app, asaas_client,
                                                 asaas_provider):
    asaas_app.config["ASAAS_WEBHOOK_TOKEN"] = ASAAS_TOKEN
    uid, txid = _mk_pix_charge(asaas_app)
    asaas_provider.payments["pay_1"] = {"status": "RECEIVED"}
    ev = _asaas_event("evt_1", "PAYMENT_RECEIVED", "pay_1", txid)

    r = _post_asaas(asaas_client, ev, token="token-forjado")
    assert r.status_code == 401

    r = _post_asaas(asaas_client, ev, token=None)  # sem header
    assert r.status_code == 401

    assert _balance(asaas_app, uid) == 0
    with asaas_app.app_context():
        assert db.session.query(AsaasWebhookEvent).count() == 0

    # Com o token certo o mesmo evento passa e credita.
    r = _post_asaas(asaas_client, ev, token=ASAAS_TOKEN)
    assert r.status_code == 200, r.get_json()
    assert _balance(asaas_app, uid) == 1000


# ============================================================================
# Asaas — crédito idempotente (dedup por evento + key do ledger)
# ============================================================================

def test_asaas_received_credita_uma_vez_dedup_e_replay(asaas_app, asaas_client,
                                                       asaas_provider):
    uid, txid = _mk_pix_charge(asaas_app)
    asaas_provider.payments["pay_1"] = {"status": "RECEIVED"}
    ev = _asaas_event("evt_1", "PAYMENT_RECEIVED", "pay_1", txid)

    r = _post_asaas(asaas_client, ev)
    assert r.status_code == 200, r.get_json()
    assert _balance(asaas_app, uid) == 1000

    # Mesmo event id de novo (entrega at-least-once) → replay-store barra.
    r = _post_asaas(asaas_client, ev)
    assert r.status_code == 200
    assert r.get_json().get("replay") is True
    assert _balance(asaas_app, uid) == 1000

    # Event id NOVO para o mesmo pagamento (o Asaas gera outro id em cada
    # tentativa) → passa do dedup, mas confirm_payment é idempotente.
    r = _post_asaas(asaas_client,
                    _asaas_event("evt_2", "PAYMENT_RECEIVED", "pay_1", txid))
    assert r.status_code == 200
    assert _balance(asaas_app, uid) == 1000, \
        "evento novo do mesmo pagamento não pode creditar de novo"

    with asaas_app.app_context():
        charge = db.session.query(PixCharge).filter_by(txid=txid).one()
        assert charge.status == PixChargeStatus.PAID
        txs = (db.session.query(Transaction)
               .filter_by(reference=charge.id, type=TxType.PURCHASE).count())
        assert txs == 1


def test_asaas_status_do_corpo_nao_manda_reconsulta_decide(asaas_app, asaas_client,
                                                           asaas_provider):
    """O webhook do Asaas NÃO é assinado — quem tiver o token não pode, com um
    corpo forjado dizendo RECEIVED, creditar um pagamento que o Asaas ainda
    não confirmou. A fonte de verdade é a reconsulta GET /v3/payments/{id}."""
    uid, txid = _mk_pix_charge(asaas_app)
    asaas_provider.payments["pay_1"] = {"status": "PENDING"}  # verdade na API

    r = _post_asaas(asaas_client,
                    _asaas_event("evt_1", "PAYMENT_RECEIVED", "pay_1", txid,
                                 body_status="RECEIVED"))
    assert r.status_code == 200
    assert asaas_provider.get_payment_calls == ["pay_1"], \
        "o handler precisa reconsultar a API antes de creditar"
    assert _balance(asaas_app, uid) == 0, \
        "corpo forjado/adiantado não credita — só o status da API"
    with asaas_app.app_context():
        charge = db.session.query(PixCharge).filter_by(txid=txid).one()
        assert charge.status == PixChargeStatus.PENDING


# ============================================================================
# Asaas — evento-veneno: falha no crédito NÃO grava replay-event
# ============================================================================

def test_asaas_falha_no_credito_nao_grava_replay_event(asaas_app, asaas_client,
                                                       asaas_provider, monkeypatch):
    """Se o crédito falhar, o AsaasWebhookEvent NÃO pode ser gravado — senão o
    reenvio do Asaas cai no dedup como replay e o pagamento se perde para
    sempre (evento-veneno). O evento só entra no replay-store DEPOIS do efeito."""
    from app.services import purchase as purchase_svc

    uid, txid = _mk_pix_charge(asaas_app)
    asaas_provider.payments["pay_1"] = {"status": "RECEIVED"}
    ev = _asaas_event("evt_1", "PAYMENT_RECEIVED", "pay_1", txid)

    real = purchase_svc.confirm_payment
    calls = {"n": 0}

    def flaky(t, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise purchase_svc.PixError("falha simulada no crédito")
        return real(t, **kw)

    monkeypatch.setattr(purchase_svc, "confirm_payment", flaky)

    # 1ª entrega: crédito falha. Resposta é 200 (15 falhas seguidas derrubam a
    # fila inteira do Asaas), mas o evento NÃO entra no replay-store.
    r = _post_asaas(asaas_client, ev)
    assert r.status_code == 200
    assert r.get_json().get("deferred") is True
    assert _balance(asaas_app, uid) == 0
    with asaas_app.app_context():
        assert db.session.query(AsaasWebhookEvent).count() == 0, \
            "gravar o evento com o crédito falhado = evento-veneno"

    # Reenvio do Asaas (mesmo event id): reprocessa e credita de verdade.
    r = _post_asaas(asaas_client, ev)
    assert r.status_code == 200
    assert r.get_json().get("replay") is None
    assert _balance(asaas_app, uid) == 1000
    with asaas_app.app_context():
        assert db.session.query(AsaasWebhookEvent).count() == 1


# ============================================================================
# Asaas — reversão é conciliação manual (política explícita)
# ============================================================================

def test_asaas_pagamento_revertido_nao_mexe_no_ledger(asaas_app, asaas_client,
                                                      asaas_provider):
    """PAYMENT_REFUNDED/CHARGEBACK no Asaas NÃO debita automaticamente — os
    pontos podem já ter sido resgatados em PIX (dinheiro real); a decisão de
    2026-08 é conciliação manual. Este teste trava a política: se alguém
    plugar débito automático aqui, precisa vir trocar este teste junto."""
    uid, txid = _mk_pix_charge(asaas_app)
    asaas_provider.payments["pay_1"] = {"status": "RECEIVED"}
    _post_asaas(asaas_client, _asaas_event("evt_1", "PAYMENT_RECEIVED", "pay_1", txid))
    assert _balance(asaas_app, uid) == 1000

    r = _post_asaas(asaas_client,
                    _asaas_event("evt_2", "PAYMENT_REFUNDED", "pay_1", txid,
                                 body_status="REFUNDED"))
    assert r.status_code == 200
    assert _balance(asaas_app, uid) == 1000, \
        "reversão no Asaas é conciliação manual — sem débito automático"
    with asaas_app.app_context():
        charge = db.session.query(PixCharge).filter_by(txid=txid).one()
        refunds = (db.session.query(Transaction)
                   .filter_by(reference=charge.id, type=TxType.REFUND).count())
        assert refunds == 0
        # O evento é registrado (processado = logado p/ conciliação manual).
        assert db.session.get(AsaasWebhookEvent, "evt_2") is not None
