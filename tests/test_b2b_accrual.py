"""B2B: emissão de pontos por rede parceira (posto, supermercado, farmácia).

O que estes testes protegem, em ordem de quanto custaria errar:

  · a TRAVA ECONÔMICA — rede cujo contrato paga menos que o custo de resgate
    do ponto não emite. É a defesa contra repetir, no B2B, o defeito que já
    existe no bônus dos pacotes (comprar mais barato do que se resgata).
  · idempotência — PDV repete a chamada em timeout de rede; repetir não pode
    creditar duas vezes nem gerar dois recebíveis.
  · o recebível congela no preço do dia da emissão; renegociar contrato não
    reescreve fatura passada.
  · autenticação por chave de máquina: chave errada, revogada ou de rede
    inativa não emite.
  · privacidade: a resposta não devolve saldo nem dados do cliente à rede.

Roda com:
    cd backend && python -m pytest tests/test_b2b_accrual.py -v
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
    Merchant, MerchantAccrual, MerchantVertical, Transaction, TxType, User, Wallet,
)
from app.services import b2b as b2b_svc

CPF_CLIENTE = "52998224725"


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


def _mk_cliente(app, cpf=CPF_CLIENTE, email="cliente@b2b.test") -> str:
    with app.app_context():
        u = User(name="Cliente B2B", email=email, cpf=cpf)
        u.set_password("StrongP@ss1!")
        db.session.add(u)
        db.session.flush()
        db.session.add(Wallet(user_id=u.id))
        db.session.commit()
        return u.id


def _mk_rede(app, *, slug="posto", accrual=1_000, bill=10, cap=500,
             ativa=True) -> tuple[str, str]:
    """Cria rede + chave. Devolve (merchant_id, chave em claro)."""
    verticais = {
        "posto": MerchantVertical.POSTO,
        "supermercado": MerchantVertical.SUPERMERCADO,
        "farmacia": MerchantVertical.FARMACIA,
    }
    with app.app_context():
        m = Merchant(
            name=f"Rede {slug}",
            cnpj=str(abs(hash(slug)))[:14].ljust(14, "0"),
            vertical=verticais[slug],
            accrual_cents_per_point=accrual,
            bill_cents_per_point=bill,
            max_points_per_tx=cap,
            is_active=ativa,
        )
        db.session.add(m)
        db.session.flush()
        _, chave = b2b_svc.issue_api_key(m, label="PDV teste")
        db.session.commit()
        return m.id, chave


def _h(chave: str) -> dict:
    return {"X-API-Key": chave}


# ───────────────────────── emissão feliz ───────────────────────── #

def test_compra_credita_pontos_e_grava_recebivel(app, client):
    """Supermercado: 1 pt a cada R$ 5. Compra de R$ 300 → 60 pts."""
    user_id = _mk_cliente(app)
    _, chave = _mk_rede(app, slug="supermercado", accrual=500, bill=10, cap=1_000)

    r = client.post("/b2b/accrual", json={"cpf": CPF_CLIENTE, "amount_cents": 30_000},
                    headers=_h(chave))
    assert r.status_code == 201, r.get_json()
    body = r.get_json()
    assert body["points_awarded"] == 60
    assert body["amount_brl"] == 300.0
    assert body["bill_brl"] == 6.0            # 60 pts × R$ 0,10

    with app.app_context():
        w = db.session.query(Wallet).filter_by(user_id=user_id).one()
        assert w.balance_pts == 60
        tx = db.session.query(Transaction).filter_by(wallet_id=w.id).one()
        assert tx.type == TxType.ACCRUAL
        assert tx.amount_pts == 60


def test_resto_da_divisao_nao_vira_ponto(app, client):
    """Farmácia: 1 pt a cada R$ 3. Compra de R$ 100 → 33 pts, não 33,33."""
    _mk_cliente(app)
    _, chave = _mk_rede(app, slug="farmacia", accrual=300, bill=10, cap=800)

    r = client.post("/b2b/accrual", json={"cpf": CPF_CLIENTE, "amount_cents": 10_000},
                    headers=_h(chave))
    assert r.status_code == 201
    assert r.get_json()["points_awarded"] == 33


def test_identifica_cliente_pelo_cartao_blaxx(app, client):
    """Posto: cliente informa o cartão (8 hex) em vez do CPF."""
    user_id = _mk_cliente(app)
    _, chave = _mk_rede(app, slug="posto", accrual=1_000, bill=10, cap=500)
    cartao = user_id[:8].upper()

    r = client.post("/b2b/accrual", json={"card_id": cartao, "amount_cents": 20_000},
                    headers=_h(chave))
    assert r.status_code == 201
    assert r.get_json()["points_awarded"] == 20     # R$ 200 ÷ R$ 10


# ───────────────────── a trava econômica ───────────────────── #

def test_contrato_abaixo_do_custo_de_resgate_nao_emite(app, client):
    """Ponto resgata a `CENTS_PER_POINT`; rede que paga menos que isso emitiria
    prejuízo. A recusa é em runtime, não só no cadastro."""
    _mk_cliente(app)
    _, chave = _mk_rede(app, slug="posto", accrual=500,
                        bill=Config.CENTS_PER_POINT - 1)

    r = client.post("/b2b/accrual", json={"cpf": CPF_CLIENTE, "amount_cents": 10_000},
                    headers=_h(chave))
    assert r.status_code == 409
    assert r.get_json()["code"] == "contract_below_redemption_cost"

    with app.app_context():
        assert db.session.query(MerchantAccrual).count() == 0
        assert db.session.query(Transaction).count() == 0


def test_recebivel_congela_no_preco_do_dia(app, client):
    """Renegociar o contrato não reescreve fatura já emitida."""
    _mk_cliente(app)
    mid, chave = _mk_rede(app, slug="supermercado", accrual=500, bill=10, cap=1_000)

    client.post("/b2b/accrual", json={"cpf": CPF_CLIENTE, "amount_cents": 5_000},
                headers=_h(chave))                       # 10 pts × R$0,10 = R$1,00

    with app.app_context():                              # rede renegocia pra R$0,20
        db.session.get(Merchant, mid).bill_cents_per_point = 20
        db.session.commit()

    r = client.get("/b2b/statement", headers=_h(chave))
    assert r.get_json()["amount_due_brl"] == 1.0         # não virou R$ 2,00


# ───────────────────────── idempotência ───────────────────────── #

def test_mesma_chave_de_idempotencia_nao_credita_duas_vezes(app, client):
    user_id = _mk_cliente(app)
    _, chave = _mk_rede(app, slug="supermercado", accrual=500, bill=10, cap=1_000)
    hdr = _h(chave) | {"Idempotency-Key": "cupom-4711"}

    r1 = client.post("/b2b/accrual", json={"cpf": CPF_CLIENTE, "amount_cents": 5_000},
                     headers=hdr)
    r2 = client.post("/b2b/accrual", json={"cpf": CPF_CLIENTE, "amount_cents": 5_000},
                     headers=hdr)
    assert r1.status_code == 201 and r2.status_code == 201
    assert r1.get_json()["id"] == r2.get_json()["id"]

    with app.app_context():
        w = db.session.query(Wallet).filter_by(user_id=user_id).one()
        assert w.balance_pts == 10                       # não 20
        assert db.session.query(MerchantAccrual).count() == 1
        assert db.session.query(Transaction).count() == 1


def test_idempotencia_e_por_rede(app, client):
    """Duas redes podem usar o mesmo número de cupom sem se anular."""
    _mk_cliente(app)
    _, chave_a = _mk_rede(app, slug="posto", accrual=1_000, bill=10, cap=500)
    _, chave_b = _mk_rede(app, slug="farmacia", accrual=300, bill=10, cap=800)

    a = client.post("/b2b/accrual", json={"cpf": CPF_CLIENTE, "amount_cents": 10_000},
                    headers=_h(chave_a) | {"Idempotency-Key": "0001"})
    b = client.post("/b2b/accrual", json={"cpf": CPF_CLIENTE, "amount_cents": 10_000},
                    headers=_h(chave_b) | {"Idempotency-Key": "0001"})
    assert a.status_code == 201 and b.status_code == 201
    assert a.get_json()["id"] != b.get_json()["id"]


# ───────────────────────── limites ───────────────────────── #

def test_compra_abaixo_do_minimo_nao_pontua(app, client):
    _mk_cliente(app)
    _, chave = _mk_rede(app, slug="posto", accrual=1_000, bill=10, cap=500)

    r = client.post("/b2b/accrual", json={"cpf": CPF_CLIENTE, "amount_cents": 500},
                    headers=_h(chave))
    assert r.status_code == 400
    assert r.get_json()["code"] == "below_minimum"


def test_teto_por_transacao_barra_pdv_maluco(app, client):
    """Contém bug de PDV mandando centavos como reais."""
    _mk_cliente(app)
    _, chave = _mk_rede(app, slug="posto", accrual=1_000, bill=10, cap=500)

    r = client.post("/b2b/accrual",
                    json={"cpf": CPF_CLIENTE, "amount_cents": 100_000_000},
                    headers=_h(chave))
    assert r.status_code == 409
    assert r.get_json()["code"] == "above_tx_cap"
    with app.app_context():
        assert db.session.query(Transaction).count() == 0


def test_cliente_inexistente_nao_cria_lancamento(app, client):
    _mk_rede(app, slug="posto", accrual=1_000, bill=10, cap=500)
    _, chave = _mk_rede(app, slug="farmacia", accrual=300, bill=10, cap=800)

    r = client.post("/b2b/accrual", json={"cpf": "11144477735", "amount_cents": 10_000},
                    headers=_h(chave))
    assert r.status_code == 404
    assert r.get_json()["code"] == "customer_not_found"


# ───────────────────────── autenticação ───────────────────────── #

def test_chave_invalida_nao_emite(app, client):
    _mk_cliente(app)
    _mk_rede(app, slug="posto")

    for chave in ("", "blx_deadbeef_naoexiste", "token-qualquer"):
        r = client.post("/b2b/accrual",
                        json={"cpf": CPF_CLIENTE, "amount_cents": 10_000},
                        headers={"X-API-Key": chave} if chave else {})
        assert r.status_code == 401


def test_chave_revogada_para_de_emitir(app, client):
    from app.models import MerchantApiKey
    _mk_cliente(app)
    _, chave = _mk_rede(app, slug="posto", accrual=1_000, bill=10, cap=500)

    assert client.get("/b2b/me", headers=_h(chave)).status_code == 200

    with app.app_context():
        k = db.session.query(MerchantApiKey).one()
        k.is_active = False
        db.session.commit()

    assert client.get("/b2b/me", headers=_h(chave)).status_code == 401


def test_rede_inativa_nao_emite(app, client):
    _mk_cliente(app)
    mid, chave = _mk_rede(app, slug="posto", accrual=1_000, bill=10, cap=500)
    with app.app_context():
        db.session.get(Merchant, mid).is_active = False
        db.session.commit()

    assert client.post("/b2b/accrual",
                       json={"cpf": CPF_CLIENTE, "amount_cents": 10_000},
                       headers=_h(chave)).status_code == 401


def test_chave_e_guardada_como_hash(app):
    """Quem lê o banco não consegue emitir pontos."""
    from app.models import MerchantApiKey
    _, chave = _mk_rede(app, slug="posto")
    with app.app_context():
        k = db.session.query(MerchantApiKey).one()
        assert chave not in k.key_hash
        assert k.key_hash != chave
        assert k.check_key(chave) is True
        assert k.check_key(chave + "x") is False


# ───────────────────────── privacidade ───────────────────────── #

def test_resposta_nao_vaza_dados_do_cliente(app, client):
    """A rede fica sabendo os pontos daquela compra. Nada além disso."""
    _mk_cliente(app)
    _, chave = _mk_rede(app, slug="supermercado", accrual=500, bill=10, cap=1_000)

    r = client.post("/b2b/accrual", json={"cpf": CPF_CLIENTE, "amount_cents": 5_000},
                    headers=_h(chave))
    corpo = r.get_json()
    for proibido in ("user_id", "balance_pts", "name", "email", "cpf"):
        assert proibido not in corpo, f"{proibido} vazou para a rede"


# ───────────────────────── fatura ───────────────────────── #

def test_statement_soma_o_periodo(app, client):
    _mk_cliente(app)
    _, chave = _mk_rede(app, slug="farmacia", accrual=300, bill=10, cap=800)

    for i in range(3):
        client.post("/b2b/accrual",
                    json={"cpf": CPF_CLIENTE, "amount_cents": 9_000},
                    headers=_h(chave) | {"Idempotency-Key": f"c{i}"})

    s = client.get("/b2b/statement", headers=_h(chave)).get_json()
    assert s["transactions"] == 3
    assert s["points_issued"] == 90            # 30 pts × 3
    assert s["gmv_brl"] == 270.0
    assert s["amount_due_brl"] == 9.0          # 90 × R$ 0,10


def test_statement_nao_mistura_redes(app, client):
    _mk_cliente(app)
    _, chave_a = _mk_rede(app, slug="posto", accrual=1_000, bill=10, cap=500)
    _, chave_b = _mk_rede(app, slug="farmacia", accrual=300, bill=10, cap=800)

    client.post("/b2b/accrual", json={"cpf": CPF_CLIENTE, "amount_cents": 10_000},
                headers=_h(chave_a))
    client.post("/b2b/accrual", json={"cpf": CPF_CLIENTE, "amount_cents": 30_000},
                headers=_h(chave_b))

    assert client.get("/b2b/statement", headers=_h(chave_a)).get_json()["points_issued"] == 10
    assert client.get("/b2b/statement", headers=_h(chave_b)).get_json()["points_issued"] == 100
