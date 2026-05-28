# Scripts · Sprint 5

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
