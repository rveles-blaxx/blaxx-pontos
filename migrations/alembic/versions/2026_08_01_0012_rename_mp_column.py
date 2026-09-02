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
    tem_antiga = _has_column("card_charges", "mp_payment_id")
    tem_nova = _has_column("card_charges", "provider_payment_id")

    # Caminho normal: só a antiga existe.
    if tem_antiga and not tem_nova:
        with op.batch_alter_table("card_charges") as batch:
            batch.alter_column("mp_payment_id", new_column_name="provider_payment_id")
        return

    # AS DUAS coexistem — foi o estado real de produção em 02/09, e derrubou
    # o deploy: a guarda antiga só perguntava pela origem, então tentava um
    # RENAME cujo destino já existia ("column provider_payment_id already
    # exists"). Não dá para renomear nem para presumir onde está o dado, então
    # copia o que faltar e segue. NÃO dropa `mp_payment_id`: derrubar coluna
    # numa migration que já falhou uma vez é como se perde dado. A limpeza é
    # item separado (T18).
    if tem_antiga and tem_nova:
        op.execute(
            "UPDATE card_charges SET provider_payment_id = mp_payment_id "
            "WHERE provider_payment_id IS NULL AND mp_payment_id IS NOT NULL"
        )
        return

    # Só a nova (dev via create_all, ou migration já aplicada): nada a fazer.


def downgrade() -> None:
    if _has_column("card_charges", "provider_payment_id"):
        with op.batch_alter_table("card_charges") as batch:
            batch.alter_column("provider_payment_id", new_column_name="mp_payment_id")
