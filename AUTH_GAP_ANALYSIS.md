# Auth · Gap Analysis (Especificação vs. Realidade)

Comparação ponto a ponto do que você pediu vs. o que já existe no sistema.
Status: ✅ pronto · 🟡 parcial · ❌ falta · ⚠️ atenção (precisa decidir trade-off)

---

## Bloco 1 · Cadastro de usuário

### Campos
| Campo | Status |
|---|---|
| Nome completo | ✅ User.name |
| CPF | ✅ User.cpf (string sem máscara) |
| Data de nascimento | ❌ adicionar User.birth_date |
| E-mail | ✅ User.email (lowercase) |
| Celular | ❌ adicionar User.phone (E.164) |
| Senha | ✅ User.password_hash (bcrypt — substituir por Argon2id) |
| Confirmação de senha | 🟡 valida só no frontend; backend não precisa receber 2 vezes |
| Aceite Termos | 🟡 User.terms_accepted_version existe, mas não rastreia separadamente |
| Aceite Política Privacidade | ❌ adicionar campo separado |
| Consentimento LGPD | ❌ adicionar tabela user_consents para histórico versionado |
| Código de indicação | ❌ adicionar User.referral_code + User.referred_by |

### Validações
| Validação | Status |
|---|---|
| Nome ≥ 2 palavras | ❌ atualmente: ≥ 3 chars |
| CPF válido matematicamente | ✅ `_valid_cpf()` em auth.py |
| CPF único | ✅ UNIQUE constraint |
| E-mail válido (regex) | ✅ `_EMAIL_RX` |
| E-mail único | ✅ UNIQUE constraint |
| Celular válido | ❌ |
| Celular único | ❌ |
| Data nascimento válida | ❌ |
| ≥ 18 anos | ❌ |
| Senha forte | ✅ `validate_password_strength()` — mas atualmente 8 chars; precisa subir para 10 |
| Aceite obrigatório | 🟡 frontend pede, backend não força |
| Sanitização inputs | ✅ `.strip()` + lowercase |
| Normalização E.164 | ❌ adicionar `phonenumbers` lib |

### Política de senha
| Item | Status |
|---|---|
| ≥ 10 chars | 🟡 atualmente 8 — subir |
| Maiúscula + minúscula + número + símbolo | ✅ |
| Bloquear senhas comuns | ✅ COMMON_PASSWORDS set |
| Bloquear igual a email/CPF/nome | 🟡 verifica nome e email; falta CPF e telefone |
| Argon2id ou bcrypt | 🟡 atualmente werkzeug bcrypt — migrar para Argon2id |
| Nunca em texto puro | ✅ |

---

## Bloco 2 · Verificação de e-mail

| Item | Status |
|---|---|
| Token único | ✅ EmailVerification.code (6 dígitos atualmente) |
| Expira em 30min | ✅ |
| Enviar e-mail | 🟡 ConsoleMailer mocked — precisa integrar SES/Mailgun |
| Não revelar duplicado | ✅ anti-enumeração ativa |
| Reenvio com rate limit | ✅ |
| Marcar verificado após clique | 🟡 atualmente código 6 dígitos, não link |
| Bloquear operações financeiras | ✅ `@email_verified_required` |

**Decisão necessária:** manter código 6 dígitos OU migrar para link (UX diferente).

---

## Bloco 3 · Login

| Item | Status |
|---|---|
| Email ou CPF | ✅ |
| Senha | ✅ |
| MFA/TOTP | ❌ |
| Trusted devices | ❌ |
| Anti-enumeração | ✅ "credenciais inválidas" |
| Hash seguro | ✅ |
| Bloquear conta suspensa | ❌ não há campo User.status ainda |
| Bloquear N tentativas | ❌ não conta falhas |
| Rate limit IP/user/device | 🟡 só por IP |
| Log IP/UA/timestamp | ❌ adicionar tabela login_attempts |
| Access token curto | 🟡 atualmente 24h (relaxado pra MVP) — pra 15min |
| Refresh token | ✅ existe |
| Rotacionar refresh | ❌ |
| Logout com revogação | ✅ RevokedToken |

---

## Bloco 4 · Sessão e tokens

| Item | Status |
|---|---|
| Access 15min | 🟡 24h atualmente |
| Refresh 7-30d | ✅ 30d |
| Armazenar seguro | 🟡 atualmente em sessionStorage (XSS-vulnerable); migrar para httpOnly cookie |
| HttpOnly Secure SameSite | ❌ |
| Blacklist | ✅ |
| Rotação refresh | ❌ |
| Logout global | ❌ |
| Expiração inativa | ❌ |
| Reauth para crítica | ❌ |

---

## Bloco 5 · Recuperação de senha

| Item | Status |
|---|---|
| Solicitação por email | ✅ |
| Mensagem genérica | ✅ |
| Token expira 15-30min | ✅ 30min |
| Rate limit | ✅ |
| Tela nova senha | ✅ ForgotPasswordView |
| Confirmação | ✅ |
| Revogar sessões | ❌ |
| Notificar usuário | 🟡 email mockado |

---

## Bloco 6 · Segurança obrigatória

| Item | Status |
|---|---|
| HTTPS | ✅ Fly.io + Netlify forçam |
| CSRF | ❌ |
| CORS restritivo | ✅ |
| CSP | ❌ |
| HSTS | ❌ |
| X-Frame-Options | ✅ Netlify _headers tem |
| X-Content-Type-Options | ✅ |
| Referrer-Policy | ✅ |
| Permissions-Policy | ❌ |
| Rate limiting | ✅ |
| Brute force | 🟡 só rate limit, sem lock progressivo |
| Credential stuffing | ❌ |
| SQL Injection | ✅ SQLAlchemy ORM previne |
| XSS | 🟡 nenhum innerHTML; sem CSP |
| IDOR | 🟡 endpoints filtram por user_id; sem testes |
| Session fixation | ❌ |
| User enumeration | ✅ |

---

## Bloco 7 · Banco de dados

| Tabela | Status |
|---|---|
| users | 🟡 existe — adicionar phone, birth_date, status, locked_until, failed_login_attempts, last_login_at |
| user_profiles | ❌ |
| user_consents | ❌ |
| email_verification_tokens | ✅ EmailVerification |
| password_reset_tokens | ✅ PasswordResetToken |
| refresh_tokens | ❌ atualmente só blacklist; criar tabela própria com rotação |
| login_attempts | ❌ |
| trusted_devices | ❌ |
| audit_logs | ❌ |
| social_accounts | ❌ atualmente só User.google_sub; criar tabela própria |

---

## Bloco 12 · Google OAuth (NOVO bloco que pediu)

| Item | Status |
|---|---|
| Botão "Continuar com Google" | ✅ Web + Mac/iOS |
| Dark mode | 🟡 GSI renderiza com theme outline; falta dark variant |
| OAuth 2.0 + OIDC | ✅ |
| PKCE | ✅ Mac/iOS (Auth Code) |
| Authorization Code (não implicit) | ✅ |
| Nome + email + Google ID | ✅ |
| Foto perfil | ❌ não captura avatar_url |
| Cadastro automático | ✅ |
| Provider = GOOGLE | 🟡 implícito (google_sub != NULL); criar campo explícito |
| email_verified = true | ✅ |
| Aceite LGPD inicial | ❌ não registra automaticamente |
| Wallet automático | ✅ |
| Vincular email existente | ✅ |
| State parameter | ✅ Mac/iOS; ❌ Web (GSI faz internamente) |
| Nonce | ✅ Mac/iOS |
| Audience | ✅ |
| Issuer | ✅ |
| Signature | ✅ google-auth verify |
| Replay protection | ✅ nonce + state |
| Backend valida tokens | ✅ |
| JWT interno (não Google token) | ✅ |
| Tabela social_accounts | ❌ |
| Campos auth_provider, google_id, avatar_url | ❌ |
| Google One Tap | ❌ |
| Logout revoga interno | ✅ |
| Logout opcional do Google | ❌ |

---

## Plano de implementação (ordenado por dependência)

1. **PRIMEIRO:** resolver HTTP 500 atual (precisa fly logs)
2. Argon2id + nova política senha (10 chars)
3. Novos campos User: phone, birth_date, status, failed_login_attempts, locked_until, last_login_at, auth_provider, avatar_url
4. Tabelas: user_consents, refresh_tokens, login_attempts, trusted_devices, audit_logs, social_accounts
5. Migration completa Neon
6. Refresh token rotation + HttpOnly cookies + access 15min
7. Login: lock progressivo + login_attempts log
8. MFA TOTP
9. Headers segurança (CSP, HSTS, Permissions-Policy)
10. Frontend: refazer cadastro (+phone, birthdate, consents), telas MFA, sessão expirada, conta bloqueada
11. Mac/iOS: idem
12. 21+16 testes pytest

**Tempo realista:** 5-7 dias úteis com escopo completo.
