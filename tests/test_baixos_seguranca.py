"""Achados BAIXOS da revisao de 20/07 — os que tinham efeito real.

Dois nao eram baixos:
  B-10 · 8 conexoes SSE esgotavam os 8 slots (2 workers x 4 threads) e o
         servico parava de responder. Negacao de servico com 8 usuarios.
  B-2  · conta so-Google nunca conseguia se excluir: direito da LGPD art. 18
         inacessivel para esse grupo.
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
    app = create_app(TestConfig)
    with app.app_context():
        db.create_all()
        yield app
        db.session.remove()
        db.drop_all()


@pytest.fixture
def client(app):
    return app.test_client()


def _mk(app, email, cpf, com_senha=True, phone=None):
    with app.app_context():
        u = User(name="U Silva", email=email, cpf=cpf, phone=phone)
        if com_senha:
            u.set_password(SENHA)
        u.email_verified_at = datetime.now(timezone.utc)
        if phone:
            u.phone_verified = True
        db.session.add(u); db.session.flush()
        db.session.add(Wallet(user_id=u.id)); db.session.commit()
        return u.id


def _tok(client, email):
    return client.post("/auth/login",
                       json={"email": email, "password": SENHA}).get_json()["token"]


# ───────────────────────── B-1 ─────────────────────────

def test_b1_prefixo_colidente_recusa_em_vez_de_estourar(app):
    """Antes: MultipleResultsFound -> 500 nao tratado. Aniversario sobre 32 bits."""
    from app.services.transfer import find_recipient
    with app.app_context():
        pref = "abcd1234"
        for i, cpf in enumerate(("52998224725", "15350946056")):
            u = User(id=f"{pref}{i:024d}", name="Colide X", email=f"c{i}@x.test", cpf=cpf)
            u.set_password(SENHA)
            db.session.add(u); db.session.flush()
            db.session.add(Wallet(user_id=u.id))
        db.session.commit()
        assert find_recipient(pref.upper()) is None      # recusa, nao 500


def test_b1_prefixo_unico_continua_achando(app):
    from app.services.transfer import find_recipient
    uid = _mk(app, "unico@x.test", "52998224725")
    with app.app_context():
        assert find_recipient(uid[:8].upper()).id == uid


# ───────────────────────── B-3 ─────────────────────────

def test_b3_ativar_2fa_exige_senha(app, client):
    """Sessao sequestrada ativava 2FA e trancava o dono fora."""
    _mk(app, "t@x.test", "52998224725", phone="+5511999998888")
    H = {"Authorization": f"Bearer {_tok(client, 't@x.test')}"}

    r = client.post("/user/2fa/sms/enable", json={}, headers=H)
    assert r.status_code == 403
    assert r.get_json()["code"] == "invalid_password"

    ok = client.post("/user/2fa/sms/enable", json={"password": SENHA}, headers=H)
    assert ok.status_code == 200


# ───────────────────────── B-2 ─────────────────────────

def test_b2_conta_google_nao_fica_presa(app, client):
    """Sem senha, `check_password` era sempre False e a conta nao se excluia."""
    uid = _mk(app, "g@x.test", "52998224725", com_senha=False)
    with app.app_context():
        assert db.session.get(User, uid).password_hash is None

    from flask_jwt_extended import create_access_token
    with app.app_context():
        tok = create_access_token(identity=uid)
    H = {"Authorization": f"Bearer {tok}"}

    r = client.delete("/auth/account",
                      json={"password": "", "confirm": "EXCLUIR MINHA CONTA"},
                      headers=H)
    # Antes: 401 "Senha incorreta", sem saida. Agora: diz o caminho.
    assert r.status_code == 400
    assert r.get_json()["code"] == "google_reauth_required"


def test_b2_conta_com_senha_segue_exigindo_senha(app, client):
    _mk(app, "s@x.test", "15350946056")
    H = {"Authorization": f"Bearer {_tok(client, 's@x.test')}"}
    r = client.delete("/auth/account",
                      json={"password": "errada", "confirm": "EXCLUIR MINHA CONTA"},
                      headers=H)
    assert r.status_code == 401
