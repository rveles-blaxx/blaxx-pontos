"""Sprint 4 — testes do MercadoPago hardening (replay store, idempotency)."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from unittest.mock import patch, MagicMock

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("MAILER", "noop")

from app import create_app
from app.config import TestConfig
from app.extensions import db
from app.models import MpWebhookEvent, PixCharge, PixChargeStatus, User, Wallet


VALID_CPF = "52998224725"


class _MPConfig(TestConfig):
    PIX_PROVIDER = "mock"  # webhook MP path testado via hot-patch
    MP_WEBHOOK_SECRET = ""
    TESTING = True


@pytest.fixture
def app():
    a = create_app(_MPConfig)
    with a.app_context():
        db.create_all()
        yield a
        db.session.remove()
        db.drop_all()


@pytest.fixture
def client(app):
    return app.test_client()


def _mk_user_and_charge(app):
    with app.app_context():
        u = User(name="MP Test", email="mp@test.com", cpf=VALID_CPF)
        u.set_password("StrongP@ss1!")
        u.email_verified_at = datetime.now(timezone.utc)
        db.session.add(u)
        db.session.flush()
        db.session.add(Wallet(user_id=u.id, balance_pts=0))
        c = PixCharge(
            user_id=u.id,
            package_key="start",
            amount_cents=1000,
            points_to_credit=100,
            br_code="00020126...",
            txid="test-txid-mp-abc123",
            status=PixChargeStatus.PENDING,
            expires_at=datetime.now(timezone.utc).replace(microsecond=0),
        )
        from datetime import timedelta
        c.expires_at = datetime.now(timezone.utc) + timedelta(minutes=30)
        db.session.add(c)
        db.session.commit()
        return u.id, c.id, c.txid


def test_mp_webhook_replay_store_blocks_duplicate(app, client):
    """Mesmo event_id processado 2x → 2ª chamada retorna ok=true,replay=true."""
    user_id, charge_id, txid = _mk_user_and_charge(app)

    # Hot-patch o provider pra simular MP
    with app.app_context():
        app.extensions["pix_provider"].name = "mercadopago"
        # Stub get_payment retornando approved + external_reference = nosso txid
        app.extensions["pix_provider"].get_payment = lambda mp_id: {
            "status": "approved",
            "external_reference": txid,
        }

        payload = {"action": "payment.updated", "data": {"id": "9999"}}

        # 1ª chamada — processa
        r1 = client.post("/pix/webhook", json=payload)
        assert r1.status_code == 200, r1.get_json()
        body1 = r1.get_json()
        assert "replay" not in body1 or body1.get("replay") is not True

        # 2ª chamada idêntica — replay detectado
        r2 = client.post("/pix/webhook", json=payload)
        assert r2.status_code == 200, r2.get_json()
        body2 = r2.get_json()
        assert body2.get("replay") is True

        # Confirmando que MpWebhookEvent só tem 1 entrada
        events = db.session.query(MpWebhookEvent).all()
        assert len(events) == 1


def test_mp_webhook_different_events_processed_independently(app, client):
    """payment.updated vs payment.created → event_ids diferentes."""
    user_id, charge_id, txid = _mk_user_and_charge(app)

    with app.app_context():
        app.extensions["pix_provider"].name = "mercadopago"
        app.extensions["pix_provider"].get_payment = lambda mp_id: {
            "status": "approved",
            "external_reference": txid,
        }

        r1 = client.post("/pix/webhook", json={
            "action": "payment.updated", "data": {"id": "1234"}
        })
        r2 = client.post("/pix/webhook", json={
            "action": "payment.created", "data": {"id": "1234"}
        })
        assert r1.status_code == 200
        assert r2.status_code == 200
        events = db.session.query(MpWebhookEvent).all()
        assert len(events) == 2


def test_mp_webhook_ignores_non_payment_action(app, client):
    """action=chargeback.created → ignorado sem criar event."""
    with app.app_context():
        app.extensions["pix_provider"].name = "mercadopago"
        r = client.post("/pix/webhook", json={
            "action": "chargeback.created", "data": {"id": "1"}
        })
        assert r.status_code == 200
        assert r.get_json().get("ignored")
        # Nada gravado
        events = db.session.query(MpWebhookEvent).all()
        assert len(events) == 0
