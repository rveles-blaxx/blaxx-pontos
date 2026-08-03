"""Resgate exige CPF verificado, não só bem formatado.

O gate anterior (S3-10) recusava placeholder do Google (`G:...`), o que é um
teste de FORMATO. Um CPF sintaticamente válido e inventado passava direto para
o trilho que tira dinheiro da conta. `services/kyc.py` já sabia validar contra
fonte externa e gravava `user.kyc_validated_at` — mas nenhum código lia esse
campo. Estes testes fixam que agora ele guarda o resgate.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("MAILER", "noop")

from app import create_app
from app.config import TestConfig
from app.extensions import db
from app.models import User, Wallet
from app.services.redeem import RedeemError, request_redeem

CPF_VALIDO = "52998224725"
SENHA = "StrongP@ss1!"


@pytest.fixture
def app():
    app = create_app(TestConfig)
    with app.app_context():
        db.create_all()
        yield app
        db.session.remove()
        db.drop_all()


def _usuario(*, kyc: bool, cpf: str = CPF_VALIDO) -> User:
    u = User(name="Teste", email="t@x.com", cpf=cpf, role="user")
    u.set_password(SENHA)
    u.email_verified_at = datetime.now(timezone.utc)
    if kyc:
        u.kyc_validated_at = datetime.now(timezone.utc)
    db.session.add(u)
    db.session.flush()
    db.session.add(Wallet(user_id=u.id, balance_pts=50_000))
    db.session.commit()
    return u


def _resgatar(u: User):
    return request_redeem(u, points=3000, pix_key="t@x.com", password=SENHA)


def test_usuario_ja_verificado_nao_chama_provedor_externo(app):
    """Quem já tem kyc_validated_at não paga latência de API no resgate."""
    u = _usuario(kyc=True)
    with patch("app.services.kyc.validate_cpf_and_mark_user") as fake:
        _resgatar(u)
        fake.assert_not_called()


def test_cpf_invalido_na_receita_bloqueia_o_resgate(app):
    u = _usuario(kyc=False)
    with patch("app.services.kyc.validate_cpf_and_mark_user",
               return_value={"valid": False, "reason": "não encontrado"}):
        with pytest.raises(RedeemError, match="validar seu CPF"):
            _resgatar(u)


def test_validacao_bem_sucedida_libera_o_resgate(app):
    """Resgate é o único caminho de retry do KYC: a validação do cadastro é
    best-effort e não existe endpoint para refazê-la."""
    u = _usuario(kyc=False)

    def marca(user, **kw):
        user.kyc_validated_at = datetime.now(timezone.utc)
        return {"valid": True}

    with patch("app.services.kyc.validate_cpf_and_mark_user", side_effect=marca):
        payout = _resgatar(u)
    assert payout is not None


def test_provedor_indisponivel_recusa_em_vez_de_liberar(app):
    """Fail-closed: num trilho de saída de dinheiro, 'não consegui verificar'
    tem de ser tratado como 'não verificado'. Fail-open aqui seria a porta
    aberta — bastaria derrubar o provedor para sacar sem verificação."""
    u = _usuario(kyc=False)
    with patch("app.services.kyc.validate_cpf_and_mark_user",
               side_effect=ConnectionError("timeout")):
        with pytest.raises(RedeemError, match="Tente novamente"):
            _resgatar(u)


def test_placeholder_do_google_continua_barrado_antes_do_kyc(app):
    """O gate de formato (S3-10) não regrediu e responde primeiro — a mensagem
    específica é mais útil que a genérica de KYC."""
    u = _usuario(kyc=False, cpf="G:abc123def456")
    with pytest.raises(RedeemError, match="complete seu CPF real"):
        _resgatar(u)
