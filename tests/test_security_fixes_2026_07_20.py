"""Regressão dos achados da revisão de segurança de 2026-07-20.

Cada teste aqui existe para provar que um achado específico está fechado.
Referência: docs/seguranca/REVISAO_SEGURANCA_2026-07-20.md

Roda com:
    cd backend && python -m pytest tests/test_security_fixes_2026_07_20.py -v
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("MAILER", "noop")

from app import create_app
from app.config import TestConfig
from app.extensions import db
from app.models import User
from app.security import validate_password_strength

VALID_CPF = "52998224725"


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


@pytest.fixture
def user_token(client, app):
    """Registra + loga um usuário COMUM (role != admin) e devolve o header."""
    client.post("/auth/register", json={
        "name": "João Silva", "email": "joao@test.com", "cpf": VALID_CPF,
        "password": "StrongP@ss1!", "phone": "11999999999",
        "accept_terms": True, "accept_privacy": True, "accept_lgpd": True,
    })
    with app.app_context():
        u = db.session.query(User).filter_by(email="joao@test.com").one_or_none()
        assert u is not None and u.role != "admin"
    resp = client.post("/auth/login", json={
        "email": "joao@test.com", "password": "StrongP@ss1!",
    })
    body = resp.get_json() or {}
    token = body.get("access_token") or body.get("token") or (
        body.get("tokens") or {}).get("access_token")
    assert token, f"login não devolveu token: {body}"
    return {"Authorization": f"Bearer {token}"}


# ───────────────────────── C-1 (CRÍTICO) ───────────────────────── #

class TestCampaignProgressRequiresAdmin:
    """C-1: /campaigns/<id>/progress aceitava amount_brl do próprio cliente e
    creditava reward_pts (TxType.BONUS) ao bater a meta — sem admin e sem
    vínculo com uma compra. Era criação de dinheiro do nada, resgatável em PIX.
    """

    def test_non_admin_cannot_add_progress(self, client, user_token):
        resp = client.post(
            "/campaigns/qualquer-id/progress",
            json={"amount_brl": 999999},
            headers=user_token,
        )
        # Tem de ser barrado pela autorização ANTES de qualquer lógica de
        # negócio — nunca 200/201, e nem 404/409 (que revelariam que a
        # requisição chegou a ser processada).
        assert resp.status_code == 403, (
            f"esperado 403 p/ não-admin, veio {resp.status_code}: "
            f"{resp.get_data(as_text=True)[:200]}"
        )

    def test_unauthenticated_cannot_add_progress(self, client):
        resp = client.post(
            "/campaigns/qualquer-id/progress", json={"amount_brl": 999999}
        )
        assert resp.status_code in (401, 403, 422)

    def test_no_points_credited_to_attacker(self, client, user_token, app):
        """O ataque completo não pode alterar saldo."""
        client.post(
            "/campaigns/qualquer-id/progress",
            json={"amount_brl": 999999},
            headers=user_token,
        )
        with app.app_context():
            u = db.session.query(User).filter_by(email="joao@test.com").one()
            wallet = u.wallet
            if wallet is not None:
                assert wallet.balance_pts == 0, "atacante creditou pontos!"


# ───────────────────────── A-1 (ALTO) ───────────────────────── #

class TestClientIpNotSpoofable:
    """A-1: o PRIMEIRO elemento de X-Forwarded-For é escrito pelo cliente; o
    proxy anexa o IP real no FIM. Ler o primeiro deixava o atacante escolher a
    própria chave de rate limit (password spraying com XFF rotativo)."""

    def test_picks_last_hop_not_client_supplied_first(self, app):
        from app.extensions import _real_client_ip

        with app.test_request_context(
            headers={"X-Forwarded-For": "1.2.3.4, 203.0.113.9"}
        ):
            assert _real_client_ip() == "203.0.113.9"

    def test_single_hop_chain(self, app):
        from app.extensions import _real_client_ip

        with app.test_request_context(headers={"X-Forwarded-For": "203.0.113.9"}):
            assert _real_client_ip() == "203.0.113.9"

    def test_rotating_forged_header_yields_same_key(self, app):
        """O cerne do achado: rotacionar o header não pode gerar buckets novos."""
        from app.extensions import _real_client_ip

        keys = set()
        for forged in ("1.1.1.1", "2.2.2.2", "3.3.3.3", "4.4.4.4"):
            with app.test_request_context(
                headers={"X-Forwarded-For": f"{forged}, 198.51.100.7"}
            ):
                keys.add(_real_client_ip())
        assert keys == {"198.51.100.7"}, (
            "rate limit ainda contornável rotacionando X-Forwarded-For"
        )

    def test_empty_chain_falls_back(self, app):
        from app.extensions import _real_client_ip

        with app.test_request_context():
            assert _real_client_ip()

    def test_audit_and_pix_share_the_same_implementation(self, app):
        """A-1 também valia para o lock de login (audit) e a whitelist do webhook."""
        from app.api.pix import _client_ip as pix_ip
        from app.services.audit import _client_ip as audit_ip

        with app.test_request_context(
            headers={"X-Forwarded-For": "9.9.9.9, 198.51.100.7"}
        ):
            assert pix_ip() == "198.51.100.7"
            assert audit_ip() == "198.51.100.7"


# ───────────────────────── A-2 (ALTO) ───────────────────────── #

class TestEnvValidationStrictInProduction:
    """A-2: validate_env(strict=False) pulava o bloco de obrigatoriedade
    condicional, então o segredo do webhook nunca era exigido. O app subia sem
    ele, o cliente pagava o PIX e o webhook rejeitava tudo com 401."""

    def test_strict_raises_when_webhook_secret_missing(self, monkeypatch):
        """O mecanismo do A-2 continua valendo — agora para o provider ativo.

        (O MercadoPago foi removido em 2026-08-01; o equivalente hoje é o
        Asaas, cujo webhook autentica o crédito de pontos.)
        """
        from app.env_schema import EnvError, validate_env

        monkeypatch.setenv("SECRET_KEY", "x" * 40)
        monkeypatch.setenv("JWT_SECRET_KEY", "y" * 40)
        monkeypatch.setenv("PIX_PROVIDER", "asaas")
        monkeypatch.setenv("ASAAS_API_KEY", "$aact_prod_" + "x" * 40)
        monkeypatch.delenv("ASAAS_WEBHOOK_TOKEN", raising=False)

        with pytest.raises(EnvError) as exc:
            validate_env(strict=True)
        assert "ASAAS_WEBHOOK_TOKEN" in str(exc.value)

    def test_non_strict_remains_permissive_for_dev(self, monkeypatch):
        """Em dev/test o boot não pode quebrar por falta das envs de produção."""
        from app.env_schema import validate_env

        monkeypatch.setenv("PIX_PROVIDER", "asaas")
        monkeypatch.delenv("ASAAS_WEBHOOK_TOKEN", raising=False)
        validate_env(strict=False)  # não levanta


# ───────────────────────── A-3 (ALTO) ───────────────────────── #

class TestSimulatePaymentGatedByEnvironment:
    """A-3: o gate do /pix/simulate-payment era só o nome do provider. Se o app
    subisse em prod com o MockPixProvider, qualquer usuário creditaria a si
    mesmo o valor integral de uma charge sem pagar."""

    def test_blocked_in_production_without_dev_flag(self, client, user_token, monkeypatch):
        monkeypatch.setenv("FLASK_ENV", "production")
        monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
        monkeypatch.setenv("ENABLE_DEV_ENDPOINTS", "0")

        resp = client.post(
            "/pix/simulate-payment", json={"charge_id": "x"}, headers=user_token
        )
        assert resp.status_code == 404, (
            f"simulate-payment deveria sumir em prod, veio {resp.status_code}"
        )


# ───────────────────────── A-4 (ALTO) ───────────────────────── #

class TestPasswordBlocklistEnforced:
    """A-4: COMMON_PASSWORDS (50 senhas) era código morto — o validador real
    era só len >= 7, então 'senha123' e 'password' passavam."""

    @pytest.mark.parametrize("pwd", ["senha123", "password", "12345678", "blaxx123"])
    def test_dictionary_passwords_rejected(self, pwd):
        issues = validate_password_strength(pwd)
        assert any(i.code == "too_common" for i in issues), (
            f"{pwd!r} deveria ser barrada pela blocklist"
        )

    def test_password_cannot_contain_email_local_part(self):
        issues = validate_password_strength(
            "ricardoveles1", email="ricardoveles@gmail.com"
        )
        assert any(i.code == "contains_email" for i in issues)

    def test_password_cannot_contain_cpf(self):
        issues = validate_password_strength("x12345678909y", cpf="123.456.789-09")
        assert any(i.code == "contains_cpf" for i in issues)

    def test_product_decision_preserved(self):
        """A spec (7+ chars, formato livre, sem exigir complexidade) segue valendo."""
        assert validate_password_strength("abcdefg") == []
        assert validate_password_strength("girassol") == []

    def test_short_password_still_rejected(self):
        assert any(i.code == "too_short" for i in validate_password_strength("abc"))
