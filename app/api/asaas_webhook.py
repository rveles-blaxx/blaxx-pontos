"""Webhook de TRANSFERÊNCIAS do Asaas (PIX de saída / resgate).

Segurança — leia antes de mexer:

O Asaas NÃO assina o corpo do webhook. A única autenticação é um token
estático no header `asaas-access-token`. Consequências no desenho daqui:

  1. Comparação do token em tempo constante (`hmac.compare_digest`).
  2. O payload é tratado como GATILHO, nunca como verdade: antes de mexer no
     ledger, re-consultamos `GET /v3/transfers/{id}` na API. Quem tiver o
     token não consegue, sozinho, confirmar um payout que o Asaas não pagou.
  3. Dedup por `id` do evento (entrega é "at least once").

Operacional: 15 falhas consecutivas INTERROMPEM a fila de webhooks do Asaas
inteira. Por isso este handler devolve 2xx em quase tudo — só devolve erro
quando a autenticação falha. Erro de processamento é logado e devolvido 200,
para não derrubar a fila por causa de um evento malformado.

Eventos: TRANSFER_CREATED · TRANSFER_PENDING · TRANSFER_IN_BANK_PROCESSING
         TRANSFER_BLOCKED · TRANSFER_DONE · TRANSFER_FAILED · TRANSFER_CANCELLED
Docs: https://docs.asaas.com/docs/webhook-para-transferencias
"""

from __future__ import annotations

import hmac

from flask import Blueprint, current_app, jsonify, request

from ..extensions import db, limiter
from ..models import AsaasWebhookEvent
from ..services import redeem as redeem_svc

bp = Blueprint("asaas_webhook", __name__)

# Status finais do Asaas que fecham o payout.
_PAID = {"DONE"}
_FAILED = {"FAILED", "CANCELLED"}


def _verify_token() -> bool:
    """Compara o `asaas-access-token` em tempo constante.

    Fail-closed: sem ASAAS_WEBHOOK_TOKEN configurado, rejeita (fora de
    dev/test). Melhor recusar o evento — o Asaas reenvia — do que aceitar
    um webhook não autenticado que mexe em dinheiro.
    """
    expected = (current_app.config.get("ASAAS_WEBHOOK_TOKEN") or "").strip()
    got = (request.headers.get("asaas-access-token") or "").strip()
    if not expected:
        if current_app.debug or current_app.config.get("TESTING"):
            return True
        current_app.logger.error(
            "ASAAS_WEBHOOK_TOKEN não configurado — rejeitando webhook do Asaas."
        )
        return False
    if not got:
        return False
    return hmac.compare_digest(expected, got)


@bp.post("/webhook")
@limiter.limit("120 per minute")
def asaas_webhook():
    if not _verify_token():
        current_app.logger.warning("asaas webhook: token inválido")
        return jsonify({"error": "unauthorized"}), 401

    payload = request.get_json(silent=True) or {}
    event_id = (payload.get("id") or "").strip()
    event = (payload.get("event") or "").strip().upper()

    is_transfer = event.startswith("TRANSFER_")
    is_payment = event.startswith("PAYMENT_")
    if not (is_transfer or is_payment):
        return jsonify({"ok": True, "ignored": event}), 200

    resource = (payload.get("transfer") if is_transfer else payload.get("payment")) or {}
    resource_id = (resource.get("id") or "").strip()
    txid = (resource.get("externalReference") or "").strip()

    # ---- dedup: entrega é "at least once" ----
    if event_id:
        seen = db.session.get(AsaasWebhookEvent, event_id)
        if seen is not None:
            return jsonify({"ok": True, "replay": True}), 200

    try:
        if is_transfer:
            _process(event, resource_id, txid, resource)
        else:
            _process_payment(event, resource_id, txid, resource)
    except Exception as exc:  # noqa: BLE001
        # Nunca propaga: 15 falhas seguidas param a fila inteira do Asaas.
        current_app.logger.exception(
            "asaas webhook: falha ao processar event=%s recurso=%s txid=%s: %s",
            event, resource_id, txid, exc,
        )
        return jsonify({"ok": True, "deferred": True}), 200

    # Registra o evento só depois do processamento dar certo, para que um
    # retry do Asaas reprocesse um evento que falhou no meio.
    if event_id:
        db.session.add(AsaasWebhookEvent(
            event_id=event_id, event=event[:40],
            transfer_id=resource_id or None, txid=txid or None,
        ))
        db.session.commit()

    return jsonify({"ok": True}), 200


def _process(event: str, transfer_id: str, txid: str, transfer: dict) -> None:
    if not txid:
        current_app.logger.warning(
            "asaas webhook %s sem externalReference (transfer=%s) — "
            "impossível amarrar a um payout; conciliar manualmente.",
            event, transfer_id,
        )
        return

    # ---- fonte de verdade: re-consulta a API ---- #
    # O webhook não é assinado; não confiamos no status que veio no corpo.
    status = (transfer.get("status") or "").upper()
    end_to_end = transfer.get("endToEndIdentifier") or ""
    fail_reason = transfer.get("failReason") or ""
    authorized = transfer.get("authorized")

    provider = _payout_provider()
    if provider is not None and transfer_id:
        try:
            fresh = provider.get_transfer(transfer_id)
            status = (fresh.get("status") or status).upper()
            end_to_end = fresh.get("endToEndIdentifier") or end_to_end
            fail_reason = fresh.get("failReason") or fail_reason
            authorized = fresh.get("authorized", authorized)
        except Exception as exc:  # noqa: BLE001
            # Sem confirmação independente não mexemos em dinheiro.
            current_app.logger.error(
                "asaas webhook: não consegui reconsultar transfer=%s (%s). "
                "Ignorando evento — o Asaas reenviará.", transfer_id, exc,
            )
            raise

    # Travada aguardando Token SMS: não é paga nem falha.
    if authorized is False:
        current_app.logger.warning(
            "asaas transfer=%s txid=%s com authorized=false (aguarda Token "
            "SMS na conta Asaas) — payout segue retido.", transfer_id, txid,
        )
        return

    if status in _PAID:
        redeem_svc.confirm_payout(txid, end_to_end_id=end_to_end or None)
        current_app.logger.info("payout %s confirmado via Asaas (e2e=%s)", txid, end_to_end)
    elif status in _FAILED:
        redeem_svc.fail_payout(
            txid, reason=(fail_reason or f"Asaas: {status}")[:255]
        )
        current_app.logger.warning("payout %s falhou no Asaas: %s", txid, fail_reason)
    else:
        # PENDING / BANK_PROCESSING / bloqueado — segue retido.
        current_app.logger.info(
            "payout %s em andamento no Asaas (status=%s)", txid, status
        )


# ─────────────────────── COBRANÇA (entrada) ─────────────────────── #

# Só creditamos pontos quando o dinheiro está DISPONÍVEL na conta Asaas.
#
# Por que não em CONFIRMED: no PIX, uma cobrança CONFIRMED pode ficar até 72h
# em retenção antifraude e terminar em REFUNDED; no cartão, CONFIRMED chega na
# hora mas o dinheiro só entra em ~D+32 e ainda pode virar chargeback. Como o
# ponto creditado é resgatável em PIX (dinheiro real, na hora), creditar em
# CONFIRMED permitiria sacar dinheiro que a empresa ainda não recebeu — e que
# pode nunca receber.
_PAYMENT_CREDIT = {"RECEIVED", "RECEIVED_IN_CASH"}

# Eventos que desfazem o pagamento → estornar pontos.
_PAYMENT_REVERSED = {
    "PAYMENT_REFUNDED",
    "PAYMENT_PARTIALLY_REFUNDED",
    "PAYMENT_CHARGEBACK_REQUESTED",
    "PAYMENT_REPROVED_BY_RISK_ANALYSIS",
    "PAYMENT_RECEIVED_IN_CASH_UNDONE",
}


def _process_payment(event: str, payment_id: str, txid: str, payment: dict) -> None:
    from ..services import purchase as purchase_svc

    if not txid:
        current_app.logger.warning(
            "asaas webhook %s sem externalReference (payment=%s) — conciliar "
            "manualmente.", event, payment_id,
        )
        return

    if event in _PAYMENT_REVERSED:
        current_app.logger.error(
            "PAGAMENTO REVERTIDO no Asaas: event=%s payment=%s txid=%s. "
            "Pontos podem já ter sido creditados e resgatados — conciliação "
            "MANUAL obrigatória.", event, payment_id, txid,
        )
        return

    # Fonte de verdade: o webhook não é assinado, reconsulta a API.
    status = (payment.get("status") or "").upper()
    provider = _payout_provider()
    if provider is not None and payment_id:
        try:
            fresh = provider.get_payment(payment_id)
            status = (fresh.get("status") or status).upper()
        except Exception as exc:  # noqa: BLE001
            current_app.logger.error(
                "asaas webhook: não consegui reconsultar payment=%s (%s). "
                "Ignorando — o Asaas reenviará.", payment_id, exc,
            )
            raise

    if status in _PAYMENT_CREDIT:
        purchase_svc.confirm_payment(txid, provider_confirmed=True)
        current_app.logger.info(
            "compra %s creditada via Asaas (status=%s)", txid, status
        )
    elif status == "CONFIRMED":
        # Pago, mas o saldo ainda não está disponível. Não credita.
        current_app.logger.info(
            "compra %s CONFIRMED no Asaas — aguardando RECEIVED para creditar "
            "(retenção antifraude/D+32).", txid,
        )
    else:
        current_app.logger.info(
            "compra %s em andamento no Asaas (status=%s)", txid, status
        )


def _payout_provider():
    """Devolve o AsaasPixProvider injetado, se houver."""
    pix = current_app.extensions.get("pix_provider")
    prov = getattr(pix, "payout_provider", None)
    if prov is not None and getattr(prov, "name", "") == "asaas":
        return prov
    if getattr(pix, "name", "") == "asaas":
        return pix
    return None
