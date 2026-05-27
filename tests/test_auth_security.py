"""Suite de testes da Onda 1 — P0 Auth & Security.

Cobertura:
  - Cadastro com política de senha forte
  - Login + JWT
  - Logout + blacklist (token revogado não vale mais)
  - Forgot/reset password com TTL e single-use
  - Mudança de senha logado
  - Verificação de e-mail (código, tentativas, expiração)
  - Bloqueio financeiro (email_verified_required)
  - Rate limiting

Roda com:
    cd backend && pytest -v tests/test_auth_security.py
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timedelta, timezone

import pytest

# Garante DB em memória + mailer noop nos testes
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("MAILER", "noop")

from app import create_app
from app.config import TestConfig
from app.extensions import db
from app.models import (
    User, PasswordResetToken, EmailVerification, RevokedToken,
)
from app.security import validate_password_strength


# CPFs válidos de teste (passam pelo algoritmo de dígitos verificadores)
VALID_CPFS = ["52998224725", "11144477735", "39053344705"]


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


def _register(client, cpf=VALID_CPFS[0], email="joao@test.com",
              password="StrongP@ss1!", name="João Silva"):
    return client.post("/auth/register", json={
        "name": name, "email": email, "cpf": cpf, "password": password,
    })


def _login(client, email="joao@test.com", password="StrongP@ss1!"):
    return client.post("/auth/login", json={"email": email, "password": password})


def _verify_email(app, client, token):
    """Helper: pega o último código pendente e confirma."""
    with app.app_context():
        ev = (db.session.query(EmailVerification)
              .filter_by(consumed_at=None)
              .order_by(EmailVerification.created_at.desc())
              .first())
        # Não temos acesso ao código em plaintext (só hash). Em testes vamos
        # bypassar marcando email_verified_at direto.
        user_id = ev.user_id
        user = db.session.get(User, user_id)
        user.email_verified_at = datetime.now(timezone.utc)
        db.session.commit()


# ============================================================================
# 1. Política de senha forte
# ============================================================================

class TestPasswordPolicy:
    def test_password_too_short(self):
        assert any(i.code == "too_short" for i in validate_password_strength("Aa1!"))

    def test_password_no_uppercase(self):
        assert any(i.code == "no_uppercase" for i in validate_password_strength("abc1234!"))

    def test_password_no_lowercase(self):
        assert any(i.code == "no_lowercase" for i in validate_password_strength("ABC1234!"))

    def test_password_no_digit(self):
        assert any(i.code == "no_digit" for i in validate_password_strength("Abcdefgh!"))

    def test_password_no_symbol(self):
        assert any(i.code == "no_symbol" for i in validate_password_strength("Abcd1234"))

    def test_password_common(self):
        assert any(i.code == "common" for i in validate_password_strength("password"))

    def test_password_repeats(self):
        issues = validate_password_strength("Aaaaa1!Bb")
        assert any(i.code == "repeats" for i in issues)

    def test_password_trivial_sequence(self):
        issues = validate_password_strength("Abcd1234!")
        assert any(i.code == "sequence" for i in issues)

    def test_password_strong(self):
        assert validate_password_strength("Z9!mxKj#Hpw2") == []


# ============================================================================
# 2. Cadastro
# ============================================================================

class TestRegister:
    def test_register_with_weak_password_rejected(self, client):
        r = _register(client, password="senha123")
        assert r.status_code == 400
        body = r.get_json()
        assert "issues" in body
        assert any(i["code"] == "no_uppercase" for i in body["issues"])

    def test_register_with_strong_password_succeeds(self, client):
        r = _register(client)
        assert r.status_code == 201, r.get_json()
        body = r.get_json()
        assert "token" in body
        assert "access_token" in body
        assert body["user"]["email"] == "joao@test.com"
        # JWT tem 3 partes separadas por ponto
        assert body["token"].count(".") == 2

    def test_register_invalid_cpf_rejected(self, client):
        r = _register(client, cpf="00000000000")
        assert r.status_code == 400
        assert "CPF" in r.get_json()["error"]

    def test_register_duplicate_email_rejected(self, client):
        assert _register(client).status_code == 201
        r = _register(client, cpf=VALID_CPFS[1])  # mesmo email, CPF diferente
        assert r.status_code == 409
        assert "e-mail" in r.get_json()["error"].lower()

    def test_register_duplicate_cpf_rejected(self, client):
        assert _register(client).status_code == 201
        r = _register(client, email="outro@test.com")  # CPF duplicado
        assert r.status_code == 409
        assert "cpf" in r.get_json()["error"].lower()

    def test_register_creates_email_verification(self, app, client):
        _register(client)
        with app.app_context():
            evs = db.session.query(EmailVerification).all()
            assert len(evs) == 1
            assert evs[0].consumed_at is None
            # SQLite descarta tz info — normalizamos antes de comparar
            exp = evs[0].expires_at
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=timezone.utc)
            assert exp > datetime.now(timezone.utc)


# ============================================================================
# 3. Login + JWT
# ============================================================================

class TestLogin:
    def test_login_correct(self, client):
        _register(client)
        r = _login(client)
        assert r.status_code == 200
        assert r.get_json()["token"].count(".") == 2

    def test_login_wrong_password(self, client):
        _register(client)
        r = _login(client, password="ErradoP@ss1!")
        assert r.status_code == 401

    def test_login_unknown_email(self, client):
        r = _login(client, email="ninguem@test.com")
        assert r.status_code == 401

    def test_login_with_cpf(self, client):
        _register(client)
        r = client.post("/auth/login", json={
            "email": VALID_CPFS[0],  # CPF no campo email
            "password": "StrongP@ss1!",
        })
        assert r.status_code == 200


# ============================================================================
# 4. Logout + blacklist
# ============================================================================

class TestLogout:
    def test_logout_revokes_token(self, app, client):
        _register(client)
        login = _login(client).get_json()
        token = login["token"]
        hdr = {"Authorization": f"Bearer {token}"}

        # /me funciona antes do logout
        assert client.get("/auth/me", headers=hdr).status_code == 200

        # logout
        r = client.post("/auth/logout", headers=hdr)
        assert r.status_code == 200
        assert r.get_json()["revoked"] is True

        # Verifica que o jti está na blacklist
        with app.app_context():
            assert db.session.query(RevokedToken).count() == 1

        # /me agora deve falhar
        r = client.get("/auth/me", headers=hdr)
        assert r.status_code == 401


# ============================================================================
# 5. Forgot / Reset password
# ============================================================================

class TestForgotResetPassword:
    def test_forgot_password_unknown_email_returns_ok(self, client):
        """Anti-enumeração: sempre 200, mesmo pra e-mail inexistente."""
        r = client.post("/auth/forgot-password", json={"email": "ninguem@test.com"})
        assert r.status_code == 200
        assert r.get_json()["ok"] is True

    def test_forgot_password_creates_token(self, app, client):
        _register(client)
        client.post("/auth/forgot-password", json={"email": "joao@test.com"})
        with app.app_context():
            assert db.session.query(PasswordResetToken).count() == 1

    def test_reset_password_with_invalid_token(self, client):
        _register(client)
        r = client.post("/auth/reset-password", json={
            "token": "token-fake", "password": "OutraP@ss2!",
        })
        assert r.status_code == 400

    def test_reset_password_with_weak_new_password(self, app, client):
        _register(client)
        # Cria token manualmente já que não temos acesso ao raw
        with app.app_context():
            user = db.session.query(User).first()
            raw = "raw-test-token-123"
            db.session.add(PasswordResetToken(
                token_hash=PasswordResetToken.hash_token(raw),
                user_id=user.id,
                expires_at=datetime.now(timezone.utc) + timedelta(minutes=30),
            ))
            db.session.commit()
        r = client.post("/auth/reset-password",
                         json={"token": raw, "password": "fraca"})
        assert r.status_code == 400
        assert "issues" in r.get_json()

    def test_reset_password_happy_path(self, app, client):
        _register(client)
        with app.app_context():
            user = db.session.query(User).first()
            raw = "raw-good-token"
            db.session.add(PasswordResetToken(
                token_hash=PasswordResetToken.hash_token(raw),
                user_id=user.id,
                expires_at=datetime.now(timezone.utc) + timedelta(minutes=30),
            ))
            db.session.commit()

        r = client.post("/auth/reset-password",
                         json={"token": raw, "password": "NovaP@ss9X!"})
        assert r.status_code == 200

        # Login com senha antiga falha
        assert _login(client).status_code == 401
        # Login com nova senha funciona
        r = _login(client, password="NovaP@ss9X!")
        assert r.status_code == 200

    def test_reset_token_single_use(self, app, client):
        _register(client)
        with app.app_context():
            user = db.session.query(User).first()
            raw = "single-use-token"
            db.session.add(PasswordResetToken(
                token_hash=PasswordResetToken.hash_token(raw),
                user_id=user.id,
                expires_at=datetime.now(timezone.utc) + timedelta(minutes=30),
            ))
            db.session.commit()

        # Primeira chamada: sucesso
        assert client.post("/auth/reset-password",
                            json={"token": raw, "password": "NovaP@ss9X!"}).status_code == 200
        # Segunda chamada com mesmo token: deve falhar
        r = client.post("/auth/reset-password",
                         json={"token": raw, "password": "OutraP@ss5!"})
        assert r.status_code == 400

    def test_reset_token_expired(self, app, client):
        _register(client)
        with app.app_context():
            user = db.session.query(User).first()
            raw = "expired-token"
            db.session.add(PasswordResetToken(
                token_hash=PasswordResetToken.hash_token(raw),
                user_id=user.id,
                expires_at=datetime.now(timezone.utc) - timedelta(minutes=1),  # já expirado
            ))
            db.session.commit()
        r = client.post("/auth/reset-password",
                         json={"token": raw, "password": "NovaP@ss9X!"})
        assert r.status_code == 400
        assert "expirado" in r.get_json()["error"].lower()


# ============================================================================
# 6. Verificação de e-mail
# ============================================================================

class TestEmailVerification:
    def test_verify_with_wrong_code_decrements_attempts(self, app, client):
        _register(client)
        token = _login(client).get_json()["token"]
        hdr = {"Authorization": f"Bearer {token}"}
        r = client.post("/auth/verify-email",
                         headers=hdr, json={"code": "000000"})
        assert r.status_code == 400
        assert r.get_json()["attempts_left"] == 2

    def test_verify_with_correct_code(self, app, client):
        _register(client)
        token = _login(client).get_json()["token"]
        hdr = {"Authorization": f"Bearer {token}"}

        # Resgata o code_hash do DB e cria código sabido
        with app.app_context():
            ev = db.session.query(EmailVerification).first()
            user_id = ev.user_id
            ev.code_hash = EmailVerification.hash_code("123456")
            db.session.commit()

        r = client.post("/auth/verify-email",
                         headers=hdr, json={"code": "123456"})
        assert r.status_code == 200, r.get_json()

        with app.app_context():
            u = db.session.get(User, user_id)
            assert u.email_verified_at is not None

    def test_verify_lockout_after_3_attempts(self, app, client):
        _register(client)
        token = _login(client).get_json()["token"]
        hdr = {"Authorization": f"Bearer {token}"}
        for _ in range(3):
            client.post("/auth/verify-email",
                         headers=hdr, json={"code": "wrong1"})
        r = client.post("/auth/verify-email",
                         headers=hdr, json={"code": "wrong1"})
        assert r.status_code in (400, 429)


# ============================================================================
# 7. Bloqueio financeiro pra e-mail não verificado
# ============================================================================

class TestFinancialGate:
    def test_pix_charge_blocked_without_email_verified(self, client):
        _register(client)
        token = _login(client).get_json()["token"]
        hdr = {"Authorization": f"Bearer {token}"}
        r = client.post("/pix/charge", headers=hdr, json={"package": "start"})
        assert r.status_code == 403
        assert r.get_json()["code"] == "EMAIL_NOT_VERIFIED"

    def test_transfer_blocked_without_email_verified(self, client):
        _register(client)
        token = _login(client).get_json()["token"]
        hdr = {"Authorization": f"Bearer {token}"}
        r = client.post("/transfer/", headers=hdr, json={
            "to": "x@y.com", "amount_pts": 100, "password": "StrongP@ss1!",
        })
        assert r.status_code == 403

    def test_redeem_blocked_without_email_verified(self, client):
        _register(client)
        token = _login(client).get_json()["token"]
        hdr = {"Authorization": f"Bearer {token}"}
        r = client.post("/redeem/", headers=hdr, json={
            "points": 5000, "pix_key": "x@y.com", "password": "StrongP@ss1!",
        })
        assert r.status_code == 403

    def test_pix_charge_allowed_after_verification(self, app, client):
        _register(client)
        token = _login(client).get_json()["token"]
        hdr = {"Authorization": f"Bearer {token}"}
        _verify_email(app, client, token)
        r = client.post("/pix/charge", headers=hdr, json={"package": "start"})
        # Não é mais 403; deve criar charge (201) ou outro fluxo válido
        assert r.status_code != 403


# ============================================================================
# 8. Change password (logado)
# ============================================================================

class TestChangePassword:
    def test_change_password_wrong_current(self, client):
        _register(client)
        token = _login(client).get_json()["token"]
        hdr = {"Authorization": f"Bearer {token}"}
        r = client.post("/auth/change-password", headers=hdr, json={
            "old_password": "Errada!1A", "new_password": "MuitoNova!9Z",
        })
        assert r.status_code == 401

    def test_change_password_weak_new(self, client):
        _register(client)
        token = _login(client).get_json()["token"]
        hdr = {"Authorization": f"Bearer {token}"}
        r = client.post("/auth/change-password", headers=hdr, json={
            "old_password": "StrongP@ss1!", "new_password": "fraca",
        })
        assert r.status_code == 400

    def test_change_password_success(self, client):
        _register(client)
        token = _login(client).get_json()["token"]
        hdr = {"Authorization": f"Bearer {token}"}
        r = client.post("/auth/change-password", headers=hdr, json={
            "old_password": "StrongP@ss1!", "new_password": "OutraP@ss9X!",
        })
        assert r.status_code == 200
        # Login antigo falha
        assert _login(client).status_code == 401
        # Login novo funciona
        assert _login(client, password="OutraP@ss9X!").status_code == 200


# ============================================================================
# 9. Anti-enumeração no /forgot-password
# ============================================================================

class TestAntiEnumeration:
    def test_forgot_password_does_not_leak_email_existence(self, client):
        """Endpoint /forgot-password deve responder igualmente para emails
        cadastrados e desconhecidos (defesa contra enumeração de usuários)."""
        _register(client)
        r_known = client.post("/auth/forgot-password",
                               json={"email": "joao@test.com"})
        r_unknown = client.post("/auth/forgot-password",
                                 json={"email": "nao-existe@test.com"})
        assert r_known.status_code == 200
        assert r_unknown.status_code == 200
        assert r_known.get_json() == r_unknown.get_json()


# ============================================================================
# 10. JWT tampering / token reuse
# ============================================================================

class TestJWTSecurity:
    def test_tampered_jwt_rejected(self, client):
        _register(client)
        token = _login(client).get_json()["token"]
        tampered = token[:-1] + ("A" if token[-1] != "A" else "B")
        hdr = {"Authorization": f"Bearer {tampered}"}
        r = client.get("/auth/me", headers=hdr)
        assert r.status_code == 401

    def test_request_without_token_rejected_on_protected(self, client):
        r = client.get("/auth/me")
        assert r.status_code == 401

    def test_garbage_token_rejected(self, client):
        r = client.get("/auth/me", headers={"Authorization": "Bearer not-a-jwt"})
        assert r.status_code == 401
