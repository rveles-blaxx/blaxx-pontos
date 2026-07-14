#!/usr/bin/env python3
"""
Migração de dados SQLite -> PostgreSQL para o backend Blaxx Pontos.

Constrói o conteúdo de um Postgres novo (ex.: o do serviço canônico
`blaxx-pontos` no Render) a partir do banco local do app (`instance/blaxx.db`).

PREMISSA: o SCHEMA do Postgres alvo já deve existir e estar em `alembic head`
(o deploy do Render roda `alembic upgrade head` no preDeploy/Docker CMD). Este
script NÃO cria schema — ele só COPIA dados. Assim o schema fica sempre canônico
(inclui tabelas novas como `card_charges`, que no SQLite local nem existem).

O que ele faz:
  1. Reflete o schema do Postgres alvo (fonte da verdade dos tipos/colunas).
  2. Para cada tabela que existe nos DOIS bancos, copia as colunas em comum,
     na ordem segura de FKs (metadata.sorted_tables).
  3. Coerção de tipos SQLite->PG: Boolean (0/1 -> bool) e DateTime/Date
     (string ISO -> datetime). Demais tipos passam direto.
  4. Reseta as sequences de colunas serial/identity (se houver).
  5. Verifica contagem de linhas origem x destino e imprime um relatório.

Colunas presentes só no destino (ex.: adicionadas por migrations mais novas que
o SQLite local) ficam com o DEFAULT/NULL do Postgres. O script AVISA antes de
inserir se alguma dessas colunas for NOT NULL sem default (inserção quebraria).

Uso:
    # dry-run: valida leitura + coerção contra o SQLite, sem tocar em Postgres
    python scripts/migrate_sqlite_to_pg.py --sqlite instance/blaxx.db --dry-run

    # execução real (schema já criado no destino):
    python scripts/migrate_sqlite_to_pg.py \
        --sqlite instance/blaxx.db \
        --pg "postgresql+psycopg://user:pass@host/db?sslmode=require" \
        --truncate

Flags:
    --truncate   TRUNCATE ... RESTART IDENTITY CASCADE nas tabelas alvo antes de
                 copiar (torna a operação idempotente / re-executável).
    --only a,b   copia só essas tabelas.
    --skip a,b   pula essas tabelas.
    --batch N    tamanho do lote de INSERT (default 1000).
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    MetaData,
    String,
    Table,
    create_engine,
    func,
    insert,
    select,
    text,
)

# alembic_version é gerenciada pelo Alembic no destino; nunca copiar.
ALWAYS_SKIP = {"alembic_version"}


def parse_dt(value):
    """String ISO do SQLite -> datetime/date. Passa datetime/None direto."""
    if value is None or isinstance(value, (datetime, date)):
        return value
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        # fromisoformat (py3.9) aceita separador espaço ou 'T'
        try:
            return datetime.fromisoformat(s)
        except ValueError:
            for fmt in (
                "%Y-%m-%d %H:%M:%S.%f",
                "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%dT%H:%M:%S.%f",
                "%Y-%m-%dT%H:%M:%S",
                "%Y-%m-%d",
            ):
                try:
                    return datetime.strptime(s, fmt)
                except ValueError:
                    continue
            raise
    return value


def to_bool(value):
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        v = value.strip().lower()
        if v in ("1", "true", "t", "yes", "y"):
            return True
        if v in ("0", "false", "f", "no", "n", ""):
            return False
    return bool(value)


def build_coercers(table: Table, columns, on_overflow="fail"):
    """Mapa coluna->função de coerção, baseado no TIPO do destino.

    on_overflow trata strings maiores que o VARCHAR(n) do destino (o SQLite
    não impõe tamanho, o Postgres sim):
      fail     -> deixa passar; o INSERT vai levantar StringDataRightTruncation
      truncate -> corta em n chars (LOSSY)
      null     -> vira NULL (só se a coluna for nullable; senão trunca)
    """
    coercers = {}
    for col_name in columns:
        col = table.c[col_name]
        col_type = col.type
        if isinstance(col_type, Boolean):
            coercers[col_name] = to_bool
        elif isinstance(col_type, (DateTime, Date)):
            coercers[col_name] = parse_dt
        elif isinstance(col_type, String) and col_type.length and on_overflow != "fail":
            n = col_type.length
            nullable = col.nullable
            coercers[col_name] = _make_overflow_fn(n, on_overflow, nullable)
        else:
            coercers[col_name] = None  # passthrough
    return coercers


def _make_overflow_fn(n, mode, nullable):
    def fn(v):
        if isinstance(v, str) and len(v) > n:
            if mode == "null" and nullable:
                return None
            return v[:n]  # truncate (fallback do 'null' em coluna NOT NULL)
        return v
    return fn


def coerce_row(row: dict, coercers) -> dict:
    out = {}
    for k, v in row.items():
        fn = coercers.get(k)
        out[k] = fn(v) if fn is not None else v
    return out


def check_missing_notnull(dest_table: Table, common_cols):
    """Colunas do destino ausentes na origem que são NOT NULL sem default."""
    problems = []
    common = set(common_cols)
    for col in dest_table.columns:
        if col.name in common:
            continue
        has_default = col.default is not None or col.server_default is not None
        if not col.nullable and not has_default:
            problems.append(col.name)
    return problems


def reset_sequences(dest_engine, dest_meta, tables):
    """Reseta sequences serial/identity p/ max(coluna)+1 (idempotente)."""
    reset = []
    with dest_engine.begin() as conn:
        for t in tables:
            for col in t.columns:
                seq = conn.execute(
                    text("SELECT pg_get_serial_sequence(:tbl, :col)"),
                    {"tbl": t.name, "col": col.name},
                ).scalar()
                if not seq:
                    continue
                maxv = conn.execute(
                    text(f'SELECT COALESCE(MAX("{col.name}"), 0) FROM "{t.name}"')
                ).scalar()
                conn.execute(
                    text("SELECT setval(:seq, :val, true)"),
                    {"seq": seq, "val": int(maxv) if maxv else 1},
                )
                reset.append(f"{t.name}.{col.name} -> {maxv}")
    return reset


def main():
    ap = argparse.ArgumentParser(description="Migra dados SQLite -> Postgres (Blaxx).")
    ap.add_argument("--sqlite", required=True, help="caminho do blaxx.db")
    ap.add_argument("--pg", help="DATABASE_URL do Postgres alvo (postgresql+psycopg://...)")
    ap.add_argument("--dry-run", action="store_true", help="valida leitura/coerção sem tocar no Postgres")
    ap.add_argument("--truncate", action="store_true", help="TRUNCATE ... RESTART IDENTITY CASCADE antes de copiar")
    ap.add_argument("--only", default="", help="csv de tabelas a copiar")
    ap.add_argument("--skip", default="", help="csv de tabelas a pular")
    ap.add_argument("--batch", type=int, default=1000)
    ap.add_argument(
        "--on-overflow",
        choices=["fail", "truncate", "null"],
        default="fail",
        help="strings maiores que o VARCHAR(n) do destino: fail (deixa estourar), "
        "truncate (corta, LOSSY), null (vira NULL se nullable). Default: fail.",
    )
    args = ap.parse_args()

    only = {t.strip() for t in args.only.split(",") if t.strip()}
    skip = {t.strip() for t in args.skip.split(",") if t.strip()} | ALWAYS_SKIP

    src_engine = create_engine(f"sqlite:///{args.sqlite}")
    src_meta = MetaData()
    src_meta.reflect(bind=src_engine)
    src_tables = set(src_meta.tables)

    if args.dry_run or not args.pg:
        if not args.dry_run:
            ap.error("--pg é obrigatório fora do --dry-run")
        # Sem Postgres: usamos o schema do SQLite como proxy do destino.
        dest_engine = None
        dest_meta = src_meta
        print("== DRY-RUN == (schema do destino simulado pelo próprio SQLite)\n")
    else:
        pg_url = args.pg
        # normaliza para o driver psycopg (v3)
        if pg_url.startswith("postgresql://"):
            pg_url = "postgresql+psycopg://" + pg_url[len("postgresql://"):]
        elif pg_url.startswith("postgres://"):
            pg_url = "postgresql+psycopg://" + pg_url[len("postgres://"):]
        dest_engine = create_engine(pg_url)
        dest_meta = MetaData()
        dest_meta.reflect(bind=dest_engine)

    # Ordem segura de FK a partir do schema do destino.
    ordered = [t for t in dest_meta.sorted_tables if t.name not in skip]
    if only:
        ordered = [t for t in ordered if t.name in only]

    # TRUNCATE (ordem reversa não é necessária com CASCADE)
    if args.truncate and dest_engine is not None:
        names = [f'"{t.name}"' for t in ordered if t.name in src_tables]
        if names:
            with dest_engine.begin() as conn:
                conn.execute(text(f"TRUNCATE {', '.join(names)} RESTART IDENTITY CASCADE"))
            print(f"TRUNCATE em {len(names)} tabela(s).\n")

    report = []
    grand_src = grand_ins = 0
    hard_errors = []

    for dest_table in ordered:
        tname = dest_table.name
        if tname not in src_tables:
            report.append((tname, "-", "-", "só no destino (vazia)"))
            continue

        src_table = src_meta.tables[tname]
        common = [c for c in dest_table.columns.keys() if c in src_table.columns.keys()]
        if not common:
            report.append((tname, "?", 0, "sem colunas em comum"))
            continue

        # avisos de colunas obrigatórias ausentes na origem
        missing_nn = check_missing_notnull(dest_table, common)
        note = ""
        if missing_nn:
            note = f"NOT NULL sem default ausente na origem: {missing_nn}"
            hard_errors.append((tname, missing_nn))

        coercers = build_coercers(dest_table, common, on_overflow=args.on_overflow)

        with src_engine.connect() as cconn:
            src_count = cconn.execute(select(func.count()).select_from(src_table)).scalar()
        grand_src += src_count or 0

        inserted = 0
        # leitura em streaming
        sel = select(*[src_table.c[c] for c in common])
        with src_engine.connect() as sconn:
            result = sconn.execution_options(stream_results=True).execute(sel)
            batch = []
            for r in result.mappings():
                batch.append(coerce_row(dict(r), coercers))
                if len(batch) >= args.batch:
                    inserted += _flush(dest_engine, dest_table, common, batch)
                    batch = []
            if batch:
                inserted += _flush(dest_engine, dest_table, common, batch)
        grand_ins += inserted

        status = "OK" if (dest_engine is None or inserted == src_count) else "DIVERGE"
        report.append((tname, src_count, inserted, note or status))

    # sequences
    if dest_engine is not None:
        seqs = reset_sequences(dest_engine, dest_meta, [t for t in ordered if t.name in src_tables])
        if seqs:
            print("Sequences resetadas:")
            for s in seqs:
                print("  ", s)
            print()

    # relatório
    print(f"{'tabela':<26}{'origem':>10}{'inserido':>10}   nota")
    print("-" * 72)
    for tname, sc, ins, note in report:
        print(f"{tname:<26}{str(sc):>10}{str(ins):>10}   {note}")
    print("-" * 72)
    print(f"{'TOTAL':<26}{grand_src:>10}{grand_ins:>10}")

    if hard_errors:
        print("\n[ATENÇÃO] Tabelas com coluna NOT NULL sem default ausente na origem:")
        for tname, cols in hard_errors:
            print(f"  {tname}: {cols}")
        print("Essas inserções falhariam num destino real — ajuste o schema ou preencha um default.")

    if args.dry_run:
        print("\n(dry-run: nada foi escrito.)")


def _flush(dest_engine, dest_table, columns, batch):
    if dest_engine is None:
        return len(batch)  # dry-run
    with dest_engine.begin() as conn:
        conn.execute(insert(dest_table), batch)
    return len(batch)


if __name__ == "__main__":
    sys.exit(main())
