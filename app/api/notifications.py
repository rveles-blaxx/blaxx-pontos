"""Endpoints de notificações in-app."""

from __future__ import annotations

from datetime import datetime, timezone

from flask import Blueprint, g, jsonify

from ..extensions import db
from ..models import Notification
from .auth import login_required

bp = Blueprint("notifications", __name__)


@bp.get("/")
@login_required
def list_notifications():
    items = (
        db.session.query(Notification)
        .filter_by(user_id=g.current_user.id)
        .order_by(Notification.created_at.desc())
        .limit(100)
        .all()
    )
    unread = sum(1 for n in items if n.read_at is None)
    return jsonify({
        "items": [n.to_dict() for n in items],
        "unread_count": unread,
    })


@bp.get("/unread-count")
@login_required
def unread_count():
    count = (
        db.session.query(Notification)
        .filter_by(user_id=g.current_user.id, read_at=None)
        .count()
    )
    return jsonify({"count": count})


@bp.patch("/<notif_id>/read")
@login_required
def mark_read(notif_id: str):
    n = db.session.get(Notification, notif_id)
    if n is None or n.user_id != g.current_user.id:
        return jsonify({"error": "Notificação não encontrada"}), 404
    if n.read_at is None:
        n.read_at = datetime.now(timezone.utc)
        db.session.commit()
    return jsonify(n.to_dict())


@bp.post("/read-all")
@login_required
def read_all():
    now = datetime.now(timezone.utc)
    updated = (
        db.session.query(Notification)
        .filter_by(user_id=g.current_user.id, read_at=None)
        .update({Notification.read_at: now})
    )
    db.session.commit()
    return jsonify({"updated": updated})
