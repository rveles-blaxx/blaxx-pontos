"""Endpoints de saldo e extrato."""

from __future__ import annotations

from flask import Blueprint, g, jsonify, request

from ..extensions import db
from ..models import Transaction, Wallet
from .auth import login_required

bp = Blueprint("wallet", __name__)


@bp.get("/")
@login_required
def get_wallet():
    wallet = db.session.query(Wallet).filter_by(user_id=g.current_user.id).one()
    return jsonify(wallet.to_dict())


@bp.get("/transactions")
@login_required
def list_transactions():
    limit = min(int(request.args.get("limit", 20)), 100)
    wallet = db.session.query(Wallet).filter_by(user_id=g.current_user.id).one()
    txs = (
        db.session.query(Transaction)
        .filter_by(wallet_id=wallet.id)
        .order_by(Transaction.created_at.desc())
        .limit(limit)
        .all()
    )
    return jsonify({"items": [t.to_dict() for t in txs]})
