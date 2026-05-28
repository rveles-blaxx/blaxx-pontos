"""Alembic env.py · Sprint 4 (S4-2).

Le o schema dos models SQLAlchemy direto (autogenerate confiavel)
e priorizia DATABASE_URL/ALEMBIC_DB_URL pra prod.
"""
from __future__ import annotations
import os
import sys
from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool
from alembic import context

# Garante que `app/` esteja no path
sys.path.insert(0, os.path.abspath(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))))

from app.extensions import db
from app import models  # noqa: F401 - registra todos os models

config = context.config

# DB URL priority: ALEMBIC_DB_URL > DATABASE_URL > alembic.ini default
db_url = (os.environ.get("ALEMBIC_DB_URL")
          or os.environ.get("DATABASE_URL")
          or config.get_main_option("sqlalchemy.url"))
# Normalizacao do prefix Heroku/Render -> psycopg
if db_url.startswith("postgres://"):
    db_url = "postgresql+psycopg://" + db_url[len("postgres://"):]
elif db_url.startswith("postgresql://") and "+psycopg" not in db_url:
    db_url = "postgresql+psycopg://" + db_url[len("postgresql://"):]
config.set_main_option("sqlalchemy.url", db_url)

if config.config_file_name is not None:
    try:
        fileConfig(config.config_file_name)
    except Exception:
        pass

target_metadata = db.metadata


def run_migrations_offline() -> None:
    """Gera SQL sem precisar de conexao real (--sql)."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(url=url, target_metadata=target_metadata,
                      literal_binds=True, dialect_opts={"paramstyle": "named"},
                      compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Conecta no DB e aplica."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection,
                          target_metadata=target_metadata,
                          compare_type=True)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
