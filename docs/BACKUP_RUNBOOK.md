# BACKUP RUNBOOK — BlaXx Pontos Backend

Sprint 5 (S5-5). Aplicável ao Postgres do Render/Neon usado pelo serviço
`blaxx-pontos-exe` (https://blaxx-pontos-exe.onrender.com).

> Owner: tech lead (escalation: ricardo.veles@gmail.com)
> Última revisão: 2026-06-30

---

## 1. SLA declarado

| Métrica | Alvo |
|---|---|
| **RPO** (Recovery Point Objective) | ≤ 1 hora |
| **RTO** (Recovery Time Objective) | ≤ 4 horas |
| Retenção | 30 dias rolling |
| Localização secundária | Off-site (S3-compatível via Backblaze B2 OU Cloudflare R2) |
| Frequência teste de restore | Mensal (1ª segunda do mês) |

Justificativa do RPO: o ledger (Transaction) e PixCharge/PixPayout têm uniqueness
constraint forte, então perdas <1h causam no máximo retentativas idempotentes
do cliente (mesmo Idempotency-Key → mesmo registro). Não há risco de
double-credit.

## 2. Backup automático (provedor gerenciado)

### 2.1. Render Postgres / Neon

- **Render Postgres**: backups diários automáticos retidos por 7 dias no plano
  Starter, 14 dias no Standard. Snapshots manuais ilimitados (até quota de disco).
- **Neon**: branching/PITR ativo por default — qualquer point-in-time dentro
  da janela de retenção (7 dias Free, 30 dias Pro).

**Confirmar status atual:**
```bash
# Render:
curl -s https://api.render.com/v1/postgres/<DB_ID>/backups \
  -H "Authorization: Bearer $RENDER_API_KEY" | jq

# Neon:
neon branches list --project-id=<PROJECT>
```

## 3. Backup manual (off-site)

> Use isso pra criar uma cópia em sua infra (S3/B2/R2) — defesa em
> profundidade caso o provedor tenha perda catastrófica do volume.

### 3.1. Dump completo

```bash
# 1) Pegue a URL canonical de conexão (read-replica preferível pra não
#    sobrecarregar o primário). NUNCA exporte DATABASE_URL pra um arquivo .sh.
export PGURL="postgresql://USER:PASS@HOST:5432/DBNAME?sslmode=require"

# 2) Dump em formato custom (paralelizável, comprimido). -F c = formato custom.
DATESTAMP=$(date -u +%Y%m%dT%H%M%SZ)
DUMPFILE="blaxx-pontos-${DATESTAMP}.pgdump"

pg_dump "$PGURL" \
  --format=custom \
  --no-owner --no-privileges \
  --compress=9 \
  --file="$DUMPFILE"

# 3) Calcule hash pra checksum
sha256sum "$DUMPFILE" > "${DUMPFILE}.sha256"

# 4) Upload off-site (exemplo com Backblaze B2)
b2 file upload blaxx-backups "$DUMPFILE" "postgres/$DUMPFILE"
b2 file upload blaxx-backups "${DUMPFILE}.sha256" "postgres/${DUMPFILE}.sha256"

# 5) Limpe local
rm -f "$DUMPFILE" "${DUMPFILE}.sha256"
```

### 3.2. Cron sugerido (executor: GitHub Actions ou Render cron job)

```cron
# Diário às 04:00 UTC (01:00 BRT)
0 4 * * * /opt/blaxx/scripts/backup_postgres.sh
```

## 4. Restauração testada

### 4.1. Restore completo em DB novo

```bash
# 1) Pegue o dump (último ou ponto específico)
b2 file download blaxx-backups/postgres/blaxx-pontos-20260601T040000Z.pgdump .

# 2) Crie DB de teste vazio
createdb -h $TEST_HOST -U $TEST_USER blaxx_restore_test

# 3) Restore
pg_restore \
  --dbname="postgresql://USER:PASS@TEST_HOST/blaxx_restore_test?sslmode=require" \
  --no-owner --no-privileges \
  --jobs=4 \
  blaxx-pontos-20260601T040000Z.pgdump

# 4) Valide integridade
psql "$TEST_URL" -c "SELECT COUNT(*) FROM users;"
psql "$TEST_URL" -c "SELECT COUNT(*) FROM transactions;"
psql "$TEST_URL" -c "SELECT COUNT(*) FROM pix_charges;"

# 5) Smoke-rode a app contra o restore (port 5001 pra não colidir)
DATABASE_URL="$TEST_URL" FLASK_ENV=development \
  gunicorn -b 0.0.0.0:5001 run:app &
sleep 5
curl -s http://localhost:5001/healthz | jq
curl -s http://localhost:5001/readyz | jq
```

### 4.2. Point-in-time recovery (Neon)

```bash
# Cria branch a partir de timestamp específico (até 30d atrás no Pro)
neon branches create --name=restore-incidente-XYZ \
  --parent=main --timestamp="2026-06-29T14:32:00Z"

# Atualize DATABASE_URL temporariamente pro novo branch
# Valide, promova se OK.
```

## 5. Teste mensal obrigatório

1ª segunda do mês, executar:

1. Restore do último backup off-site num DB throwaway (Render free Postgres,
   ou Docker local).
2. Rodar `pytest tests/ -m "not smoke"` apontando pra esse DB.
3. Validar contagens vs. produção: `users`, `wallets`, `transactions`, `pix_charges`,
   `pix_payouts`. Tolerância: ±0.1% (transactions vivas durante o dump).
4. Registrar resultado em `docs/BACKUP_TESTS.md` (criar arquivo, anexar log).
5. Se falhar: abrir incidente, investigar root cause, ajustar runbook.

## 6. Disaster Recovery — playbook

**Cenário catastrófico** (Render zone-down, Neon corrupção):

1. **Comunicação**: status page → "DEGRADED" em <5min via Slack #blaxx-status
   ou banner no SPA.
2. **Provisionar Postgres novo** (alternative provider: Supabase, Crunchy, RDS).
3. **Restaurar último backup off-site** (ver 4.1).
4. **Atualizar `DATABASE_URL` no Render env vars** → trigger redeploy.
5. **Validar `/readyz` em 200**.
6. **Comunicar resolução** + abrir postmortem em até 48h.

## 7. O que NÃO está coberto

- Logs aplicacionais (vão pro Render Dashboard, retenção limitada). Para
  retenção longa, exportar via Logtail/Datadog (Sprint futuro).
- Sentry events: retidos pelo próprio Sentry conforme plano.
- Static assets (`app/static/`): versionados no repo, restauram via git.
- Render env vars: documentadas em `LAUNCH_PENDING_CREDENTIALS.md`,
  cadastradas via dashboard (não há export programático — fazer backup
  manual num cofre tipo 1Password).
