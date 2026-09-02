"""M-6 (enumeracao no cadastro + oraculo de temporizacao) e M-7 (whitelist de IP).

M-6: /auth/register respondia com mensagem distinta por campo, permitindo
verificar se um CPF ou celular pertence a cliente BlaXx — CPF e' dado sensivel
sob LGPD. E o login nao executava o Argon2 quando o usuario nao existia, o que
devolvia a resposta ordens de magnitude mais rapido.

M-7: a whitelist de IP do webhook lia o PRIMEIRO elemento de X-Forwarded-For,
escrito pelo cliente. Ja corrigido junto do A-1; este teste trava a regressao.
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timezone

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("MAILER", "noop")

from app import create_app
from app.config import TestConfig
from app.extensions import db
from app.models import User, Wallet

SENHA = "StrongP@ss1!"
CPF_A, CPF_B = "52998224725", "15350946056"


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


def _existente(app, email="dono@x.test", cpf=CPF_A, phone="+5511999998888"):
    with app.app_context():
        u = User(name="Dono Silva", email=email, cpf=cpf, phone=phone)
        u.set_password(SENHA)
        u.email_verified_at = datetime.now(timezone.utc)
        db.session.add(u); db.session.flush()
        db.session.add(Wallet(user_id=u.id)); db.session.commit()


def _cadastro(email, cpf, phone=None):
    corpo = {"name": "Novo Cliente", "email": email, "cpf": cpf,
             "password": "OutraSenh@9!", "accept_terms": True,
             "accept_privacy": True, "accept_lgpd": True}
    if phone:
        corpo["phone"] = phone
    return corpo


def test_cpf_alheio_nao_e_confirmado(app, client):
    """O caso do achado: 'Este CPF ja esta cadastrado' era um oraculo."""
    _existente(app)
    r = client.post("/auth/register", json=_cadastro("outro@x.test", CPF_A))
    assert r.status_code == 409
    msg = r.get_json()["error"]
    assert "CPF" not in msg, f"a mensagem ainda entrega o campo: {msg}"
    assert "não foi possível" in msg.lower()


def test_celular_alheio_nao_e_confirmado(app, client):
    _existente(app)
    r = client.post("/auth/register",
                    json=_cadastro("outro@x.test", CPF_B, phone="11999998888"))
    assert r.status_code == 409
    assert "celular" not in r.get_json()["error"].lower()


def test_email_mantem_mensagem_propria(app, client):
    """Minimo de UX: a pessoa digitou o proprio e-mail e precisa saber."""
    _existente(app)
    r = client.post("/auth/register", json=_cadastro("dono@x.test", CPF_B))
    assert r.status_code == 409
    assert "e-mail" in r.get_json()["error"].lower()


def test_cpf_e_celular_dao_a_MESMA_mensagem(app, client):
    """Se diferissem entre si, o oraculo voltava por outro caminho."""
    _existente(app)
    r_cpf = client.post("/auth/register", json=_cadastro("a@x.test", CPF_A))
    r_tel = client.post("/auth/register",
                        json=_cadastro("b@x.test", CPF_B, phone="11999998888"))
    assert r_cpf.get_json()["error"] == r_tel.get_json()["error"]


def test_login_de_conta_inexistente_gasta_tempo_de_hash(app, client):
    """Oraculo de temporizacao: sem o hash descartavel, a conta inexistente
    respondia ordens de magnitude mais rapido."""
    _existente(app)

    def cronometrar(email):
        t = time.perf_counter()
        r = client.post("/auth/login", json={"email": email, "password": "ErradaX9!"})
        return time.perf_counter() - t, r.status_code

    t_existe, s1 = cronometrar("dono@x.test")
    t_nao, s2 = cronometrar("fantasma@x.test")
    assert s1 == s2 == 401

    # Nao exijo tempos iguais (ruido de CI); exijo a mesma ORDEM DE GRANDEZA.
    # Antes da correcao a diferenca era de ~100x.
    assert t_nao > t_existe / 10, (
        f"conta inexistente respondeu rapido demais: {t_nao:.4f}s vs {t_existe:.4f}s")


def test_m7_xff_forjado_nao_escolhe_o_ip(app):
    """M-7: ler o PRIMEIRO elemento deixava o atacante escolher o proprio IP."""
    from app.extensions import _real_client_ip
    with app.test_request_context(
            headers={"X-Forwarded-For": "203.0.113.7, 10.0.0.9"},
            environ_base={"REMOTE_ADDR": "10.0.0.9"}):
        ip = _real_client_ip()
    assert ip != "203.0.113.7", "voltou a confiar no primeiro elemento do XFF"
