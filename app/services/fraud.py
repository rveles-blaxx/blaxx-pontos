"""B14 — detecção simples de transações suspeitas (alertas para admin).

Regras (configuráveis em Config):
  * valor alto  — amount >= ALERT_HIGH_VALUE_PTS
  * velocidade  — >= ALERT_VELOCITY_COUNT envios na janela ALERT_VELOCITY_WINDOW_MIN
  * fan-out     — >= ALERT_DISTINCT_RECIPIENTS destinatários distintos em 1h

Ao detectar, grava um AuditLog (event='suspicious_transfer', status='warn')
DENTRO da mesma transação do envio (commit=False) — aparece em /admin/alerts.
Não bloqueia a operação (é alerta, não barreira).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from ..config import Config
from ..extensions import db
from ..models import Transfer
from . import audit as audit_svc


def evaluate_transfer(sender_id: str, recipient_id: str, amount_pts: int) -> list[str]:
    """Avalia regras e, se suspeito, loga alerta (commit=False). Retorna motivos."""
    reasons: list[str] = []

    if (amount_pts or 0) >= Config.ALERT_HIGH_VALUE_PTS:
        reasons.append(f"valor alto ({amount_pts} pts)")

    now = datetime.now(timezone.utc)
    win = now - timedelta(minutes=Config.ALERT_VELOCITY_WINDOW_MIN)
    hour = now - timedelta(hours=1)

    def _aware(dt):
        # SQLite devolve naive; Postgres pode devolver aware. Normaliza p/ UTC.
        if dt is None:
            return None
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)

    # Pega os últimos envios do remetente e filtra em Python (evita mismatch
    # de timezone na query entre SQLite e Postgres).
    recent = (
        db.session.query(Transfer)
        .filter(Transfer.sender_id == sender_id)
        .order_by(Transfer.created_at.desc())
        .limit(50)
        .all()
    )
    in_hour = [t for t in recent if _aware(t.created_at) and _aware(t.created_at) >= hour]
    in_win = [t for t in in_hour if _aware(t.created_at) >= win]

    # in_win já inclui a transferência atual (flush no caller).
    if len(in_win) >= Config.ALERT_VELOCITY_COUNT:
        reasons.append(f"velocidade ({len(in_win)} envios em {Config.ALERT_VELOCITY_WINDOW_MIN}min)")

    distinct = {t.recipient_id for t in in_hour}
    distinct.add(recipient_id)
    if len(distinct) >= Config.ALERT_DISTINCT_RECIPIENTS:
        reasons.append(f"muitos destinatários ({len(distinct)} em 1h)")

    if reasons:
        audit_svc.log_event(
            "suspicious_transfer", user_id=sender_id, status="warn",
            reason="; ".join(reasons),
            extra={"recipient_id": recipient_id, "amount_pts": amount_pts, "reasons": reasons},
            commit=False,
        )
    return reasons
