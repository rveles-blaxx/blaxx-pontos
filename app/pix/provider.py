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
