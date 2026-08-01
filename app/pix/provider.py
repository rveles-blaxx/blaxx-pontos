"""Interface PIX — abstrai o provedor (Mercado Pago, Asaas, Efí, Stark, etc.).

Trocar de provedor = escrever uma nova subclasse e injetar no app factory.
Nenhuma regra de negócio precisa mudar.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(frozen=True)
class PixChargeRequest:
    txid: str
    amount_cents: int
    description: str
    payer_name: str
    payer_cpf: str
    expires_in_seconds: int
    payer_email: str = ""    # Onda 3 — MP exige email válido do payer


@dataclass(frozen=True)
class PixChargeResponse:
    txid: str
    br_code: str           # "copia e cola" no padrão EMV BR Code
    qr_code_image: str     # data URI base64 (opcional)


@dataclass(frozen=True)
class CardChargeRequest:
    """Pagamento com cartão via Checkout API (token gerado no frontend).

    PCI: card_token é o token single-use do SDK JS — nunca PAN/CVV.
    """
    external_reference: str      # "card-<charge.id>" — roteia o webhook
    amount_cents: int
    description: str
    card_token: str
    payment_method_id: str       # visa | master | amex | elo | hipercard...
    installments: int
    payer_name: str
    payer_cpf: str
    payer_email: str
    issuer_id: str = ""          # opcional — o Brick envia quando aplicável
    statement_descriptor: str = ""


@dataclass(frozen=True)
class CardChargeResponse:
    mp_payment_id: str
    status: str                  # approved | in_process | rejected | ...
    status_detail: str = ""      # cc_rejected_* quando recusado
    card_brand: str = ""
    card_last4: str = ""
    # URL de checkout hospedado, quando o provider cobra por redirect em vez
    # de tokenização no frontend (caso do Asaas, que não tem SDK JS). O
    # cliente é enviado para lá e o webhook confirma depois.
    redirect_url: str = ""


@dataclass(frozen=True)
class PixPayoutRequest:
    txid: str
    amount_cents: int
    pix_key: str
    description: str


@dataclass(frozen=True)
class PixPayoutResponse:
    txid: str
    end_to_end_id: str
    status: str            # "processing" | "paid" | "failed"
    failure_reason: str | None = None
    # Id da transferência no provedor (ex.: id do /v3/transfers do Asaas).
    # Persistido no payout para permitir reconsulta e conciliação.
    provider_transfer_id: str | None = None


class PixProvider(ABC):
    """Contrato mínimo que qualquer integração PIX precisa cumprir."""

    name: str = "abstract"

    @abstractmethod
    def create_charge(self, req: PixChargeRequest) -> PixChargeResponse: ...

    @abstractmethod
    def request_payout(self, req: PixPayoutRequest) -> PixPayoutResponse: ...

    # opcional — alguns provedores expõem polling/consulta
    def get_charge_status(self, txid: str) -> str:
        return "unknown"

    # opcional — cartão de crédito (Checkout API). Providers sem suporte
    # herdam este default e o serviço devolve erro amigável.
    def create_card_payment(self, req: CardChargeRequest) -> CardChargeResponse:
        raise NotImplementedError(
            f"provider {self.name} não suporta pagamento com cartão"
        )
