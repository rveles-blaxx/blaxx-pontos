# Construir o Postgres do Render do zero (a partir do app local)

> Objetivo: subir um **Postgres novo** para o serviço canônico **`blaxx-pontos`**
> (hoje caído/vazio) e populá-lo com o conteúdo do banco local do app
> (`instance/blaxx.db`, ~53k linhas / dados de homologação-load-test).
> **Não toca no `blaxx-pontos-exe`** (o vivo, com usuários reais).

Data: 2026-07-14. Validado ponta-a-ponta contra um Postgres real (embutido) antes
de escrever este runbook.

---

## Fatos que moldam o procedimento (leia antes)

1. **O schema NÃO vem do Alembic num banco virgem.** A migration `0001` é um
   **no-op** e as `0002`–`0005` têm guarda `if tabela not in tables: return`. Só
   a `0006`/`0007` fazem create/alter. Ou seja, `alembic upgrade head` sozinho
   **não cria as tabelas base**. O schema canônico nasce de **`db.create_all()`**
   (a partir de `app/models.py`) — foi assim que o `-exe` foi criado.
   → Provável causa do "Exited with status 1" do `blaxx-pontos`: banco vazio, sem
   schema, porque em produção `create_app()` pula `create_all()`.

2. **Correção de schema aplicada** (aprovada nesta sessão): `transactions.
   idempotency_key` passou de `String(64)` → `String(128)` (model + migration
   `0007`). O resgate de benefício gera chave de ~73 chars e estourava em
   qualquer Postgres (`StringDataRightTruncation`). Com o model já em 128, o
   `create_all` cria a coluna certa e os dados entram intactos.

3. **Enums nativos**: `create_all` cria tipos ENUM nativos no Postgres
   (`txtype`, `txstatus`, `pixchargestatus`, `pixpayoutstatus`, `cardchargestatus`).
   A cópia de dados insere as strings e o Postgres casa com os labels (mesma
   origem: as classes Python que geraram os dados). Testado: OK.

4. **Dados**: 25 tabelas, **53.107 linhas**. ~5003 usuários são sintéticos
   (`load.NNNNN@homolog.blaxx.test`) + 258 partners Livelo reais. Sem dados
   pessoais reais.

---

## Pré-requisitos do lado do operador (você, no dashboard do Render)

Só você tem acesso ao Render. Os passos 1–4 são seus; o passo 5 (cópia) posso
rodar eu, se você me passar a connection string externa.

### 1. Provisionar o Postgres
- Render → **New → PostgreSQL** (ou reaproveitar um Neon). Região igual à do
  serviço (`oregon`). Anote as duas URLs:
  - **Internal Database URL** (para o serviço, mesma rede) →
    `postgresql://user:pass@dpg-xxx/blaxx` (sem `?sslmode`).
  - **External Database URL** (para rodar a cópia de fora) →
    `postgresql://user:pass@xxx.oregon-postgres.render.com/blaxx` — **acrescente
    `?sslmode=require`** ao usar de fora.

### 2. Ligar a env var no serviço `blaxx-pontos`
No serviço canônico → Settings → Environment:
- `DATABASE_URL` = **Internal** Database URL do passo 1.
- `SECRET_KEY`, `JWT_SECRET_KEY` = `python -c "import secrets;print(secrets.token_hex(32))"` (um cada).
- `PIX_PROVIDER=mock` **por enquanto** (mantém o `env_schema` leniente; troca
  para `mercadopago` só depois, com os segredos do MP — ver
  `SESSAO_2026-07-14…md §4.1`).
- `FLASK_ENV=production` (já é o default do `render.yaml`).

### 3. Fazer deploy do código
- Garanta que o serviço aponta para o repo/branch com o backend novo
  (`eed08a6` + as mudanças desta sessão: model 128 + migration `0007`).
- O deploy roda `alembic upgrade head` (preDeploy) — inofensivo no banco vazio —
  e sobe o gunicorn. O app **boota mesmo sem tabelas** (o autoseed de partners
  tem `try/except`). Confirme que subiu: `GET /healthz` → 200.

### 4. Criar o schema + carimbar o Alembic (Render → Shell do serviço)
No **Shell** do serviço `blaxx-pontos` (Settings → Shell), rode:

```bash
# cria TODAS as tabelas + tipos enum a partir dos models (idempotente)
python -c "from app import create_app; from app.extensions import db; \
app=create_app(); ctx=app.app_context(); ctx.push(); db.create_all(); \
print('create_all OK')"

# marca o banco como estando na head (0007) p/ os próximos deploys
alembic stamp head
```

Verifique:
```bash
python -c "from app import create_app; from app.extensions import db; \
app=create_app(); ctx=app.app_context(); ctx.push(); \
print(sorted(db.metadata.tables))"          # deve listar 26 tabelas (inclui card_charges)
```

> A partir daqui o banco já é utilizável (schema completo, vazio ou só com os 258
> partners auto-semeados). Se você **só quisesse schema limpo**, poderia parar
> aqui. Como a decisão foi **clonar os dados locais**, siga para o passo 5.

### 5. Copiar os dados (SQLite local → Postgres do Render)
Isto roda da máquina onde está o `instance/blaxx.db` (este Mac). O script está em
`scripts/migrate_sqlite_to_pg.py` e já foi validado.

```bash
# venv com sqlalchemy + psycopg (Python 3.9+ serve)
python -m venv .venv-mig && . .venv-mig/bin/activate
pip install "SQLAlchemy>=2.0" "psycopg[binary]>=3.2"

# dry-run (não escreve nada) — confere leitura/coerção/contagens
python scripts/migrate_sqlite_to_pg.py --sqlite instance/blaxx.db --dry-run

# cópia real (schema já criado no passo 4). --truncate torna re-executável.
python scripts/migrate_sqlite_to_pg.py \
  --sqlite instance/blaxx.db \
  --pg "postgresql://user:pass@xxx.oregon-postgres.render.com/blaxx?sslmode=require" \
  --truncate
```

O script:
- reflete o schema do Postgres (fonte da verdade), copia as colunas em comum na
  ordem FK-safe, coage `Boolean`/`DateTime`, reseta sequences e imprime um
  relatório origem×destino que deve fechar em **53.107**;
- `--on-overflow` (default `fail`) não é necessário porque a coluna já é 128.

### 6. Conferir no Postgres
```sql
SELECT count(*) FROM users;          -- 5004
SELECT count(*) FROM transactions;   -- 15938
SELECT max(length(idempotency_key)) FROM transactions;  -- 73 (intacto)
SELECT count(*) FROM wallets w LEFT JOIN users u ON w.user_id=u.id WHERE u.id IS NULL; -- 0
```

---

## Notas / decisões

- **Idempotência**: o passo 5 com `--truncate` pode ser repetido à vontade.
- **Se o serviço não der Shell** (ex.: plano sem Shell): rode o passo 4 como um
  "Job" one-off, ou temporariamente adicione `db.create_all()` num
  `preDeployCommand`, ou rode o passo 4 da sua máquina apontando `DATABASE_URL`
  para a **External** URL (precisa das deps do app instaladas localmente).
- **Segurança**: a connection string é segredo. Se for me passar a **External
  URL** para eu rodar o passo 5, prefira colá-la num arquivo (ex.:
  `scratchpad/pg_url.txt`) a deixá-la no chat; e **rotacione a senha do
  Postgres** depois da carga.
- Depois de validado, decidir o destino do serviço `blaxx-pontos-exe` e do
  domínio de API (`window.BLAXX_API`) — fora do escopo deste runbook.
```
