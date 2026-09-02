"""Teto de seguranca da expiracao (O05).

Expiracao e DEBITO em massa e nao tem desfazer barato: um erro de data zeraria
as carteiras. O comando roda uma passada em dry-run antes de gravar e aborta se
o total passar do teto.
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
from app.models import Transaction, TxStatus, TxType, User, Wallet


@pytest.fixture
def app():
    app = create_app(TestConfig)
    with app.app_context():
        db.create_all()
        yield app
        db.session.remove()
        db.drop_all()


def _carteira_velha(app, pts: int, cpf: str, email: str) -> str:
    """Usuario com `pts` creditados ha 800 dias — alem da janela de 730."""
    with app.app_context():
        u = User(name="V", email=email, cpf=cpf)
        u.set_password("StrongP@ss1!")
        db.session.add(u); db.session.flush()
        w = Wallet(user_id=u.id, balance_pts=pts)
        db.session.add(w); db.session.flush()
        antigo = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=800)
        db.session.add(Transaction(
            wallet_id=w.id, type=TxType.PURCHASE, status=TxStatus.CONFIRMED,
            amount_pts=pts, description="compra antiga", created_at=antigo))
        db.session.commit()
        return w.id


def _rodar(app, *args):
    return app.test_cli_runner().invoke(args=["expirar-pontos", *args])


def test_dry_run_nao_altera_saldo(app):
    wid = _carteira_velha(app, 5_000, "52998224725", "a@x.test")
    r = _rodar(app, "--dry-run")
    assert r.exit_code == 0
    with app.app_context():
        assert db.session.get(Wallet, wid).balance_pts == 5_000


def test_abaixo_do_teto_expira(app):
    wid = _carteira_velha(app, 5_000, "52998224725", "a@x.test")
    r = _rodar(app, "--max-pontos", "1000000")
    assert r.exit_code == 0
    with app.app_context():
        assert db.session.get(Wallet, wid).balance_pts == 0


def test_acima_do_teto_aborta_sem_gravar(app):
    """O caso que justifica o teto: nada pode ser debitado."""
    wid = _carteira_velha(app, 900_000, "52998224725", "a@x.test")
    r = _rodar(app, "--max-pontos", "1000")
    assert r.exit_code == 2
    assert "ABORTADO" in r.output
    with app.app_context():
        assert db.session.get(Wallet, wid).balance_pts == 900_000   # intacto


def test_forcar_passa_por_cima_do_teto(app):
    wid = _carteira_velha(app, 900_000, "52998224725", "a@x.test")
    r = _rodar(app, "--max-pontos", "1000", "--forcar")
    assert r.exit_code == 0
    with app.app_context():
        assert db.session.get(Wallet, wid).balance_pts == 0


def test_teto_nao_bloqueia_dry_run(app):
    """A previa precisa rodar sempre — e dela que sai a decisao de forcar."""
    wid = _carteira_velha(app, 900_000, "52998224725", "a@x.test")
    r = _rodar(app, "--dry-run", "--max-pontos", "1000")
    assert r.exit_code == 0
    with app.app_context():
        assert db.session.get(Wallet, wid).balance_pts == 900_000
