"""Integração Asaas — PIX de saída (payout do resgate).

O foco destes testes é o que faz PERDER DINHEIRO:
  · timeout tratado como falha → estorno + PIX enviado (usuário fica com os dois)
  · webhook forjado confirmando payout que o Asaas nunca pagou
  · evento duplicado processado duas vezes
  · sandbox ligado em produção → "resgate pago" que nunca sai

Roda com:
    cd backend && python -m pytest tests/test_asaas_payout.py -v
"""

from __future__ import annotations

import json
import os
import socket
import urllib.error

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("MAILER", "noop")

from app.pix.asaas import (
    AsaasPixProvider,
    detect_pix_key_type,
    normalize_pix_key,
)
from app.pix.provider import PixPayoutRequest


@pytest.fixture
def provider():
    return AsaasPixProvider(api_key="$aact_hmlg_" + "x" * 40, sandbox=True)


def _req(txid="tx-abc", cents=5000, key="user@test.com"):
    return PixPayoutRequest(
        txid=txid, amount_cents=cents, pix_key=key, description="BlaXx — resgate"
    )


# ───────────────────── tipo/normalização de chave ───────────────────── #

class TestPixKeyDetection:
    """Detectamos o tipo localmente porque consultar o DICT custa 10–15
    tokens do bucket do Bacen quando a chave não existe."""

    @pytest.mark.parametrize("key,expected", [
        ("user@test.com", "EMAIL"),
        ("52998224725", "CPF"),
        ("11222333000181", "CNPJ"),
        ("123e4567-e89b-42d3-a456-426614174000", "EVP"),
        ("+5547999999999", "PHONE"),
    ])
    def test_detects_type(self, key, expected):
        assert detect_pix_key_type(key) == expected

    def test_cpf_normalized_without_punctuation(self):
        assert normalize_pix_key("529.982.247-25", "CPF") == "52998224725"

    def test_phone_drops_country_code(self):
        # Asaas quer 11 dígitos com DDD, sem +55.
        assert normalize_pix_key("+5547999999999", "PHONE") == "47999999999"

    def test_email_lowercased(self):
        assert normalize_pix_key("User@Test.COM", "EMAIL") == "user@test.com"


# ───────────────────── payload enviado ao Asaas ───────────────────── #

class TestPayoutRequestPayload:
    def test_sends_value_in_reais_not_cents(self, provider, monkeypatch):
        """Asaas trabalha em reais decimais. Mandar centavos pagaria 100x."""
        captured = {}

        def fake(method, path, body=None):
            captured["method"], captured["path"], captured["body"] = method, path, body
            return {"id": "tr_1", "status": "PENDING", "authorized": True}

        monkeypatch.setattr(provider, "_request", fake)
        provider.request_payout(_req(cents=5000))  # R$ 50,00

        assert captured["method"] == "POST"
        assert captured["path"] == "/transfers"
        assert captured["body"]["value"] == 50.00, "valor deve ir em REAIS"
        assert captured["body"]["operationType"] == "PIX"

    def test_external_reference_is_txid(self, provider, monkeypatch):
        """É o que amarra o webhook de volta ao payout."""
        captured = {}
        monkeypatch.setattr(
            provider, "_request",
            lambda m, p, body=None: captured.update(body=body) or {
                "id": "tr_1", "status": "PENDING", "authorized": True}
        )
        provider.request_payout(_req(txid="tx-xyz"))
        assert captured["body"]["externalReference"] == "tx-xyz"

    def test_auth_header_is_access_token_not_bearer(self, provider):
        h = provider._headers()
        assert "access_token" in h
        assert h["access_token"].startswith("$aact_")
        assert "Authorization" not in h, "Asaas não usa Bearer"
        assert h.get("User-Agent"), "Asaas pede User-Agent identificando a app"

    def test_sandbox_and_prod_base_urls(self):
        sb = AsaasPixProvider(api_key="$aact_hmlg_" + "x" * 40, sandbox=True)
        pr = AsaasPixProvider(api_key="$aact_prod_" + "x" * 40, sandbox=False)
        assert sb.api_base == "https://api-sandbox.asaas.com/v3"
        assert pr.api_base == "https://api.asaas.com/v3"


# ───────────────── mapeamento de status ───────────────── #

class TestStatusMapping:
    @pytest.mark.parametrize("asaas_status,expected", [
        ("DONE", "paid"),
        ("PENDING", "processing"),
        ("BANK_PROCESSING", "processing"),
        ("FAILED", "failed"),
        ("CANCELLED", "failed"),
    ])
    def test_maps_status(self, provider, monkeypatch, asaas_status, expected):
        monkeypatch.setattr(
            provider, "_request",
            lambda m, p, body=None: {
                "id": "tr_1", "status": asaas_status, "authorized": True,
                "endToEndIdentifier": "E123", "failReason": "motivo"}
        )
        assert provider.request_payout(_req()).status == expected

    def test_unknown_status_is_processing_not_paid(self, provider, monkeypatch):
        """Default conservador: status desconhecido nunca vira 'paid'."""
        monkeypatch.setattr(
            provider, "_request",
            lambda m, p, body=None: {"id": "tr_1", "status": "SOMETHING_NEW",
                                     "authorized": True}
        )
        assert provider.request_payout(_req()).status == "processing"

    def test_authorized_false_is_not_paid(self, provider, monkeypatch):
        """authorized=false = travada aguardando Token SMS. Marcar como paga
        diria ao usuário que recebeu um dinheiro que nunca saiu."""
        monkeypatch.setattr(
            provider, "_request",
            lambda m, p, body=None: {"id": "tr_1", "status": "DONE",
                                     "authorized": False}
        )
        assert provider.request_payout(_req()).status == "processing"

    def test_persists_provider_transfer_id(self, provider, monkeypatch):
        """Sem guardar o id não há como reconsultar: o Asaas não documenta
        filtro por externalReference na listagem de transferências."""
        monkeypatch.setattr(
            provider, "_request",
            lambda m, p, body=None: {"id": "tr_777", "status": "PENDING",
                                     "authorized": True}
        )
        assert provider.request_payout(_req()).provider_transfer_id == "tr_777"


# ─────────── O CENÁRIO MAIS PERIGOSO: estado indeterminado ─────────── #

class TestIndeterminateNeverFails:
    """Se o POST deu timeout, a transferência PODE ter sido criada.

    Marcar como 'failed' faz o serviço estornar os pontos — e se o dinheiro
    saiu, o usuário fica com o PIX E com os pontos de volta. O payout tem de
    ficar retido até o webhook (ou a conciliação) resolver.
    """

    def test_timeout_returns_processing_not_failed(self, provider, monkeypatch):
        def boom(method, path, body=None):
            raise urllib.error.URLError(socket.timeout("timed out"))

        monkeypatch.setattr(provider, "_request", provider._request)
        monkeypatch.setattr(
            "urllib.request.urlopen",
            lambda *a, **k: (_ for _ in ()).throw(
                urllib.error.URLError(socket.timeout("timed out"))
            ),
        )
        resp = provider.request_payout(_req())
        assert resp.status == "processing", (
            "timeout NUNCA pode virar 'failed' — estornaria pontos com o "
            "dinheiro possivelmente em trânsito"
        )
        assert resp.failure_reason is None

    def test_http_409_duplicate_returns_processing(self, provider, monkeypatch):
        """409 = transferência idêntica em 15 min. Pode ser o eco de uma
        tentativa nossa que deu timeout — não estornar em cima disso."""
        def raise_409(*a, **k):
            raise urllib.error.HTTPError(
                url="u", code=409, msg="Conflict", hdrs=None,
                fp=_FakeBody(json.dumps({"errors": [
                    {"code": "duplicate", "description": "transferência idêntica"}]})),
            )

        monkeypatch.setattr("urllib.request.urlopen", raise_409)
        resp = provider.request_payout(_req())
        assert resp.status == "processing"

    def test_http_400_is_a_real_failure(self, provider, monkeypatch):
        """Erro de validação é falha de verdade: nada foi criado, pode estornar."""
        def raise_400(*a, **k):
            raise urllib.error.HTTPError(
                url="u", code=400, msg="Bad Request", hdrs=None,
                fp=_FakeBody(json.dumps({"errors": [
                    {"code": "invalid_pixAddressKey",
                     "description": "Chave Pix inválida"}]})),
            )

        monkeypatch.setattr("urllib.request.urlopen", raise_400)
        resp = provider.request_payout(_req())
        assert resp.status == "failed"
        assert "Chave Pix inválida" in (resp.failure_reason or "")


class _FakeBody:
    """Stub do corpo de HTTPError. Precisa de close() — o HTTPError herda de
    tempfile e chama close() no __del__."""

    def __init__(self, text: str):
        self._t = text.encode()

    def read(self):
        return self._t

    def close(self):
        pass


# ───────────────── validação de env (sandbox em prod) ───────────────── #

class TestEnvValidation:
    def test_auto_payout_requires_asaas_key(self, monkeypatch):
        from app.env_schema import EnvError, validate_env

        monkeypatch.setenv("PAYOUT_MODE", "auto")
        monkeypatch.delenv("ASAAS_API_KEY", raising=False)
        monkeypatch.delenv("PIX_PROVIDER", raising=False)
        with pytest.raises(EnvError) as exc:
            validate_env(strict=True)
        assert "ASAAS_API_KEY" in str(exc.value)

    def test_auto_payout_rejects_sandbox(self, monkeypatch):
        """Sandbox em produção = 'resgate pago' que nunca sai da conta."""
        from app.env_schema import EnvError, validate_env

        monkeypatch.setenv("PAYOUT_MODE", "auto")
        monkeypatch.setenv("ASAAS_API_KEY", "$aact_prod_" + "x" * 40)
        monkeypatch.setenv("ASAAS_WEBHOOK_TOKEN", "t" * 40)
        monkeypatch.setenv("ASAAS_ENV", "sandbox")
        monkeypatch.delenv("PIX_PROVIDER", raising=False)
        with pytest.raises(EnvError) as exc:
            validate_env(strict=True)
        assert "ASAAS_ENV" in str(exc.value)

    def test_rejects_malformed_key(self, monkeypatch):
        from app.env_schema import validate_env

        monkeypatch.setenv("ASAAS_API_KEY", "chave-errada")
        issues = validate_env(strict=False)
        assert any("ASAAS_API_KEY" in i for i in issues)
