"""Módulo Administrador.

Endpoints (todos exigem login + role='admin'):

  GET   /admin/users                  → paginado, busca por nome/email/cpf
  GET   /admin/users/<id>             → detalhe + perfil + métricas
  PATCH /admin/users/<id>/vip         → toggle is_vip
  PATCH /admin/users/<id>/role        → promove/rebaixa admin (cuidado)
  GET   /admin/transactions           → todas as transações do sistema
  GET   /admin/transactions/<user_id> → transações de um user específico
  GET   /admin/stats                  → totais agregados (users, balance, vol PIX)

Segurança:
  - Acesso restrito por role='admin'
  - Toda ação é logada (futuro audit_logs)
  - PATCH /role retorna 403 se admin tentar rebaixar a si mesmo (evita lock-out)
"""

from __future__ import annotations

from functools import wraps

from flask import Blueprint, g, jsonify, request
from sqlalchemy import or_, func, select

from ..extensions import db, limiter
from ..models import Transaction, TxType, User, Wallet
from .auth import login_required

bp = Blueprint("admin", __name__)


# ─────────────────────────── decorator ─────────────────────────── #

def admin_required(fn):
    """Garante que o usuário autenticado tem role='admin'.

    Use SEMPRE depois de @login_required.
    Retorna 403 com mensagem genérica (não revela que o endpoint existe).
    """
    @wraps(fn)
    def wrapper(*args, **kwargs):
        u = getattr(g, "current_user", None)
        if u is None or u.role != "admin":
            return jsonify({"error": "acesso restrito"}), 403
        return fn(*args, **kwargs)
    return wrapper


# ─────────────────────────── /users ─────────────────────────── #

@bp.get("/users")
@login_required
@admin_required
def list_users():
    """Lista usuários paginados. Aceita ?q=, ?limit=, ?offset=, ?role=, ?vip=true."""
    q = (request.args.get("q") or "").strip().lower()
    limit = min(int(request.args.get("limit", 50)), 200)
    offset = max(int(request.args.get("offset", 0)), 0)
    role_filter = request.args.get("role")
    vip_filter = request.args.get("vip")

    stmt = select(User)
    if q:
        like = f"%{q}%"
        stmt = stmt.where(
            or_(User.name.ilike(like), User.email.ilike(like), User.cpf.ilike(like))
        )
    if role_filter in ("user", "admin"):
        stmt = stmt.where(User.role == role_filter)
    if vip_filter == "true":
        stmt = stmt.where(User.is_vip.is_(True))
    elif vip_filter == "false":
        stmt = stmt.where(User.is_vip.is_(False))

    total = db.session.execute(
        select(func.count()).select_from(stmt.subquery())
    ).scalar_one()

    stmt = stmt.order_by(User.created_at.desc()).limit(limit).offset(offset)
    users = db.session.execute(stmt).scalars().all()

    return jsonify({
        "total": total,
        "limit": limit,
        "offset": offset,
        "items": [u.to_admin_dict() for u in users],
    })


@bp.get("/users/<user_id>")
@login_required
@admin_required
def get_user(user_id: str):
    u = db.session.get(User, user_id)
    if u is None:
        return jsonify({"error": "usuário não encontrado"}), 404
    data = u.to_admin_dict()
    # Adiciona últimas 20 transações pra contexto
    if u.wallet:
        recent = (
            db.session.query(Transaction)
            .filter_by(wallet_id=u.wallet.id)
            .order_by(Transaction.created_at.desc())
            .limit(20)
            .all()
        )
        data["recent_transactions"] = [t.to_dict() for t in recent]
    return jsonify(data)


@bp.patch("/users/<user_id>/vip")
@login_required
@admin_required
def set_vip(user_id: str):
    """Toggle ou seta is_vip. Body opcional: {"is_vip": true/false} (default toggle)."""
    u = db.session.get(User, user_id)
    if u is None:
        return jsonify({"error": "usuário não encontrado"}), 404
    body = request.get_json(silent=True) or {}
    if "is_vip" in body:
        u.is_vip = bool(body["is_vip"])
    else:
        u.is_vip = not u.is_vip
    db.session.commit()
    return jsonify({"id": u.id, "is_vip": u.is_vip})


@bp.patch("/users/<user_id>/role")
@login_required
@admin_required
def set_role(user_id: str):
    """Promove/rebaixa role. Body: {"role": "admin"|"user"}.
    Bloqueia o admin de rebaixar a si mesmo (proteção contra lock-out)."""
    body = request.get_json(silent=True) or {}
    new_role = (body.get("role") or "").lower()
    if new_role not in ("user", "admin"):
        return jsonify({"error": "role inválido (use 'user' ou 'admin')"}), 400
    u = db.session.get(User, user_id)
    if u is None:
        return jsonify({"error": "usuário não encontrado"}), 404
    if u.id == g.current_user.id and new_role != "admin":
        return jsonify({"error": "você não pode remover seu próprio acesso admin"}), 403
    u.role = new_role
    db.session.commit()
    return jsonify({"id": u.id, "role": u.role})


# ─────────────────────────── /transactions ─────────────────────────── #

@bp.get("/transactions")
@login_required
@admin_required
def all_transactions():
    """Todas as transações do sistema, paginadas. Filtros: ?type=, ?user_id=."""
    limit = min(int(request.args.get("limit", 100)), 500)
    offset = max(int(request.args.get("offset", 0)), 0)
    tx_type = request.args.get("type")
    user_id = request.args.get("user_id")

    stmt = select(Transaction).join(Wallet, Wallet.id == Transaction.wallet_id)
    if tx_type:
        try:
            stmt = stmt.where(Transaction.type == TxType(tx_type))
        except ValueError:
            return jsonify({"error": f"type inválido: {tx_type}"}), 400
    if user_id:
        stmt = stmt.where(Wallet.user_id == user_id)

    total = db.session.execute(
        select(func.count()).select_from(stmt.subquery())
    ).scalar_one()
    stmt = stmt.order_by(Transaction.created_at.desc()).limit(limit).offset(offset)
    txs = db.session.execute(stmt).scalars().all()

    # Enriquece com nome do user
    items = []
    for t in txs:
        d = t.to_dict()
        w = db.session.get(Wallet, t.wallet_id)
        if w:
            u = db.session.get(User, w.user_id)
            d["user_name"] = u.name if u else "—"
            d["user_email"] = u.email if u else "—"
        items.append(d)

    return jsonify({"total": total, "limit": limit, "offset": offset, "items": items})


# ─────────────────────────── /stats ─────────────────────────── #

@bp.get("/stats")
@login_required
@admin_required
def stats():
    """Totais agregados do sistema. Útil pra dashboard do admin."""
    total_users = db.session.execute(
        select(func.count()).select_from(User)
    ).scalar_one()
    total_admins = db.session.execute(
        select(func.count()).select_from(User).where(User.role == "admin")
    ).scalar_one()
    total_vips = db.session.execute(
        select(func.count()).select_from(User).where(User.is_vip.is_(True))
    ).scalar_one()
    verified = db.session.execute(
        select(func.count()).select_from(User).where(User.email_verified_at.is_not(None))
    ).scalar_one()

    # Saldo total em pontos no sistema
    total_balance = db.session.execute(
        select(func.coalesce(func.sum(Wallet.balance_pts), 0))
    ).scalar_one() or 0

    # Volume por tipo de transação (últimos 30 dias)
    from datetime import datetime, timedelta, timezone
    cutoff = datetime.now(timezone.utc) - timedelta(days=30)
    vol_by_type = {}
    for tx_type in TxType:
        v = db.session.execute(
            select(func.coalesce(func.sum(func.abs(Transaction.amount_pts)), 0))
            .where(Transaction.type == tx_type)
            .where(Transaction.created_at >= cutoff)
        ).scalar_one() or 0
        vol_by_type[tx_type.value] = int(v)

    # Pagamentos PIX pendentes de confirmação manual
    from ..models import PixCharge, PixChargeStatus
    pending_payments = db.session.execute(
        select(func.count()).select_from(PixCharge).where(
            PixCharge.status == PixChargeStatus.PENDING_CONFIRMATION
        )
    ).scalar_one() or 0

    return jsonify({
        "total_users": total_users,
        "total_admins": total_admins,
        "total_vips": total_vips,
        "email_verified_users": verified,
        "total_balance_pts": int(total_balance),
        "volume_last_30d_by_type": vol_by_type,
        "pending_payments": int(pending_payments),
    })


# ─────────────────────── PIX manual · confirmação ─────────────────────── #

@bp.get("/charges/pending")
@login_required
@admin_required
def list_pending_charges():
    """Lista charges aguardando admin confirmar (PENDING_CONFIRMATION).

    Sprint 4 (S4-7): JOIN unico com User (era N+1 — 1 query base + N
    db.session.get(User) no loop). Pra 200 rows isso era 201 queries.
    """
    from ..models import PixCharge, PixChargeStatus
    rows = (
        db.session.query(PixCharge, User)
        .join(User, PixCharge.user_id == User.id)
        .filter(PixCharge.status == PixChargeStatus.PENDING_CONFIRMATION)
        .order_by(PixCharge.claimed_paid_at.asc())
        .limit(200)
        .all()
    )
    items = []
    for c, u in rows:
        d = c.to_dict()
        d["user_name"] = u.name if u else "—"
        d["user_email"] = u.email if u else "—"
        items.append(d)
    return jsonify({"items": items, "total": len(items)})


@bp.post("/charges/<charge_id>/confirm")
@login_required
@admin_required
def admin_confirm_charge(charge_id: str):
    """Admin confirma o recebimento do PIX → libera os pontos."""
    from datetime import datetime, timezone
    from ..models import PixCharge, PixChargeStatus, TxType, Notification
    from ..services import wallet as wallet_svc

    charge = db.session.get(PixCharge, charge_id)
    if charge is None:
        return jsonify({"error": "charge não encontrada"}), 404
    if charge.status not in (PixChargeStatus.PENDING_CONFIRMATION,
                              PixChargeStatus.PENDING):
        return jsonify({"error": f"charge não pode ser confirmada (status atual: {charge.status.value})"}), 400

    charge.status = PixChargeStatus.PAID
    charge.paid_at = datetime.now(timezone.utc)
    charge.confirmed_by_user_id = g.current_user.id

    wallet_svc.credit(
        user_id=charge.user_id,
        amount_pts=charge.points_to_credit,
        tx_type=TxType.PURCHASE,
        description=f"Compra de pontos PIX (manual) — R$ {charge.amount_cents/100:.2f}",
        reference=charge.id,
        idempotency_key=f"charge:{charge.id}",
    )

    # Notifica o cliente
    db.session.add(Notification(
        user_id=charge.user_id, type="system",
        title="Pontos liberados!",
        body=f"Recebemos seu PIX de R$ {charge.amount_cents/100:.2f} · "
             f"{charge.points_to_credit} pts creditados.",
        icon="✓",
        reference=charge.id,
    ))
    db.session.commit()
    return jsonify({"ok": True, "charge": charge.to_dict()})


@bp.post("/charges/<charge_id>/reject")
@login_required
@admin_required
def admin_reject_charge(charge_id: str):
    """Admin rejeita (não recebeu o PIX)."""
    from ..models import PixCharge, PixChargeStatus, Notification

    data = request.get_json(silent=True) or {}
    reason = (data.get("reason") or "PIX não recebido").strip()

    charge = db.session.get(PixCharge, charge_id)
    if charge is None:
        return jsonify({"error": "charge não encontrada"}), 404
    if charge.status not in (PixChargeStatus.PENDING_CONFIRMATION,
                              PixChargeStatus.PENDING):
        return jsonify({"error": "charge já está em status final"}), 400

    charge.status = PixChargeStatus.REJECTED
    charge.confirmed_by_user_id = g.current_user.id

    db.session.add(Notification(
        user_id=charge.user_id, type="system",
        title="Pagamento PIX não confirmado",
        body=f"Sua compra de R$ {charge.amount_cents/100:.2f} não foi creditada: {reason}. "
             f"Se você realmente pagou, mostre o comprovante no suporte.",
        icon="⚠",
        reference=charge.id,
    ))
    db.session.commit()
    return jsonify({"ok": True, "status": charge.status.value, "reason": reason})


# =========================================================================
# Sprint 2 · Expiracao de pontos (cron mensal disparado por endpoint admin)
# =========================================================================

@bp.post("/expire-points")
@admin_required
def admin_expire_points():
    """Dispara varredura de expiracao de pontos > 24 meses.

    Body opcional:
        { "dry_run": true }  # so calcula, nao commita

    Para uso em cron mensal: configure um Render Cron Service ou GitHub
    Actions chamando POST /admin/expire-points com Authorization Bearer
    de um admin tecnico (idealmente um service account dedicado).

    Retorna estatisticas + lista de erros (se houver).
    """
    from ..services.expiration import expire_old_points_all
    data = request.get_json(silent=True) or {}
    dry = bool(data.get("dry_run"))
    result = expire_old_points_all(dry_run=dry)
    return jsonify(result)


# =========================================================================
# Sprint 5 (S5-6) · A/B testing
# =========================================================================

@bp.get("/experiments")
@login_required
@admin_required
def list_experiments():
    """Lista experimentos registrados."""
    from ..services.experiments import list_active
    return jsonify({"items": list_active()})


# ───────────────── Moderação: bloquear / desbloquear usuário ───────────────── #

@bp.patch("/users/<user_id>/status")
@login_required
@admin_required
def set_user_status(user_id: str):
    """Bloqueia (suspended) ou reativa (active) um usuário.

    Body: {"status": "active"|"suspended", "reason": "..."}
    Usuário suspenso é barrado no /auth/login (status != active → 403).
    """
    from ..services import audit as audit_svc
    body = request.get_json(silent=True) or {}
    new_status = (body.get("status") or "").lower().strip()
    if new_status not in ("active", "suspended"):
        return jsonify({"error": "status inválido (use 'active' ou 'suspended')"}), 400
    u = db.session.get(User, user_id)
    if u is None:
        return jsonify({"error": "usuário não encontrado"}), 404
    if u.id == g.current_user.id and new_status != "active":
        return jsonify({"error": "você não pode suspender a si mesmo"}), 403
    u.status = new_status
    audit_svc.log_event(
        "admin_user_status", user_id=g.current_user.id, status="ok",
        reason=(body.get("reason") or None),
        extra={"target_user": u.id, "new_status": new_status}, commit=False,
    )
    db.session.commit()
    return jsonify({"id": u.id, "status": u.status})


# ───────────────── Estorno de transferência P2P ───────────────── #

@bp.post("/transfers/<transfer_id>/reverse")
@login_required
@admin_required
def reverse_transfer(transfer_id: str):
    """Estorna uma transferência P2P: debita o destinatário e devolve ao remetente.

    Body: {"reason": "justificativa obrigatória"}.
    Idempotente (não estorna duas vezes). Atômico. Auditado.
    """
    from ..models import Transfer, Transaction, Notification, TxType
    from ..services import wallet as wallet_svc, audit as audit_svc

    body = request.get_json(silent=True) or {}
    reason = (body.get("reason") or "").strip()
    if len(reason) < 5:
        return jsonify({"error": "justificativa obrigatória (mín. 5 caracteres)"}), 400

    t = db.session.get(Transfer, transfer_id)
    if t is None:
        return jsonify({"error": "transferência não encontrada"}), 404

    out_key = f"transfer-reverse-out:{t.id}"   # débito do destinatário
    in_key = f"transfer-reverse-in:{t.id}"     # crédito de volta ao remetente

    # Idempotência: se já há crédito de estorno para o remetente, devolve.
    already = (
        db.session.query(Transaction)
        .join(Wallet, Wallet.id == Transaction.wallet_id)
        .filter(Wallet.user_id == t.sender_id, Transaction.idempotency_key == in_key)
        .one_or_none()
    )
    if already is not None:
        return jsonify({"ok": True, "already_reversed": True, "transfer_id": t.id}), 200

    try:
        wallet_svc.debit(
            user_id=t.recipient_id, amount_pts=t.amount_pts, tx_type=TxType.REFUND,
            description=f"Estorno da transferência {t.receipt_code}",
            reference=t.id, idempotency_key=out_key,
        )
        wallet_svc.credit(
            user_id=t.sender_id, amount_pts=t.amount_pts, tx_type=TxType.REFUND,
            description=f"Estorno recebido — {t.receipt_code}",
            reference=t.id, idempotency_key=in_key,
        )
        db.session.add(Notification(
            user_id=t.recipient_id, type="system", title="Transferência estornada",
            body=f"{t.amount_pts} pts foram estornados. Motivo: {reason}",
            icon="↩", reference=t.id,
        ))
        audit_svc.log_event(
            "admin_transfer_reverse", user_id=g.current_user.id, status="ok", reason=reason,
            extra={"transfer_id": t.id, "amount_pts": t.amount_pts,
                   "sender_id": t.sender_id, "recipient_id": t.recipient_id}, commit=False,
        )
    except wallet_svc.InsufficientBalance:
        db.session.rollback()
        return jsonify({"error": "destinatário não tem saldo suficiente para o estorno"}), 409

    db.session.commit()
    return jsonify({"ok": True, "reversed": True, "transfer_id": t.id, "amount_pts": t.amount_pts}), 200


# ───────────────── Exportação CSV (transações) ───────────────── #

@bp.get("/export/transactions.csv")
@login_required
@admin_required
def export_transactions_csv():
    """Exporta as transações do sistema em CSV (últimas 5000)."""
    import csv
    import io
    from flask import Response

    rows = (
        db.session.query(Transaction, User)
        .join(Wallet, Wallet.id == Transaction.wallet_id)
        .join(User, User.id == Wallet.user_id)
        .order_by(Transaction.created_at.desc())
        .limit(5000)
        .all()
    )
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["created_at", "user_email", "type", "status", "amount_pts",
                     "description", "reference", "tx_id"])
    for tx, u in rows:
        writer.writerow([
            tx.created_at.isoformat() if tx.created_at else "",
            u.email,
            tx.type.value if hasattr(tx.type, "value") else tx.type,
            tx.status.value if hasattr(tx.status, "value") else tx.status,
            tx.amount_pts,
            (tx.description or "").replace("\n", " "),
            tx.reference or "",
            tx.id,
        ])
    return Response(
        buf.getvalue(), mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=blaxx-transactions.csv"},
    )


# ───────────────── Alertas de transações suspeitas (B14) ───────────────── #

@bp.get("/alerts")
@login_required
@admin_required
def list_alerts():
    """Lista alertas de segurança/fraude (eventos de auditoria nível 'warn').

    Inclui: suspicious_transfer (B14), login_blocked_by_ip, account_locked, etc.
    """
    import json as _json
    from ..models import AuditLog
    try:
        limit = min(int(request.args.get("limit", 100) or 100), 500)
    except (TypeError, ValueError):
        limit = 100
    rows = (
        db.session.query(AuditLog)
        .filter(AuditLog.status == "warn")
        .order_by(AuditLog.created_at.desc())
        .limit(limit)
        .all()
    )
    items = []
    for a in rows:
        extra = None
        if a.extra_data:
            try:
                extra = _json.loads(a.extra_data)
            except Exception:
                extra = a.extra_data
        items.append({
            "id": a.id,
            "event": a.event,
            "status": a.status,
            "reason": a.reason,
            "user_id": a.user_id,
            "ip": a.ip,
            "created_at": a.created_at.isoformat() if a.created_at else None,
            "extra": extra,
        })
    return jsonify({"items": items, "count": len(items)})

# ============================================================================
# Sprint 4 (S4-AML) · AML alerts review
# ============================================================================

@bp.get("/aml/alerts")
@login_required
@admin_required
def list_aml_alerts():
    """Lista AmlAlerts paginados. Filtros: ?severity=, ?kind=, ?resolved=true|false."""
    from ..models import AmlAlert

    try:
        limit = min(int(request.args.get("limit", 50) or 50), 200)
        offset = max(int(request.args.get("offset", 0) or 0), 0)
    except (TypeError, ValueError):
        limit, offset = 50, 0

    severity = (request.args.get("severity") or "").strip().lower() or None
    kind = (request.args.get("kind") or "").strip().lower() or None
    resolved = (request.args.get("resolved") or "").strip().lower()

    stmt = select(AmlAlert)
    if severity in ("low", "medium", "high"):
        stmt = stmt.where(AmlAlert.severity == severity)
    if kind:
        stmt = stmt.where(AmlAlert.kind == kind)
    if resolved == "true":
        stmt = stmt.where(AmlAlert.resolved_at.is_not(None))
    elif resolved == "false":
        stmt = stmt.where(AmlAlert.resolved_at.is_(None))

    total = db.session.execute(
        select(func.count()).select_from(stmt.subquery())
    ).scalar_one()
    stmt = stmt.order_by(AmlAlert.created_at.desc()).limit(limit).offset(offset)
    rows = db.session.execute(stmt).scalars().all()

    return jsonify({
        "total": total, "limit": limit, "offset": offset,
        "items": [a.to_dict() for a in rows],
    })


@bp.post("/aml/alerts/<alert_id>/resolve")
@login_required
@admin_required
def resolve_aml_alert(alert_id: str):
    """Marca alerta como resolvido. Body: { \"note\": \"...\" }"""
    from datetime import datetime, timezone
    from ..models import AmlAlert

    alert = db.session.get(AmlAlert, alert_id)
    if alert is None:
        return jsonify({"error": "alert não encontrado"}), 404
    if alert.resolved_at is not None:
        return jsonify({"error": "já resolvido", "resolved_at": alert.resolved_at.isoformat()}), 400
    data = request.get_json(silent=True) or {}
    note = (data.get("note") or "").strip()[:500] or None
    alert.resolved_at = datetime.now(timezone.utc)
    alert.resolved_by = g.current_user.id
    alert.resolution_note = note
    db.session.commit()
    return jsonify(alert.to_dict())

