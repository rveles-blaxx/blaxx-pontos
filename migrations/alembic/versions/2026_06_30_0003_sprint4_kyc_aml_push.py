"""Sprint 4-7 · KYC, AML, MercadoPago replay store, push devices.

Adiciona:
  - users.kyc_validated_at, users.kyc_provider
  - cpf_validations (cache RF/BrasilAPI)
  - aml_alerts (registro de alertas suspeitos)
  - mp_webhook_events (replay store)
  - push_devices (APNS/FCM)

Idempotente: cada ADD COLUMN/CREATE TABLE checa existência antes.

Revision ID: 20260630_0003
Revises: 20260629_0002
Create Date: 2026-06-30
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260630_0003"
down_revision = "20260629_0002"
branch_labels = None
depends_on = None


def _table_exists(bind, name: str) -> bool:
    return name in sa.inspect(bind).get_table_names()


def _col_exists(bind, table: str, col: str) -> bool:
    insp = sa.inspect(bind)
    if not insp.has_table(table):
        return False
    return col in {c["name"] for c in insp.get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()

    # ---- users.kyc_* ----
    if _table_exists(bind, "users"):
        if not _col_exists(bind, "users", "kyc_validated_at"):
            op.add_column("users", sa.Column("kyc_validated_at", sa.DateTime(), nullable=True))
        if not _col_exists(bind, "users", "kyc_provider"):
            op.add_column("users", sa.Column("kyc_provider", sa.String(length=40), nullable=True))

    # ---- cpf_validations ----
    if not _table_exists(bind, "cpf_validations"):
        op.create_table(
            "cpf_validations",
            sa.Column("id", sa.String(length=32), primary_key=True),
            sa.Column("cpf", sa.String(length=14), nullable=False),
            sa.Column("valid", sa.Boolean(), nullable=True),
            sa.Column("validated_at", sa.DateTime(), nullable=False),
            sa.Column("provider", sa.String(length=40), nullable=False, server_default="brasilapi"),
            sa.Column("raw_response_hash", sa.String(length=64), nullable=True),
            sa.Column("error_msg", sa.String(length=255), nullable=True),
        )
        op.create_index("ix_cpf_validations_cpf", "cpf_validations", ["cpf"])
        op.create_index(
            "ix_cpf_validations_validated_at",
            "cpf_validations",
            ["validated_at"],
        )

    # ---- aml_alerts ----
    if not _table_exists(bind, "aml_alerts"):
        op.create_table(
            "aml_alerts",
            sa.Column("id", sa.String(length=32), primary_key=True),
            sa.Column("user_id", sa.String(length=32),
                      sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
            sa.Column("kind", sa.String(length=32), nullable=False),
            sa.Column("severity", sa.String(length=16), nullable=False, server_default="medium"),
            sa.Column("payload", sa.String(length=2000), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("resolved_at", sa.DateTime(), nullable=True),
            sa.Column("resolved_by", sa.String(length=32),
                      sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
            sa.Column("resolution_note", sa.String(length=500), nullable=True),
        )
        op.create_index("ix_aml_alerts_user_id", "aml_alerts", ["user_id"])
        op.create_index("ix_aml_alerts_kind", "aml_alerts", ["kind"])
        op.create_index("ix_aml_alerts_severity", "aml_alerts", ["severity"])
        op.create_index("ix_aml_alerts_created_at", "aml_alerts", ["created_at"])

    # ---- mp_webhook_events ----
    if not _table_exists(bind, "mp_webhook_events"):
        op.create_table(
            "mp_webhook_events",
            sa.Column("event_id", sa.String(length=80), primary_key=True),
            sa.Column("processed_at", sa.DateTime(), nullable=False),
            sa.Column("payment_id", sa.String(length=80), nullable=True),
            sa.Column("action", sa.String(length=40), nullable=True),
        )
        op.create_index("ix_mp_webhook_events_processed_at", "mp_webhook_events", ["processed_at"])
        op.create_index("ix_mp_webhook_events_payment_id", "mp_webhook_events", ["payment_id"])

    # ---- push_devices ----
    if not _table_exists(bind, "push_devices"):
        op.create_table(
            "push_devices",
            sa.Column("id", sa.String(length=32), primary_key=True),
            sa.Column("user_id", sa.String(length=32),
                      sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
            sa.Column("token", sa.String(length=500), nullable=False, unique=True),
            sa.Column("platform", sa.String(length=16), nullable=False),
            sa.Column("app_version", sa.String(length=20), nullable=True),
            sa.Column("registered_at", sa.DateTime(), nullable=False),
            sa.Column("last_used_at", sa.DateTime(), nullable=False),
            sa.Column("revoked_at", sa.DateTime(), nullable=True),
        )
        op.create_index("ix_push_devices_user_id", "push_devices", ["user_id"])


def downgrade() -> None:
    bind = op.get_bind()
    for tbl in ("push_devices", "mp_webhook_events", "aml_alerts", "cpf_validations"):
        if _table_exists(bind, tbl):
            op.drop_table(tbl)
    if _table_exists(bind, "users"):
        if _col_exists(bind, "users", "kyc_provider"):
            op.drop_column("users", "kyc_provider")
        if _col_exists(bind, "users", "kyc_validated_at"):
            op.drop_column("users", "kyc_validated_at")
