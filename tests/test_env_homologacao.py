"""BLAXX_HOMOLOGACAO — a exceção que libera chave Stripe de teste em produção.

A trava existe porque `sk_test_` não cobra de verdade: o checkout parece
funcionar e nenhum dinheiro entra. Estes testes fixam que a exceção é
*deliberada e estreita* — sem a flag, chave de teste continua derrubando o
boot; com a flag, só essa checagem cede, e nada mais.
"""
from __future__ import annotations

import pytest

from app.env_schema import EnvError, validate_env

CHAVE_TESTE = "sk_test_" + "x" * 40
CHAVE_LIVE = "sk_live_" + "x" * 40
WHSEC = "whsec_" + "y" * 40


@pytest.fixture(autouse=True)
def ambiente_limpo(monkeypatch):
    """Produção, com os obrigatórios preenchidos e o resto zerado."""
    for var in (
        "BLAXX_HOMOLOGACAO", "STRIPE_API_KEY", "STRIPE_WEBHOOK_SECRET",
        "STRIPE_PUBLISHABLE_KEY", "CARD_ENABLED", "PIX_PROVIDER",
        "ASAAS_API_KEY", "ASAAS_WEBHOOK_TOKEN", "ASAAS_ENV",
        "PAYOUT_MODE", "ENABLE_DEV_ENDPOINTS",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("FLASK_ENV", "production")
    monkeypatch.setenv("SECRET_KEY", "z" * 40)
    monkeypatch.setenv("JWT_SECRET_KEY", "w" * 40)


def _stripe_de_teste(monkeypatch):
    monkeypatch.setenv("STRIPE_API_KEY", CHAVE_TESTE)
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", WHSEC)


def test_sem_flag_chave_de_teste_derruba_o_boot(monkeypatch):
    _stripe_de_teste(monkeypatch)
    with pytest.raises(EnvError) as exc:
        validate_env(strict=True)
    assert "STRIPE_API_KEY" in str(exc.value)
    # A mensagem precisa ensinar a saída, senão o operador afrouxa FLASK_ENV.
    assert "BLAXX_HOMOLOGACAO" in str(exc.value)


def test_com_flag_chave_de_teste_passa(monkeypatch):
    _stripe_de_teste(monkeypatch)
    monkeypatch.setenv("BLAXX_HOMOLOGACAO", "1")
    assert validate_env(strict=True) == []


def test_flag_nao_dispensa_o_webhook_secret(monkeypatch):
    """A exceção é só sobre live-vs-teste. Sem whsec_ nada credita, e isso
    continua sendo erro mesmo em homologação."""
    monkeypatch.setenv("BLAXX_HOMOLOGACAO", "1")
    monkeypatch.setenv("STRIPE_API_KEY", CHAVE_TESTE)
    with pytest.raises(EnvError) as exc:
        validate_env(strict=True)
    assert "STRIPE_WEBHOOK_SECRET" in str(exc.value)


def test_flag_nao_dispensa_asaas_com_pix_asaas(monkeypatch):
    """Nem afrouxa as exigências do Asaas."""
    monkeypatch.setenv("BLAXX_HOMOLOGACAO", "1")
    monkeypatch.setenv("PIX_PROVIDER", "asaas")
    with pytest.raises(EnvError) as exc:
        validate_env(strict=True)
    texto = str(exc.value)
    assert "ASAAS_API_KEY" in texto and "ASAAS_WEBHOOK_TOKEN" in texto


def test_chave_live_passa_sem_flag(monkeypatch):
    """O caminho normal não regride."""
    monkeypatch.setenv("STRIPE_API_KEY", CHAVE_LIVE)
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", WHSEC)
    assert validate_env(strict=True) == []


def test_flag_avisa_sobre_chave_asaas_de_sandbox(monkeypatch, capsys):
    """Homologação tem de ser BARULHENTA: chave de sandbox passa calada no
    validador (is_asaas_key aceita hmlg), então o aviso é a única pista."""
    monkeypatch.setenv("BLAXX_HOMOLOGACAO", "1")
    monkeypatch.setenv("PIX_PROVIDER", "asaas")
    monkeypatch.setenv("ASAAS_API_KEY", "$aact_hmlg_" + "k" * 60)
    monkeypatch.setenv("ASAAS_WEBHOOK_TOKEN", "t" * 40)
    assert validate_env(strict=True) == []
    saida = capsys.readouterr().err
    assert "HOMOLOGAÇÃO" in saida and "SANDBOX" in saida


def test_valor_invalido_na_flag_e_recusado(monkeypatch):
    monkeypatch.setenv("BLAXX_HOMOLOGACAO", "sim")
    with pytest.raises(EnvError) as exc:
        validate_env(strict=True)
    assert "BLAXX_HOMOLOGACAO" in str(exc.value)
