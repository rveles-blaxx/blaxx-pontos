"""Renomeia card_charges.mp_payment_id -> provider_payment_id.

O MercadoPago foi descontinuado (2026-08-01): PIX passou para o Asaas e
cartão para a Stripe. A coluna guarda o id do pagamento no provedor ativo
(`pi_…` no Stripe, `pay_…` no Asaas), então o nome antigo virou enganoso.

Rename simples e reversível — não move dados.

Revision ID: 20260801_0012
Revises: 20260801_0011
"""
from alembic import op
import sqlalchemy as sa

revision = "20260801_0012"
down_revision = "20260801_0011"
branch_labels = None
depends_on = None


def _has_column(table: str, column: str) -> bool:
    insp = sa.inspect(op.get_bind())
    return any(c["name"] == column for c in insp.get_columns(table))


def upgrade() -> None:
    # Idempotente: em bancos criados por create_all (dev) a coluna já nasce
    # com o nome novo.
    if _has_column("card_charges", "mp_payment_id"):
        with op.batch_alter_table("card_charges") as batch:
            batch.alter_column("mp_payment_id", new_column_name="provider_payment_id")


def downgrade() -> None:
    if _has_column("card_charges", "provider_payment_id"):
        with op.batch_alter_table("card_charges") as batch:
            batch.alter_column("provider_payment_id", new_column_name="mp_payment_id")
