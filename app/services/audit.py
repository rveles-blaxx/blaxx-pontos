"""Serviço de auditoria — registra eventos de segurança e conta.

Eventos rastreados (Spec do user, seção 8):
  - register, login_success, login_fail, logout
  - password_change, password_reset_request
  - email_verified, token_revoked
  - account_locked, account_unlocked
  - vip_changed, role_changed
  - mfa_enabled, mfa_disabled
  - consent_accepted, profile_updated

Cada log captura: user_id, IP, user_agent, device_id, event, timestamp, status,
reason, correlation_id (request_id pra agregar fluxo).
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from flask import request

from ..extensions import db
from ..models import AuditLog, LoginAttempt


def _client_ip() -> str | None:
    """Pega IP real respeitando proxy do Fly.io."""
    fwd = request.headers.get("X-Forwarded-For", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.remote_addr


def _correlation_id() -> str:
    """ID de correlação por request (gerado se não veio do client)."""
    return request.headers.get("X-Request-ID") or uuid.uuid4().hex[:16]


def log_event(
    event: str,
    *,
    user_id: str | None = None,
    status: str = "ok",
    reason: str | None = None,
    device_id: str | None = None,
    extra: dict[str, Any] | None = None,
    commit: bool = True,
) -> AuditLog:
    """Registra um evento de auditoria. Sempre chamado dentro de request context."""
    log = AuditLog(
        user_id=user_id,
        event=event,
        ip=_client_ip(),
        user_agent=(request.headers.get("User-Agent") or "")[:500],
        device_id=device_id,
        status=status,
        reason=reason,
        correlation_id=_correlation_id(),
        extra_data=json.dumps(extra, default=str)[:1000] if extra else None,
    )
    db.session.add(log)
    if commit:
        db.session.commit()
    return log


def log_login_attempt(
    email_attempted: str,
    success: bool,
    *,
    user_id: str | None = None,
    reason: str | None = None,
    commit: bool = True,
) -> LoginAttempt:
    """Registra tentativa de login (sucesso E falha). Anti-enumeração: mesmo
    quando user não existe, registramos pelo email tentado."""
    attempt = LoginAttempt(
        user_id=user_id,
        email_attempted=email_attempted[:180],
        ip=_client_ip(),
        user_agent=(request.headers.get("User-Agent") or "")[:500],
        success=success,
        reason=reason,
    )
    db.session.add(attempt)
    if commit:
        db.session.commit()
    return attempt
