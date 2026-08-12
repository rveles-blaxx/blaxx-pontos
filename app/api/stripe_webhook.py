"""Webhook do Stripe (cartão internacional).

Diferença importante para o webhook do Asaas: a Stripe **assina o corpo**
(`Stripe-Signature` = HMAC-SHA256 de `"{timestamp}.{corpo}"` com o webhook
secret, com janela anti-replay). Isso é verificação criptográfica de verdade,
então aqui a assinatura é a autenticação — não precisamos reconsultar a API
por questão de confiança.

Ainda assim reconsultamos `GET /payment_intents/{id}` antes de creditar, por
um motivo diferente: o corpo do evento é um *snapshot* do momento em que ele
foi gerado e a fila pode entregar fora de ordem. O estado atual manda.

Eventos tratados:
  payment_intent.succeeded        → aprova e credita
  payment_intent.payment_failed   → rejeita
  charge.refunded                 → estorna
  charge.dispute.created          → chargeback
"""

from __future__ import annotations

from flask import Blueprint, current_app, jsonify, request

from ..extensions import db, limiter
from ..models import CardCharge

bp = Blueprint("stripe_webhook", __name__)

_EVENT_TO_STATUS = {
    "payment_intent.succeeded": "approved",
    "payment_intent.payment_failed": "rejected",
    "charge.refunded": "refunded",
    "charge.dispute.created": "charged_back",
}


def _provider():
    return current_app.extensions.get("card_intl_provider")


@bp.post("/webhook")
@limiter.limit("120 per minute")
def stripe_webhook():
    provider = _provider()
    if provider is None:
        # Stripe não configurado — não existe rota útil aqui.
        return jsonify({"error": "not configured"}), 404

    raw = request.get_data() or b""
    signature = request.headers.get("Stripe-Signature", "")

    try:
        event = provider.verify_webhook(raw, signature)
    except Exception as exc:  # noqa: BLE001
        current_app.logger.warning("stripe webhook: assinatura inválida (%s)", exc)
        return jsonify({"error": "invalid signature"}), 401

    event_type = (event.get("type") or "").strip()
    target = _EVENT_TO_STATUS.get(event_type)
    if target is None:
        return jsonify({"ok": True, "ignored": event_type}), 200

    obj = ((event.get("data") or {}).get("object")) or {}
    # payment_intent.* traz o PI direto; charge.* traz o id em payment_intent.
    pi_id = obj.get("id") if obj.get("object") == "payment_intent" else obj.get("payment_intent")
    if not pi_id:
        current_app.logger.warning(
            "stripe webhook %s sem payment_intent — conciliar manualmente.", event_type
        )
        return jsonify({"ok": True}), 200

    try:
        _process(provider, pi_id, target, event_type)
    except Exception as exc:  # noqa: BLE001
        current_app.logger.exception(
            "stripe webhook: falha ao processar %s pi=%s: %s", event_type, pi_id, exc
        )
        # 500 faz a Stripe reenviar com backoff (ela não pausa a fila como o
        # Asaas), então aqui devolver erro é o comportamento correto.
        return jsonify({"error": "processing failed"}), 500

    return jsonify({"ok": True}), 200


def _process(provider, pi_id: str, target_status: str, event_type: str) -> None:
    from ..services import card_purchase as card_svc

    charge = (
        db.session.query(CardCharge)
        .filter_by(provider_payment_id=pi_id)
        .one_or_none()
    )
    if charge is None:
        current_app.logger.warning(
            "stripe webhook %s: nenhuma CardCharge com id de provedor %s",
            event_type, pi_id,
        )
        return

    # Estado atual manda sobre o snapshot do evento (entrega fora de ordem).
    if target_status == "approved":
        fresh = provider.payment_status(pi_id)
        if fresh != "approved":
            current_app.logger.info(
                "stripe %s diz succeeded mas o PI está %s — ignorando.",
                pi_id, fresh,
            )
            return

    card_svc.apply_webhook_status(charge, target_status, status_detail=event_type)
