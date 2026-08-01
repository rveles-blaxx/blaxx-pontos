"""Asaas — cobrança PIX (entrada de dinheiro).

O foco é a regra que protege o caixa: **pontos só são creditados em
`RECEIVED`**, nunca em `CONFIRMED`. Como o ponto é resgatável em PIX na hora,
creditar em CONFIRMED permitiria sacar dinheiro que a empresa ainda não
recebeu — e que pode nunca receber (PIX pode ficar 72h em antifraude e virar
REFUNDED; cartão só liquida em ~D+32 e pode virar chargeback).

Roda com:
    cd backend && python -m pytest tests/test_asaas_cobranca.py -v
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("MAILER", "noop")

from app.pix.asaas import AsaasPixProvider
from app.pix.provider import PixChargeRequest


@pytest.fixture
def provider():
    return AsaasPixProvider(api_key="$aact_hmlg_" + "x" * 40, sandbox=True)


def _charge_req(txid="tx-compra-1", cents=18000):
    return PixChargeRequest(
        txid=txid,
        amount_cents=cents,
        description="Pacote Start",
        payer_name="João Silva",
        payer_cpf="529.982.247-25",
        expires_in_seconds=3600,
        payer_email="joao@test.com",
    )


class _Router:
    """Roteia chamadas do _request por (método, prefixo do path)."""

    def __init__(self, **routes):
        self.routes = routes
        self.calls = []

    def __call__(self, method, path, body=None):
        self.calls.append((method, path, body))
        for key, resp in self.routes.items():
            if key in path:
                return resp(body) if callable(resp) else resp
        return {}


# ───────────────────── criação da cobrança ───────────────────── #

class TestCreateCharge:
    def test_creates_customer_then_payment_then_fetches_qr(self, provider, monkeypatch):
        router = _Router(**{
            "/customers?": {"data": []},                       # não existe ainda
            "/customers": {"id": "cus_123"},                   # criação
            "/payments?externalReference": {"data": []},       # sem cobrança prévia
            "/payments/pay_9/pixQrCode": {
                "payload": "00020126BR.GOV.BCB.PIX...",
                "encodedImage": "iVBORw0KGgo=",
            },
            "/payments": {"id": "pay_9", "status": "PENDING"},
        })
        monkeypatch.setattr(provider, "_request", router)

        resp = provider.create_charge(_charge_req())

        assert resp.br_code.startswith("00020126")
        assert resp.qr_code_image.startswith("data:image/png;base64,")

        # billingType PIX e valor em REAIS
        post_payment = [c for c in router.calls
                        if c[0] == "POST" and c[1] == "/payments"][0]
        assert post_payment[2]["billingType"] == "PIX"
        assert post_payment[2]["value"] == 180.00
        assert post_payment[2]["customer"] == "cus_123"
        assert post_payment[2]["externalReference"] == "tx-compra-1"

    def test_reuses_existing_customer_by_cpf(self, provider, monkeypatch):
        router = _Router(**{
            "/customers?": {"data": [{"id": "cus_ja_existe"}]},
            "/payments?externalReference": {"data": []},
            "/payments/pay_1/pixQrCode": {"payload": "brcode", "encodedImage": ""},
            "/payments": {"id": "pay_1"},
        })
        monkeypatch.setattr(provider, "_request", router)
        provider.create_charge(_charge_req())

        # não pode ter criado customer novo
        assert not [c for c in router.calls if c[0] == "POST" and c[1] == "/customers"]
        post_payment = [c for c in router.calls
                        if c[0] == "POST" and c[1] == "/payments"][0]
        assert post_payment[2]["customer"] == "cus_ja_existe"

    def test_idempotent_reuses_existing_payment(self, provider, monkeypatch):
        """externalReference É filtro documentado em GET /v3/payments — se o
        POST anterior deu timeout mas criou, reaproveitamos em vez de cobrar
        o cliente duas vezes."""
        router = _Router(**{
            "/payments?externalReference": {"data": [{"id": "pay_existente"}]},
            "/payments/pay_existente/pixQrCode": {
                "payload": "brcode-existente", "encodedImage": ""},
        })
        monkeypatch.setattr(provider, "_request", router)

        resp = provider.create_charge(_charge_req())

        assert resp.br_code == "brcode-existente"
        assert not [c for c in router.calls if c[0] == "POST" and c[1] == "/payments"], \
            "não pode criar segunda cobrança para o mesmo txid"

    def test_raises_when_no_brcode(self, provider, monkeypatch):
        """Sem chave PIX na conta o QR não vem — falhar claro é melhor que
        devolver charge sem copia-e-cola."""
        from app.pix.asaas import AsaasError

        router = _Router(**{
            "/customers?": {"data": [{"id": "cus_1"}]},
            "/payments?externalReference": {"data": []},
            "/payments/pay_1/pixQrCode": {},
            "/payments": {"id": "pay_1"},
        })
        monkeypatch.setattr(provider, "_request", router)
        with pytest.raises(AsaasError, match="copia-e-cola"):
            provider.create_charge(_charge_req())

    def test_due_date_is_future(self, provider, monkeypatch):
        from datetime import datetime, timezone

        router = _Router(**{
            "/customers?": {"data": [{"id": "cus_1"}]},
            "/payments?externalReference": {"data": []},
            "/payments/pay_1/pixQrCode": {"payload": "b", "encodedImage": ""},
            "/payments": {"id": "pay_1"},
        })
        monkeypatch.setattr(provider, "_request", router)
        provider.create_charge(_charge_req())

        due = [c for c in router.calls
               if c[0] == "POST" and c[1] == "/payments"][0][2]["dueDate"]
        assert due >= datetime.now(timezone.utc).strftime("%Y-%m-%d"), (
            "vencimento no passado faz o Asaas recusar a cobrança"
        )


# ─────────── a regra que protege o caixa: RECEIVED vs CONFIRMED ─────────── #

class TestCreditOnlyOnReceived:
    """`CONFIRMED` = pago mas saldo indisponível. `RECEIVED` = dinheiro na
    conta. Como o ponto vira PIX na hora, creditar em CONFIRMED deixaria
    sacar dinheiro que a empresa ainda não tem."""

    def test_confirmed_is_not_in_credit_set(self):
        from app.api.asaas_webhook import _PAYMENT_CREDIT

        assert "CONFIRMED" not in _PAYMENT_CREDIT, (
            "creditar em CONFIRMED permite sacar dinheiro não recebido "
            "(PIX pode virar REFUNDED em até 72h; cartão liquida em D+32)"
        )

    def test_received_credits(self):
        from app.api.asaas_webhook import _PAYMENT_CREDIT

        assert "RECEIVED" in _PAYMENT_CREDIT

    def test_reversal_events_are_flagged(self):
        from app.api.asaas_webhook import _PAYMENT_REVERSED

        for ev in ("PAYMENT_REFUNDED", "PAYMENT_CHARGEBACK_REQUESTED",
                   "PAYMENT_REPROVED_BY_RISK_ANALYSIS"):
            assert ev in _PAYMENT_REVERSED, f"{ev} precisa ser tratado como reversão"


# ───────────────────── consultas ───────────────────── #

class TestQueries:
    def test_get_charge_status_by_txid(self, provider, monkeypatch):
        monkeypatch.setattr(
            provider, "_request",
            lambda m, p, body=None: {"data": [{"id": "pay_1", "status": "RECEIVED"}]}
        )
        assert provider.get_charge_status("tx-1") == "RECEIVED"

    def test_get_charge_status_unknown_when_absent(self, provider, monkeypatch):
        monkeypatch.setattr(provider, "_request", lambda m, p, body=None: {"data": []})
        assert provider.get_charge_status("tx-inexistente") == "unknown"


# ───────────────── cartão via checkout hospedado ───────────────── #

class TestCardViaHostedCheckout:
    """O Asaas não tem SDK JS nem checkout transparente. Cobramos criando a
    cobrança SEM dados de cartão e redirecionando para o `invoiceUrl` — assim
    o número do cartão nunca passa pelo nosso backend (escopo PCI mínimo)."""

    def _router(self, payment):
        return _Router(**{
            "/customers?": {"data": [{"id": "cus_1"}]},
            "/payments?externalReference": {"data": []},
            "/payments": payment,
        })

    def test_never_sends_card_data(self, provider, monkeypatch):
        r = self._router({"id": "pay_c1", "status": "PENDING",
                          "invoiceUrl": "https://asaas.com/i/abc"})
        monkeypatch.setattr(provider, "_request", r)
        provider.create_card_payment(_card_req())
        body = [c for c in r.calls if c[0] == "POST" and c[1] == "/payments"][0][2]
        for proibido in ("creditCard", "creditCardHolderInfo", "creditCardToken"):
            assert proibido not in body, f"{proibido} traria o PAN para o backend"
        assert body["billingType"] == "CREDIT_CARD"

    def test_returns_redirect_url(self, provider, monkeypatch):
        monkeypatch.setattr(provider, "_request", self._router(
            {"id": "pay_c1", "status": "PENDING", "invoiceUrl": "https://asaas.com/i/abc"}))
        resp = provider.create_card_payment(_card_req())
        assert resp.redirect_url == "https://asaas.com/i/abc"
        assert resp.mp_payment_id == "pay_c1"

    def test_pending_is_never_approved(self, provider, monkeypatch):
        """A cobrança nasce PENDING — só o webhook confirma."""
        monkeypatch.setattr(provider, "_request", self._router(
            {"id": "pay_c1", "status": "PENDING", "invoiceUrl": "u"}))
        assert provider.create_card_payment(_card_req()).status == "in_process"

    def test_confirmed_is_not_approved_either(self, provider, monkeypatch):
        """CONFIRMED = pago mas indisponível (cartão liquida em ~D+32)."""
        monkeypatch.setattr(provider, "_request", self._router(
            {"id": "pay_c1", "status": "CONFIRMED", "invoiceUrl": "u"}))
        assert provider.create_card_payment(_card_req()).status == "in_process"

    def test_received_is_approved(self, provider, monkeypatch):
        monkeypatch.setattr(provider, "_request", self._router(
            {"id": "pay_c1", "status": "RECEIVED", "invoiceUrl": "u"}))
        assert provider.create_card_payment(_card_req()).status == "approved"

    def test_installments_sent_only_above_one(self, provider, monkeypatch):
        r = self._router({"id": "pay_c1", "status": "PENDING", "invoiceUrl": "u"})
        monkeypatch.setattr(provider, "_request", r)
        provider.create_card_payment(_card_req(installments=1))
        body = [c for c in r.calls if c[0] == "POST" and c[1] == "/payments"][0][2]
        assert "installmentCount" not in body, "1x não envia atributos de parcelamento"

        r2 = self._router({"id": "pay_c2", "status": "PENDING", "invoiceUrl": "u"})
        monkeypatch.setattr(provider, "_request", r2)
        provider.create_card_payment(_card_req(installments=6))
        body2 = [c for c in r2.calls if c[0] == "POST" and c[1] == "/payments"][0][2]
        assert body2["installmentCount"] == 6

    def test_idempotent_by_external_reference(self, provider, monkeypatch):
        r = _Router(**{"/payments?externalReference": {
            "data": [{"id": "pay_ja", "status": "PENDING", "invoiceUrl": "u"}]}})
        monkeypatch.setattr(provider, "_request", r)
        resp = provider.create_card_payment(_card_req())
        assert resp.mp_payment_id == "pay_ja"
        assert not [c for c in r.calls if c[0] == "POST" and c[1] == "/payments"], \
            "não pode criar segunda cobrança para a mesma referência"


def _card_req(installments=1):
    from app.pix.provider import CardChargeRequest
    return CardChargeRequest(
        external_reference="card-tx-1", amount_cents=18000,
        description="Pacote Start", card_token="", payment_method_id="",
        installments=installments, payer_name="João Silva",
        payer_cpf="529.982.247-25", payer_email="joao@test.com",
    )
