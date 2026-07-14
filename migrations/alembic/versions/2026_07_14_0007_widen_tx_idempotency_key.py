"""Alarga transactions.idempotency_key VARCHAR(64) -> VARCHAR(128).

Bug real: o resgate de benefício (app/api/benefits.py) grava
`idempotency_key = f"benefit:{benefit.id}:{datetime.now(timezone.utc).isoformat()}"`,
que tem ~73 caracteres (8 + uuid.hex(32) + 1 + ISO8601-com-tz(32)). A coluna
era VARCHAR(64) — no SQLite (dev) passa porque o tamanho não é imposto, mas em
qualquer Postgres o INSERT estoura com StringDataRightTruncation (500 em todo
resgate de benefício). 128 dá folga confortável.

Revision ID: 20260714_0007
Revises: 20260713_0006
Create Date: 2026-07-14
"""
from alembic import op
import sqlalchemy as sa


revision = "20260714_0007"
down_revision = "20260713_0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if not insp.has_table("transactions"):
        # DB virgem sem create_all — nada a alargar; create_all já cria String(128).
        return
    cols = {c["name"]: c for c in insp.get_columns("transactions")}
    if "idempotency_key" not in cols:
        return
    # SQLite não suporta ALTER COLUMN TYPE e ignora o tamanho de qualquer forma.
    if bind.dialect.name == "sqlite":
        return
    op.alter_column(
        "transactions",
        "idempotency_key",
        existing_type=sa.String(length=64),
        type_=sa.String(length=128),
        existing_nullable=True,
    )


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if not insp.has_table("transactions"):
        return
    if bind.dialect.name == "sqlite":
        return
    # Reverter pode truncar dados existentes; USING corta explicitamente.
    op.alter_column(
        "transactions",
        "idempotency_key",
        existing_type=sa.String(length=128),
        type_=sa.String(length=64),
        existing_nullable=True,
        postgresql_using="left(idempotency_key, 64)",
    )
