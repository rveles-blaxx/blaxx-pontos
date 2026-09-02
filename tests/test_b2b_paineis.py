"""Painel da rede parceira e gestão B2B no painel do admin.

O que estes testes protegem:

  · SEPARAÇÃO DOS TRILHOS — chave de PDV não abre o painel; sessão de painel
    não emite pontos. Se um dos dois vazar, o estrago fica contido.
  · isolamento entre redes — a rede A não vê lançamento nem fatura da rede B.
  · papéis — `staff` consulta, só `owner` mexe em chave de PDV.
  · o segredo da chave aparece uma única vez; a listagem devolve só o prefixo.
  · o admin enxerga o recebível total e quais redes estão insolventes.

Roda com:
    cd backend && python -m pytest tests/test_b2b_paineis.py -v
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("MAILER", "noop")

from app import create_app
from app.config import Config, TestConfig
from app.extensions import db
from app.models import (
    Merchant, MerchantApiKey, MerchantUser, MerchantVertical, User, Wallet,
)
from app.services import b2b as b2b_svc

CPF_CLIENTE = "52998224725"
CPF_ADMIN = "15350946056"
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


def _cliente(app):
    with app.app_context():
        u = User(name="Cliente", email="c@b2b.test", cpf=CPF_CLIENTE)
        u.set_password(SENHA)
        db.session.add(u); db.session.flush()
        db.session.add(Wallet(user_id=u.id)); db.session.commit()
        return u.id


def _admin_token(app, client):
    with app.app_context():
        from datetime import datetime, timezone
        u = User(name="Admin", email="adm@b2b.test", cpf=CPF_ADMIN, role="admin")
        u.set_password(SENHA)
        u.email_verified_at = datetime.now(timezone.utc)
        db.session.add(u); db.session.flush()
        db.session.add(Wallet(user_id=u.id)); db.session.commit()
    r = client.post("/auth/login", json={"email": "adm@b2b.test", "password": SENHA})
    return r.get_json()["token"]


def _rede(app, *, nome="Rede X", cnpj="11111111000191", slug="posto",
          accrual=1_000, bill=10, email="dono@rede.test", role="owner"):
    verticais = {"posto": MerchantVertical.POSTO,
                 "supermercado": MerchantVertical.SUPERMERCADO,
                 "farmacia": MerchantVertical.FARMACIA}
    with app.app_context():
        m = Merchant(name=nome, cnpj=cnpj, vertical=verticais[slug],
                     accrual_cents_per_point=accrual, bill_cents_per_point=bill,
                     max_points_per_tx=10_000)
        db.session.add(m); db.session.flush()
        _, chave = b2b_svc.issue_api_key(m, label="PDV")
        u = MerchantUser(merchant_id=m.id, name="Dono", email=email, role=role)
        u.set_password(SENHA)
        db.session.add(u)
        db.session.commit()
        return m.id, chave, email


def _painel_token(client, email):
    r = client.post("/b2b/panel/login", json={"email": email, "password": SENHA})
    assert r.status_code == 200, r.get_json()
    return r.get_json()["token"]


def _bearer(t):
    return {"Authorization": f"Bearer {t}"}


# ───────────────── separação dos dois trilhos ───────────────── #

def test_chave_de_pdv_nao_abre_o_painel(app, client):
    _, chave, _ = _rede(app)
    r = client.get("/b2b/panel/summary", headers={"Authorization": f"Bearer {chave}"})
    assert r.status_code in (401, 422)      # não é JWT válido


def test_sessao_de_painel_nao_emite_pontos(app, client):
    """O token do painel não vale como chave de emissão."""
    _cliente(app)
    _, _, email = _rede(app)
    tok = _painel_token(client, email)

    r = client.post("/b2b/accrual", json={"cpf": CPF_CLIENTE, "amount_cents": 10_000},
                    headers=_bearer(tok))
    assert r.status_code == 401
    assert r.get_json()["code"] == "invalid_api_key"


def test_login_errado_nao_diz_se_o_email_existe(app, client):
    _, _, email = _rede(app)
    a = client.post("/b2b/panel/login", json={"email": email, "password": "errada!!"})
    b = client.post("/b2b/panel/login", json={"email": "naoexiste@x.test",
                                              "password": "errada!!"})
    assert a.status_code == b.status_code == 401
    assert a.get_json() == b.get_json()


# ───────────────── isolamento entre redes ───────────────── #

def test_painel_nao_ve_dados_de_outra_rede(app, client):
    _cliente(app)
    _, chave_a, email_a = _rede(app, nome="Rede A", cnpj="11111111000191",
                                email="a@rede.test")
    _, chave_b, _ = _rede(app, nome="Rede B", cnpj="22222222000172",
                          slug="farmacia", accrual=300, email="b@rede.test")

    client.post("/b2b/accrual", json={"cpf": CPF_CLIENTE, "amount_cents": 50_000},
                headers={"X-API-Key": chave_b})          # só a B pontuou

    s = client.get("/b2b/panel/summary", headers=_bearer(_painel_token(client, email_a)))
    assert s.status_code == 200
    dados = s.get_json()
    assert dados["all_time"]["points_issued"] == 0
    assert dados["merchant"]["name"] == "Rede A"

    linhas = client.get("/b2b/panel/accruals",
                        headers=_bearer(_painel_token(client, email_a))).get_json()
    assert linhas["items"] == []


# ───────────────── chaves ───────────────── #

def test_listagem_de_chaves_nao_devolve_o_segredo(app, client):
    _, chave, email = _rede(app)
    r = client.get("/b2b/panel/keys", headers=_bearer(_painel_token(client, email)))
    assert r.status_code == 200
    itens = r.get_json()["items"]
    assert len(itens) == 1
    bruto = str(itens)
    assert chave not in bruto
    assert itens[0]["prefix"] in chave      # o prefixo identifica, sem abrir


def test_chave_nova_aparece_uma_vez_e_funciona(app, client):
    _cliente(app)
    _, _, email = _rede(app)
    tok = _painel_token(client, email)

    r = client.post("/b2b/panel/keys", json={"label": "PDV loja 2"}, headers=_bearer(tok))
    assert r.status_code == 201
    nova = r.get_json()["key"]

    ok = client.post("/b2b/accrual", json={"cpf": CPF_CLIENTE, "amount_cents": 10_000},
                     headers={"X-API-Key": nova})
    assert ok.status_code == 201


def test_revogar_chave_para_a_emissao(app, client):
    _cliente(app)
    _, chave, email = _rede(app)
    tok = _painel_token(client, email)

    kid = client.get("/b2b/panel/keys", headers=_bearer(tok)).get_json()["items"][0]["id"]
    assert client.delete(f"/b2b/panel/keys/{kid}", headers=_bearer(tok)).status_code == 200

    r = client.post("/b2b/accrual", json={"cpf": CPF_CLIENTE, "amount_cents": 10_000},
                    headers={"X-API-Key": chave})
    assert r.status_code == 401


def test_staff_consulta_mas_nao_mexe_em_chave(app, client):
    _, _, email = _rede(app, email="staff@rede.test", role="staff")
    tok = _painel_token(client, email)

    assert client.get("/b2b/panel/summary", headers=_bearer(tok)).status_code == 200
    assert client.get("/b2b/panel/keys", headers=_bearer(tok)).status_code == 403
    assert client.post("/b2b/panel/keys", json={}, headers=_bearer(tok)).status_code == 403


def test_rede_inativa_derruba_o_painel(app, client):
    mid, _, email = _rede(app)
    tok = _painel_token(client, email)
    with app.app_context():
        db.session.get(Merchant, mid).is_active = False
        db.session.commit()
    assert client.get("/b2b/panel/summary", headers=_bearer(tok)).status_code == 403


# ───────────────── painel do admin ───────────────── #

def test_admin_ve_recebivel_total_e_insolvencia(app, client):
    _cliente(app)
    tok_adm = _admin_token(app, client)
    _, chave, _ = _rede(app, nome="Boa", cnpj="11111111000191", bill=10,
                        email="boa@rede.test")
    _rede(app, nome="Defasada", cnpj="22222222000172", slug="farmacia",
          accrual=300, bill=Config.CENTS_PER_POINT - 1, email="def@rede.test")

    client.post("/b2b/accrual", json={"cpf": CPF_CLIENTE, "amount_cents": 100_000},
                headers={"X-API-Key": chave})            # 100 pts → R$ 10,00

    r = client.get("/admin/merchants", headers=_bearer(tok_adm))
    assert r.status_code == 200
    d = r.get_json()
    assert d["totals"]["merchants"] == 2
    assert d["totals"]["receivable_brl"] == 10.0
    assert d["totals"]["insolvent"] == 1
    por_nome = {m["name"]: m for m in d["items"]}
    assert por_nome["Boa"]["solvent"] is True
    assert por_nome["Defasada"]["solvent"] is False


def test_admin_cadastra_rede_e_emite_credenciais(app, client):
    _cliente(app)
    tok = _admin_token(app, client)

    r = client.post("/admin/merchants", json={
        "name": "Supermercado Novo", "cnpj": "44.444.444/0001-95",
        "vertical": "supermercado", "accrual_cents_per_point": 500,
        "bill_cents_per_point": 10,
    }, headers=_bearer(tok))
    assert r.status_code == 201, r.get_json()
    mid = r.get_json()["merchant"]["id"]

    chave = client.post(f"/admin/merchants/{mid}/keys", json={},
                        headers=_bearer(tok)).get_json()["key"]
    assert client.post("/b2b/accrual",
                       json={"cpf": CPF_CLIENTE, "amount_cents": 5_000},
                       headers={"X-API-Key": chave}).status_code == 201

    u = client.post(f"/admin/merchants/{mid}/users",
                    json={"email": "novo@rede.test", "password": SENHA,
                          "name": "Gerente"}, headers=_bearer(tok))
    assert u.status_code == 201
    assert _painel_token(client, "novo@rede.test")


def test_admin_avisa_contrato_insolvente_no_cadastro(app, client):
    tok = _admin_token(app, client)
    r = client.post("/admin/merchants", json={
        "name": "Rede Defasada", "cnpj": "55555555000166", "vertical": "posto",
        "accrual_cents_per_point": 1_000,
        "bill_cents_per_point": Config.CENTS_PER_POINT - 1,
    }, headers=_bearer(tok))
    assert r.status_code == 201
    assert r.get_json()["insolvent"] is True          # cadastra, mas avisa


def test_admin_cnpj_duplicado_e_recusado(app, client):
    tok = _admin_token(app, client)
    corpo = {"name": "A", "cnpj": "66666666000177", "vertical": "posto",
             "accrual_cents_per_point": 500, "bill_cents_per_point": 10}
    assert client.post("/admin/merchants", json=corpo, headers=_bearer(tok)).status_code == 201
    assert client.post("/admin/merchants", json=corpo, headers=_bearer(tok)).status_code == 409


def test_usuario_comum_nao_acessa_gestao_de_redes(app, client):
    _cliente(app)
    r = client.post("/auth/login", json={"email": "c@b2b.test", "password": SENHA})
    tok = r.get_json()["token"]
    assert client.get("/admin/merchants", headers=_bearer(tok)).status_code == 403


# ───────────────── série do painel ───────────────── #

def test_serie_preenche_dias_sem_movimento(app, client):
    _cliente(app)
    _, chave, email = _rede(app, accrual=500, bill=10)
    client.post("/b2b/accrual", json={"cpf": CPF_CLIENTE, "amount_cents": 5_000},
                headers={"X-API-Key": chave})

    s = client.get("/b2b/panel/summary",
                   headers=_bearer(_painel_token(client, email))).get_json()
    assert len(s["series"]) == 30                     # 30 dias, com zeros
    assert s["series"][-1]["points"] == 10            # hoje
    assert s["series"][0]["points"] == 0
    assert s["customers_30d"] == 1
