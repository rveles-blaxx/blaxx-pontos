# Arquitetura Blaxx Pontos — Banco e Sistemas

> 🟢 **CORREÇÃO (27/06/2026) — hospedagem do backend.** Onde este doc cita
> **Fly.io** / `blaxx-pontos-backend.fly.dev` / `fly secrets`, leia o
> equivalente em **Render**: a produção roda em
> `https://blaxx-pontos-exe.onrender.com` (Flask + Gunicorn, Dockerfile +
> `render.yaml`), repo `rveles-blaxx/blaxx-pontos`. O **Neon PostgreSQL**
> permanece o banco. Secrets/logs ficam no painel Render, não no `fly`.

## Topologia (1 banco, 4 clientes)

```
                         ┌─────────────────────────────┐
                         │   Neon PostgreSQL           │
                         │   project: blaxx-pontos     │
                         │   region: gru (São Paulo)   │
                         └──────────────┬──────────────┘
                                        │
                                        │ (DATABASE_URL secret)
                                        │
                         ┌──────────────▼──────────────┐
                         │   Backend Flask + Gunicorn  │
                         │   blaxx-pontos-backend.fly  │
                         │   region: gru               │
                         └──────────────┬──────────────┘
                                        │
                                        │ (HTTPS + JWT Bearer)
                                        │
       ┌────────────────────────────────┼────────────────────────────────┐
       │                                │                                │
       ▼                                ▼                                ▼
┌─────────────┐              ┌─────────────────┐              ┌──────────────────┐
│ Site /blaxx │              │ Renderer (Mac)  │              │ Mac/iOS Swift App│
│ Netlify CDN │              │ HTML embarcado  │              │ SwiftData cache  │
└─────────────┘              └─────────────────┘              └──────────────────┘
```

## Único source of truth: Neon PostgreSQL

Todos os dados (users, wallets, transactions, partners, benefits, vouchers,
campaigns, notifications, refresh_tokens, audit_logs) ficam **apenas** no
Neon. Não há replicação, não há banco separado por sistema.

| Sistema | Banco que usa | Tipo de cache |
|---|---|---|
| Backend Flask (Fly.io) | Neon PostgreSQL | nenhum (queries diretas) |
| Site Netlify (/blaxx/) | chama Backend via API | sessionStorage local |
| Renderer (Mac embarcado) | chama Backend via API | sessionStorage local |
| Mac/iOS Swift App | chama Backend via API | SwiftData (CachedWallet, CachedTransaction) |

## Ambiente dev local

Em desenvolvimento local (no seu Mac), o backend usa **SQLite** em
`backend/instance/blaxx.db` — recriado do zero quando você roda `seed.py`.
Não há sincronização entre SQLite local e Neon prod — são bancos completamente
separados por design.

Para fazer dev contra o Neon de prod (raro, perigoso):

```bash
export DATABASE_URL="postgresql://neondb_owner:...@..."
python3 run.py
```

## Migrations

Arquivos SQL em `backend/migrations/AAAA-MM-DD_descricao.sql`. **Não usamos
Alembic** ainda — migrations são manuais e rodadas no SQL Editor do
console.neon.tech.

| Migration | Aplicação |
|---|---|
| `2026-05-25_google_oauth.sql` | Adiciona `google_sub` e relaxa `password_hash` NOT NULL |

Para qualquer mudança de schema:
1. Edite o model em `app/models.py`
2. Crie um SQL em `migrations/`
3. Aplique no Neon via SQL Editor (prod) E no SQLite local (dev) via `rm instance/blaxx.db && python3 run.py && python3 seed.py`

## Backend antigo (descontinuado)

A pasta `/blaxx/backend/` foi arquivada em `/blaxx/.archive-backend-antigo/`
em 2026-05-25. Era um esqueleto Flask anterior à refatoração que apontava
para o app Fly.io `blaxx-rewards-pix` (que não existe mais). **O backend
atual é `/blaxx_app/backend/`** e só ele.

## Cleanup futuro

- Adicionar Alembic para migrations versionadas
- Adicionar branch dev no Neon para staging
- Snapshot/backup diário do Neon via cron

## Configurações sensíveis

Todas as credenciais ficam em `fly secrets` (nunca commitadas):

```bash
fly secrets list --app blaxx-pontos-backend
```

Esperado:
- `DATABASE_URL` — URL completa do Neon
- `SECRET_KEY` — assinatura Flask
- `JWT_SECRET_KEY` — assinatura JWT
- `CORS_ORIGINS` — domínios autorizados (Netlify + custom domain)
- `GOOGLE_WEB_CLIENT_ID` — Client ID OAuth Web
- `GOOGLE_IOS_CLIENT_ID` — Client ID OAuth iOS
- `MP_ACCESS_TOKEN` — Mercado Pago (quando ativar PIX real)
- `MP_WEBHOOK_SECRET` — assinatura HMAC webhook MP
- `PIX_WEBHOOK_SECRET` — assinatura genérica (fallback)

Nunca commitar `.env` no Git.
