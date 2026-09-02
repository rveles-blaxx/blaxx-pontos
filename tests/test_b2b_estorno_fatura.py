"""Estorno de acúmulo e fechamento de fatura B2B.

O que estes testes protegem:

  · O CASO DIFÍCIL do estorno — cliente já gastou os pontos. Não há clawback,
    e a rede continua devendo por essa parte. Se a BlaXx absorvesse a perda,
    "cancelar a venda depois do resgate" viraria saque de graça.
  · fatura fechada é IMUTÁVEL: estorno que chega depois entra como crédito na
    próxima, não reescreve a anterior.
  · a mesma venda não é cobrada duas vezes (carimbo `invoice_id`), e o mesmo
    estorno não é creditado duas vezes (carimbo `credit_invoice_id`).
  · anular fatura SOLTA as linhas — senão a receita some em silêncio.

Roda com:
    cd backend && python -m pytest tests/test_b2b_estorno_fatura.py -v
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("MAILER", "noop")

from app import create_app
from app.config import TestConfig
from app.extensions import db
from app.models import (
    InvoiceStatus, Merchant, MerchantAccrual, MerchantInvoice, MerchantUser,
    MerchantVertical, Transaction, TxType, User, Wallet,
)
from app.services import b2b as b2b_svc
from app.services import wallet as wallet_svc

CPF = "52998224725"
CPF_ADM = "15350946056"
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
        u = User(name="Cliente", email="c@x.test", cpf=CPF)
        u.set_password(SENHA)
        db.session.add(u); db.session.flush()
        db.session.add(Wallet(user_id=u.id)); db.session.commit()
        return u.id


def _rede(app, *, accrual=500, bill=10, cnpj="11111111000191", email=None):
    with app.app_context():
        m = Merchant(name="Rede Teste", cnpj=cnpj, vertical=MerchantVertical.SUPERMERCADO,
                     accrual_cents_per_point=accrual, bill_cents_per_point=bill,
                     max_points_per_tx=100_000)
        db.session.add(m); db.session.flush()
        _, chave = b2b_svc.issue_api_key(m, label="PDV")
        if email:
            u = MerchantUser(merchant_id=m.id, name="Dono", email=email, role="owner")
            u.set_password(SENHA); db.session.add(u)
        db.session.commit()
        return m.id, chave


def _adm_token(app, client):
    with app.app_context():
        u = User(name="Adm", email="a@x.test", cpf=CPF_ADM, role="admin")
        u.set_password(SENHA); u.email_verified_at = datetime.now(timezone.utc)
        db.session.add(u); db.session.flush()
        db.session.add(Wallet(user_id=u.id)); db.session.commit()
    return client.post("/auth/login", json={"email": "a@x.test",
                                            "password": SENHA}).get_json()["token"]


def _h(k): return {"X-API-Key": k}
def _b(t): return {"Authorization": f"Bearer {t}"}


def _vender(client, chave, cents, idem):
    r = client.post("/b2b/accrual", json={"cpf": CPF, "amount_cents": cents},
                    headers=_h(chave) | {"Idempotency-Key": idem})
    assert r.status_code == 201, r.get_json()
    return r.get_json()


# ───────────────────────── estorno ───────────────────────── #

def test_estorno_devolve_pontos_e_credita_a_rede(app, client):
    uid = _cliente(app)
    _, chave = _rede(app)
    v = _vender(client, chave, 50_000, "nf-1")          # R$500 → 100 pts → R$10

    r = client.post(f"/b2b/accrual/{v['id']}/reverse",
                    json={"reason": "cliente desistiu"}, headers=_h(chave))
    assert r.status_code == 200
    d = r.get_json()
    assert d["reversed"] is True
    assert d["reversed_points"] == 100
    assert d["points_not_recovered"] == 0
    assert d["net_brl"] == 0.0

    with app.app_context():
        w = db.session.query(Wallet).filter_by(user_id=uid).one()
        assert w.balance_pts == 0
        tipos = [t.type for t in db.session.query(Transaction).filter_by(wallet_id=w.id).all()]
        assert TxType.ACCRUAL in tipos and TxType.REFUND in tipos

    s = client.get("/b2b/statement", headers=_h(chave)).get_json()
    assert s["amount_due_brl"] == 0.0
    assert s["credit_brl"] == 10.0


def test_cliente_ja_gastou_os_pontos_rede_continua_devendo(app, client):
    """O caso que decide o desenho. 100 pts emitidos, cliente gastou 70.
    Voltam 30; a rede segue devendo pelos 70 já entregues."""
    uid = _cliente(app)
    _, chave = _rede(app)
    v = _vender(client, chave, 50_000, "nf-1")           # 100 pts, R$ 10,00

    with app.app_context():                              # cliente gasta 70
        wallet_svc.debit(user_id=uid, amount_pts=70, tx_type=TxType.REDEEM,
                         description="resgate", idempotency_key="gastou")
        db.session.commit()

    r = client.post(f"/b2b/accrual/{v['id']}/reverse", json={}, headers=_h(chave))
    d = r.get_json()
    assert d["reversed_points"] == 30
    assert d["points_not_recovered"] == 70
    assert "já haviam sido usados" in d["note"]
    assert d["net_brl"] == 7.0                           # 70 pts × R$0,10

    with app.app_context():
        w = db.session.query(Wallet).filter_by(user_id=uid).one()
        assert w.balance_pts == 0                        # não ficou negativo


def test_estorno_e_idempotente(app, client):
    uid = _cliente(app)
    _, chave = _rede(app)
    v = _vender(client, chave, 50_000, "nf-1")

    a = client.post(f"/b2b/accrual/{v['id']}/reverse", json={}, headers=_h(chave))
    b = client.post(f"/b2b/accrual/{v['id']}/reverse", json={}, headers=_h(chave))
    assert a.status_code == b.status_code == 200
    assert a.get_json()["reversed_at"] == b.get_json()["reversed_at"]

    with app.app_context():
        w = db.session.query(Wallet).filter_by(user_id=uid).one()
        assert w.balance_pts == 0
        assert db.session.query(Transaction).filter_by(
            wallet_id=w.id, type=TxType.REFUND).count() == 1


def test_estorno_pelo_numero_do_cupom(app, client):
    """O caixa tem a nota, não o UUID."""
    _cliente(app)
    _, chave = _rede(app)
    _vender(client, chave, 20_000, "cupom-9911")

    r = client.post("/b2b/accrual/reverse",
                    json={"idempotency_key": "cupom-9911"}, headers=_h(chave))
    assert r.status_code == 200
    assert r.get_json()["reversed"] is True


def test_rede_nao_estorna_venda_de_outra(app, client):
    _cliente(app)
    _, chave_a = _rede(app, cnpj="11111111000191")
    _, chave_b = _rede(app, cnpj="22222222000172")
    v = _vender(client, chave_a, 20_000, "nf-1")

    r = client.post(f"/b2b/accrual/{v['id']}/reverse", json={}, headers=_h(chave_b))
    assert r.status_code == 404


# ───────────────────────── fatura ───────────────────────── #

def _fechar(client, tok, mid, ini, fim):
    return client.post(f"/admin/merchants/{mid}/invoices/close",
                       json={"period_start": ini.isoformat(),
                             "period_end": fim.isoformat()}, headers=_b(tok))


def test_fechamento_congela_totais_e_carimba_as_linhas(app, client):
    _cliente(app)
    tok = _adm_token(app, client)
    mid, chave = _rede(app)
    for i in range(3):
        _vender(client, chave, 20_000, f"nf-{i}")       # 40 pts × 3 = R$ 12,00

    amanha = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=1)
    r = _fechar(client, tok, mid, amanha - timedelta(days=30), amanha)
    assert r.status_code == 201, r.get_json()
    f = r.get_json()
    assert f["transactions"] == 3
    assert f["points_issued"] == 120
    assert f["amount_brl"] == 12.0
    assert f["status"] == "open"
    assert f["number"].startswith("BLX-SUP-")

    with app.app_context():
        assert db.session.query(MerchantAccrual).filter_by(invoice_id=None).count() == 0


def test_fechar_de_novo_nao_cobra_a_mesma_venda(app, client):
    _cliente(app)
    tok = _adm_token(app, client)
    mid, chave = _rede(app)
    _vender(client, chave, 20_000, "nf-1")

    amanha = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=1)
    assert _fechar(client, tok, mid, amanha - timedelta(days=30), amanha).status_code == 201
    segunda = _fechar(client, tok, mid, amanha - timedelta(days=29), amanha)
    assert segunda.status_code == 400
    assert segunda.get_json()["code"] == "nothing_to_invoice"


def test_estorno_apos_fechamento_vai_para_a_proxima_fatura(app, client):
    """Fatura fechada não muda. O crédito entra no próximo fechamento."""
    _cliente(app)
    tok = _adm_token(app, client)
    mid, chave = _rede(app)
    v = _vender(client, chave, 50_000, "nf-1")          # R$ 10,00

    amanha = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=1)
    f1 = _fechar(client, tok, mid, amanha - timedelta(days=30), amanha).get_json()
    assert f1["amount_brl"] == 10.0

    client.post(f"/b2b/accrual/{v['id']}/reverse", json={}, headers=_h(chave))

    # a fatura antiga continua igual
    listagem = client.get(f"/admin/merchants/{mid}/invoices", headers=_b(tok)).get_json()
    antiga = [x for x in listagem["items"] if x["id"] == f1["id"]][0]
    assert antiga["amount_brl"] == 10.0

    # e o crédito aparece como saldo negativo em aberto
    assert listagem["open_balance"]["amount_brl"] == -10.0

    f2 = _fechar(client, tok, mid, amanha, amanha + timedelta(days=30)).get_json()
    assert f2["credit_brl"] == 10.0
    assert f2["amount_brl"] == -10.0


def test_credito_nao_e_devolvido_duas_vezes(app, client):
    _cliente(app)
    tok = _adm_token(app, client)
    mid, chave = _rede(app)
    v = _vender(client, chave, 50_000, "nf-1")
    client.post(f"/b2b/accrual/{v['id']}/reverse", json={}, headers=_h(chave))

    amanha = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=1)
    f1 = _fechar(client, tok, mid, amanha - timedelta(days=30), amanha).get_json()
    assert f1["amount_brl"] == 0.0        # cobrou 10 e creditou 10 na mesma

    segunda = _fechar(client, tok, mid, amanha, amanha + timedelta(days=30))
    assert segunda.status_code == 400     # não sobrou crédito para devolver


def test_pagar_fatura_e_idempotente(app, client):
    _cliente(app)
    tok = _adm_token(app, client)
    mid, chave = _rede(app)
    _vender(client, chave, 20_000, "nf-1")
    amanha = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=1)
    fid = _fechar(client, tok, mid, amanha - timedelta(days=30), amanha).get_json()["id"]

    a = client.post(f"/admin/invoices/{fid}/pay", json={"note": "TED 12/08"}, headers=_b(tok))
    b = client.post(f"/admin/invoices/{fid}/pay", json={}, headers=_b(tok))
    assert a.status_code == b.status_code == 200
    assert a.get_json()["status"] == "paid"
    assert a.get_json()["paid_at"] == b.get_json()["paid_at"]


def test_anular_fatura_solta_as_linhas(app, client):
    _cliente(app)
    tok = _adm_token(app, client)
    mid, chave = _rede(app)
    _vender(client, chave, 20_000, "nf-1")
    amanha = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=1)
    fid = _fechar(client, tok, mid, amanha - timedelta(days=30), amanha).get_json()["id"]

    r = client.post(f"/admin/invoices/{fid}/void", json={"note": "fechei errado"},
                    headers=_b(tok))
    assert r.status_code == 200 and r.get_json()["status"] == "void"

    with app.app_context():
        assert db.session.query(MerchantAccrual).filter_by(invoice_id=None).count() == 1

    # e pode ser refaturada
    assert _fechar(client, tok, mid, amanha - timedelta(days=30), amanha).status_code == 201


def test_fatura_paga_nao_pode_ser_anulada(app, client):
    _cliente(app)
    tok = _adm_token(app, client)
    mid, chave = _rede(app)
    _vender(client, chave, 20_000, "nf-1")
    amanha = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=1)
    fid = _fechar(client, tok, mid, amanha - timedelta(days=30), amanha).get_json()["id"]
    client.post(f"/admin/invoices/{fid}/pay", json={}, headers=_b(tok))

    r = client.post(f"/admin/invoices/{fid}/void", json={}, headers=_b(tok))
    assert r.status_code == 409
    assert r.get_json()["code"] == "invoice_paid"


def test_admin_ve_total_a_receber_em_aberto(app, client):
    _cliente(app)
    tok = _adm_token(app, client)
    mid, chave = _rede(app)
    _vender(client, chave, 100_000, "nf-1")             # R$ 20,00
    amanha = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=1)
    _fechar(client, tok, mid, amanha - timedelta(days=30), amanha)

    d = client.get("/admin/invoices?status=open", headers=_b(tok)).get_json()
    assert d["totals"]["unpaid_brl"] == 20.0
    assert len(d["items"]) == 1


def test_painel_da_rede_ve_as_proprias_faturas(app, client):
    _cliente(app)
    tok = _adm_token(app, client)
    mid, chave = _rede(app, email="dono@rede.test")
    _vender(client, chave, 20_000, "nf-1")
    amanha = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=1)
    _fechar(client, tok, mid, amanha - timedelta(days=30), amanha)

    pt = client.post("/b2b/panel/login", json={"email": "dono@rede.test",
                                               "password": SENHA}).get_json()["token"]
    d = client.get("/b2b/panel/invoices", headers=_b(pt)).get_json()
    assert len(d["items"]) == 1
    assert d["unpaid_brl"] == 4.0
    assert d["open_balance"]["amount_brl"] == 0.0
