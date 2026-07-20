"""Adiciona colunas de RASCUNHO a point_packages (staging edit → publicar).

O Admin edita como rascunho (draft_*), que não afeta /pix/packages nem a
cobrança até `POST /admin/packages/publish` promover draft → publicado.

Revision ID: 20260715_0009
Revises: 20260715_0008
Create Date: 2026-07-15
"""
from alembic import op
import sqlalchemy as sa


revision = "20260715_0009"
down_revision = "20260715_0008"
branch_labels = None
depends_on = None

_COLS = [
    ("draft_price_cents", sa.Integer()),
    ("draft_points", sa.Integer()),
    ("draft_label", sa.String(length=64)),
    ("draft_active", sa.Boolean()),
    ("draft_updated_at", sa.DateTime()),
    ("draft_by", sa.String(length=32)),
]


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if not insp.has_table("point_packages"):
        return  # dev cria via create_all já com as colunas
    existing = {c["name"] for c in insp.get_columns("point_packages")}
    for name, type_ in _COLS:
        if name not in existing:
            op.add_column("point_packages", sa.Column(name, type_, nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if not insp.has_table("point_packages"):
        return
    existing = {c["name"] for c in insp.get_columns("point_packages")}
    for name, _type in reversed(_COLS):
        if name in existing:
            op.drop_column("point_packages", name)
