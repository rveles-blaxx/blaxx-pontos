"""Cartão de crédito (MercadoPago Checkout API) — tabela card_charges.

Compra de pontos com cartão. Tabela separada de pix_charges (br_code/txid/
expires_at/flow são NOT NULL PIX-specific; contrato de PixCharge.to_dict é
consumido pelas 5 plataformas).

Colunas:
  - id String(32) PK
  - user_id FK users.id NOT NULL (indexada)
  - package_key String(20) NOT NULL default 'custom'
  - amount_cents Integer NOT NULL
  - points_to_credit Integer NOT NULL
  - mp_payment_id String(80) UNIQUE nullable (reconciliação do webhook)
  - status Enum(pending,in_process,approved,rejected,cancelled,refunded,
    charged_back) NOT NULL default 'pending' (indexada)
  - installments Integer NOT NULL default 1
  - card_brand String(20) nullable
  - card_last4 String(4) nullable
  - status_detail String(80) nullable
  - paid_at DateTime nullable
  - created_at DateTime (indexada)
  - idempotency_key String(64) nullable
    + UNIQUE parcial (user_id, idempotency_key) WHERE NOT NULL
  - índice composto (user_id, created_at)

Idempotente.

Revision ID: 20260713_0006
Revises: 20260630_0005
Create Date: 2026-07-13
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "20260713_0006"
down_revision = "20260630_0005"
branch_labels = None
depends_on = None


def _has_table(name: str) -> bool:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    return name in insp.get_table_names()


def _has_index(table: str, name: str) -> bool:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    return any(i["name"] == name for i in insp.get_indexes(table))


_STATUS = sa.Enum(
    "PENDING", "IN_PROCESS", "APPROVED", "REJECTED",
    "CANCELLED", "REFUNDED", "CHARGED_BACK",
    name="cardchargestatus",
)


def upgrade() -> None:
    if not _has_table("card_charges"):
        op.create_table(
            "card_charges",
            sa.Column("id", sa.String(32), primary_key=True),
            sa.Column("user_id", sa.String(32),
                      sa.ForeignKey("users.id"), nullable=False),
            sa.Column("package_key", sa.String(20), nullable=False,
                      server_default="custom"),
            sa.Column("amount_cents", sa.Integer, nullable=False),
            sa.Column("points_to_credit", sa.Integer, nullable=False),
            sa.Column("mp_payment_id", sa.String(80), nullable=True,
                      unique=True),
            sa.Column("status", _STATUS, nullable=False,
                      server_default="PENDING"),
            sa.Column("installments", sa.Integer, nullable=False,
                      server_default="1"),
            sa.Column("card_brand", sa.String(20), nullable=True),
            sa.Column("card_last4", sa.String(4), nullable=True),
            sa.Column("status_detail", sa.String(80), nullable=True),
            sa.Column("paid_at", sa.DateTime, nullable=True),
            # server_default: linhas inseridas fora do ORM (SQL bruto,
            # reconciliação) ainda recebem timestamp em vez de NULL — a fila
            # admin e o índice (user_id, created_at) dependem disso.
            sa.Column("created_at", sa.DateTime, nullable=True,
                      server_default=sa.func.now()),
            sa.Column("idempotency_key", sa.String(64), nullable=True),
        )

    if not _has_index("card_charges", "ix_card_charges_user_id"):
        op.create_index("ix_card_charges_user_id", "card_charges", ["user_id"])
    if not _has_index("card_charges", "ix_card_charges_status"):
        op.create_index("ix_card_charges_status", "card_charges", ["status"])
    if not _has_index("card_charges", "ix_card_charges_created_at"):
        op.create_index("ix_card_charges_created_at", "card_charges", ["created_at"])
    if not _has_index("card_charges", "ix_cardcharge_user_created"):
        op.create_index(
            "ix_cardcharge_user_created", "card_charges",
            ["user_id", "created_at"],
        )
    if not _has_index("card_charges", "uq_cardcharge_user_idem"):
        op.create_index(
            "uq_cardcharge_user_idem", "card_charges",
            ["user_id", "idempotency_key"],
            unique=True,
            postgresql_where=sa.text("idempotency_key IS NOT NULL"),
            sqlite_where=sa.text("idempotency_key IS NOT NULL"),
        )


def downgrade() -> None:
    if _has_table("card_charges"):
        op.drop_table("card_charges")
    # Postgres: enum é objeto separado da tabela
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        _STATUS.drop(bind, checkfirst=True)
