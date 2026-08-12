"""Renomeia mp_webhook_events -> gateway_webhook_events.

Último resíduo de nomenclatura do gateway anterior. A tabela é um replay-store
genérico de eventos de webhook; o nome antigo amarrava a um provedor que não
existe mais no sistema.

Revision ID: 20260801_0013
Revises: 20260801_0012
"""
from alembic import op
import sqlalchemy as sa

revision = "20260801_0013"
down_revision = "20260801_0012"
branch_labels = None
depends_on = None


def _has_table(name: str) -> bool:
    return name in sa.inspect(op.get_bind()).get_table_names()


def upgrade() -> None:
    if _has_table("mp_webhook_events") and not _has_table("gateway_webhook_events"):
        op.rename_table("mp_webhook_events", "gateway_webhook_events")


def downgrade() -> None:
    if _has_table("gateway_webhook_events") and not _has_table("mp_webhook_events"):
        op.rename_table("gateway_webhook_events", "mp_webhook_events")
