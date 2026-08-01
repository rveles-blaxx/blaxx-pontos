"""Stripe — cartão internacional.

Foco: a verificação da assinatura do webhook (a Stripe assina o corpo, ao
contrário do Asaas) e o mapeamento de status.

Roda com:
    cd backend && python -m pytest tests/test_stripe_card.py -v
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import socket
import time
import urllib.error

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("MAILER", "noop")

from app.pix.provider import CardChargeRequest
from app.pix.stripe_card import StripeCardProvider, StripeError

WEBHOOK_SECRET = "whsec_" + "s" * 32


@pytest.fixture
def provider():
    return StripeCardProvider(
        api_key="sk_test_" + "x" * 24, webhook_secret=WEBHOOK_SECRET
    )


def _req(ref="card-1", cents=18000):
    return CardChargeRequest(
        external_reference=ref,
        amount_cents=cents,
        description="Pacote Start",
        card_token="pm_1234567890",
        payment_method_id="visa",
        installments=1,
        payer_name="John Doe",
        payer_cpf="529.982.247-25",
        payer_email="john@test.com",
    )


class _Body:
    def __init__(self, text):
        self._t = text.encode()

    def read(self):
        return self._t

    def close(self):
        pass


# ───────────────── criação do PaymentIntent ───────────────── #

class TestCreatePayment:
    def test_sends_amount_in_cents_and_idempotency_key(self, provider, monkeypatch):
        captured = {}

        def fake(method, path, params=None, idempotency_key=None):
            captured.update(method=method, path=path, params=params,
                            idem=idempotency_key)
            return {"id": "pi_1", "status": "succeeded", "charges": {"data": []}}

        monkeypatch.setattr(provider, "_request", fake)
        provider.create_card_payment(_req(cents=18000))

        assert captured["path"] == "/payment_intents"
        # Stripe usa a MENOR unidade — centavos, igual ao nosso amount_cents.
        assert captured["params"]["amount"] == 18000
        assert captured["params"]["currency"] == "brl"
        assert captured["params"]["payment_method"] == "pm_1234567890"
        # Idempotência nativa: retry não cobra duas vezes.
        assert captured["idem"] == "card:card-1"

    def test_cpf_is_masked_in_metadata(self, provider, monkeypatch):
        """Metadata da Stripe é lida por humanos no dashboard — não mandar
        documento inteiro para fora."""
        captured = {}
        monkeypatch.setattr(
            provider, "_request",
            lambda m, p, params=None, idempotency_key=None: captured.update(
                params=params) or {"id": "pi_1", "status": "succeeded"}
        )
        provider.create_card_payment(_req())
        cpf_meta = captured["params"]["metadata[payer_cpf]"]
        assert "52998224725" not in cpf_meta
        assert cpf_meta.startswith("***")

    def test_no_redirect_flows(self, provider, monkeypatch):
        """O resgate acontece no app; não há página de retorno para 3DS."""
        captured = {}
        monkeypatch.setattr(
            provider, "_request",
            lambda m, p, params=None, idempotency_key=None: captured.update(
                params=params) or {"id": "pi_1", "status": "succeeded"}
        )
        provider.create_card_payment(_req())
        assert captured["params"]["automatic_payment_methods[allow_redirects]"] == "never"

    @pytest.mark.parametrize("stripe_status,expected", [
        ("succeeded", "approved"),
        ("processing", "in_process"),
        ("requires_action", "in_process"),
        ("requires_payment_method", "rejected"),
        ("canceled", "cancelled"),
    ])
    def test_status_mapping(self, provider, monkeypatch, stripe_status, expected):
        monkeypatch.setattr(
            provider, "_request",
            lambda m, p, params=None, idempotency_key=None: {
                "id": "pi_1", "status": stripe_status}
        )
        assert provider.create_card_payment(_req()).status == expected

    def test_timeout_is_in_process_not_rejected(self, provider, monkeypatch):
        """Com Idempotency-Key o retry é seguro, mas ainda não sabemos o
        resultado — deixar in_process e esperar o webhook."""
        def boom(*a, **k):
            raise urllib.error.URLError(socket.timeout("timed out"))

        monkeypatch.setattr("urllib.request.urlopen", boom)
        resp = provider.create_card_payment(_req())
        assert resp.status == "in_process"

    def test_decline_is_rejected_with_reason(self, provider, monkeypatch):
        def raise_402(*a, **k):
            raise urllib.error.HTTPError(
                url="u", code=402, msg="Payment Required", hdrs=None,
                fp=_Body(json.dumps({"error": {
                    "code": "card_declined",
                    "decline_code": "insufficient_funds",
                    "message": "Your card has insufficient funds."}})),
            )

        monkeypatch.setattr("urllib.request.urlopen", raise_402)
        resp = provider.create_card_payment(_req())
        assert resp.status == "rejected"
        assert "insufficient_funds" in resp.status_detail


# ───────────── verificação da assinatura do webhook ───────────── #

class TestWebhookSignature:
    """A Stripe assina o corpo (HMAC-SHA256 de "{t}.{corpo}") — é a diferença
    central para o Asaas, cujo webhook só traz um token estático."""

    def _sign(self, body: bytes, secret=WEBHOOK_SECRET, timestamp=None):
        ts = str(int(timestamp or time.time()))
        sig = hmac.new(
            secret.encode(), f"{ts}.".encode() + body, hashlib.sha256
        ).hexdigest()
        return f"t={ts},v1={sig}"

    def test_valid_signature_accepted(self, provider):
        body = json.dumps({"type": "payment_intent.succeeded"}).encode()
        event = provider.verify_webhook(body, self._sign(body))
        assert event["type"] == "payment_intent.succeeded"

    def test_tampered_body_rejected(self, provider):
        body = json.dumps({"amount": 100}).encode()
        header = self._sign(body)
        tampered = json.dumps({"amount": 999999}).encode()
        with pytest.raises(StripeError, match="assinatura"):
            provider.verify_webhook(tampered, header)

    def test_wrong_secret_rejected(self, provider):
        body = b'{"type":"x"}'
        with pytest.raises(StripeError, match="assinatura"):
            provider.verify_webhook(body, self._sign(body, secret="whsec_outro"))

    def test_old_timestamp_rejected_replay(self, provider):
        """Anti-replay: um evento capturado não pode ser reenviado depois."""
        body = b'{"type":"x"}'
        old = time.time() - 3600
        with pytest.raises(StripeError, match="anti-replay"):
            provider.verify_webhook(body, self._sign(body, timestamp=old))

    def test_malformed_header_rejected(self, provider):
        with pytest.raises(StripeError, match="malformado"):
            provider.verify_webhook(b"{}", "lixo")

    def test_missing_secret_fails_closed(self):
        p = StripeCardProvider(api_key="sk_test_" + "x" * 24, webhook_secret="")
        with pytest.raises(StripeError, match="não configurado"):
            p.verify_webhook(b"{}", "t=1,v1=abc")


# ───────────── papel do Stripe na arquitetura ───────────── #

class TestStripeScope:
    def test_livemode_detected_from_key(self):
        assert StripeCardProvider(api_key="sk_live_" + "x" * 24).livemode is True
        assert StripeCardProvider(api_key="sk_test_" + "x" * 24).livemode is False

    def test_env_validation_requires_webhook_secret(self, monkeypatch):
        from app.env_schema import EnvError, validate_env

        monkeypatch.setenv("STRIPE_API_KEY", "sk_live_" + "x" * 24)
        monkeypatch.delenv("STRIPE_WEBHOOK_SECRET", raising=False)
        monkeypatch.delenv("PIX_PROVIDER", raising=False)
        monkeypatch.delenv("PAYOUT_MODE", raising=False)
        with pytest.raises(EnvError) as exc:
            validate_env(strict=True)
        assert "STRIPE_WEBHOOK_SECRET" in str(exc.value)

    def test_env_rejects_test_key_in_production(self, monkeypatch):
        from app.env_schema import EnvError, validate_env

        monkeypatch.setenv("STRIPE_API_KEY", "sk_test_" + "x" * 24)
        monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_" + "s" * 32)
        monkeypatch.delenv("PIX_PROVIDER", raising=False)
        monkeypatch.delenv("PAYOUT_MODE", raising=False)
        with pytest.raises(EnvError) as exc:
            validate_env(strict=True)
        assert "sk_live_" in str(exc.value)
