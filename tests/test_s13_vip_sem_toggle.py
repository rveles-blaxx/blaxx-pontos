"""S13: PATCH /admin/users/<id>/vip alternava is_vip com corpo vazio.

Uma sondagem da varredura administrativa bateu no endpoint so para ver o
status HTTP e inverteu o is_vip de uma conta real em producao (conta de teste,
restaurada). is_vip governa o teto diario de resgate — uma rota que muda esse
teto nao pode mudar estado sem instrucao explicita.

Nenhum chamador real depende do toggle: PWA (admin.js) e iOS (API.swift)
sempre mandam {"is_vip": true/false} no corpo.
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
from app.models import User, Wallet

SENHA = "StrongP@ss1!"


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


def _mk_user(app, email, cpf, *, role="user", is_vip=False):
    with app.app_context():
        u = User(name="Test", email=email, cpf=cpf, role=role, is_vip=is_vip)
        u.set_password(SENHA)
        u.email_verified_at = datetime.now(timezone.utc)
        db.session.add(u)
        db.session.flush()
        db.session.add(Wallet(user_id=u.id, balance_pts=0))
        db.session.commit()
        return u.id


def _login(client, email):
    r = client.post("/auth/login", json={"email": email, "password": SENHA})
    assert r.status_code == 200
    return r.get_json()["access_token"]


def test_corpo_vazio_e_rejeitado_nao_alterna(app, client):
    """O bug em si: corpo vazio nao pode mais mudar nada."""
    _mk_user(app, "admin@x.test", "52998224725", role="admin")
    uid = _mk_user(app, "alvo@x.test", "11144477735", is_vip=True)
    tok = _login(client, "admin@x.test")

    r = client.patch(f"/admin/users/{uid}/vip", json={},
                      headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 400

    with app.app_context():
        assert db.session.get(User, uid).is_vip is True, (
            "corpo vazio nao pode mudar is_vip — antes ele alternava para False"
        )


def test_corpo_sem_json_tambem_e_rejeitado(app, client):
    """Content-Type errado / body ausente cai no mesmo caminho de rejeicao."""
    _mk_user(app, "admin@x.test", "52998224725", role="admin")
    uid = _mk_user(app, "alvo@x.test", "11144477735", is_vip=False)
    tok = _login(client, "admin@x.test")

    r = client.patch(f"/admin/users/{uid}/vip",
                      headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 400

    with app.app_context():
        assert db.session.get(User, uid).is_vip is False


def test_valor_explicito_e_aplicado(app, client):
    _mk_user(app, "admin@x.test", "52998224725", role="admin")
    uid = _mk_user(app, "alvo@x.test", "11144477735", is_vip=False)
    tok = _login(client, "admin@x.test")

    r = client.patch(f"/admin/users/{uid}/vip", json={"is_vip": True},
                      headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 200
    assert r.get_json()["is_vip"] is True

    r2 = client.patch(f"/admin/users/{uid}/vip", json={"is_vip": False},
                       headers={"Authorization": f"Bearer {tok}"})
    assert r2.status_code == 200
    assert r2.get_json()["is_vip"] is False
