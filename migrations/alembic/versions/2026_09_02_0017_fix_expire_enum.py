"""Corrige o label 'expire' minúsculo no enum txtype.

Revision ID: 20260902_0017
Revises: 20260812_0016

O tipo `txtype` em produção tinha:

    PURCHASE | TRANSFER_OUT | TRANSFER_IN | REDEEM | REFUND | BONUS | expire

Um label minúsculo entre sete maiúsculos. O SQLAlchemy grava e compara pelo
NOME do membro (`EXPIRE`), então qualquer query que tocasse `TxType.EXPIRE`
estourava com InvalidTextRepresentation. `/admin/stats` itera todos os membros
do enum — retornava 500 em produção.

Ficou invisível porque produção nunca teve usuário com role=admin: ninguém
conseguia chamar a rota. Dois defeitos se escondendo.

Aditiva e idempotente: acrescenta o label certo e migra as linhas antigas.
Não remove `expire` — o Postgres não remove valor de enum, e deixá-lo órfão
não custa nada.
"""

from alembic import op
import sqlalchemy as sa

revision = "20260902_0017"
down_revision = "20260812_0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return  # SQLite gera o CHECK a partir do modelo; sempre consistente

    # ADD VALUE não roda na mesma transação que depois usa o label.
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE txtype ADD VALUE IF NOT EXISTS 'EXPIRE'")

    op.execute("UPDATE transactions SET type = 'EXPIRE' WHERE type::text = 'expire'")


def downgrade() -> None:
    # Sem volta: o Postgres não remove valor de enum, e reverter o UPDATE
    # recriaria exatamente o estado que quebrava /admin/stats.
    pass
