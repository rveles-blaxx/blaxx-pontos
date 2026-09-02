"""B2B: estorno de acúmulo e fechamento de fatura.

Revision ID: 20260812_0016
Revises: 20260812_0015

`merchant_invoices` é criada ANTES das colunas de `merchant_accruals`, porque
`invoice_id` referencia a fatura.

Sem UNIQUE por (rede, período): a trava contra cobrança dupla é o carimbo
`invoice_id` na linha de acúmulo, e uma UNIQUE aqui impediria refaturar um
período depois de anular a fatura errada.

Labels do enum em MAIÚSCULO — nomes dos membros, não os valores. Ver armadilha
21; escrever minúsculo aqui produziria um tipo que o ORM nunca consegue usar.
"""

from alembic import op
import sqlalchemy as sa

revision = "20260812_0016"
down_revision = "20260812_0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    is_pg = bind.dialect.name == "postgresql"

    status = sa.Enum("OPEN", "PAID", "VOID", name="invoicestatus")
    if is_pg:
        status.create(bind, checkfirst=True)

    op.create_table(
        "merchant_invoices",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("merchant_id", sa.String(length=32),
                  sa.ForeignKey("merchants.id"), nullable=False),
        sa.Column("number", sa.String(length=40), nullable=False),
        sa.Column("period_start", sa.DateTime(), nullable=False),
        sa.Column("period_end", sa.DateTime(), nullable=False),
        sa.Column("transactions", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("points_issued", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("points_reversed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("gmv_cents", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("gross_cents", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("credit_cents", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("amount_cents", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", status, nullable=False, server_default="OPEN"),
        sa.Column("due_date", sa.DateTime(), nullable=True),
        sa.Column("paid_at", sa.DateTime(), nullable=True),
        sa.Column("payment_note", sa.String(length=200), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_merchant_invoices_merchant_id", "merchant_invoices", ["merchant_id"])
    op.create_index("ix_merchant_invoices_number", "merchant_invoices", ["number"])
    op.create_index("ix_merchant_invoices_status", "merchant_invoices", ["status"])
    op.create_index("ix_merchant_invoices_created_at", "merchant_invoices", ["created_at"])

    with op.batch_alter_table("merchant_accruals") as batch:
        batch.add_column(sa.Column("reversed_at", sa.DateTime(), nullable=True))
        batch.add_column(sa.Column("reversed_points", sa.Integer(),
                                   nullable=False, server_default="0"))
        batch.add_column(sa.Column("credit_cents", sa.Integer(),
                                   nullable=False, server_default="0"))
        batch.add_column(sa.Column("reversal_transaction_id", sa.String(length=32),
                                   nullable=True))
        batch.add_column(sa.Column("reversal_reason", sa.String(length=200), nullable=True))
        batch.add_column(sa.Column("invoice_id", sa.String(length=32), nullable=True))
        batch.add_column(sa.Column("credit_invoice_id", sa.String(length=32), nullable=True))
    op.create_index("ix_merchant_accruals_invoice_id", "merchant_accruals", ["invoice_id"])
    op.create_index("ix_merchant_accruals_credit_invoice_id", "merchant_accruals",
                    ["credit_invoice_id"])


def downgrade() -> None:
    op.drop_index("ix_merchant_accruals_credit_invoice_id", table_name="merchant_accruals")
    op.drop_index("ix_merchant_accruals_invoice_id", table_name="merchant_accruals")
    with op.batch_alter_table("merchant_accruals") as batch:
        for col in ("credit_invoice_id", "invoice_id", "reversal_reason", "reversal_transaction_id",
                    "credit_cents", "reversed_points", "reversed_at"):
            batch.drop_column(col)
    op.drop_table("merchant_invoices")
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        sa.Enum(name="invoicestatus").drop(bind, checkfirst=True)
