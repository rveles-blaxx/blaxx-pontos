"""Sprint 7 — testes do push notifications (registry + console mode)."""

from __future__ import annotations

import os
from datetime import datetime, timezone

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("MAILER", "noop")

from app import create_app
from app.config import TestConfig
from app.extensions import db
from app.models import PushDevice, User, Wallet


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


def _mk_user(app, email="push@test.com", cpf=VALID_CPF):
    with app.app_context():
        u = User(name="Push Test", email=email, cpf=cpf)
        u.set_password("StrongP@ss1!")
        u.email_verified_at = datetime.now(timezone.utc)
        db.session.add(u)
        db.session.flush()
        db.session.add(Wallet(user_id=u.id, balance_pts=0))
        db.session.commit()
        return db.session.get(User, u.id)


def test_register_device_creates_entry(app):
    user = _mk_user(app)
    with app.app_context():
        from app.services import push
        user_db = db.session.get(User, user.id)
        d = push.register_device(user_db, "token-abc", "ios", app_version="1.2.3")
        assert d.id
        assert d.platform == "ios"
        assert d.app_version == "1.2.3"


def test_register_device_rejects_invalid_platform(app):
    user = _mk_user(app)
    with app.app_context():
        from app.services import push
        user_db = db.session.get(User, user.id)
        with pytest.raises(ValueError):
            push.register_device(user_db, "token-x", "macos")


def test_register_same_token_reassigns(app):
    """Token único globalmente: 2 users diferentes registrando o mesmo token
    → o último vence (caso real: usuário trocou de conta no device)."""
    user1 = _mk_user(app, email="u1@test.com", cpf=VALID_CPF)
    user2 = _mk_user(app, email="u2@test.com", cpf="11144477735")
    with app.app_context():
        from app.services import push
        u1 = db.session.get(User, user1.id)
        u2 = db.session.get(User, user2.id)
        d1 = push.register_device(u1, "same-token", "ios")
        d2 = push.register_device(u2, "same-token", "ios")
        assert d1.id == d2.id  # mesmo registro reatribuído
        # Pertence agora ao u2
        rec = db.session.query(PushDevice).filter_by(token="same-token").one()
        assert rec.user_id == user2.id


def test_send_to_user_console_mode_returns_counter(app):
    user = _mk_user(app)
    with app.app_context():
        from app.services import push
        u = db.session.get(User, user.id)
        push.register_device(u, "t-ios", "ios")
        push.register_device(u, "t-android", "android")
        counters = push.send_to_user(user.id, "Hi", "World", data={"k": "v"})
        # Sem APNS/FCM env vars → tudo cai no console
        assert counters["console"] == 2
        assert counters["sent"] == 0
        assert counters["failed"] == 0


def test_revoke_device(app):
    user = _mk_user(app)
    with app.app_context():
        from app.services import push
        u = db.session.get(User, user.id)
        d = push.register_device(u, "t-revoke", "ios")
        ok = push.revoke_device(d.id, user.id)
        assert ok is True
        # Não aparece em list_user_devices
        active = push.list_user_devices(user.id, include_revoked=False)
        assert not any(dd.id == d.id for dd in active)


def test_register_device_endpoint(app, client):
    _mk_user(app)
    r_login = client.post("/auth/login",
                          json={"email": "push@test.com", "password": "StrongP@ss1!"})
    token = r_login.get_json()["access_token"]
    r = client.post("/push/devices/register",
                    json={"token": "real-token", "platform": "ios", "app_version": "1.0"},
                    headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 201, r.get_json()
    body = r.get_json()
    assert body["platform"] == "ios"
