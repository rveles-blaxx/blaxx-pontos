"""Login humano do painel da rede parceira.

Revision ID: 20260812_0015
Revises: 20260812_0014
"""

from alembic import op
import sqlalchemy as sa

revision = "20260812_0015"
down_revision = "20260812_0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "merchant_users",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("merchant_id", sa.String(length=32),
                  sa.ForeignKey("merchants.id"), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("email", sa.String(length=180), nullable=False, unique=True),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("role", sa.String(length=20), nullable=False, server_default="owner"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("last_login_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_merchant_users_merchant_id", "merchant_users", ["merchant_id"])
    op.create_index("ix_merchant_users_email", "merchant_users", ["email"])


def downgrade() -> None:
    op.drop_table("merchant_users")
