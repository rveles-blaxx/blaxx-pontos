# Scripts do backend

Reorganizado em 2026-09-03. Antes, 16 scripts moravam na **raiz do repositório**,
misturados com o que o Render e o Alembic executam. A raiz agora contém apenas
o que o deploy toca — `run.py`, `alembic.ini`, `migrations/`, `render.yaml`,
`requirements.txt`, `runtime.txt`, `Dockerfile`, `pytest.ini`, `openapi.yaml`,
`seed.py` — e nada aqui é chamado por código da aplicação.

| Pasta | O que vive aqui |
|---|---|
| `operacao/` | Ferramentas que falam com **produção** por HTTP. Todas pedem senha de admin via `getpass` e **nenhuma carrega credencial**. |
| `seed/` | Carga de dados: catálogo, parceiros Livelo, redes B2B. |
| `qa/` | Homologação e loop de QA. |
| `deploy/` | Shell de apoio ao deploy e setup de máquina. |
| *(raiz de `scripts/`)* | Utilitários de banco e carga anteriores ao Sprint 5. |

## `operacao/` — cuidado ao usar

Estes scripts **escrevem em produção**. Duas regras que saíram de erro real:

1. **Só sonde endpoint cujo corpo vazio seja comprovadamente inválido.**
   `PATCH /admin/users/<id>/vip` **alterna** o valor com corpo vazio — uma
   sondagem inverteu o `is_vip` de uma conta real. `restaurar_vip.py` existe por
   causa disso.
2. **Todo script de carga fecha com contagem contra o esperado** (`21/21`), não
   com "terminou", e captura exceção genérica. `cadastrar_catalogo.py` morreu num
   `URLError` no meio da carga e a saída até ali era toda de sucesso.

`conferir_conteudo.py` confere **quantidade** em produção, não código HTTP —
`200` com lista vazia era exatamente o bug que a varredura precisava pegar.

## Chamadas — os caminhos mudaram

```bash
python3 scripts/operacao/checar_endpoints.py
python3 scripts/seed/seed_merchants.py
python3 scripts/qa/qa_homolog.py
./scripts/deploy/redeploy.sh
```

---


## backup_neon_to_s3.py · Backup semanal Neon → S3

### Como usar local

```bash
export DATABASE_URL="postgresql://user:pass@neon.tech/blaxx"
export AWS_ACCESS_KEY_ID=...
export AWS_SECRET_ACCESS_KEY=...
export BLAXX_S3_BUCKET=blaxx-backups
export BLAXX_S3_PREFIX=neon/
pip install boto3
python scripts/backup_neon_to_s3.py
```

### Como configurar GitHub Actions (recomendado)

Adicionar `.github/workflows/backup.yml`:

```yaml
name: Backup Neon
on:
  schedule:
    - cron: '0 3 * * 0'   # Domingo 03h UTC
  workflow_dispatch:
jobs:
  backup:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: |
          sudo apt-get install -y postgresql-client-16
          pip install boto3
      - run: python backend/scripts/backup_neon_to_s3.py
        env:
          DATABASE_URL: ${{ secrets.NEON_DATABASE_URL }}
          AWS_ACCESS_KEY_ID: ${{ secrets.AWS_KEY }}
          AWS_SECRET_ACCESS_KEY: ${{ secrets.AWS_SECRET }}
          BLAXX_S3_BUCKET: blaxx-backups
```

### Restore (cenario de incidente)

```bash
# 1. Baixa o dump mais recente
aws s3 ls s3://blaxx-backups/neon/ --recursive | sort | tail -1
aws s3 cp s3://blaxx-backups/neon/2026-W22/blaxx-20260601T030000Z.dump .

# 2. Restore num branch Neon novo (evita sobrescrever prod)
pg_restore --clean --if-exists --no-owner --no-acl \
  -d "$NEON_RESTORE_URL" blaxx-20260601T030000Z.dump
```

### Custo estimado (S3)

- 100MB/semana × 12 semanas = 1.2GB armazenado
- S3 Standard-IA: ~$0.0125/GB/mes = $0.015/mes
- Transferencia OUT: $0 (sem read em rotina)
