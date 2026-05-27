"""Provedor PIX mock - simula gateway real."""
from __future__ import annotations
import secrets
from .provider import (PixChargeRequest, PixChargeResponse, PixPayoutRequest, PixPayoutResponse, PixProvider)


def _emv_field(tag: str, value: str) -> str:
    return f"{tag}{len(value):02d}{value}"


def _crc16_ccitt(payload: str) -> str:
    crc = 0xFFFF
    for byte in payload.encode("utf-8"):
        crc ^= byte << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = (crc << 1) ^ 0x1021
            else:
                crc <<= 1
            crc &= 0xFFFF
    return f"{crc:04X}"


def build_br_code(*, pix_key, merchant_name, merchant_city, amount_cents, txid):
    payload = "".join([
        _emv_field("00", "01"),
        _emv_field("01", "12"),
        _emv_field("26", _emv_field("00", "BR.GOV.BCB.PIX") + _emv_field("01", pix_key)),
        _emv_field("52", "0000"),
        _emv_field("53", "986"),
        _emv_field("54", f"{amount_cents/100:.2f}"),
        _emv_field("58", "BR"),
        _emv_field("59", merchant_name[:25]),
        _emv_field("60", merchant_city[:15]),
        _emv_field("62", _emv_field("05", txid[:25])),
        "6304",
    ])
    return payload + _crc16_ccitt(payload)


class MockPixProvider(PixProvider):
    name = "mock"
    MERCHANT_KEY = "blaxxpontos@blaxx.com.br"
    MERCHANT_NAME = "BLAXX PONTOS"
    MERCHANT_CITY = "SAO PAULO"

    def create_charge(self, req: PixChargeRequest) -> PixChargeResponse:
        br_code = build_br_code(
            pix_key=self.MERCHANT_KEY, merchant_name=self.MERCHANT_NAME,
            merchant_city=self.MERCHANT_CITY, amount_cents=req.amount_cents,
            txid=req.txid)
        return PixChargeResponse(txid=req.txid, br_code=br_code, qr_code_image="")

    def request_payout(self, req: PixPayoutRequest) -> PixPayoutResponse:
        if req.pix_key.startswith("fail-"):
            return PixPayoutResponse(txid=req.txid, end_to_end_id="",
                                      status="failed",
                                      failure_reason="chave PIX invalida ou bloqueada")
        # Bacen EndToEndID: 32 chars (E + 31)
        eid = "E" + secrets.token_hex(16).upper()[:31]
        return PixPayoutResponse(txid=req.txid, end_to_end_id=eid,
                                  status="paid", failure_reason=None)

    def get_charge_status(self, txid: str) -> str:
        return "unknown"
