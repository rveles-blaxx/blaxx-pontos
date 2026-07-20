"""Pacotes de compra editáveis pelo Admin — fonte ÚNICA de preço.

O ponto crítico (dinheiro real): o preço que o Admin edita tem que refletir
TANTO no que o cliente vê (GET /pix/packages) QUANTO no que ele paga
(purchase.create_charge). Estes testes travam essa invariante.
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
from app.models import PointPackage, User, Wallet
from app.services import purchase as purchase_svc

VALID_CPF_A = "52998224725"
VALID_CPF_B = "11144477735"


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


def _mk_user(app, email, cpf, *, role="user"):
    with app.app_context():
        u = User(name="Test", email=email, cpf=cpf, role=role)
        u.set_password("StrongP@ss1!")
        u.email_verified_at = datetime.now(timezone.utc)
        db.session.add(u)
        db.session.flush()
        db.session.add(Wallet(user_id=u.id, balance_pts=0))
        db.session.commit()
        return u.id


def _login(client, email):
    r = client.post("/auth/login", json={"email": email, "password": "StrongP@ss1!"})
    assert r.status_code == 200, r.get_data(as_text=True)
    return r.get_json()["access_token"]


def _auth(tok):
    return {"Authorization": f"Bearer {tok}"}


def test_seed_populates_default_packages(app):
    with app.app_context():
        keys = {p.key for p in db.session.query(PointPackage).all()}
        assert {"start", "plus", "prime", "black"} <= keys
        start = db.session.get(PointPackage, "start")
        assert start.price_cents == 18_000 and start.points == 2_000


def test_public_packages_served_from_db(client):
    data = client.get("/pix/packages").get_json()
    assert data["start"]["price_brl"] == 180.0
    assert data["prime"]["points"] == 12_000


def test_draft_edit_does_not_affect_site_until_publish(app, client):
    _mk_user(app, "adm@x.com", VALID_CPF_A, role="admin")
    tok = _login(client, "adm@x.com")

    # editar no Admin = grava RASCUNHO
    r = client.put(
        "/admin/packages/start",
        json={"price_brl": 199.90, "points": 2_100},
        headers=_auth(tok),
    )
    assert r.status_code == 200, r.get_data(as_text=True)
    j = r.get_json()
    assert j["has_draft"] is True
    assert j["price_brl"] == 180.0             # publicado NÃO mudou
    assert j["draft"]["price_brl"] == 199.90   # rascunho tem o novo

    # o site (público) e a cobrança AINDA veem o valor antigo
    assert client.get("/pix/packages").get_json()["start"]["price_brl"] == 180.0
    with app.app_context():
        assert purchase_svc.list_packages()["start"]["price_brl"] == 180.0

    # PUBLICAR
    rp = client.post("/admin/packages/publish", headers=_auth(tok))
    assert rp.status_code == 200
    assert "start" in rp.get_json()["published"]

    # agora o site E a cobrança refletem (mesma fonte)
    pub = client.get("/pix/packages").get_json()
    assert pub["start"]["price_brl"] == 199.90 and pub["start"]["points"] == 2_100
    with app.app_context():
        row = db.session.get(PointPackage, "start")
        assert row.price_cents == 19_990 and row.has_draft is False
        assert purchase_svc.list_packages()["start"]["price_brl"] == 199.90


def test_discard_draft_reverts(app, client):
    _mk_user(app, "adm@x.com", VALID_CPF_A, role="admin")
    tok = _login(client, "adm@x.com")
    client.put("/admin/packages/plus", json={"price_brl": 999.0}, headers=_auth(tok))
    rd = client.post("/admin/packages/discard", headers=_auth(tok))
    assert rd.status_code == 200 and "plus" in rd.get_json()["discarded"]
    items = client.get("/admin/packages", headers=_auth(tok)).get_json()["items"]
    plus = next(p for p in items if p["key"] == "plus")
    assert plus["has_draft"] is False and plus["price_brl"] == 470.0


def test_admin_edit_rejects_out_of_range(app, client):
    _mk_user(app, "adm@x.com", VALID_CPF_A, role="admin")
    tok = _login(client, "adm@x.com")
    # preço abaixo do mínimo (R$ 0,50)
    r = client.put("/admin/packages/start", json={"price_cents": 50}, headers=_auth(tok))
    assert r.status_code == 400
    # pontos <= 0
    r2 = client.put("/admin/packages/start", json={"points": 0}, headers=_auth(tok))
    assert r2.status_code == 400
    # pacote inexistente
    r3 = client.put("/admin/packages/nope", json={"price_brl": 10}, headers=_auth(tok))
    assert r3.status_code == 404


def test_non_admin_cannot_edit_packages(app, client):
    _mk_user(app, "user@x.com", VALID_CPF_B, role="user")
    tok = _login(client, "user@x.com")
    r = client.put("/admin/packages/start", json={"price_brl": 1.0}, headers=_auth(tok))
    assert r.status_code == 403
    # e o preço não mudou
    assert client.get("/pix/packages").get_json()["start"]["price_brl"] == 180.0
