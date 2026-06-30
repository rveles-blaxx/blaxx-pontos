"""Sprint 1-2 (P0) · idempotency_key em pix_payouts.

Substitui o cache in-memory de /redeem por idempotencia em DB:
  - adiciona coluna `idempotency_key VARCHAR(64) NULL`
  - cria UNIQUE INDEX parcial (user_id, idempotency_key) WHERE key NOT NULL

Isso garante que retries do cliente (mesmo header Idempotency-Key) NUNCA
debitam pontos duas vezes, mesmo em multi-worker / multi-instancia.

Revision ID: 20260629_0002
Revises: 20260528_0001
Create Date: 2026-06-29
"""
from alembic import op
import sqlalchemy as sa


revision = "20260629_0002"
down_revision = "20260528_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    cols = {c["name"] for c in insp.get_columns("pix_payouts")} if insp.has_table("pix_payouts") else set()

    if "pix_payouts" not in insp.get_table_names():
        # Baseline ainda nao criou a tabela — provavel DB virgem em test/dev
        # sem create_all rodado. Aborta o upgrade silenciosamente; create_all
        # vai criar a coluna ja com o novo schema.
        return

    if "idempotency_key" not in cols:
        op.add_column(
            "pix_payouts",
            sa.Column("idempotency_key", sa.String(length=64), nullable=True),
        )

    # Index parcial — Postgres usa WHERE; SQLite tambem suporta.
    # Idempotente: tenta criar; ignora erro se ja existe.
    existing_idx = {i["name"] for i in insp.get_indexes("pix_payouts")}
    if "uq_pixpayout_user_idem" not in existing_idx:
        dialect = bind.dialect.name
        if dialect in ("postgresql", "sqlite"):
            op.create_index(
                "uq_pixpayout_user_idem",
                "pix_payouts",
                ["user_id", "idempotency_key"],
                unique=True,
                postgresql_where=sa.text("idempotency_key IS NOT NULL"),
                sqlite_where=sa.text("idempotency_key IS NOT NULL"),
            )
        else:
            # Outros dialetos: index nao-parcial (aceita multiplos NULL
            # apenas em alguns dialetos; revisar se outro DB for adotado).
            op.create_index(
                "uq_pixpayout_user_idem",
                "pix_payouts",
                ["user_id", "idempotency_key"],
                unique=True,
            )


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if not insp.has_table("pix_payouts"):
        return
    existing_idx = {i["name"] for i in insp.get_indexes("pix_payouts")}
    if "uq_pixpayout_user_idem" in existing_idx:
        op.drop_index("uq_pixpayout_user_idem", table_name="pix_payouts")
    cols = {c["name"] for c in insp.get_columns("pix_payouts")}
    if "idempotency_key" in cols:
        op.drop_column("pix_payouts", "idempotency_key")
