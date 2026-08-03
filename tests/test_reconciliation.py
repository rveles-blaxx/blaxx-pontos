"""Conciliação do ledger — cada teste encena uma forma de o dinheiro sumir.

A rotina só vale se pegar divergência real E não gritar em base sadia. Falso
positivo aqui é pior que ausência: ensina o operador a ignorar o alerta.
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
from app.models import (
    PixCharge, PixChargeStatus, PixPayout, PixPayoutStatus,
    Transaction, TxStatus, TxType, User, Wallet,
)
from app.services.reconciliation import conciliar


@pytest.fixture
def app():
    app = create_app(TestConfig)
    with app.app_context():
        db.create_all()
        yield app
        db.session.remove()
        db.drop_all()


_seq = iter(range(1, 10_000))


def _carteira(saldo: int = 0, email: str = "a@b.com") -> Wallet:
    # cpf é NOT NULL e UNIQUE no modelo; sequência evita colisão entre carteiras.
    n = next(_seq)
    u = User(email=email, name="Teste", cpf=f"{n:011d}", password_hash="x")
    db.session.add(u)
    db.session.flush()
    w = Wallet(user_id=u.id, balance_pts=saldo)
    db.session.add(w)
    db.session.flush()
    return w


def _tx(w: Wallet, pts: int, *, tipo=TxType.BONUS, status=TxStatus.CONFIRMED,
        ref: str | None = None, chave: str | None = None) -> Transaction:
    t = Transaction(wallet_id=w.id, type=tipo, status=status, amount_pts=pts,
                    description="t", reference=ref, idempotency_key=chave)
    db.session.add(t)
    db.session.flush()
    return t


def _cobranca(w: Wallet, *, status: PixChargeStatus, cents: int = 5000) -> PixCharge:
    c = PixCharge(user_id=w.user_id, package_key="p1", amount_cents=cents,
                  points_to_credit=cents, br_code="000201...", status=status,
                  expires_at=datetime(2030, 1, 1))
    db.session.add(c)
    db.session.flush()
    return c


def _resgate(w: Wallet, *, status: PixPayoutStatus, cents: int = 1000) -> PixPayout:
    p = PixPayout(user_id=w.user_id, points_debited=cents, amount_cents=cents,
                  pix_key="a@b.com", status=status)
    db.session.add(p)
    db.session.flush()
    return p


# --------------------------------------------------------------------------- #
def test_base_sadia_nao_gera_achado(app):
    w = _carteira(saldo=300)
    _tx(w, 500)
    _tx(w, -200, tipo=TxType.REDEEM)
    db.session.commit()

    rel = conciliar()
    assert rel.ok, rel.resumo()
    assert rel.carteiras_verificadas == 1
    assert rel.transacoes_verificadas == 2


def test_carteira_vazia_sem_transacoes_esta_ok(app):
    """Saldo 0 sem histórico é o estado de quem acabou de se cadastrar."""
    _carteira(saldo=0)
    db.session.commit()
    assert conciliar().ok


def test_pendente_e_revertida_nao_contam_no_saldo(app):
    """Só CONFIRMED move saldo — somar PENDING/REVERSED daria divergência falsa."""
    w = _carteira(saldo=100)
    _tx(w, 100)
    _tx(w, 9999, status=TxStatus.PENDING)
    _tx(w, 5555, status=TxStatus.REVERSED)
    db.session.commit()
    assert conciliar().ok


def test_saldo_a_mais_que_o_ledger_e_detectado(app):
    """O caso caro: usuário com pontos sem lastro, resgatáveis em dinheiro."""
    w = _carteira(saldo=1000)
    _tx(w, 400)
    db.session.commit()

    rel = conciliar()
    assert not rel.ok
    a = next(a for a in rel.achados if a.tipo == "saldo_divergente")
    assert a.delta_pts == 600
    assert rel.exposicao_pts == 600


def test_saldo_a_menos_que_o_ledger_e_detectado(app):
    w = _carteira(saldo=100)
    _tx(w, 400)
    db.session.commit()

    a = next(a for a in conciliar().achados if a.tipo == "saldo_divergente")
    assert a.delta_pts == -300


def test_idempotencia_ja_e_impedida_pelo_banco(app):
    """A UNIQUE (wallet_id, idempotency_key) torna a duplicata impossível pelo
    ORM — este teste fixa esse fato. O check da conciliação segue existindo como
    backstop para dado anterior à constraint ou chave que vire NULL."""
    import sqlalchemy.exc
    w = _carteira(saldo=200)
    _tx(w, 100, chave="compra:abc")
    with pytest.raises(sqlalchemy.exc.IntegrityError):
        _tx(w, 100, chave="compra:abc")
    db.session.rollback()



def test_chaves_nulas_nao_contam_como_duplicata(app):
    """Transação sem idempotency_key é o caso comum — não pode virar achado."""
    w = _carteira(saldo=300)
    _tx(w, 100)
    _tx(w, 100)
    _tx(w, 100)
    db.session.commit()
    assert not [a for a in conciliar().achados if a.tipo == "idempotencia_duplicada"]


def test_cobranca_paga_sem_credito_e_detectada(app):
    """Cliente pagou e não recebeu: nada falha, o dinheiro só não vira ponto."""
    w = _carteira(saldo=0)
    c = _cobranca(w, status=PixChargeStatus.PAID)
    db.session.commit()

    rel = conciliar()
    a = next(a for a in rel.achados if a.tipo == "cobranca_paga_sem_credito")
    assert c.id in a.chave
    assert "50.00" in a.detalhe


def test_cobranca_paga_com_credito_nao_gera_achado(app):
    w = _carteira(saldo=5000)
    c = _cobranca(w, status=PixChargeStatus.PAID)
    db.session.flush()
    _tx(w, 5000, tipo=TxType.PURCHASE, ref=c.id)
    db.session.commit()
    assert conciliar().ok


def test_cobranca_pendente_nao_exige_credito(app):
    """Só PAID exige crédito — cobrar PENDING geraria ruído em toda base."""
    w = _carteira(saldo=0)
    _cobranca(w, status=PixChargeStatus.PENDING)
    db.session.commit()
    assert conciliar().ok


def test_resgate_sem_debito_e_detectado(app):
    """Payout em curso sem os pontos terem saído = dinheiro saindo de graça."""
    w = _carteira(saldo=1000)
    _tx(w, 1000)
    p = _resgate(w, status=PixPayoutStatus.PROCESSING)
    db.session.commit()

    rel = conciliar()
    a = next(a for a in rel.achados if a.tipo == "resgate_sem_debito")
    assert p.id in a.chave


def test_resgate_falho_nao_exige_debito(app):
    """FAILED foi estornado — exigir débito acusaria divergência onde não há."""
    w = _carteira(saldo=1000)
    _tx(w, 1000)
    _resgate(w, status=PixPayoutStatus.FAILED)
    db.session.commit()
    assert conciliar().ok


def test_conciliacao_nao_escreve_no_banco(app):
    """Regra do módulo: só lê. Corrigir sozinha apagaria a evidência do bug."""
    w = _carteira(saldo=1000)
    _tx(w, 400)
    db.session.commit()
    antes = (w.balance_pts, db.session.query(Transaction).count())

    conciliar()
    db.session.expire_all()

    w2 = db.session.get(Wallet, w.id)
    assert (w2.balance_pts, db.session.query(Transaction).count()) == antes


def test_relatorio_agrega_varios_achados(app):
    w1 = _carteira(saldo=1000, email="um@b.com")
    _tx(w1, 400)
    w2 = _carteira(saldo=0, email="dois@b.com")
    _cobranca(w2, status=PixChargeStatus.PAID)
    db.session.commit()

    rel = conciliar()
    tipos = {a.tipo for a in rel.achados}
    assert "saldo_divergente" in tipos and "cobranca_paga_sem_credito" in tipos
    assert "DIVERGÊNCIA" in rel.resumo()
