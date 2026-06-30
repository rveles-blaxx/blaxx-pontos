"""Sprint 4 — testes do KYC service (validação CPF mockando BrasilAPI)."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from unittest.mock import patch, MagicMock

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("MAILER", "noop")

from app import create_app
from app.config import TestConfig
from app.extensions import db
from app.models import CpfValidation, User, Wallet


VALID_CPF = "52998224725"


@pytest.fixture
def app():
    a = create_app(TestConfig)
    with a.app_context():
        db.create_all()
        yield a
        db.session.remove()
        db.drop_all()


def _mk_user(app, cpf=VALID_CPF, email="kyc@test.com"):
    with app.app_context():
        u = User(name="Test KYC", email=email, cpf=cpf, role="user")
        u.set_password("StrongP@ss1!")
        db.session.add(u)
        db.session.flush()
        db.session.add(Wallet(user_id=u.id, balance_pts=0))
        db.session.commit()
        return db.session.get(User, u.id)


def test_validate_cpf_malformed_returns_invalid(app):
    with app.app_context():
        from app.services import kyc
        res = kyc.validate_cpf_remote("123")
        assert res["valid"] is False
        assert res["error"] == "cpf_malformado"


def test_validate_cpf_brasilapi_success(app):
    """Mock BrasilAPI retornando 200 com payload válido."""
    with app.app_context():
        from app.services import kyc
        fake_response = MagicMock()
        fake_response.__enter__ = lambda self: self
        fake_response.__exit__ = lambda *a: None
        fake_response.read = lambda: json.dumps({"cpf": VALID_CPF, "nome": "TESTE"}).encode()
        with patch("urllib.request.urlopen", return_value=fake_response):
            res = kyc.validate_cpf_remote(VALID_CPF)
        assert res["valid"] is True
        # Foi cacheado
        rows = db.session.query(CpfValidation).filter_by(cpf=VALID_CPF).all()
        assert len(rows) == 1
        assert rows[0].valid is True
        assert rows[0].provider == "brasilapi"


def test_validate_cpf_brasilapi_404_returns_invalid(app):
    """BrasilAPI 404 → CPF não encontrado/invalid."""
    import urllib.error
    with app.app_context():
        from app.services import kyc
        with patch("urllib.request.urlopen", side_effect=urllib.error.HTTPError(
            "url", 404, "Not Found", {}, None,
        )):
            res = kyc.validate_cpf_remote("12345678901")
        assert res["valid"] is False


def test_validate_cpf_provider_down_returns_none(app):
    """Provider offline → valid=None (NÃO bloqueia cadastro)."""
    import urllib.error
    with app.app_context():
        from app.services import kyc
        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("dns")):
            res = kyc.validate_cpf_remote(VALID_CPF)
        assert res["valid"] is None
        assert res["error"] and "indisponivel" in res["error"]
        # Mesmo offline, gravamos o tentativa (audita)
        rows = db.session.query(CpfValidation).filter_by(cpf=VALID_CPF).all()
        assert len(rows) >= 1


def test_validate_and_mark_user(app):
    """Sucesso na validação → user.kyc_validated_at preenchido."""
    user = _mk_user(app)
    with app.app_context():
        from app.services import kyc
        fake_response = MagicMock()
        fake_response.__enter__ = lambda self: self
        fake_response.__exit__ = lambda *a: None
        fake_response.read = lambda: json.dumps({"cpf": VALID_CPF, "nome": "X"}).encode()
        with patch("urllib.request.urlopen", return_value=fake_response):
            user_db = db.session.get(User, user.id)
            res = kyc.validate_cpf_and_mark_user(user_db)
        assert res["valid"] is True
        user_db = db.session.get(User, user.id)
        assert user_db.kyc_validated_at is not None
        assert user_db.kyc_provider == "brasilapi"
        assert user_db.kyc_pending is False


def test_kyc_pending_true_by_default(app):
    """User recém-criado sem validação → kyc_pending=True."""
    user = _mk_user(app)
    with app.app_context():
        user_db = db.session.get(User, user.id)
        assert user_db.kyc_pending is True


def test_cache_hit_avoids_second_call(app):
    """Segunda chamada com mesmo CPF dentro do TTL não chama BrasilAPI."""
    with app.app_context():
        from app.services import kyc
        # Insere validação válida recente direto no cache
        db.session.add(CpfValidation(
            cpf=VALID_CPF, valid=True, provider="brasilapi",
        ))
        db.session.commit()
        call_count = {"n": 0}

        def _spy(*args, **kwargs):
            call_count["n"] += 1
            raise AssertionError("BrasilAPI não deveria ter sido chamado")

        with patch("urllib.request.urlopen", side_effect=_spy):
            res = kyc.validate_cpf_remote(VALID_CPF)
        assert res["valid"] is True
        assert res["cached"] is True
        assert call_count["n"] == 0
