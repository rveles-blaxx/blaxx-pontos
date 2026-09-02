"""B2B: redes parceiras que emitem pontos (merchants, chaves e acúmulos).

Revision ID: 20260812_0014
Revises: 20260801_0013

Cuidado com o enum (dois, na verdade)
------------------------------------
1. `Transaction.type` é `Enum(TxType)`, que no Postgres vira o tipo nativo
   `txtype`. Acrescentar valor exige ALTER TYPE, que **não roda dentro da
   transação** que depois o usa — por isso o `autocommit_block()`. Em SQLite o
   Enum vira VARCHAR + CHECK gerado do modelo, e o bloco é pulado.

2. O label gravado no Postgres é o **NOME** do membro (`ACCRUAL`), não o valor
   (`accrual`) — é assim que o SQLAlchemy monta `Enum(ClasseEnum)`, e é o que
   já está em produção: `PURCHASE`, `BONUS`, `EXPIRE`. Escrever minúsculo aqui
   acrescentaria um label que nenhum código usa e deixaria `ACCRUAL` faltando;
   todo acúmulo B2B morreria com `invalid input value for enum txtype`.
   Verificado contra Postgres 16 real antes de commitar.
"""

from alembic import op
import sqlalchemy as sa

revision = "20260812_0014"
down_revision = "20260801_0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    is_pg = bind.dialect.name == "postgresql"

    if is_pg:
        with op.get_context().autocommit_block():
            op.execute("ALTER TYPE txtype ADD VALUE IF NOT EXISTS 'ACCRUAL'")

    # Labels em MAIÚSCULO pelo mesmo motivo do txtype: são os nomes dos membros
    # de `MerchantVertical`. O JSON da API continua devolvendo minúsculo, via
    # `.value` no `to_dict()` — contrato de fora e storage são coisas distintas.
    vertical = sa.Enum("POSTO", "SUPERMERCADO", "FARMACIA", name="merchantvertical")
    if is_pg:
        vertical.create(bind, checkfirst=True)

    op.create_table(
        "merchants",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("legal_name", sa.String(length=180), nullable=True),
        sa.Column("cnpj", sa.String(length=14), nullable=False, unique=True),
        sa.Column("vertical", vertical, nullable=False),
        sa.Column("accrual_cents_per_point", sa.Integer(), nullable=False),
        sa.Column("bill_cents_per_point", sa.Integer(), nullable=False),
        sa.Column("max_points_per_tx", sa.Integer(), nullable=False, server_default="10000"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_merchants_vertical", "merchants", ["vertical"])

    op.create_table(
        "merchant_api_keys",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("merchant_id", sa.String(length=32),
                  sa.ForeignKey("merchants.id"), nullable=False),
        sa.Column("prefix", sa.String(length=16), nullable=False, unique=True),
        sa.Column("key_hash", sa.String(length=255), nullable=False),
        sa.Column("label", sa.String(length=80), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("last_used_at", sa.DateTime(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_merchant_api_keys_merchant_id", "merchant_api_keys", ["merchant_id"])
    op.create_index("ix_merchant_api_keys_prefix", "merchant_api_keys", ["prefix"])

    op.create_table(
        "merchant_accruals",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("merchant_id", sa.String(length=32),
                  sa.ForeignKey("merchants.id"), nullable=False),
        sa.Column("user_id", sa.String(length=32),
                  sa.ForeignKey("users.id"), nullable=False),
        sa.Column("store_code", sa.String(length=40), nullable=True),
        sa.Column("amount_cents", sa.Integer(), nullable=False),
        sa.Column("points_awarded", sa.Integer(), nullable=False),
        sa.Column("bill_cents", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=True),
        sa.Column("transaction_id", sa.String(length=32),
                  sa.ForeignKey("transactions.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("merchant_id", "idempotency_key",
                            name="uq_merchant_accrual_idem"),
    )
    op.create_index("ix_merchant_accruals_merchant_id", "merchant_accruals", ["merchant_id"])
    op.create_index("ix_merchant_accruals_user_id", "merchant_accruals", ["user_id"])
    op.create_index("ix_merchant_accruals_created_at", "merchant_accruals", ["created_at"])


def downgrade() -> None:
    op.drop_table("merchant_accruals")
    op.drop_table("merchant_api_keys")
    op.drop_index("ix_merchants_vertical", table_name="merchants")
    op.drop_table("merchants")

    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        sa.Enum(name="merchantvertical").drop(bind, checkfirst=True)
    # 'accrual' fica no enum txtype: o Postgres não remove valor de enum, e
    # lançamentos já gravados com ele continuariam válidos de qualquer forma.
