"""Remove gateway_webhook_events — replay-store morto (T18).

O nome já era o segundo desta tabela (mp_webhook_events -> gateway_webhook_events,
migration 0013). Nenhum código lê ou escreve nela desde o corte de gateway:
Asaas e Stripe têm replay-store próprio (asaas_webhook_events e o SDK do
Stripe, respectivamente). Verificado antes desta migration: nenhum
`GatewayWebhookEvent(` fora da declaração do modelo em app/models.py.

Guarda de segurança: só derruba se a tabela estiver VAZIA. Se algo a
preencheu por fora do código Python (script avulso, import manual), a
migration aborta em vez de apagar dado sem saber o que é — igual ao padrão da
migration 0012, que também não presume o estado do banco.

Revision ID: 20260903_0021
Revises: 20260902_0020
"""
from alembic import op
import sqlalchemy as sa

revision = "20260903_0021"
down_revision = "20260902_0020"
branch_labels = None
depends_on = None


def _has_table(name: str) -> bool:
    return name in sa.inspect(op.get_bind()).get_table_names()


def upgrade() -> None:
    if not _has_table("gateway_webhook_events"):
        return
    conn = op.get_bind()
    count = conn.execute(sa.text("SELECT COUNT(*) FROM gateway_webhook_events")).scalar()
    if count:
        raise RuntimeError(
            f"gateway_webhook_events tem {count} linha(s) — não é o replay-store "
            "morto que esta migration espera encontrar. Abortando sem apagar; "
            "investigue antes de rodar de novo."
        )
    op.drop_table("gateway_webhook_events")


def downgrade() -> None:
    if _has_table("gateway_webhook_events"):
        return
    op.create_table(
        "gateway_webhook_events",
        sa.Column("event_id", sa.String(80), primary_key=True),
        sa.Column("processed_at", sa.DateTime(), nullable=False),
        sa.Column("payment_id", sa.String(80), nullable=True),
        sa.Column("action", sa.String(40), nullable=True),
    )
    op.create_index(
        "ix_gateway_webhook_events_processed_at",
        "gateway_webhook_events", ["processed_at"],
    )
    op.create_index(
        "ix_gateway_webhook_events_payment_id",
        "gateway_webhook_events", ["payment_id"],
    )
