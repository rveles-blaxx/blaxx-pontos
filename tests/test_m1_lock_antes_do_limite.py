"""M-1: o lock da carteira precisa vir ANTES da soma do periodo.

O padrao antigo era soma -> compara -> debita, com o lock adquirido so dentro
do debit. Duas requisicoes simultaneas liam o mesmo total e ambas passavam,
furando o teto diario/mensal. O saldo nunca esteve em risco; o que vazava era
o limite regulatorio de AML.

Este teste nao tenta reproduzir concorrencia real (SQLite nao serve para isso).
Ele prova a INVARIANTE que torna a corrida impossivel: o SELECT ... FOR UPDATE
da carteira acontece antes da primeira leitura de agregado.
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
from app.services import redeem as redeem_svc
from app.services import transfer as transfer_svc
from app.services import wallet as wallet_svc

SENHA = "StrongP@ss1!"


@pytest.fixture
def app():
    app = create_app(TestConfig)
    with app.app_context():
        db.create_all()
        yield app
        db.session.remove()
        db.drop_all()


def _user(app, cpf, email, saldo):
    with app.app_context():
        u = User(name="U", email=email, cpf=cpf)
        u.set_password(SENHA)
        u.email_verified_at = datetime.now(timezone.utc)
        u.pix_key = email
        db.session.add(u); db.session.flush()
        db.session.add(Wallet(user_id=u.id, balance_pts=saldo))
        db.session.commit()
        return u.id


def _ordem(monkeypatch, modulo):
    """Grava a ordem das chamadas relevantes no servico."""
    ordem = []
    real_lock = wallet_svc.get_wallet_for_update

    def lock(uid, *a, **k):
        ordem.append("lock")
        return real_lock(uid, *a, **k)

    monkeypatch.setattr(modulo.wallet_svc, "get_wallet_for_update", lock)
    for nome in ("debited_today", "debited_this_month",
                 "net_redeemed_today", "net_redeemed_this_month"):
        if hasattr(modulo.wallet_svc, nome):
            real = getattr(wallet_svc, nome)
            def faz(n=nome, r=real):
                def f(uid, *a, **k):
                    ordem.append("soma")
                    return r(uid, *a, **k)
                return f
            monkeypatch.setattr(modulo.wallet_svc, nome, faz())
    return ordem


def test_resgate_trava_a_carteira_antes_de_somar(app, monkeypatch):
    uid = _user(app, "52998224725", "r@x.test", 50_000)
    ordem = _ordem(monkeypatch, redeem_svc)
    with app.app_context():
        u = db.session.get(User, uid)
        try:
            redeem_svc.request_redeem(u, points=2_000, pix_key="r@x.test", password=SENHA)
        except Exception:
            pass
    assert "lock" in ordem, "a carteira nunca foi travada"
    assert ordem.index("lock") < ordem.index("soma"), \
        f"soma do periodo rodou ANTES do lock: {ordem}"


def test_transferencia_trava_a_carteira_antes_de_somar(app, monkeypatch):
    uid = _user(app, "52998224725", "a@x.test", 50_000)
    _user(app, "15350946056", "b@x.test", 0)
    ordem = _ordem(monkeypatch, transfer_svc)
    with app.app_context():
        u = db.session.get(User, uid)
        try:
            transfer_svc.send(u, recipient_identifier="b@x.test", amount_pts=1_000,
                              password=SENHA, message=None)
        except Exception:
            pass
    assert "lock" in ordem, "a carteira nunca foi travada"
    assert ordem.index("lock") < ordem.index("soma"), \
        f"soma do periodo rodou ANTES do lock: {ordem}"
