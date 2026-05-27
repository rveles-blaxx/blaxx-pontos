# Arquitetura de Segurança · Blaxx Pontos

Documento de referência interno. Descreve os controles de segurança implementados, os trade-offs assumidos, e as ações pendentes. Atualizado em 2026-05-25 (release pós-PIX MP).

> **Audiência**: engenharia (manutenção), produto (decisões de risco), auditoria interna/externa, consultoria de pen-test.
> **Não-audiência**: usuários finais. Para política externa, ver `/termos.html` e `/privacidade.html`.

---

## Sumário

1. [Modelo de ameaças](#1-modelo-de-ameaças)
2. [Identidade e autenticação](#2-identidade-e-autenticação)
3. [Autorização e RBAC](#3-autorização-e-rbac)
4. [Gestão de sessões e tokens](#4-gestão-de-sessões-e-tokens)
5. [Senhas e MFA](#5-senhas-e-mfa)
6. [Login social (Google OAuth)](#6-login-social-google-oauth)
7. [Proteção de endpoints](#7-proteção-de-endpoints)
8. [PIX · Cobrança e webhook MP](#8-pix--cobrança-e-webhook-mp)
9. [Dados sensíveis · CPF, e-mail, telefone, LGPD](#9-dados-sensíveis--cpf-e-mail-telefone-lgpd)
10. [Headers HTTP de segurança](#10-headers-http-de-segurança)
11. [Logs e auditoria](#11-logs-e-auditoria)
12. [Rate limiting e abuse](#12-rate-limiting-e-abuse)
13. [Segredos e configuração](#13-segredos-e-configuração)
14. [Infraestrutura · Fly.io + Neon](#14-infraestrutura--flyio--neon)
15. [Frontend · CSP, XSS, CSRF](#15-frontend--csp-xss-csrf)
16. [Mobile · iOS/Mac](#16-mobile--iosmac)
17. [Resposta a incidentes e pendências](#17-resposta-a-incidentes-e-pendências)

---

## 1. Modelo de ameaças

### Atores

- **Atacante anônimo da internet**: scraping, força bruta, descoberta de endpoints, replay de webhooks.
- **Atacante autenticado (cliente comum)**: tentar escalar privilégios, acessar dados de outros usuários, abusar do programa de pontos.
- **Atacante autenticado (cliente comprometido)**: token JWT vazado por phishing, malware no device, ou shoulder-surfing.
- **Insider malicioso**: acesso ao banco Neon, Fly.io secrets, ou ao código.
- **Atacante na cadeia de suprimentos**: dependência Python/JS comprometida.
- **Fraude financeira**: comprar pontos com cartão clonado, vender pontos, lavagem.

### Ativos protegidos

- Saldo de pontos dos usuários (representação econômica)
- Cobranças PIX em andamento e histórico de pagamentos
- Dados pessoais sob LGPD (CPF, e-mail, telefone, endereço, data de nascimento)
- Credenciais MP (Access Token de produção, webhook secret)
- Chave assinatura JWT
- Credenciais Neon/Fly

### Superfícies de ataque

- API pública (`https://blaxx-pontos-backend.fly.dev/*`)
- Site público (Netlify) — JS, formulários
- Apps Mac/iOS (binário distribuído)
- Webhooks recebidos do Mercado Pago
- Banco PostgreSQL (Neon — acesso via TLS apenas)
- Painel admin (mesmo domínio, endpoints `/admin/*`)

---

## 2. Identidade e autenticação

### Fluxos suportados

1. **E-mail + senha** (`POST /auth/register` → `POST /auth/login`)
2. **Google Sign-In** (`POST /auth/google` com ID token JWT do Google)
3. **MFA opcional** (TOTP — habilitado por usuário via `POST /auth/mfa/setup`)

### Validações no cadastro

- CPF: algoritmo Receita Federal (dígitos verificadores), unicidade no banco.
- E-mail: regex `^[^@\s]+@[^@\s]+\.[^@\s]+$`, normalização lowercase, unicidade.
- Telefone: formato E.164 internacional (`+5511999999999`).
- Data de nascimento: deve resultar em idade ≥ 18 (LGPD).
- Senha: política em §5.

### Verificação de e-mail

- Código de 6 dígitos enviado após cadastro.
- Hash do código armazenado em `email_verifications` (não plaintext).
- TTL: 30 minutos. Tentativas: 5. Após 5 erradas, código é invalidado.
- Operações financeiras (compra/resgate PIX, envio de pontos) bloqueadas até verificação (`@email_verified_required`).

---

## 3. Autorização e RBAC

### Roles atuais

| Role | Capacidades |
|---|---|
| `user` | Próprio saldo, próprias compras, próprias transferências |
| `admin` | Tudo de user + painel admin: confirmar charges manuais, listar usuários, ajustar saldos, ver auditoria |

### Decorators usados

- `@login_required`: exige JWT válido + user existente
- `@email_verified_required`: exige `email_verified_at IS NOT NULL`
- `@admin_required`: exige `user.role == "admin"`
- `@reauth_required` (operações críticas): exige password reauth nos últimos 5 min

### Modelo de objeto

Endpoints que retornam recursos individuais sempre validam `resource.user_id == g.current_user.id` ou role admin. Isso impede IDOR (Insecure Direct Object Reference). Ex: `GET /pix/charge/<id>` retorna 404 se a charge não pertencer ao usuário logado.

---

## 4. Gestão de sessões e tokens

### JWT (Flask-JWT-Extended)

- **Access token**: 15 min, HS256, claim `sub` = user_id (UUID).
- **Refresh token**: 7 dias, HS256, claim `sub` + `jti` único.
- Algoritmo: **HS256** com `JWT_SECRET_KEY` (32+ bytes aleatórios em Fly secrets).
- Header esperado: `Authorization: Bearer <token>`.
- Claims customizados: `role`, `email_verified` (cache anti-DB-hit em hot path).

### Refresh rotation

- A cada `POST /auth/refresh`, **revoga o refresh anterior** (RevokedToken) e emite par novo.
- Detecta reuso: se o refresh já foi revogado e for usado de novo → **invalida toda a família** (suspeita de roubo). Forçar relogin.

### Blacklist (RevokedToken)

- Tabela `revoked_tokens` armazena JTI de tokens explicitamente revogados.
- Verificada em todo `@jwt_required` via hook do flask-jwt-extended.
- Logout chama `POST /auth/logout` que insere o JTI atual.

### Limites

- Não temos hoje **expiração progressiva por inatividade** (sliding session). A access expira em 15min absolutos.
- Não temos **device binding** (token funciona em qualquer browser/IP).

---

## 5. Senhas e MFA

### Hashing

- **Argon2id** (lib `argon2-cffi`), parâmetros padrão da biblioteca (memory_cost=64MB, time_cost=3, parallelism=4).
- Hashes têm formato `$argon2id$v=19$m=65536,t=3,p=4$<salt>$<hash>`.
- **Migração**: usuários antigos com hash bcrypt são re-hashados para Argon2id no próximo login bem-sucedido.

### Política de senha (em cadastro e troca)

- Mínimo 12 caracteres
- Pelo menos: 1 maiúscula, 1 minúscula, 1 dígito, 1 símbolo
- Não pode ser senha comum (lista interna de 200 termos)
- Não pode conter trechos do nome ou e-mail

### MFA TOTP

- Opt-in por usuário em `/perfil/seguranca`.
- Segredo armazenado em `mfa_secrets.secret` (texto — **TODO encriptar com Fernet em Sprint 4**).
- Algoritmo: TOTP RFC 6238, janela ±1.
- Trusted devices: cookie HMAC-assinado, TTL 30 dias.
- Reauth para operações críticas (mudar senha, desativar MFA, sacar PIX): exige re-digitar senha **mesmo se MFA estiver ativo**.

### Forgot password

- `POST /auth/forgot-password` → e-mail com token URL-safe 32 bytes, TTL 30min.
- Token single-use: marcado consumed_at após uso.
- Rate limit: 3/hora por e-mail.
- Não revela se o e-mail existe (mensagem genérica).

---

## 6. Login social (Google OAuth)

### Validação criptográfica do ID token

1. Lib `google-auth` valida: assinatura RSA (JWKS), `exp`, `iss == accounts.google.com`, `aud == GOOGLE_*_CLIENT_ID`.
2. Defensivo extra: backend re-verifica `iss` manualmente.
3. Aceita audience web OU iOS.

### Anti-replay com `nonce`

- Cliente gera nonce aleatório (24 bytes web / 32 chars iOS) e envia ao Google na request OAuth.
- ID token retornado contém `payload.nonce`.
- Cliente envia o nonce junto com `id_token` ao backend.
- Backend compara `payload.nonce == client_nonce`. Mismatch → 401.

### Anti-CSRF (apenas iOS — fluxo PKCE)

- Web usa **fluxo de id_token implícito** (GIS popup) — não suscetível a CSRF.
- iOS usa **fluxo authorization code + PKCE**: parâmetro `state` aleatório validado no callback.

### Linkage de contas

- Se existe `User.google_sub == sub` → login direto.
- Senão, se existe `User.email == email` → linka (atribui google_sub).
- Senão, cria novo User + Wallet, CPF placeholder `G:<sub[:12]>` (usuário completa depois).

### email_verified obrigatório

- Backend rejeita ID tokens onde Google atestou `email_verified=false`.

### Audit trail

- `audit_logs` recebe `google_login_ok` / `google_login_failed` / `google_login_nonce_mismatch`.
- `social_accounts` tabela rastreia provider + provider_user_id + avatar + e-mail.

---

## 7. Proteção de endpoints

### Categorização

| Categoria | Auth | Email verif | Rate limit |
|---|---|---|---|
| Pública (health, packages) | — | — | 30/min |
| Auth (login, register) | — | — | 5–10/min |
| Cliente comum (wallet, charges) | JWT | sim | 60/min |
| Financeiro crítico (transfer, redeem) | JWT + reauth (5min) | sim | 10/min |
| Admin | JWT + role=admin | — | sem limite no painel, log em todas ações |
| Webhook PIX | HMAC | — | 60/min/IP |

### Erros padronizados

- 400: input inválido
- 401: auth ausente ou inválida
- 403: auth válida mas sem permissão
- 404: recurso não encontrado (também usado quando ownership não bate, pra não vazar existência)
- 422: validação semântica (ex: saldo insuficiente)
- 429: rate limit
- 500: erro inesperado

### Inputs

- Todos os endpoints validam content-type, parsing seguro de JSON (`request.get_json(silent=True)`).
- Strings normalizadas: e-mail lowercase, telefone E.164, CPF apenas dígitos.
- Limites de tamanho: nome 200 chars, e-mail 180, descrição PIX 255.

---

## 8. PIX · Cobrança e webhook MP

### Fluxo de cobrança

1. Cliente autenticado faz `POST /pix/charge` com `{package}` ou `{amount_brl}`.
2. Backend cria `pix_charges` em estado `PENDING` com TXID UUID.
3. Backend chama `MercadoPago.create_payment` com `external_reference=txid`.
4. MP devolve `br_code` + `qr_code_image` (base64).
5. Backend persiste e retorna ao frontend.

### Validações no servidor

- Mínimo R$ 10, máximo R$ 100k (não-VIP).
- TTL da charge: 30 minutos.
- `external_reference` (nosso TXID) usado como chave de idempotência.

### Webhook MP

Cadeia de validação no `POST /pix/webhook`:

1. **Rate limit** 60/min por IP.
2. **IP whitelist** (opcional, `PIX_WEBHOOK_ALLOWED_IPS`).
3. **HMAC-SHA256** sobre `id:<data.id>;request-id:<x-request-id>;ts:<ts>;` com `MP_WEBHOOK_SECRET` (constant-time compare).
4. **Anti-replay**: `ts` deve estar dentro de ±5 min do agora (`MP_WEBHOOK_MAX_CLOCK_SKEW`).
5. **Re-consulta** o pagamento na API do MP (`GET /v1/payments/{id}`) — não confia no body do webhook.
6. **Status check**: só processa se `payment.status == "approved"`.
7. **Idempotência**: `wallet_svc.credit(idempotency_key=f"charge:{charge.id}")` impede crédito duplo.

### Operações de payout (resgate PIX)

- Atualmente fluxo manual com confirmação admin (`/admin/redeem-requests`).
- Próximo: integração MP payout, mas exige permissão extra na conta MP (PJ).

---

## 9. Dados sensíveis · CPF, e-mail, telefone, LGPD

### Inventário

| Dado | Tabela | Armazenamento | Acesso |
|---|---|---|---|
| CPF | `users.cpf` | plaintext (UniqueConstraint) | self + admin |
| E-mail | `users.email` | plaintext lowercase | self + admin |
| Telefone | `users.phone` | E.164 plaintext | self + admin |
| Nascimento | `users.birth_date` | DATE | self + admin |
| Endereço | `user_profiles` | plaintext | self + admin |
| Hash senha | `users.password_hash` | Argon2id | nunca exposto |
| Secret TOTP | `mfa_secrets.secret` | plaintext (TODO encriptar) | sistema |
| Refresh JTI | `revoked_tokens.jti` | plaintext | sistema |

### LGPD

- **Consentimento**: tabela `user_consents` rastreia opt-in/out por categoria (termos, marketing, comunicação).
- **Direito ao acesso**: endpoint `GET /perfil/dados-pessoais` devolve tudo.
- **Direito ao apagamento**: `DELETE /perfil/conta` apaga User + Wallet + Transactions vinculadas (cascade). Audit log preservado por 5 anos (obrigação fiscal).
- **Retenção**: conforme política de privacidade — dados ativos enquanto a conta existir, históricos financeiros 5 anos pós-encerramento.

### Mascaramento

- API nunca devolve `password_hash`, `mfa_secrets.secret`, `cpf` completo em listagens (usa `xxx.xxx.xxx-12`).
- Em logs: e-mail e CPF aparecem mascarados (`j****@gmail.com`, `xxx.xxx.xxx-12`).

---

## 10. Headers HTTP de segurança

Aplicados em produção pelo middleware (`app/__init__.py`):

| Header | Valor | Por quê |
|---|---|---|
| `Strict-Transport-Security` | `max-age=63072000; includeSubDomains` | HSTS força HTTPS |
| `X-Content-Type-Options` | `nosniff` | Impede MIME sniffing |
| `X-Frame-Options` | `DENY` | Sem iframe (anti-clickjacking) |
| `Referrer-Policy` | `strict-origin-when-cross-origin` | Não vaza URL completa |
| `Content-Security-Policy` | ver §15 | Restringe origens JS/CSS |
| `Permissions-Policy` | `camera=(), microphone=(), geolocation=()` | Bloqueia APIs sensitivas |

### CORS

- Origens permitidas configuradas em `CORS_ORIGINS` (env): `https://blaxxpontos.netlify.app`, dev local.
- `Access-Control-Allow-Credentials: true` (necessário pro JWT em header).
- Métodos permitidos: GET, POST, DELETE, PATCH.

---

## 11. Logs e auditoria

### Audit log estruturado (`audit_logs`)

Schema: `(id, user_id, action, payload_json, ip, user_agent, created_at)`.

Ações rastreadas:

- `login_ok`, `login_failed`, `logout`
- `password_change`, `password_reset_requested`, `password_reset_consumed`
- `google_login_ok`, `google_login_failed`, `google_login_nonce_mismatch`
- `email_verified`
- `mfa_enabled`, `mfa_disabled`, `mfa_challenge_failed`
- `wallet_credit`, `wallet_debit`, `transfer_sent`, `transfer_received`
- `pix_charge_created`, `pix_payment_confirmed`, `pix_webhook_invalid`
- `admin_action_*` (qualquer ação do painel admin)

### Logs aplicacionais

- `current_app.logger` → stdout (capturado pelo Fly).
- Níveis: DEBUG (dev), INFO (operacional), WARNING (suspeito), ERROR (falha).
- Sem PII em logs (e-mail/CPF/telefone mascarados).

### Retenção

- Audit log: 5 anos (obrigação fiscal / LGPD art. 16).
- Logs Fly stdout: 7 dias (padrão Fly).

---

## 12. Rate limiting e abuse

### Implementação

- `Flask-Limiter` com backend Redis (Fly Upstash) em prod, in-memory em dev.
- Key padrão: IP do cliente (via `X-Forwarded-For` quando atrás de proxy Fly).

### Limites por endpoint

| Endpoint | Limite |
|---|---|
| `POST /auth/login` | 10/min/IP |
| `POST /auth/register` | 3/hora/IP |
| `POST /auth/forgot-password` | 3/hora/IP + 3/hora/email |
| `POST /auth/google` | 20/min, 100/hora |
| `POST /pix/charge` | 30/hora/user |
| `POST /pix/custom-charge` | 10/hora/user |
| `POST /pix/webhook` | 60/min/IP |
| `POST /transfer/send` | 5/min/user, 20/hora/user |
| `POST /redeem` | 10/hora/user |
| `GET /*` em geral | 100/min/IP |

### Bloqueios

- 5 logins falhos seguidos no mesmo e-mail → `locked_until = now + 15min`.
- Após 3 períodos de lockout consecutivos → escala pra admin (notification).

---

## 13. Segredos e configuração

### Inventário de secrets em Fly

| Nome | Uso |
|---|---|
| `DATABASE_URL` | Postgres Neon (TLS) |
| `SECRET_KEY` | Flask session cookie signing |
| `JWT_SECRET_KEY` | HS256 JWT |
| `CORS_ORIGINS` | Lista de origens permitidas |
| `MP_ACCESS_TOKEN` | Mercado Pago Access Token (PROD) |
| `MP_WEBHOOK_SECRET` | HMAC do webhook MP |
| `MP_NOTIFICATION_URL` | URL do webhook (usada no create_payment) |
| `PIX_PROVIDER` | "mercadopago" ou "mock" |
| `PIX_WEBHOOK_SECRET` | HMAC fallback genérico (não-MP) |
| `GOOGLE_WEB_CLIENT_ID` | Audience aceita |
| `GOOGLE_IOS_CLIENT_ID` | Audience aceita |
| `MAILER_API_KEY` | Resend/SES |

### Política

- **Nunca** commitar `.env` no git (gitignore enforced).
- **Nunca** logar valores de secrets.
- **Rotação**: tokens MP e webhook secret rotacionados a cada 90 dias OU após qualquer vazamento suspeito.
- Acesso ao painel Fly: apenas Owner. Org membros que não precisam, removidos.

### Local dev

- `.env.example` no repo com placeholders. Cada dev cria `.env` local.
- Senhas/tokens de dev (Neon dev branch, MP sandbox) são diferentes de prod.

---

## 14. Infraestrutura · Fly.io + Neon

### Fly.io (compute)

- App: `blaxx-pontos-backend`, região `gru` (São Paulo).
- 2 máquinas, autoscale 0-2 (cold start aceitável).
- TLS terminação no edge Fly. Certificate via Let's Encrypt auto-renew.
- Healthcheck `GET /health` a cada 30s.

### Neon (Postgres)

- Conexão via `DATABASE_URL` com TLS obrigatório (`sslmode=require`).
- Pool de conexões via `psycopg` + SQLAlchemy.
- Backup automático diário (Neon retention 7 dias no plano atual — pendente upgrade pra 30d).
- Branch dev separada da prod.

### Domínios

- Backend: `blaxx-pontos-backend.fly.dev` (Fly subdomain — TODO migrar pra `api.blaxxpontos.com.br`).
- Frontend: `blaxxpontos.netlify.app` (Netlify — TODO migrar pra `app.blaxxpontos.com.br`).
- TLS via Let's Encrypt em ambos.

### Limitações conhecidas

- Sem WAF na frente (Fly não oferece nativo). Mitigação: rate limit + IP whitelist em endpoints sensíveis.
- Sem CDN nas APIs (Fly edge faz roteamento, não cache).

---

## 15. Frontend · CSP, XSS, CSRF

### Content Security Policy

```
default-src 'self';
script-src 'self' https://accounts.google.com https://apis.google.com 'unsafe-inline';
style-src 'self' https://fonts.googleapis.com 'unsafe-inline';
font-src 'self' https://fonts.gstatic.com;
img-src 'self' data: https://lh3.googleusercontent.com https://api.mercadopago.com;
connect-src 'self' https://blaxx-pontos-backend.fly.dev https://accounts.google.com https://oauth2.googleapis.com;
frame-src https://accounts.google.com;
```

- `'unsafe-inline'` em script e style é uma concessão temporária (estilos inline + GIS callback). TODO: migrar pra nonces.

### XSS

- Todo render de dados de usuário usa `textContent` (não `innerHTML`).
- Quando precisa de HTML (ex: rich descriptions de campanhas), passa por DOMPurify (TODO: hoje confiamos no admin que cadastra).
- Sanitização de URL antes de redirect: `https://` obrigatório, allowlist de domínios pro PIX bank-open.

### CSRF

- API usa JWT em header `Authorization` — não cookies — então CSRF clássico (cookie auto-enviado em forms) não aplica.
- Formulários do site Netlify (cadastro, login) usam POST com JSON, não form-encoded.
- Para mutações via cookie de sessão (caso futuro), usar `SameSite=Strict` + token CSRF duplo no header.

### Storage

- `sessionStorage` para tokens (cleared ao fechar tab — mais seguro que localStorage).
- Não há tokens em cookies (logo, não há flag HttpOnly aplicável).

### Service Worker

- Cacheia HTML/CSS/JS estáticos. Versão `blaxx-v3-pix-mp` (bumpada manualmente em cada release).
- Nunca cacheia endpoints `/auth`, `/wallet`, `/pix`, `/transfer`, `/redeem`, `/health`.

---

## 16. Mobile · iOS/Mac

### Armazenamento

- Token JWT: **Keychain** (`kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly`).
- User cache (SwiftData): plaintext local, sem dados sensíveis críticos (sem CPF, sem senha).
- Refresh token: também Keychain, separado do access.

### Pinning

- TLS pinning **não implementado**. Confiamos no trust store iOS + Let's Encrypt. (TODO Sprint 5).

### Google Sign-In

- `ASWebAuthenticationSession` com `prefersEphemeralWebBrowserSession=false` (compartilha cookie Google).
- PKCE S256 + state + nonce (§6).
- Custom URL scheme reverso do Client ID iOS.

### Distribuição

- Apple Developer Personal Team (sem capacidades de Push/iCloud — limitação conhecida).
- Notarização **pendente** pra distribuição via DMG.

### Atualização forçada

- App envia `X-App-Version` header em todo request. Backend compara com `MIN_APP_VERSION` (env) — se cliente abaixo, retorna 426 e telas mostram "Atualize o app".

---

## 17. Resposta a incidentes e pendências

### Plano de resposta (alto nível)

1. **Detecção**: logs Fly + audit_logs + alerta UptimeRobot.
2. **Triagem**: severidade definida em P0 (data leak / fraude ativa), P1 (login bug, payment down), P2 (degradação).
3. **Contenção**:
   - P0: revogar todos JWTs (bump `JWT_SECRET_KEY`), pausar webhook (toggle config), notificar usuários afetados.
   - P1: rollback Fly (`fly deploy --image <previous>`).
4. **Erradicação**: patch, deploy.
5. **Recuperação**: validar com pequena % de tráfego, depois 100%.
6. **Postmortem**: blameless, em até 5 dias úteis.

### Contatos

- DPO (LGPD): a definir, hoje Ricardo (fundador).
- Comunicação ANPD em vazamento de dados: até 2 dias úteis (art. 48 LGPD).

### Pendências priorizadas

| # | Item | Severidade | Prazo alvo |
|---|---|---|---|
| 1 | Encriptar `mfa_secrets.secret` com Fernet | ALTA | Sprint 4 |
| 2 | Migrar custom domains com cert pinning | MÉDIA | Sprint 5 |
| 3 | TLS pinning iOS | MÉDIA | Sprint 5 |
| 4 | Migração CSP sem `unsafe-inline` (usar nonce por request) | MÉDIA | Sprint 5 |
| 5 | DPO formalmente nomeado + treinamento LGPD | ALTA | até 90 dias |
| 6 | Configurar `PIX_WEBHOOK_ALLOWED_IPS` com IPs publicados pelo MP | BAIXA | Sprint 4 |
| 7 | Pen test externo formal | ALTA | antes de 1.000 usuários ativos |
| 8 | Backup retention upgrade Neon (7d → 30d) | MÉDIA | quando passar de R$ 10k/mês em PIX |
| 9 | WAF (Cloudflare na frente do Fly) | BAIXA | quando passar de 100 RPS sustentado |
| 10 | SIEM/observability (Datadog ou similar) | BAIXA | quando o time > 3 devs |
| 11 | Sub-resource integrity (SRI) em scripts externos (GIS) | BAIXA | Sprint 5 |

### Boas práticas operacionais

- Toda mudança em endpoint financeiro PRECISA de teste pytest cobrindo o caso feliz + 1 negativo.
- Nenhum item do backlog pode ser fechado sem teste funcional + evidência (screenshot/log) anexada à task na planilha de backlog.
- Code review obrigatório (2 olhos) para mudanças em `app/api/auth.py`, `app/api/pix.py`, `app/services/wallet.py`, `app/security.py`.

---

**Última revisão**: 2026-05-25 · pós-deploy PIX MP em produção · auditoria webhook concluída.

**Próxima revisão programada**: 2026-08-25 (trimestral).
