"""M-2: resgate de beneficio — idempotencia, estoque e gates.

Ficou urgente em 02/09: o catalogo foi populado em producao, entao o endpoint
saiu do papel. Antes: a chave de idempotencia embutia o timestamp da chamada,
double-tap debitava duas vezes; `stock -= 1` sem lock permitia overselling; e
faltavam gate de e-mail e rate limit que /redeem e /transfer ja tinham.
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
from app.models import Benefit, Transaction, User, Voucher, Wallet

SENHA = "StrongP@ss1!"


@pytest.fixture
def app():
    app = create_app(TestConfig)
    with app.app_context():
        db.create_all()
        yield app
        db.session.remove()
        db.drop_all()


@pytest.fixture
def client(app):
    return app.test_client()


def _user(app, client, saldo=10_000, verificado=True):
    with app.app_context():
        u = User(name="C", email="c@x.test", cpf="52998224725")
        u.set_password(SENHA)
        if verificado:
            u.email_verified_at = datetime.now(timezone.utc)
        db.session.add(u); db.session.flush()
        db.session.add(Wallet(user_id=u.id, balance_pts=saldo))
        db.session.commit()
        uid = u.id
    t = client.post("/auth/login", json={"email": "c@x.test", "password": SENHA}).get_json()["token"]
    return uid, {"Authorization": f"Bearer {t}"}


def _beneficio(app, custo=1_000, estoque=-1):
    with app.app_context():
        b = Benefit(name="Voucher", category="voucher", cost_pts=custo,
                    stock=estoque, expires_in_days=90)
        db.session.add(b); db.session.commit()
        return b.id


def test_double_tap_nao_debita_duas_vezes(app, client):
    """O caso do achado: mesma chave, um debito, um voucher."""
    uid, H = _user(app, client)
    bid = _beneficio(app)
    h = H | {"Idempotency-Key": "toque-1"}

    r1 = client.post(f"/benefits/{bid}/redeem", headers=h)
    r2 = client.post(f"/benefits/{bid}/redeem", headers=h)
    assert r1.status_code == 201 and r2.status_code == 200
    assert r1.get_json()["code"] == r2.get_json()["code"]

    with app.app_context():
        assert db.session.query(Voucher).count() == 1
        w = db.session.query(Wallet).filter_by(user_id=uid).one()
        assert w.balance_pts == 9_000                      # debitou uma vez
        assert db.session.query(Transaction).count() == 1


def test_sem_header_o_minuto_colapsa_o_double_tap(app, client):
    uid, H = _user(app, client)
    bid = _beneficio(app)
    client.post(f"/benefits/{bid}/redeem", headers=H)
    client.post(f"/benefits/{bid}/redeem", headers=H)
    with app.app_context():
        assert db.session.query(Voucher).count() == 1
        assert db.session.query(Wallet).filter_by(user_id=uid).one().balance_pts == 9_000


def test_estoque_nao_fica_negativo(app, client):
    _, H = _user(app, client, saldo=100_000)
    bid = _beneficio(app, custo=1_000, estoque=2)
    for i in range(4):
        client.post(f"/benefits/{bid}/redeem", headers=H | {"Idempotency-Key": f"k{i}"})
    with app.app_context():
        b = db.session.get(Benefit, bid)
        assert b.stock == 0
        assert db.session.query(Voucher).count() == 2      # nao vendeu 4


def test_email_nao_verificado_e_barrado(app, client):
    _, H = _user(app, client, verificado=False)
    bid = _beneficio(app)
    r = client.post(f"/benefits/{bid}/redeem", headers=H)
    assert r.status_code == 403
    with app.app_context():
        assert db.session.query(Voucher).count() == 0


def test_saldo_insuficiente_nao_consome_estoque(app, client):
    """Rollback explicito: o decremento do estoque tem de voltar."""
    _, H = _user(app, client, saldo=10)
    bid = _beneficio(app, custo=5_000, estoque=3)
    r = client.post(f"/benefits/{bid}/redeem", headers=H)
    assert r.status_code == 402
    with app.app_context():
        assert db.session.get(Benefit, bid).stock == 3     # intacto
        assert db.session.query(Voucher).count() == 0
