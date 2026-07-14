"""Provider PIX usando a API do Mercado Pago.

Docs oficiais:
  - https://www.mercadopago.com.br/developers/pt/docs/checkout-api/payment-management/receive-payment-by-pix
  - https://www.mercadopago.com.br/developers/pt/docs/your-integrations/notifications/webhooks

Credenciais:
  - Sandbox/Teste: criar em https://www.mercadopago.com.br/developers/panel/app
  - Production: mesmo lugar após KYC do CNPJ

Setup necessário no Fly:
  fly secrets set PIX_PROVIDER=mercadopago
  fly secrets set MP_ACCESS_TOKEN="TEST-1234-..."  # TEST-... pra sandbox; APP_USR-... pra prod
  fly secrets set MP_WEBHOOK_SECRET="..."           # gerado no painel MP
"""

from __future__ import annotations

import json
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone
from typing import Any

from .provider import (
    PixProvider,
    PixChargeRequest, PixChargeResponse,
    PixPayoutRequest, PixPayoutResponse,
    CardChargeRequest, CardChargeResponse,
)


class PayerDataError(Exception):
    """Dados do pagador inválidos para uma cobrança REAL (CPF/e-mail).

    Em produção não podemos cair em fallbacks de teste (CPF 19119119100,
    e-mail @example.com): a charge sairia com payer fake no gateway, o
    antifraude do MP penaliza e o comprovante fica errado. O serviço
    converte esta exceção em erro 400 amigável pro usuário corrigir o perfil.
    """


def _is_valid_cpf(cpf: str) -> bool:
    """Validador do algoritmo da Receita Federal (mesmo do backend/auth.py)."""
    if not cpf or len(cpf) != 11 or cpf == cpf[0] * 11:
        return False
    for i in (9, 10):
        s = sum(int(cpf[j]) * ((i + 1) - j) for j in range(i))
        d = (s * 10) % 11
        if d == 10:
            d = 0
        if d != int(cpf[i]):
            return False
    return True


API_BASE = "https://api.mercadopago.com"


class MercadoPagoPixProvider(PixProvider):
    """Integração com PIX do Mercado Pago.

    Cobranças funcionam tanto em sandbox (TEST-...) quanto produção (APP_USR-...).

    Payouts ('PIX de saída' / resgate de cashback) requerem permissão extra
    no painel MP. Se você precisa só de cobrança e usa outro provedor pra
    payout (Efí Bank, p. ex.), passe `payout_provider=EfiBankPixProvider(...)`
    no construtor — caímos no fallback dele.
    """

    name = "mercadopago"

    def __init__(
        self,
        access_token: str,
        notification_url: str | None = None,
        payout_provider: PixProvider | None = None,
        timeout: int = 15,
        strict_payer: bool = False,
    ):
        if not access_token:
            raise ValueError("MercadoPago: access_token obrigatório")
        self.access_token = access_token
        self.notification_url = notification_url
        self.payout_provider = payout_provider
        self.timeout = timeout
        # strict_payer=True (produção): CPF/e-mail inválidos viram
        # PayerDataError em vez de fallback de teste do MP.
        self.strict_payer = strict_payer

    # ---------------- HTTP helpers ---------------- #

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json",
            "X-Idempotency-Key": "",  # preenchido por chamada
        }

    def _request(self, method: str, path: str,
                 body: dict | None = None,
                 idempotency_key: str | None = None) -> dict:
        url = API_BASE + path
        data = json.dumps(body).encode() if body else None
        req = urllib.request.Request(url, data=data, method=method)
        for k, v in self._headers().items():
            req.add_header(k, v)
        if idempotency_key:
            req.add_header("X-Idempotency-Key", idempotency_key)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            try:
                err_body = json.loads(e.read().decode())
            except Exception:
                err_body = {"raw": str(e)}
            raise RuntimeError(
                f"MercadoPago {method} {path} → HTTP {e.code}: {err_body}"
            )
        except Exception as e:
            raise RuntimeError(f"MercadoPago {method} {path} → {e!r}")

    # ---------------- create_charge ---------------- #

    def create_charge(self, req: PixChargeRequest) -> PixChargeResponse:
        """Cria pagamento PIX e devolve BR code + imagem QR.

        Mercado Pago retorna no .point_of_interaction.transaction_data:
          - qr_code: string copia-e-cola (BR Code EMV)
          - qr_code_base64: PNG do QR já renderizado, em base64
          - ticket_url: link público com QR e instruções
        """
        # Payer: e-mail/CPF reais do usuário. Em strict_payer (produção)
        # dado inválido é PayerDataError; fora dele (sandbox/homolog) cai
        # nos fallbacks de teste do MP (@example.com / 19119119100).
        # Regras centralizadas em _build_payer — compartilhadas com cartão.
        payer = self._build_payer(req.payer_name, req.payer_cpf, req.payer_email)

        body = {
            "transaction_amount": round(req.amount_cents / 100, 2),
            "payment_method_id": "pix",
            "description": req.description[:255],  # MP limita descrição
            "external_reference": req.txid,
            "payer": payer,
        }
        # Alinha a expiração do QR no MP com o TTL local da charge. Sem isso
        # o MP usa o default do gateway (~24h) e o cliente consegue pagar um
        # QR que pra nós já expirou (30 min) — dinheiro entra sem crédito.
        # Formato exigido pelo MP: ISO-8601 com millis e offset numérico.
        if req.expires_in_seconds:
            exp = datetime.now(timezone.utc) + timedelta(seconds=req.expires_in_seconds)
            body["date_of_expiration"] = exp.strftime("%Y-%m-%dT%H:%M:%S.000+00:00")
        if self.notification_url:
            body["notification_url"] = self.notification_url

        resp = self._request(
            "POST", "/v1/payments",
            body=body, idempotency_key=req.txid,
        )

        poi = resp.get("point_of_interaction", {})
        td = poi.get("transaction_data", {})
        br_code = td.get("qr_code", "")
        qr_b64 = td.get("qr_code_base64", "")

        if not br_code:
            raise RuntimeError(
                f"MercadoPago: resposta sem qr_code · payment_id={resp.get('id')}"
            )

        # Armazenamos o payment_id do MP no campo txid (sobrescrevendo o nosso).
        # Isso permite consultar status via GET /v1/payments/{mp_id} depois.
        # Mas mantemos o nosso txid no external_reference pro idempotency.
        mp_id = str(resp.get("id", req.txid))
        return PixChargeResponse(
            txid=mp_id,
            br_code=br_code,
            qr_code_image=("data:image/png;base64," + qr_b64) if qr_b64 else "",
        )

    # ---------------- create_card_payment ---------------- #

    def _build_payer(self, name: str, cpf: str, email: str) -> dict:
        """Monta o bloco payer com as mesmas regras strict do PIX."""
        payer_email = (email or "").strip().lower()
        if "@" not in payer_email or "." not in payer_email.split("@", 1)[-1]:
            if self.strict_payer:
                raise PayerDataError(
                    "E-mail da conta inválido para pagamento. "
                    "Atualize seu e-mail no perfil antes de comprar."
                )
            payer_email = f"user{cpf or 'anon'}@example.com"
        cpf_digits = "".join(c for c in (cpf or "") if c.isdigit())
        if not _is_valid_cpf(cpf_digits):
            if self.strict_payer:
                raise PayerDataError(
                    "CPF da conta inválido para pagamento. "
                    "Atualize seu CPF no perfil antes de comprar."
                )
            cpf_digits = "19119119100"  # CPF de teste publicado pelo MP
        first_name = name.split(" ")[0] if name else "Cliente"
        last_name = " ".join(name.split(" ")[1:]) if name else "Blaxx"
        return {
            "email": payer_email,
            "first_name": first_name,
            "last_name": last_name or "Blaxx",
            "identification": {"type": "CPF", "number": cpf_digits},
        }

    def create_card_payment(self, req: CardChargeRequest) -> CardChargeResponse:
        """Pagamento com cartão via Checkout API (POST /v1/payments).

        O token single-use vem do SDK JS no frontend (PCI SAQ-A: PAN/CVV
        nunca tocam este backend). X-Idempotency-Key = external_reference —
        retry de rede não cobra o cartão duas vezes (o MP deduplica).
        """
        body: dict[str, Any] = {
            "transaction_amount": round(req.amount_cents / 100, 2),
            "token": req.card_token,
            "description": req.description[:255],
            "installments": max(1, int(req.installments or 1)),
            "payment_method_id": req.payment_method_id,
            "capture": True,
            "external_reference": req.external_reference,
            "payer": self._build_payer(req.payer_name, req.payer_cpf, req.payer_email),
        }
        if req.issuer_id:
            body["issuer_id"] = req.issuer_id
        if req.statement_descriptor:
            body["statement_descriptor"] = req.statement_descriptor[:22]
        if self.notification_url:
            body["notification_url"] = self.notification_url

        resp = self._request(
            "POST", "/v1/payments",
            body=body, idempotency_key=req.external_reference,
        )

        card = resp.get("card") or {}
        return CardChargeResponse(
            mp_payment_id=str(resp.get("id", "")),
            status=resp.get("status", "unknown"),
            status_detail=resp.get("status_detail", "") or "",
            card_brand=resp.get("payment_method_id", "") or "",
            card_last4=str(card.get("last_four_digits", "") or ""),
        )

    # ---------------- request_payout ---------------- #

    def request_payout(self, req: PixPayoutRequest) -> PixPayoutResponse:
        """Payout PIX (resgate via cashback).

        MercadoPago suporta payouts via 'money transfer' (/v1/payouts) com
        permissão especial. Para a maioria dos integradores brasileiros, é
        mais simples usar outro provedor (Efí Bank, Stark Bank) só pra payout.

        Se você passou `payout_provider` no construtor, delega pra ele.
        Caso contrário, devolve "failed" com mensagem clara.
        """
        if self.payout_provider is not None:
            return self.payout_provider.request_payout(req)
        return PixPayoutResponse(
            txid=req.txid,
            end_to_end_id="",
            status="failed",
            failure_reason="MercadoPago não habilitado pra payout. Configure outro provider.",
        )

    # ---------------- get_charge_status ---------------- #

    def get_charge_status(self, mp_payment_id: str) -> str:
        """Consulta status atual da cobrança no MP.

        Retorna: 'pending' | 'approved' | 'authorized' | 'in_process'
                 | 'in_mediation' | 'rejected' | 'cancelled' | 'refunded'
                 | 'charged_back' | 'unknown'
        """
        try:
            resp = self._request("GET", f"/v1/payments/{mp_payment_id}")
            return resp.get("status", "unknown")
        except Exception:
            return "unknown"

    def get_payment(self, mp_payment_id: str) -> dict:
        """Retorna o payment completo (útil pro webhook validar)."""
        return self._request("GET", f"/v1/payments/{mp_payment_id}")
