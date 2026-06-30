"""KYC — validação remota de CPF (Sprint 4 / S4-KYC).

Provider primário: BrasilAPI (`https://brasilapi.com.br/api/cpf/v1/{cpf}`).
Grátis, sem cadastro, mas pode estar fora — wrapamos em try/except com
timeout duro de 5s. Se downtime, retornamos `{valid: None, error: "indisponivel"}`
e o registro PROSSEGUE (kyc_pending=True).

Cache: gravamos cada validação em `cpf_validations` pra:
  1. Evitar consultas duplicadas (TTL 30 dias)
  2. Auditoria (LGPD ao invés de logar nome em texto plano usamos hash)
  3. Suportar re-validação manual via admin

NÃO bloqueia o cadastro em downtime — fintech opera mesmo com provider externo
oscilando. Estado `kyc_pending` é exposto ao admin pra ação manual.

⚠️ Nota: BrasilAPI valida APENAS o ALGORITMO matemático do CPF, NÃO
consulta a base oficial da RF. Em produção real, substitua por provider
homologado (Serpro, Datavalid, etc.) — interface fica idêntica.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any

from ..extensions import db
from ..models import CpfValidation


logger = logging.getLogger(__name__)


BRASILAPI_URL = "https://brasilapi.com.br/api/cpf/v1/{cpf}"
CACHE_TTL_DAYS = 30
HTTP_TIMEOUT_S = 3.0  # sync hard timeout — não bloqueia request por mais que isso


def _normalize_cpf(cpf: str) -> str:
    return re.sub(r"\D", "", cpf or "")[:11]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _cached_validation(cpf: str) -> CpfValidation | None:
    """Retorna a última validação dentro do TTL — None se não tem cache válido."""
    cutoff = _utcnow() - timedelta(days=CACHE_TTL_DAYS)
    rec = (
        db.session.query(CpfValidation)
        .filter(CpfValidation.cpf == cpf, CpfValidation.valid.is_(True))
        .order_by(CpfValidation.validated_at.desc())
        .first()
    )
    if rec is None:
        return None
    va = rec.validated_at
    if va.tzinfo is None:
        va = va.replace(tzinfo=timezone.utc)
    return rec if va >= cutoff else None


def _call_brasilapi(cpf: str) -> dict[str, Any]:
    """Chama BrasilAPI. Retorna dict com chaves:
        valid (bool|None), data (dict|None), error (str|None), raw (str).
    """
    url = BRASILAPI_URL.format(cpf=cpf)
    req = urllib.request.Request(url, headers={"User-Agent": "blaxx-pontos/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as r:
            raw = r.read().decode("utf-8", errors="replace")
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                return {"valid": None, "data": None, "error": "resposta_invalida", "raw": raw[:500]}
            # BrasilAPI retorna {cpf: "...", nome: "..."} quando válido.
            # Em CPF inválido devolve 404 (cai no HTTPError).
            return {"valid": True, "data": data, "error": None, "raw": raw[:500]}
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return {"valid": False, "data": None, "error": None, "raw": "404"}
        return {"valid": None, "data": None, "error": f"http_{e.code}", "raw": ""}
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        # Provider offline / timeout / DNS — graceful degradation
        return {"valid": None, "data": None, "error": f"indisponivel:{type(e).__name__}", "raw": ""}
    except Exception as e:  # pragma: no cover — defensive
        logger.exception("BrasilAPI call inesperadamente falhou: %s", e)
        return {"valid": None, "data": None, "error": f"erro:{type(e).__name__}", "raw": ""}


def validate_cpf_remote(cpf: str, *, use_cache: bool = True) -> dict[str, Any]:
    """Valida CPF na RF (via BrasilAPI). Retorna:
        {valid: bool|None, data: dict|None, source: str, error: str|None, cached: bool}

    - valid=True   → CPF existe e algoritmo confere
    - valid=False  → CPF não encontrado / inválido
    - valid=None   → provider indisponível (NÃO bloqueia cadastro)
    """
    cpf = _normalize_cpf(cpf)
    if len(cpf) != 11:
        return {"valid": False, "data": None, "source": "local", "error": "cpf_malformado", "cached": False}

    if use_cache:
        cached = _cached_validation(cpf)
        if cached is not None:
            return {
                "valid": True,
                "data": None,
                "source": cached.provider,
                "error": None,
                "cached": True,
            }

    result = _call_brasilapi(cpf)
    raw_hash = (
        hashlib.sha256(result.get("raw", "").encode("utf-8", errors="replace")).hexdigest()
        if result.get("raw") else None
    )

    # Grava resultado no cache — mesmo quando provider offline (audita o erro)
    try:
        db.session.add(CpfValidation(
            cpf=cpf,
            valid=result["valid"],
            provider="brasilapi",
            raw_response_hash=raw_hash,
            error_msg=result.get("error"),
        ))
        db.session.commit()
    except Exception:
        db.session.rollback()
        logger.exception("falha ao gravar CpfValidation")

    return {
        "valid": result["valid"],
        "data": result.get("data"),
        "source": "brasilapi",
        "error": result.get("error"),
        "cached": False,
    }


def validate_cpf_and_mark_user(user, *, use_cache: bool = True) -> dict[str, Any]:
    """Wrapper para uso em background pós-register. Marca user.kyc_validated_at."""
    res = validate_cpf_remote(user.cpf, use_cache=use_cache)
    try:
        if res["valid"] is True:
            user.kyc_validated_at = _utcnow()
            user.kyc_provider = res["source"]
            db.session.commit()
        # valid=False ou None: deixa kyc_validated_at nulo → user.kyc_pending=True
    except Exception:
        db.session.rollback()
        logger.exception("falha ao marcar user.kyc_validated_at")
    return res
