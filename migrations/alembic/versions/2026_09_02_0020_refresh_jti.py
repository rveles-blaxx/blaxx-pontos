"""jti no refresh_tokens — revogacao granular de sessao (achado M-4).

Revision ID: 20260902_0020
Revises: 20260902_0019
"""

from alembic import op
import sqlalchemy as sa

revision = "20260902_0020"
down_revision = "20260902_0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("refresh_tokens") as batch:
        batch.add_column(sa.Column("jti", sa.String(length=64), nullable=True))
    op.create_index("ix_refresh_tokens_jti", "refresh_tokens", ["jti"])


def downgrade() -> None:
    op.drop_index("ix_refresh_tokens_jti", table_name="refresh_tokens")
    with op.batch_alter_table("refresh_tokens") as batch:
        batch.drop_column("jti")
