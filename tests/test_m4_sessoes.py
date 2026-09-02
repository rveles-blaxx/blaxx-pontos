"""M-4: a tela de sessoes mentia.

`RefreshTokenDB` existia e era LIDA por GET /user/sessions, mas nada nunca a
escrevia: a listagem devolvia [] e a revogacao individual 404. E marcar a linha
como revogada nao invalidava o token — quem decide e o blocklist, que consulta
RevokedToken pelo jti.
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
from app.models import RefreshTokenDB, RevokedToken, User, Wallet

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


def _login(app, client, email="s@x.test", cpf="52998224725"):
    with app.app_context():
        u = User(name="S", email=email, cpf=cpf)
        u.set_password(SENHA)
        u.email_verified_at = datetime.now(timezone.utc)
        db.session.add(u); db.session.flush()
        db.session.add(Wallet(user_id=u.id)); db.session.commit()
    r = client.post("/auth/login", json={"email": email, "password": SENHA})
    j = r.get_json()
    return j["token"], j["refresh_token"]


def test_login_registra_a_sessao(app, client):
    """Antes: a tabela nunca era escrita e a lista vinha vazia."""
    tok, _ = _login(app, client)
    r = client.get("/user/sessions", headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 200
    sessoes = r.get_json()["sessions"]
    assert len(sessoes) == 1
    assert sessoes[0]["id"]
    with app.app_context():
        linha = db.session.query(RefreshTokenDB).one()
        assert linha.jti, "sem jti nao da para revogar"


def test_revogar_sessao_invalida_o_token_de_verdade(app, client):
    """Antes: mudava a tela e o token continuava valendo."""
    tok, _ = _login(app, client)
    H = {"Authorization": f"Bearer {tok}"}
    sid = client.get("/user/sessions", headers=H).get_json()["sessions"][0]["id"]

    assert client.delete(f"/user/sessions/{sid}", headers=H).status_code == 200

    with app.app_context():
        linha = db.session.get(RefreshTokenDB, sid)
        assert linha.revoked_at is not None
        assert db.session.get(RevokedToken, linha.jti) is not None, \
            "jti nao entrou no blocklist — o token seguiria valido"

    assert client.get("/user/sessions", headers=H).get_json()["sessions"] == []


def test_sessao_de_outro_usuario_nao_e_revogavel(app, client):
    tok_a, _ = _login(app, client)
    sid = client.get("/user/sessions",
                     headers={"Authorization": f"Bearer {tok_a}"}).get_json()["sessions"][0]["id"]
    tok_b, _ = _login(app, client, email="b@x.test", cpf="15350946056")
    r = client.delete(f"/user/sessions/{sid}", headers={"Authorization": f"Bearer {tok_b}"})
    assert r.status_code == 404


def test_dois_logins_aparecem_como_duas_sessoes(app, client):
    tok, _ = _login(app, client)
    client.post("/auth/login", json={"email": "s@x.test", "password": SENHA})
    r = client.get("/user/sessions", headers={"Authorization": f"Bearer {tok}"})
    assert len(r.get_json()["sessions"]) == 2
