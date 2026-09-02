"""Corrige os labels minúsculos no enum pixchargestatus.

Revision ID: 20260902_0018
Revises: 20260902_0017

Mesmo defeito da 0017, em outro tipo. O enum em produção era:

    PENDING | PAID | EXPIRED | REFUNDED | pending_confirmation | rejected

Dois labels minúsculos entre quatro maiúsculos. O SQLAlchemy compara pelo NOME
do membro (`PENDING_CONFIRMATION`, `REJECTED`), então qualquer query que os
tocasse estourava — era a causa restante do 500 em `/admin/stats`, que conta
cobranças pendentes de confirmação manual.

Os dois casos (aqui e na 0017) vêm de migrations antigas que escreveram o
*valor* do membro em vez do nome. Ver armadilha 21.

Aditiva e idempotente. Não remove os labels antigos: o Postgres não remove
valor de enum, e deixá-los órfãos não custa nada.
"""

from alembic import op
import sqlalchemy as sa

revision = "20260902_0018"
down_revision = "20260902_0017"
branch_labels = None
depends_on = None

# (label correto, label legado em minúsculo)
PARES = [("PENDING_CONFIRMATION", "pending_confirmation"), ("REJECTED", "rejected")]


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    with op.get_context().autocommit_block():
        for correto, _ in PARES:
            op.execute(f"ALTER TYPE pixchargestatus ADD VALUE IF NOT EXISTS '{correto}'")

    for correto, legado in PARES:
        op.execute(
            f"UPDATE pix_charges SET status = '{correto}' "
            f"WHERE status::text = '{legado}'"
        )


def downgrade() -> None:
    # Sem volta: reverter recriaria o estado que quebrava /admin/stats.
    pass
