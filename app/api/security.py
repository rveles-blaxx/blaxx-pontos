"""Onda 3 — Endpoints de segurança adicionais.

Implementa fluxos que não existiam no backend Onda 1/2:
  - Cadastro + verificação de telefone (POST/DELETE /user/phone, /user/phone/verify)
  - 2FA por SMS (POST /user/2fa/sms/enable|disable)
  - Listagem e revogação individual de sessões (GET/DELETE /user/sessions)
  - Histórico de acessos (GET /user/access-log)
  - Challenge de login 2FA SMS (POST /auth/login/2fa)

Convenções:
  - Reusa @login_required + login JWT existente de api/auth.py
  - Reusa @limiter de extensions.py
  - Reusa services/audit.py + services/sms.py
  - Telefone armazenado em E.164 via security.normalize_phone
"""
from __future__ import annotations

import re
import secrets
from datetime import datetime, timedelta, timezone

from flask import Blueprint, current_app, g, jsonify, request

from ..extensions import db, limiter
from ..models import (
    AuditLog,
    MfaChallenge,
    PhoneOtp,
    RefreshTokenDB,
    User,
)
from ..security import normalize_phone
from ..services import audit as audit_svc
from ..services import sms as sms_svc
from .auth import _bearer_user, _issue_tokens, _auth_response, login_required


bp = Blueprint("security", __name__)


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------
def _client_ip() -> str | None:
    fwd = request.headers.get("X-Forwarded-For", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.remote_addr


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _gen_code(digits: int = 6) -> str:
    return f"{secrets.randbelow(10 ** digits):0{digits}d}"


def _mask_phone(phone: str | None) -> str:
    if not phone or len(phone) < 4:
        return "***"
    return "***" + phone[-4:]


def _mask_ip(ip: str | None) -> str:
    if not ip:
        return ""
    parts = ip.split(".")
    if len(parts) == 4:
        return f"{parts[0]}.{parts[1]}.***.***"
    if ":" in ip:
        return ip.split(":")[0] + "::***"
    return "***"


def _device_from_ua(ua: str | None) -> str:
    if not ua:
        return "Desconhecido"
    u = ua.lower()
    os_ = "Desconhecido"
    if "iphone" in u: os_ = "iPhone"
    elif "ipad" in u: os_ = "iPad"
    elif "android" in u: os_ = "Android"
    elif "mac os" in u or "macintosh" in u: os_ = "Mac"
    elif "windows" in u: os_ = "Windows"
    elif "linux" in u: os_ = "Linux"
    browser = "Desconhecido"
    if "edg/" in u: browser = "Edge"
    elif "chrome/" in u: browser = "Chrome"
    elif "firefox/" in u: browser = "Firefox"
    elif "safari/" in u: browser = "Safari"
    return f"{os_} · {browser}"


# ----------------------------------------------------------------------
# Cooldown simples em memória (fallback se Limiter não cobrir)
# ----------------------------------------------------------------------
_cooldown_state: dict[str, float] = {}


def _cooldown_check(key: str, seconds: int) -> tuple[bool, int]:
    """Retorna (permitido, retry_in_seconds)."""
    import time
    now = time.time()
    last = _cooldown_state.get(key)
    if last is not None and (now - last) < seconds:
        return False, int(seconds - (now - last))
    _cooldown_state[key] = now
    return True, 0


# ======================================================================
# POST /user/phone — cadastra/troca telefone + envia OTP
# ======================================================================
@bp.post("/phone")
@login_required
@limiter.limit("5 per minute; 30 per hour")
def request_phone():
    cfg = current_app.config
    data = request.get_json(silent=True) or {}
    user: User = g.current_user

    raw = data.get("phone")
    phone = normalize_phone(raw or "")
    if not phone:
        return jsonify({"error": "Telefone inválido. Use (11) 99999-9999 ou +5511999999999",
                        "code": "invalid_phone"}), 400

    can, retry = _cooldown_check(
        f"phone_otp:user:{user.id}", cfg.get("PHONE_OTP_COOLDOWN", 60)
    )
    if not can:
        return jsonify({"error": "Aguarde antes de pedir novo código",
                        "code": "cooldown", "retry_in": retry}), 429

    # Conflito: se outro user já tem esse telefone, bloqueia
    other = db.session.query(User).filter(User.phone == phone, User.id != user.id).first()
    if other:
        return jsonify({"error": "Este celular já está vinculado a outra conta",
                        "code": "phone_in_use"}), 409

    # Salva como pendente (não verifica ainda)
    user.phone = phone
    user.phone_verified = False
    code = _gen_code(6)
    db.session.add(PhoneOtp(
        user_id=user.id,
        phone=phone,
        code_hash=PhoneOtp.hash_code(code),
        purpose="verify_phone",
        expires_at=_utcnow() + timedelta(seconds=cfg.get("PHONE_OTP_TTL", 600)),
    ))
    db.session.commit()

    sms_svc.send_otp(phone, code, "verify_phone")
    audit_svc.log_event("phone_otp_sent", user_id=user.id,
                         extra={"purpose": "verify_phone"})

    return jsonify({
        "message": "Enviamos um código de 6 dígitos por SMS",
        "phone_masked": _mask_phone(phone),
    })


# ======================================================================
# POST /user/phone/verify — confirma OTP
# ======================================================================
@bp.post("/phone/verify")
@login_required
@limiter.limit("10 per minute")
def verify_phone():
    data = request.get_json(silent=True) or {}
    user: User = g.current_user
    code = (data.get("code") or "").strip()

    if not re.match(r"^\d{6}$", code):
        return jsonify({"error": "Código inválido", "code": "invalid_code"}), 400

    record = db.session.query(PhoneOtp).filter_by(
        user_id=user.id, purpose="verify_phone", used_at=None
    ).order_by(PhoneOtp.created_at.desc()).first()
    if not record:
        return jsonify({"error": "Nenhum código pendente. Solicite um novo.",
                        "code": "no_pending_code"}), 400

    record.attempts = (record.attempts or 0) + 1
    db.session.commit()

    if not record.is_valid():
        return jsonify({"error": "Código expirado ou bloqueado por tentativas",
                        "code": "code_expired"}), 400
    if record.code_hash != PhoneOtp.hash_code(code):
        return jsonify({"error": "Código não confere", "code": "wrong_code"}), 400

    record.used_at = _utcnow()
    user.phone = record.phone
    user.phone_verified = True
    db.session.commit()

    audit_svc.log_event("phone_verified", user_id=user.id)
    return jsonify({"message": "Telefone verificado", "user": user.to_dict()})


# ======================================================================
# DELETE /user/phone — remove telefone (desativa MFA SMS se ativo)
# ======================================================================
@bp.delete("/phone")
@login_required
def remove_phone():
    data = request.get_json(silent=True) or {}
    user: User = g.current_user
    password = data.get("password") or ""

    if not user.has_password or not user.check_password(password):
        return jsonify({"error": "Senha incorreta", "code": "wrong_password"}), 400

    user.phone = None
    user.phone_verified = False
    if user.mfa_enabled and user.mfa_method == "sms":
        user.mfa_enabled = False
        user.mfa_method = None
        audit_svc.log_event("mfa_disabled", user_id=user.id,
                             reason="phone_removed", commit=False)
    db.session.commit()

    audit_svc.log_event("phone_removed", user_id=user.id)
    return jsonify({"user": user.to_dict()})


# ======================================================================
# POST /user/2fa/sms/enable — ativa 2FA SMS (requer phone_verified)
# ======================================================================
@bp.post("/2fa/sms/enable")
@login_required
@limiter.limit("5 per hour")
def enable_2fa_sms():
    user: User = g.current_user
    if not user.phone_verified or not user.phone:
        return jsonify({"error": "Verifique seu telefone antes de ativar 2FA",
                        "code": "phone_not_verified"}), 400
    if user.mfa_enabled and user.mfa_method == "sms":
        return jsonify({"message": "2FA SMS já ativo", "user": user.to_dict()})

    # Se tinha TOTP ativo, mantemos exclusivo (1 método por vez)
    user.mfa_enabled = True
    user.mfa_method = "sms"
    db.session.commit()

    audit_svc.log_event("mfa_enabled", user_id=user.id, extra={"method": "sms"})
    sms_svc.send_security_alert(user.phone, "2FA por SMS foi ATIVADA")
    return jsonify({"message": "2FA por SMS ativada", "user": user.to_dict()})


# ======================================================================
# POST /user/2fa/sms/disable — desativa (exige senha)
# ======================================================================
@bp.post("/2fa/sms/disable")
@login_required
@limiter.limit("5 per hour")
def disable_2fa_sms():
    data = request.get_json(silent=True) or {}
    user: User = g.current_user
    password = data.get("password") or ""

    if not user.has_password or not user.check_password(password):
        return jsonify({"error": "Senha incorreta", "code": "wrong_password"}), 400
    if not user.mfa_enabled or user.mfa_method != "sms":
        return jsonify({"message": "2FA SMS já estava desativada", "user": user.to_dict()})

    user.mfa_enabled = False
    user.mfa_method = None
    db.session.commit()
    audit_svc.log_event("mfa_disabled", user_id=user.id)
    if user.phone:
        sms_svc.send_security_alert(user.phone, "2FA por SMS foi DESATIVADA")
    return jsonify({"message": "2FA desativada", "user": user.to_dict()})


# ======================================================================
# GET /user/sessions — lista sessões ativas (refresh tokens ativos)
# ======================================================================
@bp.get("/sessions")
@login_required
def list_sessions():
    user: User = g.current_user
    rows = db.session.query(RefreshTokenDB).filter(
        RefreshTokenDB.user_id == user.id,
        RefreshTokenDB.revoked_at.is_(None),
    ).order_by(RefreshTokenDB.created_at.desc()).all()

    # Marca a sessão atual: olha pelo IP+UA do request — heurística (na falta
    # do JTI atual no refresh, comparar device_id/ip ajuda)
    cur_ua = (request.headers.get("User-Agent") or "")[:500]
    cur_ip = _client_ip()
    return jsonify({
        "sessions": [
            {
                "id": s.id,
                "device_name": _device_from_ua(s.user_agent),
                "ip_address": s.ip,
                "user_agent": s.user_agent,
                "created_at": s.created_at.isoformat() if s.created_at else None,
                "last_used_at": s.created_at.isoformat() if s.created_at else None,
                "expires_at": s.expires_at.isoformat() if s.expires_at else None,
                "current": (s.ip == cur_ip and (s.user_agent or "") == cur_ua),
            }
            for s in rows
        ]
    })


# ======================================================================
# DELETE /user/sessions/<id> — revoga sessão específica
# ======================================================================
@bp.delete("/sessions/<session_id>")
@login_required
def kill_session(session_id: str):
    user: User = g.current_user
    sess = db.session.get(RefreshTokenDB, session_id)
    if not sess or sess.user_id != user.id:
        return jsonify({"error": "Sessão não encontrada", "code": "session_not_found"}), 404
    if sess.revoked_at is not None:
        return jsonify({"message": "Sessão já revogada"})
    sess.revoked_at = _utcnow()
    db.session.commit()
    audit_svc.log_event("session_revoked", user_id=user.id,
                         extra={"session_id": session_id})
    return jsonify({"ok": True})


# ======================================================================
# GET /user/access-log — histórico de eventos relevantes
# ======================================================================
@bp.get("/access-log")
@login_required
def access_log():
    user: User = g.current_user
    limit = int(request.args.get("limit", 50))
    limit = max(1, min(200, limit))

    relevant = [
        "login_success", "login_fail", "account_locked", "account_unlocked",
        "logout", "sessions_revoked_all", "session_revoked",
        "password_change", "password_reset_request",
        "mfa_enabled", "mfa_disabled",
        "mfa_challenge_issued", "mfa_challenge_success", "mfa_challenge_fail",
        "phone_verified", "phone_removed",
        "email_verified", "email_changed",
    ]
    rows = db.session.query(AuditLog).filter(
        AuditLog.user_id == user.id,
        AuditLog.event.in_(relevant),
    ).order_by(AuditLog.created_at.desc()).limit(limit).all()

    import json as _json
    return jsonify({
        "items": [
            {
                "event": r.event,
                "ip": _mask_ip(r.ip),
                "user_agent": r.user_agent,
                "device": _device_from_ua(r.user_agent),
                "status": r.status,
                "reason": r.reason,
                "metadata": _json.loads(r.extra_data) if r.extra_data else None,
                "at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ]
    })


# ======================================================================
# POST /auth/login/2fa — completa o challenge SMS
# Body: { challenge_token, code }
# Retorno: { token, refresh_token, user } como /auth/login
# ======================================================================
def register_login_2fa_route(auth_bp: Blueprint):
    """Registra o endpoint /auth/login/2fa no blueprint de auth existente.

    Chamado pelo factory create_app depois de importar este módulo, para
    manter as rotas SMS-MFA com o mesmo prefix /auth/* já estabelecido.

    IDEMPOTENTE (fix pytest): em testes, create_app() roda multiplas vezes
    e o blueprint e' modulo-nivel (instancia unica). Flask 3+ proibe
    adicionar rotas a blueprint ja registrado. Aqui usamos um flag pra
    so registrar UMA vez no processo — chamadas subsequentes sao no-op.
    """
    # Idempotency guard (mantém a função singleton sem precisar de lock)
    if getattr(register_login_2fa_route, "_registered", False):
        return
    register_login_2fa_route._registered = True

    @auth_bp.post("/login/2fa")
    @limiter.limit("10 per minute")
    def login_2fa():
        cfg = current_app.config
        data = request.get_json(silent=True) or {}
        token = (data.get("challenge_token") or "").strip()
        code = (data.get("code") or "").strip()

        if not token:
            return jsonify({"error": "Challenge inválido",
                            "code": "missing_challenge"}), 400
        if not re.match(r"^\d{6}$", code):
            return jsonify({"error": "Código inválido",
                            "code": "invalid_code"}), 400

        challenge = db.session.query(MfaChallenge).filter_by(
            challenge_token_hash=MfaChallenge.hash_token(token)
        ).first()
        if not challenge or not challenge.is_valid():
            audit_svc.log_event("mfa_challenge_fail",
                                  reason="invalid_or_expired_challenge", status="warn")
            return jsonify({"error": "Challenge inválido ou expirado",
                            "code": "challenge_expired"}), 400

        user = db.session.get(User, challenge.user_id)
        if not user or user.status != "active":
            return jsonify({"error": "Conta indisponível",
                            "code": "account_inactive"}), 403

        otp = db.session.get(PhoneOtp, challenge.phone_otp_id) if challenge.phone_otp_id else None
        if not otp:
            return jsonify({"error": "OTP não encontrado",
                            "code": "otp_not_found"}), 400
        otp.attempts = (otp.attempts or 0) + 1
        db.session.commit()
        if not otp.is_valid():
            audit_svc.log_event("mfa_challenge_fail", user_id=user.id,
                                  reason="otp_invalid", status="warn", commit=False)
            db.session.commit()
            return jsonify({"error": "Código expirado ou bloqueado",
                            "code": "code_expired"}), 400
        if otp.code_hash != PhoneOtp.hash_code(code):
            audit_svc.log_event("mfa_challenge_fail", user_id=user.id,
                                  reason="wrong_code", status="warn", commit=False)
            db.session.commit()
            return jsonify({"error": "Código não confere",
                            "code": "wrong_code"}), 400

        # OK — consome OTP+challenge, emite JWTs
        otp.used_at = _utcnow()
        challenge.used_at = _utcnow()
        user.failed_login_attempts = 0
        user.locked_until = None
        user.last_login_at = _utcnow()
        audit_svc.log_event("mfa_challenge_success", user_id=user.id, commit=False)
        audit_svc.log_event("login_success", user_id=user.id, extra={"via": "mfa_sms"},
                              commit=False)
        db.session.commit()

        # SEC-1: seta cookie httpOnly + body com token (compat native).
        return _auth_response(user)
