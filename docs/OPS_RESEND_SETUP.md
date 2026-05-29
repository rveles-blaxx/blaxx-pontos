# Setup: e-mails reais via Resend

> Status atual: backend em produção (Render) roda com `MAILER=console` — o
> código de verificação só aparece no log e no `_dev_code` do response.
> Pra envio real, troca pra `MAILER=resend`.

Resend oferece **3.000 e-mails/mês grátis** sem cartão de crédito. Suficiente
pra fase de validação e small-scale.

---

## Passo 1 — Criar conta na Resend

1. Acesse <https://resend.com/signup>
2. Cadastre com seu e-mail corporativo (`@blaxxpontos.com.br` se já tiver
   DNS, ou Gmail pessoal pra testar)
3. Confirme o e-mail de verificação

## Passo 2 — Decidir domínio do remetente

### Opção A — Teste rápido (sem DNS próprio)
Usa o subdomínio gratuito `onboarding@resend.dev`. Não precisa configurar
nada. Limitação: o e-mail chega assinado por `resend.dev`, não pela Blaxx —
clientes podem desconfiar.

### Opção B — Domínio próprio (recomendado pra produção)
Mais trabalhoso (10 minutos) mas e-mails saem de `noreply@blaxxpontos.com.br`:

1. Resend Dashboard → **Domains** → **Add Domain**
2. Digite `blaxxpontos.com.br` (ou subdomínio: `mail.blaxxpontos.com.br`)
3. A Resend mostra 4 registros DNS pra adicionar:
   - 1× `MX` (recebe bounces)
   - 1× `TXT` (SPF)
   - 1× `CNAME` ou `TXT` (DKIM)
   - 1× `TXT` (DMARC, opcional mas recomendado)
4. No painel do seu provedor de DNS (Registro.br, Cloudflare, Route53),
   adicione os 4 registros exatamente como mostrado
5. Volte ao Resend → **Verify**. Pode levar até 30min pra propagar
6. Quando ficar verde, o domínio está pronto

## Passo 3 — Gerar API Key

1. Resend Dashboard → **API Keys** → **Create API Key**
2. Nome: `blaxx-render-prod` (ou similar)
3. Permission: **Sending Access** (não precisa `Full Access`)
4. Copie a key — começa com `re_` e tem ~32 caracteres
5. **Anote em local seguro** — Resend não mostra ela de novo

## Passo 4 — Configurar Render

1. <https://dashboard.render.com> → Service `blaxx-pontos-backend` → **Environment**
2. Adicione/atualize 3 variáveis:

   | Key                | Value                                          |
   |--------------------|------------------------------------------------|
   | `MAILER`           | `resend`                                       |
   | `RESEND_API_KEY`   | `re_xxx...` (copiada no passo 3)               |
   | `EMAIL_FROM`       | `Blaxx Pontos <noreply@blaxxpontos.com.br>` (opção B)<br>OU<br>`Blaxx Pontos <onboarding@resend.dev>` (opção A) |

3. Clique **Save Changes** — Render redeploya automaticamente (~2min)

## Passo 5 — Validar funcionamento

Após o redeploy:

```bash
# 1. Health check confirma novo provider ativo
curl https://blaxx-pontos-backend.onrender.com/health
# {"service":"blaxx-pontos-backend","status":"ok","uptime_s":3,...}
```

Logs do Render devem mostrar (ao receber a primeira request com sessão):
```
[MAILER] Inicializado: ResendMailer · from=Blaxx Pontos <noreply@...> · key=re_xxxxx...abcd
```

Pra testar end-to-end:
1. Cadastra um e-mail seu de teste no site
2. No fluxo de "Confirmar e-mail", clica em "Enviar código"
3. Esperado: receber um e-mail real (verifique inbox + spam) com código
   de 6 dígitos
4. Logs Render mostram: `[MAIL Resend] enviado para foo@example.com · id=xxx`

## Troubleshooting

### "Domain not verified" no log
A API key foi gerada antes do domínio estar verificado. Re-checa em
Resend → Domains. Se estiver verde, gere uma nova API key.

### E-mail não chega mas log diz "enviado"
- Verifica spam/lixo eletrônico
- Em Resend Dashboard → **Emails** mostra histórico com status (delivered,
  bounced, complained). Se `bounced`, o e-mail destino rejeitou — problema
  do destinatário, não da Resend.
- Se `delivered` mas usuário não vê, é fundo do filtro spam do provider
  destino (Gmail/Outlook são agressivos). Adicione registro DMARC + SPF
  válido (opção B do passo 2) pra melhorar reputação.

### 429 Rate Limit no Resend
Plano free tier: 100 e-mails/dia, 3k/mês. Se atingir, upgrade pra plano pago
($20/mês — 50k e-mails) ou implementar fila com retry no nosso backend.

## Rollback rápido

Se precisar voltar pra `console` (debug, ou Resend caiu):

1. Render Dashboard → Environment
2. Mude `MAILER` de `resend` pra `console`
3. Save — redeploya em ~2min

Os e-mails voltam a ficar só nos logs + `_dev_code` no response. Nada quebra.

## Custo

| Tier | E-mails/mês | Preço |
|------|-------------|-------|
| Free | 3.000       | R$ 0  |
| Pro  | 50.000      | ~R$ 100/mês |
| Scale | 100.000+   | sob consulta |

Pra blaxxpontos.com.br, free tier deve durar até atingir ~30k usuários
cadastrados (assumindo 1 e-mail por mês por usuário em média).
