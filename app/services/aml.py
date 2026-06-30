"""AML/PLD básico — threshold, velocity, sanctions list (Sprint 4 / S4-AML).

Aplicado em:
  * transfer.send()  — antes do commit
  * redeem.request_redeem() — antes do debit
  * purchase.create_charge() — na criação da cobrança

Comportamento:
  * `is_sanctioned()` → BLOQUEIA (chama callsite levanta exceção)
  * `check_transaction_threshold()` / `check_velocity()` → REGISTRA alerta
    em aml_alerts mas NÃO bloqueia (review humano via /admin/aml/alerts)

Sanctions list: CSV em app/data/sanctions_list.csv (formato `name,cpf,reason`,
header obrigatório). Vazio = sem bloqueio. Carregado em memória no primeiro
uso, recarregado se o mtime mudar (admin atualiza arquivo sem reboot).
"""

from __future__ import annotations

import csv
import json
import logging
import os
import re
import threading
from datetime import datetime, timedelta, timezone
from typing import Any

from ..extensions import db
from ..models import AmlAlert, Transaction, TxType, Wallet


logger = logging.getLogger(__name__)


# ---------------------- Thresholds ---------------------- #
# Configuráveis via env var (override Config). Defaults conservadores fintech.
def _env_int(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, "") or default)
    except (TypeError, ValueError):
        return default


THRESHOLD_SINGLE_OP_PTS = _env_int("AML_THRESHOLD_SINGLE_OP_PTS", 30_000)
THRESHOLD_MONTHLY_PCT = float(os.environ.get("AML_THRESHOLD_MONTHLY_PCT", "0.80"))
VELOCITY_MAX_OPS_PER_HOUR = _env_int("AML_VELOCITY_MAX_OPS_PER_HOUR", 5)


# ---------------------- Sanctions list cache ---------------------- #

_sanctions_lock = threading.Lock()
_sanctions_cache: dict[str, dict] = {}
_sanctions_mtime: float = 0.0


def _sanctions_csv_path() -> str:
    return os.path.join(
        os.path.dirname(os.path.dirname(__file__)),
        "data", "sanctions_list.csv",
    )


def _load_sanctions() -> dict[str, dict]:
    """Carrega CSV em memória. Re-lê se mtime mudou. Thread-safe."""
    global _sanctions_cache, _sanctions_mtime
    path = _sanctions_csv_path()
    if not os.path.isfile(path):
        return {}
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return _sanctions_cache or {}

    if mtime == _sanctions_mtime and _sanctions_cache:
        return _sanctions_cache

    with _sanctions_lock:
        if mtime == _sanctions_mtime and _sanctions_cache:
            return _sanctions_cache
        cache: dict[str, dict] = {}
        try:
            with open(path, encoding="utf-8") as fh:
                reader = csv.DictReader(fh)
                for row in reader:
                    cpf = re.sub(r"\D", "", (row.get("cpf") or ""))[:11]
                    name = (row.get("name") or "").strip().lower()
                    if cpf:
                        cache[f"cpf:{cpf}"] = row
                    if name:
                        cache[f"name:{name}"] = row
        except Exception:
            logger.exception("Falha ao carregar sanctions_list.csv")
            return _sanctions_cache or {}
        _sanctions_cache = cache
        _sanctions_mtime = mtime
        return cache


def is_sanctioned(cpf: str | None = None, name: str | None = None) -> dict | None:
    """Retorna o registro se CPF ou nome estiver na lista; None caso contrário.

    Match por CPF (exato) OU por nome (lowercased, exato). Não usa fuzzy match
    pra evitar falsos positivos — sanctions list deve ser sempre dados precisos.
    """
    cache = _load_sanctions()
    if not cache:
        return None
    if cpf:
        cpf_norm = re.sub(r"\D", "", cpf)[:11]
        if cpf_norm and f"cpf:{cpf_norm}" in cache:
            return cache[f"cpf:{cpf_norm}"]
    if name:
        name_norm = name.strip().lower()
        if name_norm and f"name:{name_norm}" in cache:
            return cache[f"name:{name_norm}"]
    return None


# ---------------------- Alert helpers ---------------------- #

def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _create_alert(
    user_id: str | None,
    kind: str,
    severity: str,
    payload: dict[str, Any],
    *,
    commit: bool = False,
) -> AmlAlert:
    """Grava um alerta. Por padrão NÃO commita — caller controla TX."""
    alert = AmlAlert(
        user_id=user_id,
        kind=kind,
        severity=severity,
        payload=json.dumps(payload, default=str)[:2000],
    )
    db.session.add(alert)
    if commit:
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()
            logger.exception("falha ao commit AmlAlert")
    # Sprint 5 metric
    try:
        from .metrics import inc_aml_alert
        inc_aml_alert(kind, severity)
    except Exception:
        pass
    return alert


# ---------------------- Public checks ---------------------- #

class SanctionsBlock(Exception):
    """Levantada quando user está em sanctions list — caller deve retornar 403."""


def check_sanctions_or_raise(user) -> None:
    """Aborta operação se user (por cpf ou name) está em sanctions list.

    Em paralelo, registra alerta `kind='sanctions'`, severity='high'.
    """
    if user is None:
        return
    hit = is_sanctioned(cpf=user.cpf, name=user.name)
    if hit:
        _create_alert(
            user_id=user.id,
            kind="sanctions",
            severity="high",
            payload={
                "cpf_matched": hit.get("cpf"),
                "name_matched": hit.get("name"),
                "reason": hit.get("reason"),
            },
            commit=True,  # alerta vira independente — ação humana sempre
        )
        raise SanctionsBlock(
            "Operação bloqueada por verificação de conformidade. "
            "Entre em contato com o suporte."
        )


def check_transaction_threshold(
    user,
    amount_pts: int,
    kind: str,
    *,
    monthly_limit_pts: int | None = None,
) -> AmlAlert | None:
    """Registra alerta se transação isolada > THRESHOLD_SINGLE_OP_PTS OU
    se amount >= THRESHOLD_MONTHLY_PCT × monthly_limit_pts.

    `kind` é livre ('transfer', 'redeem', 'purchase') e vira o payload.kind_op.
    NÃO bloqueia — só registra pra review humano.
    """
    if not user or amount_pts <= 0:
        return None
    severity = None
    reasons = []
    if amount_pts >= THRESHOLD_SINGLE_OP_PTS:
        reasons.append(f"single_op>={THRESHOLD_SINGLE_OP_PTS}")
        severity = "medium"
    if monthly_limit_pts and monthly_limit_pts > 0:
        pct = amount_pts / monthly_limit_pts
        if pct >= THRESHOLD_MONTHLY_PCT:
            reasons.append(f"pct_monthly={pct:.2f}")
            severity = "high"
    if not reasons:
        return None
    return _create_alert(
        user_id=user.id,
        kind="threshold",
        severity=severity or "medium",
        payload={
            "amount_pts": amount_pts,
            "kind_op": kind,
            "reasons": reasons,
            "monthly_limit_pts": monthly_limit_pts,
        },
        commit=False,
    )


def check_velocity(user, *, tx_type: TxType | None = None) -> AmlAlert | None:
    """Conta operações do user na última hora. Alerta se >= VELOCITY_MAX_OPS_PER_HOUR.

    Conta SAÍDAS (débitos do ledger) — transfer_out, redeem, purchase é crédito
    mas conta separadamente. Default conta todos os tipos não-bonus.
    NÃO bloqueia.
    """
    if not user:
        return None
    cutoff = _utcnow() - timedelta(hours=1)
    q = (
        db.session.query(Transaction)
        .join(Wallet, Wallet.id == Transaction.wallet_id)
        .filter(Wallet.user_id == user.id, Transaction.created_at >= cutoff)
    )
    if tx_type:
        q = q.filter(Transaction.type == tx_type)
    else:
        # Excluir bonus/expire/refund (operações internas, não usuário-iniciadas)
        q = q.filter(Transaction.type.in_([
            TxType.TRANSFER_OUT, TxType.REDEEM, TxType.PURCHASE,
        ]))
    count = q.count()
    if count < VELOCITY_MAX_OPS_PER_HOUR:
        return None
    return _create_alert(
        user_id=user.id,
        kind="velocity",
        severity="medium",
        payload={
            "ops_last_hour": count,
            "threshold": VELOCITY_MAX_OPS_PER_HOUR,
            "tx_type": tx_type.value if tx_type else "any",
        },
        commit=False,
    )
