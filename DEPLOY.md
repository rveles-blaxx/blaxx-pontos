# Deploy do Blaxx Pontos · Sprint 1

> 🟢 **DESATUALIZADO (verificado 27/06/2026).** Este guia descreve deploy em
> **Fly.io** (`blaxx-pontos-backend.fly.dev`), que é **legado**. A produção real
> hoje roda em **Render**: `https://blaxx-pontos-exe.onrender.com` (Flask +
> Gunicorn via Dockerfile + `render.yaml`; healthcheck `/readyz`), repo
> `rveles-blaxx/blaxx-pontos`, com **Neon PostgreSQL** + Alembic. Onde abaixo se
> lê `fly …` / `*.fly.dev`, o equivalente vive no painel Render. Env vars de
> produção: `LAUNCH_PENDING_CREDENTIALS.md` (raiz) §2.1; pipeline/CI: §7.

Coloca o backend Flask no ar em ~30 minutos, com PostgreSQL gerenciado e
frontend Netlify apontando para ele.

## Arquitetura final do Sprint 1

```
┌─────────────────┐         ┌──────────────────────────┐
│   Netlify       │  HTTPS  │ Fly.io (gru, São Paulo)  │
│   /blaxx/       ├────────▶│ blaxx-pontos-backend     │
│ frontend HTML   │         │ Flask + gunicorn         │
└─────────────────┘         └────────────┬─────────────┘
                                         │ psycopg
                                         ▼
                            ┌──────────────────────────┐
                            │ Neon (PostgreSQL)        │
                            │ região: south-america-east│
                            └──────────────────────────┘
```

Custo total: **R$ 0/mês** (todos os serviços em free tier).

---

## Passo 1 — Banco PostgreSQL no Neon (~5 min)

1. Acesse [neon.tech](https://neon.tech) → "Sign up" (login com GitHub é o
   mais rápido)
2. **Create project**:
   - Project name: `blaxx-pontos`
   - Postgres version: 16
   - Region: **AWS São Paulo (sa-east-1)**
   - Database name: `blaxx`
3. Após criar, copie a connection string que aparece no card "Connection
   string" — algo como:
   ```
   postgresql://USER:SENHA@ep-xxx-pooler.sa-east-1.aws.neon.tech/blaxx?sslmode=require
   ```
   ⚠ Use a versão **pooled** (com `-pooler` no host). Guarde essa string.

---

## Passo 2 — Deploy do backend no Fly.io (~10 min)

### 2.1 — Instala o `flyctl` (uma vez)

```bash
brew install flyctl
fly auth signup     # ou: fly auth login (se já tem conta)
```

A primeira conta pede cartão (não cobra nada no free tier, é antifraude).

### 2.2 — Cria o app

```bash
cd "/Users/ricardoveles/Library/CloudStorage/Dropbox/Blaxx Pontos/blaxx_app/backend"

# Cria sem deploy (queremos configurar segredos antes)
fly launch --no-deploy --copy-config --name blaxx-pontos-backend --region gru
```

Se o nome `blaxx-pontos-backend` já estiver tomado, escolhe outro (vai entrar
na URL final: `https://SEU-NOME.fly.dev`).

### 2.3 — Configura os segredos

```bash
# Substitua pela connection string que você copiou do Neon
fly secrets set DATABASE_URL="postgresql://USER:SENHA@ep-xxx-pooler.sa-east-1.aws.neon.tech/blaxx?sslmode=require"

# Chave secreta para sessions/cookies (gera uma nova)
fly secrets set SECRET_KEY="$(openssl rand -hex 32)"

# Origens CORS permitidas (Netlify e seu domínio futuro)
fly secrets set CORS_ORIGINS="https://blaxxpontos.netlify.app,https://blaxxpontos.com,http://localhost:5050"
```

### 2.4 — Deploy

```bash
fly deploy
```

O deploy demora ~3 minutos (build do Docker + push). No final você verá:

```
==> Monitoring deployment
 ✔ [info] Machine 4d8...started
 ✔ [info] Machine 4d8...passed health check on /health

Visit your newly deployed app at https://blaxx-pontos-backend.fly.dev/
```

### 2.5 — Popula dados iniciais (uma vez)

```bash
fly ssh console -C "python seed.py"
```

Saída esperada: 2 usuários demo + 8 parceiros + 10 benefícios + 3 campanhas.

### 2.6 — Confirma que está rodando

```bash
# Healthcheck
curl https://blaxx-pontos-backend.fly.dev/health
# → {"status":"ok","service":"blaxx-pontos-backend"}

# Login com Mariana
curl -X POST https://blaxx-pontos-backend.fly.dev/auth/login \
  -H "Content-Type: application/json" \
  -d '{"email":"mariana@blaxx.com","password":"123456"}'
# → {"token":"...","token_type":"Bearer","user":{...}}
```

---

## Passo 3 — Frontend Netlify aponta pra produção (~5 min)

### 3.1 — Atualiza a URL do backend no JS

Edite `/blaxx/assets/blaxx-config.js`, troque a URL de produção:

```js
window.BLAXX_API = "https://blaxx-pontos-backend.fly.dev";
```

(Se você usou outro nome no `fly launch`, ajuste aqui.)

### 3.2 — Sobe pro Netlify

Se o Netlify já está conectado ao repo:

```bash
cd "/Users/ricardoveles/Library/CloudStorage/Dropbox/Blaxx Pontos/blaxx"
git add assets/blaxx-config.js
git commit -m "chore: aponta para backend de produção no Fly"
git push
```

Se ainda não está conectado:

1. Acesse [app.netlify.com/drop](https://app.netlify.com/drop)
2. Arraste a pasta `/blaxx/` inteira para a página
3. Aguarde upload (~30s) → recebe URL `https://nome-aleatorio.netlify.app`
4. Renomeie em **Site settings → General → Change site name** para
   `blaxxpontos` → URL final fica `blaxxpontos.netlify.app`

### 3.3 — Teste end-to-end

Abra `https://blaxxpontos.netlify.app/cadastro.html`. Preencha o formulário.
Após criar, deve cair em `dashboard.html` logado.

Confirma no Fly que o usuário foi criado:

```bash
fly ssh console
$ python
>>> from app import create_app
>>> from app.models import User
>>> with create_app().app_context():
...     print(User.query.count())
3   # Mariana + Lucas + você
```

---

## Passo 4 — App Mac e iPhone usam o mesmo backend

Já que agora o backend é remoto, ajuste os apps Swift para apontar pra ele:

### Mac app

Antes de rodar pela primeira vez no Xcode, no Console abra a aplicação e
digite (Cmd+Shift+C no Xcode):

```swift
UserDefaults.standard.set("https://blaxx-pontos-backend.fly.dev", forKey: "blaxx_backend_url")
```

Ou edite `API.swift` linha do `baseURL` para usar essa URL como default.

### iPhone (Simulator ou device)

Mesma coisa. Ou adicione uma tela "Configurações → Servidor" mais tarde.

Como o banco é o mesmo do Netlify, o **cadastro feito na web aparece logado
no iPhone e no Mac instantaneamente**.

---

## Operação no dia-a-dia

```bash
# Ver logs em tempo real
fly logs

# Re-deploy depois de mudar código
fly deploy

# Acessar o banco direto via psql (precisa instalar localmente)
fly proxy 5433:5432 -a blaxx-pontos-backend &
psql "postgresql://USER:SENHA@localhost:5433/blaxx"
# (use a string do Neon trocando o host por localhost)

# Reiniciar (raramente necessário)
fly apps restart blaxx-pontos-backend

# Backups do Neon: automáticos, retenção 7 dias no free tier
```

---

## Troubleshooting

**"fly: command not found"** — O Homebrew instalou em `/opt/homebrew/bin/fly`
em Macs Apple Silicon. Adicione ao PATH ou abra um novo Terminal.

**Healthcheck falhando após deploy** — Veja `fly logs` na hora do deploy.
99% das vezes é `DATABASE_URL` errada (cole de novo, certifique que tem
`?sslmode=require` no final pro Neon).

**"connection refused" do frontend** — CORS. Confirme com:
```bash
fly secrets list
```
Tem que ter `CORS_ORIGINS` listando seu domínio Netlify. Se não, set de novo
e `fly deploy`.

**Postgres atingiu o limite** — Free tier do Neon: 0.5GB. O Blaxx usa ~10MB
por 1000 usuários. Quando passar, upgrade para o plano Launch (US$ 19/mês,
10GB).

**Quero apagar tudo e refazer** — `fly apps destroy blaxx-pontos-backend`
e refaça do passo 2.2.

---

## Próximos Sprints (já planejados)

- **Sprint 2** — JWT real + HMAC nos webhooks + rate limiting (1 semana)
- **Sprint 3** — Mercado Pago em sandbox + testes end-to-end (1 semana)
- **Sprint 4** — Termos LGPD revisados + TestFlight (2 semanas)
