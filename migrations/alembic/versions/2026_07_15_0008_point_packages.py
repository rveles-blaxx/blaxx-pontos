"""Cria point_packages — pacotes de compra editáveis pelo Admin.

Fonte ÚNICA de verdade do preço: alimenta GET /pix/packages (o que o cliente
vê) e create_charge (o que ele paga no PIX). Preço em CENTAVOS (Integer),
seguindo a convenção do ledger. Seed inicial a partir dos defaults históricos
(iguais a Config.POINT_PACKAGES). Em dev, db.create_all() cria a tabela e o
auto-seed do boot popula; esta migração cobre prod/Postgres (alembic upgrade).

Revision ID: 20260715_0008
Revises: 20260714_0007
Create Date: 2026-07-15
"""
from alembic import op
import sqlalchemy as sa


revision = "20260715_0008"
down_revision = "20260714_0007"
branch_labels = None
depends_on = None


# (key, label, points, price_cents, sort_order) — iguais a Config.POINT_PACKAGES
_DEFAULTS = [
    ("start", "Start", 2_000, 18_000, 0),    # R$ 180,00
    ("plus", "Plus", 5_500, 47_000, 1),      # R$ 470,00
    ("prime", "Prime", 12_000, 97_200, 2),   # R$ 972,00
    ("black", "Black", 28_000, 214_200, 3),  # R$ 2.142,00
]


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if insp.has_table("point_packages"):
        # Dev já criou via create_all (e o boot-seed populou) — nada a fazer.
        return
    op.create_table(
        "point_packages",
        sa.Column("key", sa.String(length=32), primary_key=True),
        sa.Column("label", sa.String(length=64), nullable=False),
        sa.Column("points", sa.Integer(), nullable=False),
        sa.Column("price_cents", sa.Integer(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.Column(
            "updated_by",
            sa.String(length=32),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.CheckConstraint("price_cents > 0", name="ck_pkg_price_pos"),
        sa.CheckConstraint("points > 0", name="ck_pkg_points_pos"),
    )
    t = sa.table(
        "point_packages",
        sa.column("key", sa.String),
        sa.column("label", sa.String),
        sa.column("points", sa.Integer),
        sa.column("price_cents", sa.Integer),
        sa.column("sort_order", sa.Integer),
    )
    op.bulk_insert(
        t,
        [
            {"key": k, "label": lbl, "points": p, "price_cents": c, "sort_order": s}
            for (k, lbl, p, c, s) in _DEFAULTS
        ],
    )


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if insp.has_table("point_packages"):
        op.drop_table("point_packages")
