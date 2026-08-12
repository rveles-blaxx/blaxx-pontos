"""Cartão INTERNACIONAL via Stripe.

Papel na arquitetura: o Stripe processa **todo o cartão**. Ele NÃO cobre
PIX no Brasil (é invite-only e exige 60 dias de histórico processando na
Stripe) e NÃO paga PIX para chave de terceiro — por isso PIX de entrada e de
saída fica no Asaas.

Limitação assumida: a Stripe não oferece parcelamento no Brasil, nem Elo,
Hipercard ou débito nacional. Cartão é à vista.

── PCI ────────────────────────────────────────────────────────────────────
Modelo PCI SAQ-A: o frontend usa **Stripe
Elements / Payment Element**, que devolve um `PaymentMethod` (`pm_…`). O
número do cartão NUNCA chega ao nosso backend — aqui só trafega o `pm_…`.
O scan de campos proibidos em `app/api/card_payments.py` continua valendo.

── Duas vantagens reais sobre o Asaas ────────────────────────────────────
1. **Idempotency-Key nativo.** O Stripe deduplica no servidor por 24h, então
   um retry após timeout não cria uma segunda cobrança. No Asaas isso teve de
   ser construído à mão.
2. **Webhook ASSINADO** (`Stripe-Signature`: HMAC-SHA256 do corpo + timestamp
   com janela anti-replay). Isso é verificação criptográfica de verdade —
   diferente do token estático do Asaas, que obriga a reconsultar a API.

Docs: https://docs.stripe.com/api/payment_intents
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import socket
import time
import urllib.error
import urllib.parse
import urllib.request

from .provider import CardChargeRequest, CardChargeResponse

log = logging.getLogger("blaxx.card.stripe")

API_BASE = "https://api.stripe.com/v1"

# PaymentIntent.status → status interno usado por card_purchase.
_STATUS_MAP = {
    "succeeded": "approved",
    "processing": "in_process",
    "requires_action": "in_process",       # 3DS pendente
    "requires_confirmation": "in_process",
    "requires_capture": "in_process",      # autorizado, sem captura
    "requires_payment_method": "rejected",  # recusado; PM precisa ser trocado
    "canceled": "cancelled",
}


class StripeError(RuntimeError):
    """Erro de comunicação/negócio com a Stripe."""


class StripeIndeterminateError(RuntimeError):
    """Timeout/rede: não sabemos se o PaymentIntent foi criado.

    Menos grave que no Asaas: como enviamos Idempotency-Key, repetir a MESMA
    requisição é seguro — a Stripe devolve o resultado original em vez de
    cobrar de novo.
    """


class StripeCardProvider:
    """Cartão internacional. Não implementa PIX nem payout."""

    name = "stripe"

    def __init__(
        self,
        api_key: str,
        *,
        webhook_secret: str = "",
        currency: str = "brl",
        timeout: int = 40,
        statement_descriptor: str = "",
    ):
        if not api_key:
            raise ValueError("Stripe: api_key obrigatória")
        self.api_key = api_key
        self.webhook_secret = webhook_secret
        self.currency = (currency or "brl").lower()
        self.timeout = timeout
        self.statement_descriptor = statement_descriptor
        self.livemode = api_key.startswith("sk_live_")

    # ---------------- HTTP ---------------- #

    def _request(
        self, method: str, path: str,
        params: dict | None = None,
        idempotency_key: str | None = None,
    ) -> dict:
        url = API_BASE + path
        data = None
        if params:
            # A API da Stripe é form-encoded, não JSON.
            data = urllib.parse.urlencode(_flatten(params)).encode()

        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", f"Bearer {self.api_key}")
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        if idempotency_key:
            req.add_header("Idempotency-Key", idempotency_key)

        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode() or "{}")
        except urllib.error.HTTPError as exc:
            raw = ""
            try:
                raw = exc.read().decode()
            except Exception:
                pass
            raise StripeError(f"HTTP {exc.code}: {_describe_error(raw)}") from exc
        except (urllib.error.URLError, socket.timeout, TimeoutError) as exc:
            raise StripeIndeterminateError(str(exc)) from exc

    # ---------------- Cobrança ---------------- #

    def create_card_payment(self, req: CardChargeRequest) -> CardChargeResponse:
        """Cria e confirma um PaymentIntent com o PaymentMethod do frontend.

        `req.card_token` carrega o `pm_…` do Stripe Elements (nome herdado do
        contrato genérico de provider).
        """
        params: dict = {
            "amount": req.amount_cents,          # menor unidade (centavos)
            "currency": self.currency,
            "payment_method": req.card_token,
            "confirm": "true",
            # Sem redirect: o resgate acontece no app, não temos página de
            # retorno. Se o cartão exigir 3DS com redirect, a Stripe recusa e
            # o cliente escolhe outro método — melhor que travar o fluxo.
            "automatic_payment_methods[enabled]": "true",
            "automatic_payment_methods[allow_redirects]": "never",
            "description": (req.description or "BlaXx — compra de pontos")[:350],
            "metadata[external_reference]": req.external_reference,
            "metadata[payer_cpf]": _mask_cpf(req.payer_cpf),
        }
        if req.payer_email:
            params["receipt_email"] = req.payer_email
        descriptor = req.statement_descriptor or self.statement_descriptor
        if descriptor:
            params["statement_descriptor_suffix"] = descriptor[:22]

        try:
            pi = self._request(
                "POST", "/payment_intents", params,
                # Idempotência nativa: retry com a mesma key devolve o
                # resultado original em vez de cobrar duas vezes.
                idempotency_key=f"card:{req.external_reference}",
            )
        except StripeIndeterminateError as exc:
            log.error(
                "Stripe timeout ref=%s (%s). Com Idempotency-Key o retry é "
                "seguro; deixando in_process até o webhook resolver.",
                req.external_reference, exc,
            )
            return CardChargeResponse(
                provider_payment_id="", status="in_process",
                status_detail="timeout_indeterminado",
            )
        except StripeError as exc:
            msg = str(exc)
            log.warning("Stripe recusou ref=%s: %s", req.external_reference, msg)
            return CardChargeResponse(
                provider_payment_id="", status="rejected", status_detail=msg[:80],
            )

        return self._to_response(pi)

    def _to_response(self, pi: dict) -> CardChargeResponse:
        status = _STATUS_MAP.get((pi.get("status") or "").lower(), "in_process")
        brand, last4 = "", ""
        charges = ((pi.get("charges") or {}).get("data") or [])
        if charges:
            details = (charges[0].get("payment_method_details") or {}).get("card") or {}
            brand = details.get("brand") or ""
            last4 = details.get("last4") or ""
        last_err = pi.get("last_payment_error") or {}
        detail = last_err.get("decline_code") or last_err.get("code") or ""
        return CardChargeResponse(
            provider_payment_id=pi.get("id") or "",
            status=status,
            status_detail=(detail or pi.get("status") or "")[:80],
            card_brand=brand,
            card_last4=last4,
        )

    def get_payment(self, payment_intent_id: str) -> dict:
        """Fonte de verdade — usada pelo webhook antes de creditar."""
        return self._request("GET", f"/payment_intents/{payment_intent_id}")

    def payment_status(self, payment_intent_id: str) -> str:
        pi = self.get_payment(payment_intent_id)
        return _STATUS_MAP.get((pi.get("status") or "").lower(), "in_process")

    # ---------------- Webhook ---------------- #

    def verify_webhook(self, raw_body: bytes, signature_header: str,
                       tolerance_seconds: int = 300) -> dict:
        """Valida `Stripe-Signature` e devolve o evento.

        Formato: `t=<timestamp>,v1=<hmac_sha256>`. Assinatura sobre
        `"{t}.{corpo_bruto}"` com o webhook secret. A janela de tolerância é
        anti-replay: sem ela, um evento capturado poderia ser reenviado
        indefinidamente.

        Levanta StripeError se a assinatura não bater — o chamador devolve 401.
        """
        if not self.webhook_secret:
            raise StripeError("STRIPE_WEBHOOK_SECRET não configurado")

        parts = dict(
            p.split("=", 1) for p in (signature_header or "").split(",") if "=" in p
        )
        timestamp = parts.get("t", "")
        received = parts.get("v1", "")
        if not timestamp or not received:
            raise StripeError("Stripe-Signature malformado")

        try:
            age = abs(time.time() - int(timestamp))
        except ValueError as exc:
            raise StripeError("timestamp inválido no Stripe-Signature") from exc
        if age > tolerance_seconds:
            raise StripeError(f"webhook fora da janela anti-replay ({age:.0f}s)")

        signed = f"{timestamp}.".encode() + raw_body
        expected = hmac.new(
            self.webhook_secret.encode(), signed, hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(expected, received):
            raise StripeError("assinatura do webhook não confere")

        return json.loads(raw_body.decode() or "{}")


# ---------------- helpers ---------------- #

def _flatten(params: dict) -> list[tuple[str, str]]:
    """A Stripe usa form-encoding com chaves aninhadas (`a[b]=c`)."""
    out: list[tuple[str, str]] = []
    for k, v in params.items():
        if v is None or v == "":
            continue
        out.append((k, str(v)))
    return out


def _describe_error(raw: str) -> str:
    """Extrai `error.message`/`error.decline_code` do corpo de erro."""
    try:
        err = (json.loads(raw or "{}") or {}).get("error") or {}
    except ValueError:
        return (raw or "")[:200]
    parts = [err.get("decline_code"), err.get("code"), err.get("message")]
    return " · ".join(p for p in parts if p)[:250] or (raw or "")[:200]


def _mask_cpf(cpf: str) -> str:
    """Nunca mandar CPF inteiro para fora — metadata da Stripe é lida por
    humanos no dashboard e não precisa do documento completo."""
    digits = "".join(c for c in (cpf or "") if c.isdigit())
    return f"***{digits[-4:]}" if len(digits) >= 4 else ""
