"""Provider de PIX de SAÍDA (payout) via Asaas.

Por que existe: o Stripe não envia PIX para a chave de um
terceiro — e é exatamente isso que o resgate do BlaXx faz (usuário troca
pontos e recebe PIX na chave dele). O Asaas é participante do arranjo Pix e
expõe `POST /v3/transfers` com `pixAddressKey`.

Este provider cobre PIX de ENTRADA (cobrança) e de SAÍDA (payout). O
cartão fica na Stripe. Ver app/__init__.py.

── Idempotência (o ponto mais perigoso deste arquivo) ─────────────────────
A API do Asaas **não tem header de idempotência**. A única proteção nativa é
um HTTP 409 quando chega uma transferência idêntica em menos de 15 minutos —
o que é rede de segurança, não garantia.

Por isso, aqui:
  · `externalReference` = txid do payout (UNIQUE na nossa tabela) — amarra
    webhook → payout e permite conciliação manual.
  · **Timeout NUNCA vira "failed".** Se a resposta não chegou, a
    transferência pode ter sido criada. Devolvemos "processing" para que o
    resgate fique retido aguardando o webhook. Marcar "failed" estornaria os
    pontos enquanto o dinheiro talvez esteja em trânsito — o usuário ficaria
    com o dinheiro E os pontos.
  · Nunca fazemos retry automático de POST /v3/transfers.

── `authorized: false` ────────────────────────────────────────────────────
Quando a conta Asaas exige Token SMS para ações críticas, a transferência
nasce travada. Tratamos como "processing" (não como paga), senão marcaríamos
como enviado algo que nunca saiu.

Docs: https://docs.asaas.com/reference/transferir-para-conta-de-outra-instituicao-ou-chave-pix
"""

from __future__ import annotations

import json
import logging
import re
import socket
import urllib.error
import urllib.request

from .provider import (
    CardChargeRequest,
    CardChargeResponse,
    PixChargeRequest,
    PixChargeResponse,
    PixPayoutRequest,
    PixPayoutResponse,
    PixProvider,
)

log = logging.getLogger("blaxx.pix.asaas")

API_BASE_PROD = "https://api.asaas.com/v3"
API_BASE_SANDBOX = "https://api-sandbox.asaas.com/v3"

# Status da transferência no Asaas → status do nosso PixPayoutResponse.
# PENDING/BANK_PROCESSING seguem retidos (o webhook fecha depois).
_STATUS_MAP = {
    "DONE": "paid",
    "PENDING": "processing",
    "BANK_PROCESSING": "processing",
    "FAILED": "failed",
    "CANCELLED": "failed",
}

_ONLY_DIGITS = re.compile(r"\D")

# Status de COBRANÇA (payment) do Asaas → vocabulário interno de cartão.
#
# Regra que protege o caixa: só `RECEIVED` (dinheiro disponível na conta)
# vira "approved". `CONFIRMED` é pago-mas-indisponível — no cartão o dinheiro
# só entra em ~D+32 e ainda pode virar chargeback; creditar ali deixaria
# resgatar em PIX um valor que a empresa ainda não recebeu.
_CARD_STATUS_MAP = {
    "RECEIVED": "approved",
    "RECEIVED_IN_CASH": "approved",
    "CONFIRMED": "in_process",
    "PENDING": "in_process",
    "AWAITING_RISK_ANALYSIS": "in_process",
    "REFUNDED": "refunded",
    "REFUND_REQUESTED": "refunded",
    "CHARGEBACK_REQUESTED": "charged_back",
    "CHARGEBACK_DISPUTE": "charged_back",
    "OVERDUE": "cancelled",
}


class AsaasError(RuntimeError):
    """Erro de comunicação/negócio com o Asaas."""


def detect_pix_key_type(key: str) -> str:
    """Deriva o `pixAddressKeyType` a partir do formato da chave.

    O Asaas aceita CPF | CNPJ | EMAIL | PHONE | EVP (chave aleatória).
    Fazemos isso localmente de propósito: consultar o DICT para descobrir o
    tipo custa 10–15 tokens do bucket do Bacen quando a chave não existe.

    Regras de formatação exigidas pelo Asaas: CPF/CNPJ sem pontuação e
    telefone com 11 dígitos incluindo DDD (sem +55).
    """
    k = (key or "").strip()
    if "@" in k:
        return "EMAIL"
    digits = _ONLY_DIGITS.sub("", k)
    # EVP (chave aleatória) é um UUID v4 — 36 chars com hífens.
    if len(k) == 36 and k.count("-") == 4:
        return "EVP"
    if len(digits) == 11 and k.startswith("+"):
        return "PHONE"
    if len(digits) == 11:
        # Ambíguo: CPF e celular com DDD têm 11 dígitos. Celular brasileiro
        # sempre tem '9' como primeiro dígito depois do DDD.
        return "PHONE" if digits[2] == "9" and k.startswith(("+", "(")) else "CPF"
    if len(digits) == 13 and digits.startswith("55"):
        return "PHONE"
    if len(digits) == 14:
        return "CNPJ"
    return "EVP"


def normalize_pix_key(key: str, key_type: str) -> str:
    """Formata a chave como o Asaas espera."""
    k = (key or "").strip()
    if key_type in ("CPF", "CNPJ"):
        return _ONLY_DIGITS.sub("", k)
    if key_type == "PHONE":
        digits = _ONLY_DIGITS.sub("", k)
        # Asaas quer 11 dígitos com DDD, sem +55.
        if len(digits) == 13 and digits.startswith("55"):
            digits = digits[2:]
        return digits
    if key_type == "EMAIL":
        return k.lower()
    return k


class AsaasPixProvider(PixProvider):
    """PIX completo: cobrança (entrada) e transferência (saída)."""

    name = "asaas"

    def __init__(
        self,
        api_key: str,
        *,
        sandbox: bool = False,
        timeout: int = 30,
        user_agent: str = "blaxx-pontos-backend",
    ):
        if not api_key:
            raise ValueError("Asaas: api_key obrigatória")
        self.api_key = api_key
        self.sandbox = sandbox
        self.api_base = API_BASE_SANDBOX if sandbox else API_BASE_PROD
        # Timeout generoso: transferência é escrita, e cortar cedo demais nos
        # deixa em estado indeterminado com mais frequência.
        self.timeout = timeout
        self.user_agent = user_agent

    # ---------------- HTTP ---------------- #

    def _headers(self) -> dict:
        # Asaas usa API key crua no header `access_token` — não é Bearer.
        return {
            "access_token": self.api_key,
            "Content-Type": "application/json",
            "User-Agent": self.user_agent,
            "Accept": "application/json",
        }

    def _request(self, method: str, path: str, body: dict | None = None) -> dict:
        url = self.api_base + path
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        for k, v in self._headers().items():
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8") or "{}"
                return json.loads(raw)
        except urllib.error.HTTPError as exc:
            raw = ""
            try:
                raw = exc.read().decode("utf-8")
            except Exception:
                pass
            raise AsaasError(f"HTTP {exc.code}: {_describe_errors(raw)}") from exc
        except (urllib.error.URLError, socket.timeout, TimeoutError) as exc:
            # Estado INDETERMINADO — a transferência pode ter sido criada.
            raise _IndeterminateError(str(exc)) from exc

    # ---------------- Payout ---------------- #

    def request_payout(self, req: PixPayoutRequest) -> PixPayoutResponse:
        key_type = detect_pix_key_type(req.pix_key)
        payload = {
            # Asaas trabalha em REAIS (decimal), não em centavos.
            "value": round(req.amount_cents / 100, 2),
            "pixAddressKey": normalize_pix_key(req.pix_key, key_type),
            "pixAddressKeyType": key_type,
            "operationType": "PIX",
            "description": (req.description or "BlaXx — resgate")[:120],
            # Amarra a transferência ao nosso payout: é o que o webhook usa
            # para achar o registro de volta.
            "externalReference": req.txid,
        }

        try:
            data = self._request("POST", "/transfers", payload)
        except _IndeterminateError as exc:
            # NÃO marcar como falha: o dinheiro pode estar em trânsito.
            log.error(
                "Asaas payout INDETERMINADO txid=%s (%s). Payout segue retido "
                "aguardando webhook; NÃO reprocessar automaticamente.",
                req.txid, exc,
            )
            return PixPayoutResponse(
                txid=req.txid,
                end_to_end_id="",
                status="processing",
                failure_reason=None,
            )
        except AsaasError as exc:
            msg = str(exc)
            # 409 = transferência idêntica nos últimos 15 min. Pode ser o eco
            # de uma tentativa nossa que deu timeout — tratar como retido, não
            # como falha, para não estornar em cima de dinheiro já enviado.
            if msg.startswith("HTTP 409"):
                log.error(
                    "Asaas 409 (duplicata em 15min) txid=%s — mantendo retido "
                    "para conciliação manual.", req.txid,
                )
                return PixPayoutResponse(
                    txid=req.txid, end_to_end_id="", status="processing",
                    failure_reason=None,
                )
            log.warning("Asaas payout falhou txid=%s: %s", req.txid, msg)
            return PixPayoutResponse(
                txid=req.txid, end_to_end_id="", status="failed",
                failure_reason=msg[:255],
            )

        return self._to_payout_response(req.txid, data)

    def _to_payout_response(self, txid: str, data: dict) -> PixPayoutResponse:
        status_raw = (data.get("status") or "").upper()
        status = _STATUS_MAP.get(status_raw, "processing")

        # `authorized: false` = travada aguardando Token SMS na conta de
        # origem. Nunca tratar como paga.
        if data.get("authorized") is False and status == "paid":
            status = "processing"

        return PixPayoutResponse(
            txid=txid,
            end_to_end_id=data.get("endToEndIdentifier") or "",
            status=status,
            failure_reason=(data.get("failReason") or None) if status == "failed" else None,
            provider_transfer_id=data.get("id") or None,
        )

    # ---------------- Consultas ---------------- #

    def get_transfer(self, transfer_id: str) -> dict:
        """GET /v3/transfers/{id} — fonte de verdade após o webhook.

        O webhook do Asaas não é assinado (só token estático), então o
        handler re-consulta aqui antes de mexer no ledger.
        """
        return self._request("GET", f"/transfers/{transfer_id}")

    def get_balance_cents(self) -> int:
        """GET /v3/finance/balance — saldo disponível para transferir."""
        data = self._request("GET", "/finance/balance")
        return int(round(float(data.get("balance") or 0) * 100))

    # ---------------- Cliente (customer) ---------------- #

    def find_or_create_customer(
        self, *, name: str, cpf: str, email: str = ""
    ) -> str:
        """Devolve o id (`cus_…`) do cliente no Asaas, criando se não existir.

        O Asaas EXIGE um customer em `POST /v3/payments` e **permite
        duplicatas** — a deduplicação é responsabilidade nossa. Buscamos por
        CPF antes de criar.
        """
        cpf_digits = _ONLY_DIGITS.sub("", cpf or "")
        if cpf_digits:
            found = self._request("GET", f"/customers?cpfCnpj={cpf_digits}&limit=1")
            data = (found.get("data") or []) if isinstance(found, dict) else []
            if data and data[0].get("id"):
                return data[0]["id"]

        payload = {"name": name or "Cliente BlaXx", "cpfCnpj": cpf_digits}
        if email:
            payload["email"] = email
        created = self._request("POST", "/customers", payload)
        cid = created.get("id")
        if not cid:
            raise AsaasError(f"Asaas não devolveu id do customer: {created}")
        return cid

    # ---------------- Cobrança PIX (entrada) ---------------- #

    def create_charge(self, req: PixChargeRequest) -> PixChargeResponse:
        """Cria cobrança PIX e devolve o copia-e-cola + QR.

        Dois passos no Asaas: `POST /v3/payments` cria a cobrança e
        `GET /v3/payments/{id}/pixQrCode` devolve o BR Code. O
        `externalReference` recebe o nosso `txid` — e aqui, diferente das
        transferências, ele **é filtro documentado** em `GET /v3/payments`,
        então dá para recuperar uma cobrança criada cujo POST deu timeout.
        """
        # Idempotência real: se já existe cobrança com este txid, reaproveita.
        existing = self._find_payment_by_reference(req.txid)
        if existing is not None:
            return self._charge_response(req.txid, existing)

        customer_id = self.find_or_create_customer(
            name=req.payer_name, cpf=req.payer_cpf, email=req.payer_email
        )

        # dueDate é obrigatório (YYYY-MM-DD). O vencimento controla a validade
        # do BR Code; o TTL fino da nossa charge continua sendo o expires_at.
        due = _due_date_from_seconds(req.expires_in_seconds)

        payment = self._request("POST", "/payments", {
            "customer": customer_id,
            "billingType": "PIX",
            "value": round(req.amount_cents / 100, 2),
            "dueDate": due,
            "description": (req.description or "BlaXx — compra de pontos")[:500],
            "externalReference": req.txid,
        })
        return self._charge_response(req.txid, payment)

    def _charge_response(self, txid: str, payment: dict) -> PixChargeResponse:
        payment_id = payment.get("id") or ""
        br_code, image = "", ""
        if payment_id:
            qr = self._request("GET", f"/payments/{payment_id}/pixQrCode")
            br_code = qr.get("payload") or ""
            encoded = qr.get("encodedImage") or ""
            image = f"data:image/png;base64,{encoded}" if encoded else ""
        if not br_code:
            raise AsaasError(
                f"Asaas não devolveu o copia-e-cola do PIX (payment={payment_id}). "
                "A conta tem chave PIX cadastrada?"
            )
        return PixChargeResponse(txid=txid, br_code=br_code, qr_code_image=image)

    def _find_payment_by_reference(self, txid: str) -> dict | None:
        """`externalReference` é filtro documentado em GET /v3/payments."""
        try:
            found = self._request("GET", f"/payments?externalReference={txid}&limit=1")
        except (AsaasError, _IndeterminateError):
            return None
        data = (found.get("data") or []) if isinstance(found, dict) else []
        return data[0] if data else None

    def get_payment(self, payment_id: str) -> dict:
        """GET /v3/payments/{id} — fonte de verdade após o webhook."""
        return self._request("GET", f"/payments/{payment_id}")

    def get_charge_status(self, txid: str) -> str:
        """Status da cobrança pelo nosso txid (externalReference)."""
        payment = self._find_payment_by_reference(txid)
        return (payment or {}).get("status", "unknown")

    # ---------------- Cartão ---------------- #

    def create_card_payment(self, req: CardChargeRequest) -> CardChargeResponse:
        """Cartão via CHECKOUT HOSPEDADO do Asaas (redirect).

        Por que redirect e não tokenização: o Asaas **não oferece SDK JS nem
        checkout transparente**. As únicas opções são (a) redirecionar para o
        `invoiceUrl`, ou (b) trafegar o número do cartão pelo NOSSO backend —
        e (b) joga o BlaXx no escopo PCI-DSS completo. Escolhemos (a): criamos
        a cobrança SEM dados de cartão e devolvemos a URL para onde o cliente
        vai digitar. O PAN nunca toca nosso servidor.

        Custo dessa escolha: o cliente sai do app para pagar. Em troca, o
        escopo PCI continua mínimo — o mesmo nível do desenho anterior.

        Parcelamento é suportado nativamente (`installmentCount`), inclusive
        acima de 12x para Visa/Mastercard.
        """
        existing = self._find_payment_by_reference(req.external_reference)
        if existing is not None:
            return self._card_response(existing)

        customer_id = self.find_or_create_customer(
            name=req.payer_name, cpf=req.payer_cpf, email=req.payer_email
        )

        payload: dict = {
            "customer": customer_id,
            "billingType": "CREDIT_CARD",
            "value": round(req.amount_cents / 100, 2),
            "dueDate": _due_date_from_seconds(3 * 86400),
            "description": (req.description or "BlaXx — compra de pontos")[:500],
            "externalReference": req.external_reference,
        }
        # 1x não deve mandar atributos de parcelamento.
        if (req.installments or 1) > 1:
            payload["installmentCount"] = int(req.installments)
            payload["totalValue"] = round(req.amount_cents / 100, 2)

        try:
            payment = self._request("POST", "/payments", payload)
        except _IndeterminateError as exc:
            log.error(
                "Asaas cartão INDETERMINADO ref=%s (%s) — não recriar; "
                "consultar por externalReference.", req.external_reference, exc,
            )
            return CardChargeResponse(
                provider_payment_id="", status="in_process",
                status_detail="timeout_indeterminado",
            )
        except AsaasError as exc:
            log.warning("Asaas cartão falhou ref=%s: %s", req.external_reference, exc)
            return CardChargeResponse(
                provider_payment_id="", status="rejected", status_detail=str(exc)[:80],
            )

        return self._card_response(payment)

    def _card_response(self, payment: dict) -> CardChargeResponse:
        # A cobrança nasce PENDING: só vira paga quando o cliente conclui no
        # checkout e o webhook chega. Nunca devolver "approved" aqui.
        status = _CARD_STATUS_MAP.get(
            (payment.get("status") or "").upper(), "in_process"
        )
        card = payment.get("creditCard") or {}
        return CardChargeResponse(
            provider_payment_id=payment.get("id") or "",
            status=status,
            status_detail=(payment.get("status") or "")[:80],
            card_brand=card.get("creditCardBrand") or "",
            card_last4=card.get("creditCardNumber") or "",
            redirect_url=payment.get("invoiceUrl") or "",
        )


class _IndeterminateError(RuntimeError):
    """Timeout/rede: não sabemos se a transferência foi criada."""


def _due_date_from_seconds(expires_in_seconds: int) -> str:
    """Vencimento YYYY-MM-DD para a cobrança PIX.

    Mínimo de 1 dia: o Asaas trabalha com data (não timestamp), e vencimento
    no passado recusa a cobrança. O TTL fino continua sendo o `expires_at` da
    nossa PixCharge.
    """
    from datetime import datetime, timedelta, timezone

    days = max(1, int((expires_in_seconds or 0) / 86400) + 1)
    return (datetime.now(timezone.utc) + timedelta(days=days)).strftime("%Y-%m-%d")


def _describe_errors(raw: str) -> str:
    """Extrai `errors[].code/description` do corpo de erro do Asaas."""
    try:
        parsed = json.loads(raw or "{}")
    except ValueError:
        return (raw or "")[:200]
    errs = parsed.get("errors")
    if isinstance(errs, list) and errs:
        return "; ".join(
            f"{e.get('code', '?')}: {e.get('description', '')}" for e in errs
        )[:250]
    return (raw or "")[:200]
