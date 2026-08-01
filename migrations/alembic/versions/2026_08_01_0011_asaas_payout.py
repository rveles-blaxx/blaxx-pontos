"""Asaas payout: id da transferência no payout + replay-store de webhooks.

Revision ID: 20260801_0011
Revises: 20260720_0010
"""

from alembic import op
import sqlalchemy as sa

revision = "20260801_0011"
down_revision = "20260720_0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Id da transferência no provedor de payout (Asaas). Nullable: payouts
    # antigos (fila manual) não têm.
    with op.batch_alter_table("pix_payouts") as batch:
        batch.add_column(
            sa.Column("provider_transfer_id", sa.String(length=80), nullable=True)
        )
    op.create_index(
        "ix_pix_payouts_provider_transfer_id",
        "pix_payouts",
        ["provider_transfer_id"],
    )

    # Replay-store: a entrega de webhooks do Asaas é "at least once".
    op.create_table(
        "asaas_webhook_events",
        sa.Column("event_id", sa.String(length=120), primary_key=True),
        sa.Column("processed_at", sa.DateTime(), nullable=False),
        sa.Column("event", sa.String(length=40), nullable=True),
        sa.Column("transfer_id", sa.String(length=80), nullable=True),
        sa.Column("txid", sa.String(length=64), nullable=True),
    )
    op.create_index(
        "ix_asaas_webhook_events_processed_at", "asaas_webhook_events", ["processed_at"]
    )
    op.create_index(
        "ix_asaas_webhook_events_transfer_id", "asaas_webhook_events", ["transfer_id"]
    )
    op.create_index(
        "ix_asaas_webhook_events_txid", "asaas_webhook_events", ["txid"]
    )


def downgrade() -> None:
    op.drop_index("ix_asaas_webhook_events_txid", table_name="asaas_webhook_events")
    op.drop_index("ix_asaas_webhook_events_transfer_id", table_name="asaas_webhook_events")
    op.drop_index("ix_asaas_webhook_events_processed_at", table_name="asaas_webhook_events")
    op.drop_table("asaas_webhook_events")
    op.drop_index("ix_pix_payouts_provider_transfer_id", table_name="pix_payouts")
    with op.batch_alter_table("pix_payouts") as batch:
        batch.drop_column("provider_transfer_id")
