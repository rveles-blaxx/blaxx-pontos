"""Sprint 5 — limites diários e mensais de resgate."""

from __future__ import annotations

import os
from datetime import datetime, timezone

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("MAILER", "noop")

from app import create_app
from app.config import TestConfig
from app.extensions import db
from app.models import User, Wallet


VALID_CPF = "52998224725"


@pytest.fixture
def app():
    a = create_app(TestConfig)
    with a.app_context():
        db.create_all()
        yield a
        db.session.remove()
        db.drop_all()


@pytest.fixture
def client(app):
    return app.test_client()


def _mk_user_with_balance(app, balance, email="ru@test.com", cpf=VALID_CPF):
    with app.app_context():
        u = User(name="Redeem User", email=email, cpf=cpf, role="user")
        u.set_password("StrongP@ss1!")
        u.email_verified_at = datetime.now(timezone.utc)
        # Marca KYC para evitar bloqueios laterais
        u.kyc_validated_at = datetime.now(timezone.utc)
        u.kyc_provider = "test"
        db.session.add(u)
        db.session.flush()
        db.session.add(Wallet(user_id=u.id, balance_pts=balance))
        db.session.commit()
        return u.id


def _login(client, email="ru@test.com"):
    r = client.post("/auth/login", json={"email": email, "password": "StrongP@ss1!"})
    assert r.status_code == 200, r.get_json()
    return r.get_json()["access_token"]


def test_redeem_zero_rejected(app, client):
    _mk_user_with_balance(app, 10_000)
    token = _login(client)
    r = client.post("/redeem/",
                    json={"points": 0, "pix_key": "k@test.com", "password": "StrongP@ss1!"},
                    headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 400


def test_redeem_succeeds_above_min(app, client):
    _mk_user_with_balance(app, 100_000)
    token = _login(client)
    r = client.post("/redeem/",
                    json={"points": 2500, "pix_key": "k@test.com", "password": "StrongP@ss1!"},
                    headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 201, r.get_json()
    body = r.get_json()
    assert body["points_debited"] == 2500


def test_redeem_insufficient_balance(app, client):
    _mk_user_with_balance(app, 500)
    token = _login(client)
    r = client.post("/redeem/",
                    json={"points": 2500, "pix_key": "k@test.com", "password": "StrongP@ss1!"},
                    headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 400


def test_redeem_wrong_password_rejected(app, client):
    _mk_user_with_balance(app, 100_000)
    token = _login(client)
    r = client.post("/redeem/",
                    json={"points": 2500, "pix_key": "k@test.com", "password": "WRONG!"},
                    headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 400
    assert "senha" in r.get_json().get("error", "").lower()
