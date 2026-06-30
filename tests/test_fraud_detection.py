"""Sprint 5 — testes do fraud.py paths."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("MAILER", "noop")

from app import create_app
from app.config import TestConfig
from app.extensions import db
from app.models import AuditLog, Transfer, User, Wallet


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
        u = User(name="Fraud Test", email="fr@test.com", cpf=VALID_CPF)
        u.set_password("StrongP@ss1!")
        u.email_verified_at = datetime.now(timezone.utc)
        db.session.add(u)
        db.session.flush()
        db.session.add(Wallet(user_id=u.id, balance_pts=100_000))
        db.session.commit()
        return db.session.get(User, u.id)


def test_evaluate_transfer_no_alert_small_value(app, user):
    """Valor baixo, sem velocity → sem alerta."""
    with app.test_request_context("/transfer/"):
        from app.services import fraud
        reasons = fraud.evaluate_transfer(user.id, "other-user-id", amount_pts=100)
        assert reasons == []


def test_evaluate_transfer_alerts_on_high_value(app, user):
    """Valor >= ALERT_HIGH_VALUE_PTS → alerta gravado."""
    from app.config import Config
    with app.test_request_context("/transfer/"):
        from app.services import fraud
        reasons = fraud.evaluate_transfer(
            user.id, "other", amount_pts=Config.ALERT_HIGH_VALUE_PTS + 1,
        )
        assert any("valor alto" in r for r in reasons)
        db.session.commit()
        logs = db.session.query(AuditLog).filter_by(
            event="suspicious_transfer", user_id=user.id,
        ).all()
        assert len(logs) >= 1
        assert logs[0].status == "warn"


def test_evaluate_transfer_alerts_on_velocity(app, user):
    """Vários envios em janela curta → alerta velocity."""
    from app.config import Config
    with app.test_request_context("/transfer/"):
        from app.services import fraud
        now = datetime.now(timezone.utc)
        for i in range(Config.ALERT_VELOCITY_COUNT + 1):
            t = Transfer(
                sender_id=user.id,
                recipient_id=f"recip-{i % 2}",
                amount_pts=50,
                receipt_code=Transfer.make_receipt(),
                created_at=now - timedelta(minutes=1),
            )
            db.session.add(t)
        db.session.commit()
        reasons = fraud.evaluate_transfer(user.id, "recip-0", 50)
        assert any("velocidade" in r for r in reasons)
