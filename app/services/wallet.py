"""Operações de saldo / ledger.

Toda mudança de saldo passa por aqui. Garantias:
  * Saldo nunca fica negativo (lock + check + CHECK constraint no banco).
  * Cada movimentação gera 1 Transaction (ledger imutável).
  * Idempotência opcional via `idempotency_key`.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from ..extensions import db
from ..models import Transaction, TxStatus, TxType, Wallet


class InsufficientBalance(Exception):
    """Saldo insuficiente para a operação."""


class DuplicateOperation(Exception):
    """Operação já foi processada (mesma idempotency_key)."""


def get_wallet_for_update(user_id: str) -> Wallet:
    """Busca a wallet usando lock pessimista (SELECT ... FOR UPDATE).

    SQLite ignora o `with_for_update`, mas o código fica correto para Postgres.
    """
    stmt = select(Wallet).where(Wallet.user_id == user_id).with_for_update()
    wallet = db.session.execute(stmt).scalar_one_or_none()
    if wallet is None:
        raise LookupError(f"wallet not found for user {user_id}")
    return wallet


def _check_idempotent(wallet_id: str, key: str | None) -> Transaction | None:
    if not key:
        return None
    stmt = select(Transaction).where(
        Transaction.wallet_id == wallet_id,
        Transaction.idempotency_key == key,
    )
    return db.session.execute(stmt).scalar_one_or_none()


def credit(
    user_id: str,
    *,
    amount_pts: int,
    tx_type: TxType,
    description: str = "",
    reference: str | None = None,
    idempotency_key: str | None = None,
) -> Transaction:
    if amount_pts <= 0:
        raise ValueError("amount_pts must be positive")

    wallet = get_wallet_for_update(user_id)

    existing = _check_idempotent(wallet.id, idempotency_key)
    if existing is not None:
        return existing

    wallet.balance_pts += amount_pts
    tx = Transaction(
        wallet_id=wallet.id,
        type=tx_type,
        status=TxStatus.CONFIRMED,
        amount_pts=amount_pts,
        description=description,
        reference=reference,
        idempotency_key=idempotency_key,
    )
    db.session.add(tx)
    db.session.flush()
    return tx


def debit(
    user_id: str,
    *,
    amount_pts: int,
    tx_type: TxType,
    description: str = "",
    reference: str | None = None,
    idempotency_key: str | None = None,
) -> Transaction:
    if amount_pts <= 0:
        raise ValueError("amount_pts must be positive")

    wallet = get_wallet_for_update(user_id)

    existing = _check_idempotent(wallet.id, idempotency_key)
    if existing is not None:
        return existing

    if wallet.balance_pts < amount_pts:
        raise InsufficientBalance(
            f"saldo insuficiente: {wallet.balance_pts} pts, requer {amount_pts} pts"
        )

    wallet.balance_pts -= amount_pts
    tx = Transaction(
        wallet_id=wallet.id,
        type=tx_type,
        status=TxStatus.CONFIRMED,
        amount_pts=-amount_pts,
        description=description,
        reference=reference,
        idempotency_key=idempotency_key,
    )
    db.session.add(tx)
    db.session.flush()
    return tx


def debited_today(user_id: str, tx_type: TxType) -> int:
    """Soma de pontos debitados hoje para um tipo (em valor absoluto)."""
    today_start = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    stmt = (
        select(func.coalesce(func.sum(Transaction.amount_pts), 0))
        .join(Wallet, Wallet.id == Transaction.wallet_id)
        .where(
            Wallet.user_id == user_id,
            Transaction.type == tx_type,
            Transaction.created_at >= today_start,
        )
    )
    total = db.session.execute(stmt).scalar_one() or 0
    return abs(int(total))
