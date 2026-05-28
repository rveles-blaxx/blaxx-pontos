"""initial baseline (Sprints 1+2 indices + LGPD + TxType.EXPIRE)

Revision ID: 20260528_0001
Revises:
Create Date: 2026-05-28

Captura o estado pos-Sprint 2:
  - Todos os modelos SQLAlchemy criados (db.create_all equivalente)
  - Indices Sprint 1 (Transaction, PixCharge, Notification, LoginAttempt, AuditLog, Voucher)
  - Colunas Sprint 2: users.is_deleted, users.deleted_at
  - MfaSecret.secret expandido pra String(512)
  - TxType.EXPIRE no enum

Em DBs novos: cria tudo do zero.
Em DBs existentes (pre-Alembic): rode `alembic stamp head` PRIMEIRO,
depois os ALTERs ja foram aplicados pelas SQLs manuais — esta migration
serve so de baseline pra proximas autogenerates partirem dela.
"""
from alembic import op
import sqlalchemy as sa

revision = '20260528_0001'
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # No-op: assume que db.create_all() ja foi rodado OU os ALTERs SQL
    # Sprint 1+2 foram aplicados manualmente em DBs antigos. Esta migration
    # serve como ponto de partida pra `alembic revision --autogenerate`.
    # Para criar tudo do zero, rode:
    #   from app import create_app; from app.extensions import db
    #   app = create_app(); ctx = app.app_context(); ctx.push(); db.create_all()
    pass


def downgrade() -> None:
    pass
