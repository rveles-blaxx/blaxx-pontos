"""Push notifications — APNS (iOS) + FCM (Android) (Sprint 7 / S7-Push).

Modos de operação:
  * Sem env vars → modo `console` (loga; não envia). Deixa o sistema
    funcionando em dev/CI/staging sem precisar configurar Apple/Google.
  * Com APNS_KEY_FILE + APNS_KEY_ID + APNS_TEAM_ID + APNS_BUNDLE_ID
    → envia pra dispositivos `platform='ios'` via APNS HTTP/2 (token-based,
    não exige cert .pem rotativo).
  * Com FCM_PROJECT_ID + FCM_SERVICE_ACCOUNT_JSON
    → envia pra dispositivos `platform='android'` via FCM HTTP v1.

API pública:
    register_device(user, token, platform, app_version=None) -> PushDevice
    send_to_user(user_id, title, body, data=None) -> dict (counts por plataforma)
    revoke_device(device_id, user_id) -> bool

Tudo é best-effort — falhas de envio NÃO levantam, só logam e retornam contadores.
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any

from ..extensions import db
from ..models import PushDevice, User


logger = logging.getLogger(__name__)


# ---------------------- Provider config gates ---------------------- #

def _apns_configured() -> bool:
    return all(os.environ.get(k) for k in
               ("APNS_KEY_ID", "APNS_TEAM_ID", "APNS_KEY_FILE", "APNS_BUNDLE_ID"))


def _fcm_configured() -> bool:
    return bool(os.environ.get("FCM_PROJECT_ID")
                and os.environ.get("FCM_SERVICE_ACCOUNT_JSON"))


def provider_status() -> dict[str, Any]:
    """Diagnóstico pro readyz / admin."""
    return {
        "apns": "configured" if _apns_configured() else "console",
        "fcm": "configured" if _fcm_configured() else "console",
    }


# ---------------------- Device registry ---------------------- #

def register_device(
    user: User,
    token: str,
    platform: str,
    *,
    app_version: str | None = None,
) -> PushDevice:
    """Idempotente: token único globalmente (UNIQUE).

    Se outro user já tinha o token (trocou de conta no device), reatribui
    pro novo user e bumpa last_used_at.
    """
    token = (token or "").strip()
    platform = (platform or "").strip().lower()
    if not token or platform not in ("ios", "android", "web"):
        raise ValueError("token e platform (ios|android|web) obrigatórios")
    if len(token) > 500:
        raise ValueError("token excede 500 chars")

    existing = db.session.query(PushDevice).filter_by(token=token).one_or_none()
    now = datetime.now(timezone.utc)
    if existing is not None:
        existing.user_id = user.id
        existing.platform = platform
        existing.last_used_at = now
        existing.revoked_at = None
        if app_version:
            existing.app_version = app_version[:20]
        db.session.commit()
        return existing
    device = PushDevice(
        user_id=user.id,
        token=token,
        platform=platform,
        app_version=(app_version or None) and app_version[:20],
        registered_at=now,
        last_used_at=now,
    )
    db.session.add(device)
    db.session.commit()
    return device


def revoke_device(device_id: str, user_id: str) -> bool:
    """Soft-delete via revoked_at. Só funciona pro próprio user."""
    dev = db.session.query(PushDevice).filter_by(id=device_id, user_id=user_id).one_or_none()
    if dev is None:
        return False
    dev.revoked_at = datetime.now(timezone.utc)
    db.session.commit()
    return True


def list_user_devices(user_id: str, *, include_revoked: bool = False) -> list[PushDevice]:
    q = db.session.query(PushDevice).filter_by(user_id=user_id)
    if not include_revoked:
        q = q.filter(PushDevice.revoked_at.is_(None))
    return q.order_by(PushDevice.registered_at.desc()).all()


# ---------------------- APNS (token-based) ---------------------- #

def _apns_jwt() -> str | None:
    """Gera JWT ES256 pro APNS (cache de 50min — APNS aceita até 60min).

    Usa cryptography (já é dep) + lib auto-implementada (evita pyjwt extra).
    """
    if not _apns_configured():
        return None
    try:
        from cryptography.hazmat.primitives import serialization, hashes
        from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature, decode_dss_signature
    except ImportError:
        return None

    key_file = os.environ["APNS_KEY_FILE"]
    key_id = os.environ["APNS_KEY_ID"]
    team_id = os.environ["APNS_TEAM_ID"]
    cache_key = (key_file, key_id, team_id)
    now = int(time.time())
    cached = _apns_jwt._cache.get(cache_key) if hasattr(_apns_jwt, "_cache") else None
    if cached and cached[1] > now:
        return cached[0]

    if not os.path.isfile(key_file):
        logger.warning("APNS_KEY_FILE não existe: %s — caindo no modo console", key_file)
        return None

    try:
        import base64
        import hashlib
        with open(key_file, "rb") as fh:
            private_key = serialization.load_pem_private_key(fh.read(), password=None)

        def _b64url(b: bytes) -> str:
            return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")

        header = _b64url(json.dumps({"alg": "ES256", "kid": key_id}, separators=(",", ":")).encode())
        payload = _b64url(json.dumps({"iss": team_id, "iat": now}, separators=(",", ":")).encode())
        signing_input = f"{header}.{payload}".encode()
        der_sig = private_key.sign(signing_input, hashes.SHA256().__class__())  # type: ignore
        # ECDSA returns DER — convert to raw r||s for JOSE
        r, s = decode_dss_signature(der_sig)
        raw = r.to_bytes(32, "big") + s.to_bytes(32, "big")
        jwt = f"{header}.{payload}.{_b64url(raw)}"
        if not hasattr(_apns_jwt, "_cache"):
            _apns_jwt._cache = {}
        _apns_jwt._cache[cache_key] = (jwt, now + 3000)  # 50min
        return jwt
    except Exception:
        logger.exception("Falha ao gerar APNS JWT")
        return None


def _send_apns(token: str, title: str, body: str, data: dict | None) -> bool:
    """Envia 1 push via APNS HTTP/2. True=ok, False=falha."""
    jwt = _apns_jwt()
    if jwt is None:
        return False
    try:
        import httpx  # type: ignore
    except ImportError:
        logger.warning("httpx não instalado — APNS desativado (fallback console)")
        return False

    bundle = os.environ["APNS_BUNDLE_ID"]
    env_host = "api.push.apple.com" if os.environ.get("APNS_PROD", "1") == "1" \
               else "api.sandbox.push.apple.com"
    url = f"https://{env_host}/3/device/{token}"
    payload = {"aps": {"alert": {"title": title, "body": body}, "sound": "default"}}
    if data:
        payload.update({k: v for k, v in data.items() if k != "aps"})

    try:
        with httpx.Client(http2=True, timeout=5.0) as client:
            r = client.post(
                url,
                content=json.dumps(payload),
                headers={
                    "authorization": f"bearer {jwt}",
                    "apns-topic": bundle,
                    "apns-push-type": "alert",
                    "content-type": "application/json",
                },
            )
            return 200 <= r.status_code < 300
    except Exception:
        logger.exception("Falha ao enviar APNS")
        return False


# ---------------------- FCM HTTP v1 ---------------------- #

_fcm_token_cache: dict[str, Any] = {}


def _fcm_oauth_token() -> str | None:
    """Pega OAuth 2.0 access token da Service Account. Cache 50min."""
    if not _fcm_configured():
        return None
    sa_path = os.environ["FCM_SERVICE_ACCOUNT_JSON"]
    if not os.path.isfile(sa_path):
        logger.warning("FCM_SERVICE_ACCOUNT_JSON não existe: %s", sa_path)
        return None

    now = int(time.time())
    cached = _fcm_token_cache.get(sa_path)
    if cached and cached["expires"] > now:
        return cached["token"]

    try:
        # Usa google-auth (já é dep do Google OAuth)
        from google.oauth2 import service_account  # type: ignore
        from google.auth.transport.requests import Request as GARequest  # type: ignore
        creds = service_account.Credentials.from_service_account_file(
            sa_path,
            scopes=["https://www.googleapis.com/auth/firebase.messaging"],
        )
        creds.refresh(GARequest())
        token = creds.token
        _fcm_token_cache[sa_path] = {"token": token, "expires": now + 3000}
        return token
    except Exception:
        logger.exception("Falha ao obter FCM access token")
        return None


def _send_fcm(token: str, title: str, body: str, data: dict | None) -> bool:
    access = _fcm_oauth_token()
    if access is None:
        return False
    try:
        import requests
    except ImportError:
        return False
    project = os.environ["FCM_PROJECT_ID"]
    url = f"https://fcm.googleapis.com/v1/projects/{project}/messages:send"
    message: dict[str, Any] = {
        "message": {
            "token": token,
            "notification": {"title": title, "body": body},
        }
    }
    if data:
        message["message"]["data"] = {k: str(v) for k, v in data.items()}
    try:
        r = requests.post(
            url, json=message,
            headers={"Authorization": f"Bearer {access}", "Content-Type": "application/json"},
            timeout=5.0,
        )
        return 200 <= r.status_code < 300
    except Exception:
        logger.exception("Falha ao enviar FCM")
        return False


# ---------------------- Public send ---------------------- #

def send_to_user(
    user_id: str,
    title: str,
    body: str,
    *,
    data: dict | None = None,
) -> dict[str, int]:
    """Envia push pra todos os devices ativos do user.

    Retorna {sent: N, failed: N, console: N, skipped: N}.
    NÃO levanta exceção — sempre best-effort.
    """
    counters = {"sent": 0, "failed": 0, "console": 0, "skipped": 0}
    devices = list_user_devices(user_id, include_revoked=False)
    if not devices:
        return counters

    for dev in devices:
        ok = False
        if dev.platform == "ios" and _apns_configured():
            ok = _send_apns(dev.token, title, body, data)
        elif dev.platform == "android" and _fcm_configured():
            ok = _send_fcm(dev.token, title, body, data)
        else:
            # Console mode — log estruturado, não envia
            logger.info(
                "[push:console] user=%s platform=%s title=%r body=%r data=%s",
                user_id, dev.platform, title, body, json.dumps(data or {}, default=str),
            )
            counters["console"] += 1
            continue
        if ok:
            counters["sent"] += 1
            dev.last_used_at = datetime.now(timezone.utc)
        else:
            counters["failed"] += 1
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
    return counters
