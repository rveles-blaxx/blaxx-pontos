"""B-5: envio legitimo repetido era engolido em silencio.

Sem chave do cliente, dois envios identicos dentro da janela devolviam a
PRIMEIRA transferencia com 201. O duplo-clique ficava resolvido — mas quem
quisesse mesmo enviar duas vezes (dividir uma conta em parcelas iguais) via
"enviado" duas vezes e so uma acontecia. O remetente acha que pagou.
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
from app.models import Transfer, User, Wallet

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


def _mk(app, email, cpf, saldo=0):
    with app.app_context():
        u = User(name="U Silva", email=email, cpf=cpf, pix_key=email)
        u.set_password(SENHA)
        u.email_verified_at = datetime.now(timezone.utc)
        db.session.add(u); db.session.flush()
        db.session.add(Wallet(user_id=u.id, balance_pts=saldo))
        db.session.commit()
        return u.id


def _envio(client, tok, extra=None):
    h = {"Authorization": f"Bearer {tok}"}
    if extra:
        h.update(extra)
    return client.post("/transfer/", json={
        "to": "b@x.test", "amount_pts": 500, "password": SENHA}, headers=h)


def test_duplicata_sem_chave_devolve_409_e_nao_finge_sucesso(app, client):
    _mk(app, "a@x.test", "52998224725", saldo=10_000)
    _mk(app, "b@x.test", "15350946056")
    tok = client.post("/auth/login",
                      json={"email": "a@x.test", "password": SENHA}).get_json()["token"]

    r1 = _envio(client, tok)
    assert r1.status_code == 201
    r2 = _envio(client, tok)
    assert r2.status_code == 409, "antes devolvia 201 com a transferencia anterior"
    corpo = r2.get_json()
    assert corpo["code"] == "DUPLICATE_SUSPECTED"
    assert corpo["previous_transfer_id"] == r1.get_json()["id"]

    with app.app_context():
        assert db.session.query(Transfer).count() == 1


def test_com_chave_propria_o_segundo_envio_acontece(app, client):
    """O caminho de saida: quem quer mesmo repetir, confirma com a chave."""
    _mk(app, "a@x.test", "52998224725", saldo=10_000)
    _mk(app, "b@x.test", "15350946056")
    tok = client.post("/auth/login",
                      json={"email": "a@x.test", "password": SENHA}).get_json()["token"]

    r1 = _envio(client, tok, {"Idempotency-Key": "parcela-1"})
    r2 = _envio(client, tok, {"Idempotency-Key": "parcela-2"})
    assert r1.status_code == 201 and r2.status_code == 201
    assert r1.get_json()["id"] != r2.get_json()["id"]

    with app.app_context():
        assert db.session.query(Transfer).count() == 2
        # O destinatario NAO e creditado na hora: P2P tem janela de 60s de
        # cancelamento, e a promocao acontece depois. O que importa aqui e que
        # os DOIS envios existem — antes, o segundo era engolido.
        remetente = db.session.query(User).filter_by(email="a@x.test").one()
        assert db.session.query(Wallet).filter_by(
            user_id=remetente.id).one().balance_pts == 9_000


def test_mesma_chave_repetida_continua_idempotente(app, client):
    """A idempotencia real nao pode ter sido quebrada pela correcao."""
    _mk(app, "a@x.test", "52998224725", saldo=10_000)
    _mk(app, "b@x.test", "15350946056")
    tok = client.post("/auth/login",
                      json={"email": "a@x.test", "password": SENHA}).get_json()["token"]

    r1 = _envio(client, tok, {"Idempotency-Key": "mesma"})
    r2 = _envio(client, tok, {"Idempotency-Key": "mesma"})
    assert r1.status_code == 201 and r2.status_code == 201
    assert r1.get_json()["id"] == r2.get_json()["id"]
    with app.app_context():
        assert db.session.query(Transfer).count() == 1
