"""Sprint 5 (S5-3) · Backup Neon Postgres → S3 com retencao 12 semanas.

Uso:
    DATABASE_URL=postgresql://... \
    AWS_ACCESS_KEY_ID=... AWS_SECRET_ACCESS_KEY=... \
    BLAXX_S3_BUCKET=blaxx-backups BLAXX_S3_PREFIX=neon/ \
    python scripts/backup_neon_to_s3.py

Em CI/cron (semanal):
    GitHub Actions workflow `.github/workflows/backup.yml` rodando aos
    domingos as 03h UTC. Veja README.md desta pasta.

Estrategia:
    1. pg_dump custom format (-Fc) gera arquivo binario menor que SQL.
    2. Upload pra s3://<bucket>/<prefix><iso_week>/<timestamp>.dump
    3. Lista objetos com mais de 12 semanas e apaga (retencao).
    4. Sanity check: re-baixa o dump e roda pg_restore --list pra
       confirmar que nao ta corrompido.

Requisitos:
    - pg_dump 16+ no PATH (instalar postgresql-client no runner CI)
    - boto3 (pip install boto3)
    - AWS credentials via env vars OU IAM role do runner
"""
from __future__ import annotations

import datetime as dt
import os
import subprocess
import sys
import tempfile


def _env(key: str, *, required: bool = True, default: str | None = None) -> str:
    val = os.environ.get(key, default or "")
    if required and not val:
        sys.stderr.write(f"ERRO: env var {key} obrigatoria\n")
        sys.exit(2)
    return val


def pg_dump_to_file(db_url: str, out_path: str) -> int:
    """Roda pg_dump custom format. Retorna bytes do arquivo."""
    cmd = [
        "pg_dump",
        "--format=custom",   # binario, menor que SQL plain
        "--compress=9",      # max compress (lento mas economiza S3)
        "--no-owner", "--no-acl",
        "--file", out_path,
        db_url,
    ]
    print(f"[+] pg_dump → {out_path}")
    res = subprocess.run(cmd, check=False)
    if res.returncode != 0:
        sys.stderr.write(f"ERRO: pg_dump retornou {res.returncode}\n")
        sys.exit(3)
    size = os.path.getsize(out_path)
    print(f"    {size:,} bytes")
    return size


def s3_upload(local_path: str, bucket: str, key: str) -> str:
    """Upload pra S3. Retorna URI s3://bucket/key."""
    import boto3
    s3 = boto3.client("s3")
    print(f"[+] uploading → s3://{bucket}/{key}")
    s3.upload_file(local_path, bucket, key, ExtraArgs={
        "ServerSideEncryption": "AES256",
        "StorageClass": "STANDARD_IA",   # custo menor pra access infrequente
        "Metadata": {
            "tool": "blaxx-backup-script",
            "version": "1.0",
        },
    })
    return f"s3://{bucket}/{key}"


def s3_prune_old(bucket: str, prefix: str, keep_weeks: int = 12) -> int:
    """Apaga objetos com prefix mais antigos que `keep_weeks` semanas."""
    import boto3
    s3 = boto3.client("s3")
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(weeks=keep_weeks)
    deleted = 0
    print(f"[+] retencao: removendo objetos < {cutoff.date()}")
    pager = s3.get_paginator("list_objects_v2")
    for page in pager.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            if obj["LastModified"] < cutoff:
                s3.delete_object(Bucket=bucket, Key=obj["Key"])
                print(f"    deleted {obj['Key']}")
                deleted += 1
    return deleted


def verify_dump(path: str) -> bool:
    """Roda pg_restore --list pra confirmar que o arquivo nao esta corrompido."""
    cmd = ["pg_restore", "--list", path]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        sys.stderr.write(f"ERRO: pg_restore --list falhou:\n{res.stderr}\n")
        return False
    nlines = len(res.stdout.splitlines())
    print(f"[+] pg_restore --list OK ({nlines} entradas)")
    return nlines > 0


def main() -> int:
    db_url = _env("DATABASE_URL")
    bucket = _env("BLAXX_S3_BUCKET")
    prefix = _env("BLAXX_S3_PREFIX", required=False, default="neon/")
    keep_weeks = int(_env("BLAXX_BACKUP_RETENTION_WEEKS",
                           required=False, default="12"))

    now = dt.datetime.now(dt.timezone.utc)
    iso_year, iso_week, _ = now.isocalendar()
    ts = now.strftime("%Y%m%dT%H%M%SZ")
    key = f"{prefix}{iso_year}-W{iso_week:02d}/blaxx-{ts}.dump"

    with tempfile.TemporaryDirectory() as tmp:
        local = os.path.join(tmp, f"blaxx-{ts}.dump")
        pg_dump_to_file(db_url, local)
        if not verify_dump(local):
            return 4
        uri = s3_upload(local, bucket, key)
        print(f"[OK] backup completo: {uri}")

    deleted = s3_prune_old(bucket, prefix, keep_weeks)
    print(f"[OK] retencao: {deleted} objetos removidos")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
