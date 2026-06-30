"""Sprint 4 — testes do AML service (sanctions, threshold, velocity)."""

from __future__ import annotations

import os
from datetime import datetime, timezone

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("MAILER", "noop")

from app import create_app
from app.config import TestConfig
from app.extensions import db
from app.models import AmlAlert, Transaction, TxType, User, Wallet


VALID_CPF = "52998224725"


@pytest.fixture
def app():
    a = create_app(TestConfig)
    with a.app_context():
        db.create_all()
        yield a
        db.session.remove()
        db.drop_all()


@pytest.fixture
def user(app):
    with app.app_context():
        u = User(name="Test User", email="aml@test.com", cpf=VALID_CPF, role="user")
        u.set_password("StrongP@ss1!")
        u.email_verified_at = datetime.now(timezone.utc)
        db.session.add(u)
        db.session.flush()
        w = Wallet(user_id=u.id, balance_pts=100_000)
        db.session.add(w)
        db.session.commit()
        return db.session.get(User, u.id)


def test_sanctions_check_empty_list_no_block(app, user):
    """Sem lista carregada / vazia → não bloqueia."""
    with app.app_context():
        from app.services import aml
        aml._sanctions_cache.clear()
        aml._sanctions_mtime = 0
        user_db = db.session.get(User, user.id)
        # Não levanta — sanctions vazio
        aml.check_sanctions_or_raise(user_db)


def test_sanctions_check_blocks_listed_cpf(app, user, tmp_path, monkeypatch):
    """CPF em sanctions list → SanctionsBlock + alerta high."""
    csv = tmp_path / "sanctions.csv"
    csv.write_text(f"name,cpf,reason\nfraudster,{VALID_CPF},test_reason\n", encoding="utf-8")
    monkeypatch.setattr(
        "app.services.aml._sanctions_csv_path",
        lambda: str(csv),
    )
    with app.app_context():
        from app.services import aml
        aml._sanctions_cache.clear()
        aml._sanctions_mtime = 0
        user_db = db.session.get(User, user.id)
        with pytest.raises(aml.SanctionsBlock):
            aml.check_sanctions_or_raise(user_db)
        # Alerta foi gravado
        alerts = db.session.query(AmlAlert).filter_by(
            user_id=user_db.id, kind="sanctions"
        ).all()
        assert len(alerts) == 1
        assert alerts[0].severity == "high"


def test_threshold_alert_on_large_op(app, user):
    """Operação >= AML_THRESHOLD_SINGLE_OP_PTS registra alerta."""
    with app.app_context():
        from app.services import aml
        user_db = db.session.get(User, user.id)
        alert = aml.check_transaction_threshold(
            user_db, amount_pts=50_000, kind="transfer",
            monthly_limit_pts=200_000,
        )
        assert alert is not None
        assert alert.kind == "threshold"
        assert alert.severity in ("medium", "high")
        db.session.commit()


def test_threshold_no_alert_on_small_op(app, user):
    with app.app_context():
        from app.services import aml
        user_db = db.session.get(User, user.id)
        alert = aml.check_transaction_threshold(
            user_db, amount_pts=100, kind="transfer", monthly_limit_pts=100_000,
        )
        assert alert is None


def test_velocity_alert_when_above_threshold(app, user):
    """>=5 ops na ultima hora → alerta velocity."""
    with app.app_context():
        from app.services import aml
        user_db = db.session.get(User, user.id)
        wallet = db.session.query(Wallet).filter_by(user_id=user_db.id).one()
        # Cria 5 transações de saída na ultima hora
        for i in range(5):
            db.session.add(Transaction(
                wallet_id=wallet.id,
                type=TxType.TRANSFER_OUT,
                amount_pts=-100,
                description=f"test {i}",
            ))
        db.session.commit()
        alert = aml.check_velocity(user_db)
        assert alert is not None
        assert alert.kind == "velocity"


def test_velocity_no_alert_when_below_threshold(app, user):
    with app.app_context():
        from app.services import aml
        user_db = db.session.get(User, user.id)
        alert = aml.check_velocity(user_db)
        assert alert is None


def test_is_sanctioned_by_name(app, tmp_path, monkeypatch):
    csv = tmp_path / "sanctions.csv"
    csv.write_text("name,cpf,reason\nJohn Doe,,sanctioned_country\n", encoding="utf-8")
    monkeypatch.setattr("app.services.aml._sanctions_csv_path", lambda: str(csv))
    with app.app_context():
        from app.services import aml
        aml._sanctions_cache.clear()
        aml._sanctions_mtime = 0
        assert aml.is_sanctioned(name="John Doe") is not None
        assert aml.is_sanctioned(name="Other Person") is None
