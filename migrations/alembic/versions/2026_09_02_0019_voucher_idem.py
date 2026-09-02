"""Idempotencia real no resgate de beneficio (achado M-2).

Revision ID: 20260902_0019
Revises: 20260902_0018
"""

from alembic import op
import sqlalchemy as sa

revision = "20260902_0019"
down_revision = "20260902_0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("vouchers") as batch:
        batch.add_column(sa.Column("idempotency_key", sa.String(length=128), nullable=True))
        batch.create_unique_constraint("uq_voucher_idem", ["user_id", "idempotency_key"])


def downgrade() -> None:
    with op.batch_alter_table("vouchers") as batch:
        batch.drop_constraint("uq_voucher_idem", type_="unique")
        batch.drop_column("idempotency_key")
