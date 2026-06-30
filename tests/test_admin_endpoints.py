"""Sprint 5 — testes dos endpoints /admin (auth, listings, AML)."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("MAILER", "noop")

from app import create_app
from app.config import TestConfig
from app.extensions import db
from app.models import AmlAlert, User, Wallet


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
    assert r.status_code == 200
    return r.get_json()["access_token"]


def test_admin_requires_admin_role(app, client):
    _mk_user(app, "user@test.com", VALID_CPF_A, role="user")
    token = _login(client, "user@test.com")
    r = client.get("/admin/users", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 403


def test_admin_users_list(app, client):
    _mk_user(app, "admin@test.com", VALID_CPF_A, role="admin")
    _mk_user(app, "user@test.com", VALID_CPF_B, role="user")
    token = _login(client, "admin@test.com")
    r = client.get("/admin/users", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    body = r.get_json()
    assert body["total"] == 2
    assert len(body["items"]) == 2


def test_admin_aml_alerts_empty(app, client):
    _mk_user(app, "admin@test.com", VALID_CPF_A, role="admin")
    token = _login(client, "admin@test.com")
    r = client.get("/admin/aml/alerts", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.get_json()
    body = r.get_json()
    assert body["total"] == 0
    assert body["items"] == []


def test_admin_aml_alerts_list_and_filter(app, client):
    admin_id = _mk_user(app, "admin@test.com", VALID_CPF_A, role="admin")
    user_id = _mk_user(app, "user@test.com", VALID_CPF_B, role="user")
    with app.app_context():
        for sev, kind in [("high", "sanctions"), ("medium", "threshold"),
                          ("low", "velocity")]:
            db.session.add(AmlAlert(
                user_id=user_id, kind=kind, severity=sev,
                payload=json.dumps({"test": sev}),
            ))
        db.session.commit()
    token = _login(client, "admin@test.com")
    # Sem filtro
    r = client.get("/admin/aml/alerts", headers={"Authorization": f"Bearer {token}"})
    assert r.json["total"] == 3
    # Filtro por severity
    r2 = client.get("/admin/aml/alerts?severity=high",
                    headers={"Authorization": f"Bearer {token}"})
    assert r2.json["total"] == 1
    assert r2.json["items"][0]["kind"] == "sanctions"
    # Filtro por kind
    r3 = client.get("/admin/aml/alerts?kind=threshold",
                    headers={"Authorization": f"Bearer {token}"})
    assert r3.json["total"] == 1


def test_admin_aml_alert_resolve(app, client):
    admin_id = _mk_user(app, "admin@test.com", VALID_CPF_A, role="admin")
    user_id = _mk_user(app, "user@test.com", VALID_CPF_B, role="user")
    with app.app_context():
        alert = AmlAlert(user_id=user_id, kind="velocity", severity="medium",
                         payload=json.dumps({}))
        db.session.add(alert)
        db.session.commit()
        alert_id = alert.id
    token = _login(client, "admin@test.com")
    r = client.post(f"/admin/aml/alerts/{alert_id}/resolve",
                    json={"note": "ok, falso positivo"},
                    headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.get_json()
    body = r.get_json()
    assert body["resolved_at"] is not None
    assert body["resolved_by"] == admin_id
    # Resolver de novo retorna erro
    r2 = client.post(f"/admin/aml/alerts/{alert_id}/resolve", json={},
                     headers={"Authorization": f"Bearer {token}"})
    assert r2.status_code == 400
