"""Adiciona a ForeignKey de point_packages.draft_by -> users.id (ON DELETE SET NULL).

A migration 0009 criou `draft_by` como String(32) PURO, sem a FK que o modelo
declara (`ForeignKey("users.id", ondelete="SET NULL")`). Resultado: em prod
(Postgres via Alembic) a coluna nascia sem a constraint, enquanto em dev
(create_all) nascia com ela — schemas divergentes, e um admin removido deixava
`draft_by` orfao em vez de virar NULL. Esta migration reconcilia prod, espelhando
`updated_by` (que ja recebeu a FK na 0008).

Postgres-only: SQLite nao suporta ADD CONSTRAINT via ALTER, e o dev usa
create_all (que ja cria a FK). Idempotente: so cria se a coluna existir e a FK
ainda nao existir.

Revision ID: 20260720_0010
Revises: 20260715_0009
Create Date: 2026-07-20
"""
from alembic import op
import sqlalchemy as sa


revision = "20260720_0010"
down_revision = "20260715_0009"
branch_labels = None
depends_on = None

_FK_NAME = "fk_point_packages_draft_by_users"


def _has_fk(table: str, name: str) -> bool:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    return any(fk.get("name") == name for fk in insp.get_foreign_keys(table))


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        # SQLite/dev usam create_all, que ja cria a FK; ALTER ADD CONSTRAINT
        # nao e suportado no SQLite.
        return
    insp = sa.inspect(bind)
    if not insp.has_table("point_packages"):
        return
    cols = {c["name"] for c in insp.get_columns("point_packages")}
    if "draft_by" not in cols:
        return
    if _has_fk("point_packages", _FK_NAME):
        return
    op.create_foreign_key(
        _FK_NAME,
        "point_packages",
        "users",
        ["draft_by"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    if _has_fk("point_packages", _FK_NAME):
        op.drop_constraint(_FK_NAME, "point_packages", type_="foreignkey")
