"""Endpoints de resgate via PIX (cashback)."""

from __future__ import annotations

import threading
import time
from collections import OrderedDict

from flask import Blueprint, g, jsonify, request

from ..extensions import db, limiter
from ..models import PixPayout
from ..security import MfaStepUpRequired
from ..services import redeem as redeem_svc
from .auth import login_required, email_verified_required

bp = Blueprint("redeem", __name__)

# ---------------------------------------------------------------------------
# Idempotency-Key (instância-local, TTL 10min)
# ---------------------------------------------------------------------------
# transfer.py já persiste idem_key na coluna `idempotency_key` de transactions.
# Aqui (resgate PIX) o modelo PixPayout não tem essa coluna ainda — pra evitar
# migração no caminho do lançamento, mantemos um cache em memória por instância.
# É suficiente pro caso de uso real (retry de cliente em segundos) e elimina o
# débito duplicado em janela curta. Quando escalar para múltiplas instâncias
# Render Pro, migrar pra Redis SETNX ou adicionar coluna idempotency_key em
# pix_payouts (com unique index por user_id+idempotency_key).
_IDEM_TTL_SECONDS = 10 * 60          # 10 min — janela típica de retry humano/cliente
_IDEM_MAX_ENTRIES = 4_096            # LRU cap pra não vazar memória
_idem_lock = threading.Lock()
_idem_cache: "OrderedDict[str, tuple[float, int, dict]]" = OrderedDict()


def _idem_key_for_user(user_id: str, key: str) -> str:
    # Escopa por usuário pra evitar colisão de keys entre clientes diferentes.
    return f"{user_id}:{key}"


def _idem_lookup(scoped_key: str) -> tuple[int, dict] | None:
    """Retorna (status, body) se houver entrada válida; senão None."""
    now = time.time()
    with _idem_lock:
        entry = _idem_cache.get(scoped_key)
        if entry is None:
            return None
        expires_at, status, body = entry
        if expires_at < now:
            _idem_cache.pop(scoped_key, None)
            return None
        # LRU touch
        _idem_cache.move_to_end(scoped_key)
        return status, body


def _idem_store(scoped_key: str, status: int, body: dict) -> None:
    with _idem_lock:
        _idem_cache[scoped_key] = (time.time() + _IDEM_TTL_SECONDS, status, body)
        _idem_cache.move_to_end(scoped_key)
        # Evita crescimento ilimitado (FIFO pelo TTL + LRU)
        while len(_idem_cache) > _IDEM_MAX_ENTRIES:
            _idem_cache.popitem(last=False)


@bp.get("/quote")
@login_required
def quote():
    try:
        points = int(request.args.get("points") or 0)
    except (TypeError, ValueError):
        return jsonify({"error": "points deve ser inteiro"}), 400
    try:
        return jsonify(redeem_svc.quote(points))
    except redeem_svc.RedeemError as exc:
        return jsonify({"error": str(exc)}), 400


@bp.post("/")
@login_required
@limiter.limit("10 per hour", key_func=lambda: g.current_user.id if hasattr(g, "current_user") else "anon")
@email_verified_required
def request_redeem():
    """POST /redeem
    {
      "points": 5000,
      "pix_key": "ricardo.veles@gmail.com",
      "password": "..."
    }

    Header opcional: `Idempotency-Key: <uuid v4 ou hash do request>`. Se enviado,
    retries com a mesma key dentro de 10min retornam a MESMA resposta sem debitar
    pontos de novo. Cliente deve gerar UUID na 1ª tentativa e repetir nos retries.
    """
    data = request.get_json(silent=True) or {}

    # 1) Idempotency lookup (rápido, antes de qualquer trabalho).
    idem_raw = (request.headers.get("Idempotency-Key") or data.get("request_id") or "").strip()
    scoped = None
    if idem_raw:
        # Limita tamanho pra não aceitar payloads gigantes como key.
        if len(idem_raw) > 64:
            return jsonify({"error": "Idempotency-Key máx 64 chars"}), 400
        scoped = _idem_key_for_user(g.current_user.id, idem_raw)
        hit = _idem_lookup(scoped)
        if hit is not None:
            status, body = hit
            return jsonify(body), status

    try:
        points = int(data.get("points") or 0)
    except (TypeError, ValueError):
        return jsonify({"error": "points deve ser inteiro"}), 400

    try:
        payout = redeem_svc.request_redeem(
            g.current_user,
            points=points,
            pix_key=(data.get("pix_key") or "").strip(),
            password=data.get("password") or "",
            mfa_code=data.get("mfa_code"),
        )
    except MfaStepUpRequired as exc:
        return jsonify({"error": str(exc), "mfa_required": True}), 401
    except redeem_svc.RedeemError as exc:
        return jsonify({"error": str(exc)}), 400

    body = payout.to_dict()
    # 2) Armazena pra próximo retry com a mesma key cair no lookup acima.
    if scoped is not None:
        _idem_store(scoped, 201, body)
    return jsonify(body), 201


@bp.get("/<payout_id>")
@login_required
def get_payout(payout_id: str):
    payout = db.session.get(PixPayout, payout_id)
    if payout is None or payout.user_id != g.current_user.id:
        return jsonify({"error": "not found"}), 404
    return jsonify(payout.to_dict())
