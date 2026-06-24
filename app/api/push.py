"""Web Push — endpoints stub gated por VAPID_PRIVATE_KEY.

Estratégia: o endpoint /push/subscribe SEMPRE existe e aceita a subscription
do cliente (PWA grava localmente também). Quando VAPID_PRIVATE_KEY estiver
setado, o backend persiste a subscription pra enviar pushes reais; sem a
chave, devolve 503 "push gated", deixando o cliente saber que está em
prontidão. Isso evita feature flag duplo (cliente + servidor).

Modelo de persistência mínimo (PushSubscription) fica em models.py quando
o lado servidor de fato disparar pushes (ainda não — requer cron/queue).
"""

from __future__ import annotations

import os

from flask import Blueprint, current_app, jsonify, request

from ..extensions import limiter
from .auth import login_required

bp = Blueprint("push", __name__)


def _vapid_configured() -> bool:
    # Backend só roda pushes se TIVER VAPID_PRIVATE_KEY E PUBLIC_KEY.
    # public é só conveniência (cliente já carrega pelo Vite); private é o gate.
    return bool(os.environ.get("VAPID_PRIVATE_KEY"))


@bp.post("/subscribe")
@login_required
@limiter.limit("10 per minute")
def subscribe():
    """Aceita um JSON serializável de PushSubscription (do navegador).

    Body típico:
      { "endpoint": "...", "keys": { "p256dh": "...", "auth": "..." } }

    Retorna 503 se VAPID_PRIVATE_KEY ainda não estiver no ambiente — o cliente
    aceita esse status como "preparado, servidor ainda gated" (vide push-web.ts).
    """
    data = request.get_json(silent=True) or {}
    endpoint = (data.get("endpoint") or "").strip()
    keys = data.get("keys") or {}
    if not endpoint or not isinstance(keys, dict):
        return jsonify({"error": "subscription inválida"}), 400
    if not _vapid_configured():
        # Estado prontidão — cliente sabe lidar (toast informativo).
        return jsonify({
            "ok": False,
            "gated": True,
            "reason": "VAPID_PRIVATE_KEY ausente no servidor",
        }), 503
    # Persistência de subscription fica pra quando o cron de push estiver
    # plugado. Por enquanto, só audita pra confirmar caminho fim-a-fim.
    current_app.logger.info("[push] subscription recebida endpoint=%s…", endpoint[:60])
    return jsonify({"ok": True}), 201


@bp.post("/unsubscribe")
@login_required
@limiter.limit("10 per minute")
def unsubscribe():
    """Remove subscription (cliente decidiu desligar)."""
    data = request.get_json(silent=True) or {}
    endpoint = (data.get("endpoint") or "").strip()
    if not endpoint:
        return jsonify({"error": "endpoint obrigatório"}), 400
    current_app.logger.info("[push] unsubscribe endpoint=%s…", endpoint[:60])
    return jsonify({"ok": True})
