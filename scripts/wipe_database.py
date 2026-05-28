"""Apaga TODAS as tabelas do banco. Use com EXTREMO cuidado.

DESTRUTIVO: nao tem rollback. Todos os usuarios, wallets, transactions,
audit_logs, vouchers, charges, payouts, sessions etc. sao perdidos.

Suporta Postgres (Neon/Render/Heroku) e SQLite (dev local).
Detecta pelo prefixo da DATABASE_URL.

Uso (PROD - Neon Postgres):
    $env:DATABASE_URL = "postgresql://user:pass@xxx.neon.tech/blaxx"
    python scripts/wipe_database.py --confirm WIPE-PRODUCTION

Uso (DEV - SQLite local):
    $env:DATABASE_URL = "sqlite:///C:/Users/User/AppData/Roaming/Blaxx Pontos/blaxx.db"
    python scripts/wipe_database.py --confirm WIPE-LOCAL

Apos o wipe, no proximo startup do backend o _apply_lightweight_migrations
recria o schema vazio. Pra dev local mais simples, basta apagar o arquivo
.db diretamente.
"""
from __future__ import annotations

import os
import sys


def main() -> int:
    # Confirmacao explicita
    if "--confirm" not in sys.argv:
        print("ERRO: faltou --confirm <token>")
        print("Use: python scripts/wipe_database.py --confirm WIPE-PRODUCTION")
        print("(ou WIPE-LOCAL para dev SQLite)")
        return 1

    valid_tokens = {"WIPE-PRODUCTION", "WIPE-LOCAL", "WIPE"}
    confirm_idx = sys.argv.index("--confirm")
    if confirm_idx + 1 >= len(sys.argv):
        print("ERRO: --confirm requer token apos")
        return 1
    token = sys.argv[confirm_idx + 1]
    if token not in valid_tokens:
        print(f"ERRO: token invalido '{token}'. Use um de: {', '.join(valid_tokens)}")
        return 1

    url = (os.environ.get("DATABASE_URL") or "").strip()
    if not url:
        print("ERRO: DATABASE_URL nao setada no ambiente.")
        print("Setar com: $env:DATABASE_URL = '...'")
        return 2

    # Normaliza prefixos Heroku/Render -> psycopg
    if url.startswith("postgres://"):
        url = "postgresql+psycopg://" + url[len("postgres://"):]
    elif url.startswith("postgresql://") and "+psycopg" not in url:
        url = "postgresql+psycopg://" + url[len("postgresql://"):]

    is_sqlite = url.startswith("sqlite")

    # Safety guard: token tem que bater com o tipo de banco
    if is_sqlite and token == "WIPE-PRODUCTION":
        print("ERRO: DATABASE_URL aponta pra SQLite mas voce usou WIPE-PRODUCTION.")
        print("Use --confirm WIPE-LOCAL para SQLite.")
        return 3
    if not is_sqlite and token == "WIPE-LOCAL":
        print("ERRO: DATABASE_URL aponta pra Postgres mas voce usou WIPE-LOCAL.")
        print("Use --confirm WIPE-PRODUCTION para Postgres.")
        return 3

    # Mostra qual banco vai ser wipado (mascara senha)
    masked = url
    if "@" in masked:
        head, tail = masked.rsplit("@", 1)
        scheme = head.split("://", 1)[0]
        masked = f"{scheme}://***:***@{tail}"
    print(f"Conectando em: {masked[:80]}...")
    print()

    try:
        from sqlalchemy import create_engine, text
    except ImportError:
        print("ERRO: sqlalchemy nao instalado. Rode: pip install sqlalchemy psycopg[binary]")
        return 4

    engine = create_engine(url)

    with engine.begin() as conn:
        # Lista todas as tabelas
        if is_sqlite:
            rows = conn.execute(text(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%'"
            )).fetchall()
        else:
            rows = conn.execute(text(
                "SELECT tablename FROM pg_tables WHERE schemaname='public'"
            )).fetchall()

        tables = [r[0] for r in rows]
        print(f"Tabelas encontradas em public: {len(tables)}")
        for t in sorted(tables):
            try:
                if is_sqlite:
                    count = conn.execute(text(f'SELECT COUNT(*) FROM "{t}"')).scalar()
                else:
                    count = conn.execute(text(f'SELECT COUNT(*) FROM "{t}"')).scalar()
                print(f"  - {t} ({count:,} rows)")
            except Exception:
                print(f"  - {t} (count indisponivel)")
        print()

        if not tables:
            print("Nada pra apagar. Banco ja vazio.")
            return 0

        print(f"APAGANDO {len(tables)} tabelas...")
        if is_sqlite:
            # SQLite: drop direto (sem CASCADE), com FK off
            conn.execute(text("PRAGMA foreign_keys = OFF"))
            for t in tables:
                conn.execute(text(f'DROP TABLE IF EXISTS "{t}"'))
            conn.execute(text("PRAGMA foreign_keys = ON"))
            print("  SQLite: DROP TABLE em cascata")
        else:
            # Postgres: TRUNCATE CASCADE de tudo numa transacao
            list_str = ", ".join(f'"{t}"' for t in tables)
            conn.execute(text(f"TRUNCATE TABLE {list_str} RESTART IDENTITY CASCADE"))
            print("  Postgres: TRUNCATE RESTART IDENTITY CASCADE")

    print()
    print(f"OK · {len(tables)} tabelas processadas.")
    print()
    if is_sqlite:
        print("PROXIMO PASSO: rode o backend (python main.py --local OU flask run)")
        print("o _apply_lightweight_migrations recria o schema vazio.")
    else:
        print("PROXIMO PASSO: o backend Render ja esta rodando e o schema ainda")
        print("existe (TRUNCATE so esvaziou os dados). Cadastros novos funcionam")
        print("imediatamente, sem precisar de restart.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
