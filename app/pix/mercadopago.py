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
from typing import Any

from .provider import (
    PixProvider,
    PixChargeRequest, PixChargeResponse,
    PixPayoutRequest, PixPayoutResponse,
)


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
    ):
        if not access_token:
            raise ValueError("MercadoPago: access_token obrigatório")
        self.access_token = access_token
        self.notification_url = notification_url
        self.payout_provider = payout_provider
        self.timeout = timeout

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
        # Email do payer: usa o real do usuário (MP rejeita .local e exige
        # TLD válido). Fallback pra um @example.com sintético se não veio
        # email — ainda assim válido pro MP.
        payer_email = (req.payer_email or "").strip().lower()
        if "@" not in payer_email or "." not in payer_email.split("@", 1)[-1]:
            payer_email = f"user{req.payer_cpf or req.txid[:8]}@example.com"
        first_name = req.payer_name.split(" ")[0] if req.payer_name else "Cliente"
        last_name = " ".join(req.payer_name.split(" ")[1:]) if req.payer_name else "Blaxx"

        # CPF: usa só dígitos. MP TEST aceita qualquer CPF matematicamente
        # válido. Como nosso seed/usuários podem ter CPF inválido (sequências
        # como 12345678900), validamos e caímos pra um placeholder de teste
        # do MP (`19119119100` - CPF de testes oficial documentado).
        cpf_digits = "".join(c for c in (req.payer_cpf or "") if c.isdigit())
        if not _is_valid_cpf(cpf_digits):
            cpf_digits = "19119119100"  # CPF de teste publicado pelo MP

        body = {
            "transaction_amount": round(req.amount_cents / 100, 2),
            "payment_method_id": "pix",
            "description": req.description[:255],  # MP limita descrição
            "external_reference": req.txid,
            "payer": {
                "email": payer_email,
                "first_name": first_name,
                "last_name": last_name or "Blaxx",
                "identification": {
                    "type": "CPF",
                    "number": cpf_digits,
                },
            },
        }
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
