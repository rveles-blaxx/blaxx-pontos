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


# ============================================================================
# Sprint 7 — APNS/FCM device registry
# ============================================================================

@bp.post("/devices/register")
@login_required
@limiter.limit("20 per hour")
def register_device():
    """Registra device pra push (iOS/Android/Web).

    Body: { "token": "<apns_or_fcm_token>", "platform": "ios"|"android"|"web",
            "app_version": "1.2.3" (opcional) }
    """
    from flask import g
    from ..services import push as push_svc

    data = request.get_json(silent=True) or {}
    token = (data.get("token") or "").strip()
    platform = (data.get("platform") or "").strip().lower()
    app_version = (data.get("app_version") or "").strip() or None
    try:
        dev = push_svc.register_device(
            g.current_user, token, platform, app_version=app_version,
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify(dev.to_dict()), 201


@bp.delete("/devices/<device_id>")
@login_required
def revoke_device(device_id: str):
    """Revoga (soft-delete) um device do user."""
    from flask import g
    from ..services import push as push_svc

    ok = push_svc.revoke_device(device_id, g.current_user.id)
    if not ok:
        return jsonify({"error": "device não encontrado"}), 404
    return jsonify({"ok": True})


@bp.get("/devices")
@login_required
def list_devices():
    """Lista os devices ativos do user (sem token)."""
    from flask import g
    from ..services import push as push_svc

    devices = push_svc.list_user_devices(g.current_user.id, include_revoked=False)
    return jsonify({"items": [d.to_dict() for d in devices]})


@bp.get("/status")
@login_required
def status():
    """Diagnóstico — gates APNS/FCM configurados?"""
    from ..services import push as push_svc
    return jsonify(push_svc.provider_status())
