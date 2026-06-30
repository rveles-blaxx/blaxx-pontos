# Sprint 3 · Integração Mercado Pago PIX

> 🟢 **Nota de infra (27/06/2026):** os comandos `fly secrets set … -a
> blaxx-pontos-backend` e URLs `*.fly.dev` deste runbook são **legado**. Hoje a
> produção é **Render** (`blaxx-pontos-exe.onrender.com`, repo
> `rveles-blaxx/blaxx-pontos`): defina as env vars do MP no painel Render e use
> `…onrender.com/pix/webhook` como `MP_NOTIFICATION_URL`. Lembrando: por ora o
> PIX **fica em mock** (homologação) — só ligar MP por decisão do dono do produto
> (ver `LAUNCH_PENDING_CREDENTIALS.md` §2.2).

Substitui o `MockPixProvider` por integração real com o **Mercado Pago**,
permitindo que o app aceite pagamento PIX de verdade em sandbox e
posteriormente em produção.

---

## O que muda na arquitetura

```
ANTES (Mock)                          DEPOIS (Mercado Pago)
─────────────────                     ──────────────────────────
[Usuário]                             [Usuário]
   ↓                                     ↓
[Frontend]                            [Frontend]
   ↓ POST /pix/charge                    ↓ POST /pix/charge
[Backend Flask]                       [Backend Flask]
   ↓                                     ↓ POST api.mercadopago.com/v1/payments
[MockPixProvider]                     [MercadoPagoPixProvider]
   ↓ gera BR Code fake                   ↓ MP retorna BR Code real
   ↓                                     ↓
   ↓ usuário aperta                      ↓ usuário paga no banco
   "simular pagamento"                   ↓
   ↓                                     ↓ MP envia webhook
[purchase_svc.confirm_payment]        [POST /pix/webhook]
   ↓ credita pontos                      ↓ valida HMAC + busca payment
                                         ↓ se status=approved →
                                         purchase_svc.confirm_payment
                                         credita pontos
```

---

## Passo 1 — Criar conta de desenvolvedor

1. Acesse **https://www.mercadopago.com.br/developers**
2. Clique em **Login** e use sua conta MP normal (ou crie uma se ainda não tem)
3. Após logar, vai em **Suas integrações** (menu superior direito)
4. Clique em **Criar aplicação**
   - Nome: `Blaxx Pontos`
   - Tipo de produto: **CheckoutAPI** (pagamentos online)
   - Você integra plataforma ou marketplace? **Não, eu integro um site próprio**
   - Solução de pagamento: **Pagamentos online**
5. Após criar, anote o **ID da aplicação** (precisa para webhook depois)

---

## Passo 2 — Obter credenciais de teste

Dentro da aplicação criada:

1. Menu lateral → **Credenciais de teste**
2. Vai aparecer duas chaves:
   - **Public Key**: começa com `TEST-...` (usada no frontend para SDK do MP — não precisamos por enquanto)
   - **Access Token**: começa com `TEST-...` (usada no backend para criar pagamentos)
3. **Copie o Access Token de teste** — esse é o `MP_ACCESS_TOKEN`

---

## Passo 3 — Configurar o webhook no painel MP

1. Menu lateral → **Webhooks**
2. Clique em **Configurar notificações**
3. Modo: **Produção** (mesmo em sandbox, deixa Produção)
4. URL: `https://blaxx-pontos-backend.fly.dev/pix/webhook`
5. Eventos: marcar **`Pagamentos`** apenas (suficiente pro nosso caso)
6. Clique em **Salvar configuração**
7. Após salvar, MP gera uma **chave secreta** para assinar os webhooks.
   Anote — é o `MP_WEBHOOK_SECRET`.

---

## Passo 4 — Setar segredos no Fly.io

No Terminal do seu Mac:

```bash
cd "/Users/ricardoveles/Library/CloudStorage/Dropbox/Blaxx Pontos/blaxx_app/backend"

# Token (TEST-...) que você copiou no Passo 2
fly secrets set MP_ACCESS_TOKEN="TEST-1234567890..." -a blaxx-pontos-backend

# Secret do webhook que apareceu no Passo 3
fly secrets set MP_WEBHOOK_SECRET="abc123def456..." -a blaxx-pontos-backend

# URL pra MP postar quando pagamento for confirmado
fly secrets set MP_NOTIFICATION_URL="https://blaxx-pontos-backend.fly.dev/pix/webhook" -a blaxx-pontos-backend

# Switch que ativa o provider
fly secrets set PIX_PROVIDER="mercadopago" -a blaxx-pontos-backend
```

---

## Passo 5 — Deploy do código atualizado

```bash
fly deploy -a blaxx-pontos-backend
```

Esperado: build limpa, healthcheck OK, logs mostrando `PIX provider: MercadoPago`.

Confirme nos logs:

```bash
fly logs -a blaxx-pontos-backend | grep "PIX provider"
```

Tem que aparecer:

```
[2026-...] INFO in __init__: PIX provider: MercadoPago
```

---

## Passo 6 — Teste end-to-end sandbox

### 6.1 — Login + criar charge

```bash
# Aquece a VM
curl https://blaxx-pontos-backend.fly.dev/health

# Login
TOKEN=$(curl -sX POST https://blaxx-pontos-backend.fly.dev/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"email":"mariana@blaxx.com","password":"123456"}' | jq -r '.token')

echo "Token: $TOKEN"

# Cria cobrança PIX real
curl -X POST https://blaxx-pontos-backend.fly.dev/pix/charge \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"package":"start"}'
```

Esperado: JSON com `br_code` real (começa com `00020126...`), `txid` (vai ser o
`payment_id` numérico do MP) e `qr_code_image` (data URI base64).

### 6.2 — Pagamento de teste

O Mercado Pago sandbox precisa de **conta comprador de teste** para
simular o pagamento. Veja:

1. Volta no painel MP: **Suas integrações → Contas de teste**
2. Clique em **Criar conta de teste**
   - Tipo: **Comprador**
   - País: **Brasil**
   - Anote o e-mail e senha gerados
3. Abra outro navegador (ou aba anônima) e faça login com essa conta de
   teste em **https://www.mercadopago.com.br**
4. Pegue o `br_code` que o passo 6.1 retornou e cole no app **PIX → Pagar com QR Code**
   da conta de teste (ou use o `ticket_url` que o MP fornece na resposta)
5. Pague — em sandbox é instantâneo

### 6.3 — Confirmação via webhook

Após o pagamento, MP envia POST `/pix/webhook` automaticamente. Confere
nos logs:

```bash
fly logs -a blaxx-pontos-backend | tail -30
```

Tem que aparecer linhas de:

- `POST /pix/webhook 200`
- Possivelmente warnings sobre signature se a chave estiver errada

### 6.4 — Verifica que pontos foram creditados

```bash
curl https://blaxx-pontos-backend.fly.dev/wallet/ \
  -H "Authorization: Bearer $TOKEN"
```

`balance_pts` deve ter aumentado em 2.000 (pacote start).

---

## Troubleshooting

### "MercadoPago: resposta sem qr_code"

O MP rejeitou alguma coisa da requisição. Veja `fly logs -a blaxx-pontos-backend`
e procure por `MercadoPago POST → HTTP 4xx`. Geralmente é:
- CPF mal formatado (precisa ter 11 dígitos sem ponto/traço)
- Email vazio ou inválido
- `transaction_amount` zerado ou negativo
- Token de teste expirado (gera novo no painel MP)

### Webhook chega mas pontos não creditam

Olha os logs:
- "HMAC inválido" → `MP_WEBHOOK_SECRET` está errado. Volta no painel MP,
  copia a secret de novo e seta no Fly.
- "data.id ausente" → o webhook não é do tipo `payment`. Confere as
  notificações marcadas no painel MP.
- "external_reference ausente no payment" → o pagamento foi feito sem
  passar pelo nosso endpoint (talvez paymentlink externo). Não credita —
  comportamento correto.

### "status != approved"

O webhook chega antes do pagamento ser efetivado (MP envia notificações
em vários estados: `pending`, `in_process`, `approved`). Apenas
`approved` credita pontos. Aguarda o webhook seguinte.

### Quero voltar pro Mock pra desenvolver

```bash
fly secrets unset PIX_PROVIDER -a blaxx-pontos-backend
# OU explicitamente:
fly secrets set PIX_PROVIDER="mock" -a blaxx-pontos-backend
fly deploy -a blaxx-pontos-backend
```

---

## Para ir a Produção

Quando estiver pronto pra cobrar de verdade:

1. **CNPJ + KYC**: o Mercado Pago exige conta empresa com CNPJ + documentos
   para liberar token de produção. Vai em **Configurações → Documentos**.
2. **Habilitar PIX no recebimento**: vai em **Cobrar → Receber via PIX** e
   completa o cadastro. Cadastra a chave PIX que recebe o dinheiro.
3. **Trocar credenciais**: no painel MP, **Credenciais de produção**. Pega o
   `APP_USR-...` token (não confundir com TEST-).
4. **Atualiza segredo no Fly**:
   ```bash
   fly secrets set MP_ACCESS_TOKEN="APP_USR-..." -a blaxx-pontos-backend
   fly deploy -a blaxx-pontos-backend
   ```
5. **Webhook em produção**: no painel MP → Webhooks, configura uma
   notificação de **produção** apontando para a mesma URL. Gera nova
   secret e atualiza no Fly.
6. **Taxas**: ~0,99% por transação PIX recebida + R$ 0,40 fixo. Sai
   ~R$ 0,60 por pacote Start (R$ 19,90). Margem: 95%.

---

## Sobre payout (resgate via PIX)

O `MercadoPagoPixProvider` **não implementa payout** (saída de dinheiro
do MP pra conta do usuário) porque MP cobra licença separada pra isso.

Solução recomendada: usar **Efí Bank** especificamente pra payout. No
construtor:

```python
from app.pix.efi import EfiBankPixProvider   # quando você criar
provider = MercadoPagoPixProvider(
    access_token=MP_TOKEN,
    payout_provider=EfiBankPixProvider(...)
)
```

Por enquanto, resgates ficam desabilitados em produção (retornam
`failure_reason: "MercadoPago não habilitado pra payout"`) e os pontos
voltam pra wallet via REFUND automático.

Quando integrar o Efí, faremos no Sprint 5.
