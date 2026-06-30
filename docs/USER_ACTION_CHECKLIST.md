# Checklist de Ações do Usuário · Produção BlaXx Pontos

> Itens que exigem acesso a contas externas, decisões de produto/negócio ou
> compras. Organizados por urgência e duração estimada.
> **Gerado automaticamente em 2026-06-30 após a análise de pendências.**

## 🔴 P0 — Bloqueia uso seguro (HOJE)

### Decisão #2 — PIX_PROVIDER: real ou mock?

**Estado atual:** `PIX_PROVIDER=mercadopago` em produção. Apps mostram selos
"demo/homologação" mas o backend **cobra dinheiro real** via MercadoPago.

**Opções:**

| Opção | Como fazer | Implicação |
|-------|-----------|------------|
| (a) Voltar pra mock | Render dashboard → Environment → `PIX_PROVIDER` = `mock` → Save | Apps demo voltam a 100% homologação. Sem dinheiro real. |
| (b) Aceitar como prod | Remover todos os selos "demo" dos apps + revisar legal (P0 jurídico) | App vira fintech real. Precisa PLD/FT, DPO etc antes. |

**Recomendação:** (a) até P0 jurídico fechar.

---

### Render #3 — Conectar conta `rveles-blaxx` ao Render (opcional, agora resolvido via workflow)

**Estado:** Render observa `RVELES/blaxx-pontos` (fork). A action `mirror-to-fork.yml`
que acabei de criar resolve isso — toda merge em canonical reflete no fork.

**Setup obrigatório (1 vez):**
1. https://github.com/settings/tokens → Generate new token (classic)
   - Scopes: `repo` (apenas)
   - Expiration: 90 dias (anote pra renovar)
   - Copie o token
2. https://github.com/rveles-blaxx/blaxx-pontos/settings/secrets/actions
   → New repository secret
   - Name: `FORK_PUSH_TOKEN`
   - Value: cole o token
3. Pronto — próximo merge em canonical dispara o mirror automaticamente.

---

## 🟠 P1 — Antes do primeiro release público

### Sentry #4 — Observability de erros

**Por quê:** App em prod sem Sentry = você só descobre bugs quando o usuário reclama.

**Setup:**
1. https://sentry.io → criar conta gratuita (5k events/mês grátis)
2. New Project → Python → Flask → nome `blaxx-pontos-backend`
3. Copiar DSN (formato `https://abc123@o123.ingest.sentry.io/456`)
4. Render → Environment → adicionar:
   - Key: `SENTRY_DSN`
   - Value: `<DSN copiado>`
5. Save → deploy automático. `env_schema` valida o formato.

---

### Apple #5+#6 — Push Notifications + APNS Key

**Por quê:** iOS não recebe push hoje (capability não provisionada + APNS key ausente).

**Setup (precisa de Apple Developer Program ativo — $99/ano):**

1. https://developer.apple.com/account/resources/identifiers/list
   → Achar App ID `Blaxx-Pontos-Inc.BlaxxPontos`
   → Edit → marcar **Push Notifications**
2. https://developer.apple.com/account/resources/profiles/list
   → Re-gerar provisioning profile pro app
3. https://developer.apple.com/account/resources/authkeys/list
   → Create Key → marcar **Apple Push Notifications service (APNs)**
   → Download o `.p8` (só baixa UMA VEZ — guarde com cuidado)
   → Anote `Key ID` (ex: `ABC123DEF4`) e seu `Team ID` (canto direito superior)
4. Backend — Render → Environment → adicionar:
   - `APNS_KEY_ID` = `ABC123DEF4`
   - `APNS_TEAM_ID` = `XXXXXXXXXX`
   - `APNS_BUNDLE_ID` = `Blaxx-Pontos-Inc.BlaxxPontos`
   - `APNS_KEY_FILE` = conteúdo do `.p8` (cole inteiro)
   - `APNS_ENV` = `production` (ou `sandbox` pra TestFlight)
5. Em Xcode: reabrir o projeto, sincronizar, build novamente.

---

### Firebase #7 — FCM para Android

**Por quê:** Android não recebe push hoje. Build até funciona porque `google-services.json`
fake foi colocado, mas FCM real não está vinculado.

**Setup:**
1. https://console.firebase.google.com → Add project → `blaxx-pontos`
2. Registrar app Android:
   - Package name: `com.blaxx.pontos` (conferir em `app/build.gradle.kts`)
   - SHA-1: rodar `./gradlew signingReport` localmente, copiar SHA-1 do debug
3. Download `google-services.json` → substituir
   `blaxx_android/app/google-services.json`
4. Cloud Messaging → Generate new private key (para o backend usar HTTP v1)
   - Download JSON da service account
   - Backend — Render → Environment:
     - `FCM_SERVICE_ACCOUNT_JSON` = conteúdo inteiro do JSON
5. Rebuild Android: `./gradlew assembleDebug`

---

### Android #8 — Keystore real para release

**Estado:** Só existe `keystore.properties.example`. Sem keystore real,
não dá pra fazer release signed pra Play Console.

**Setup:**
```bash
cd "blaxx_android"

# Gera keystore (responde Java prompts — nome, org, etc)
keytool -genkey -v -keystore app/blaxx-release.jks \
  -keyalg RSA -keysize 2048 -validity 25000 \
  -alias blaxx-release

# Cria keystore.properties (gitignored — guarde a senha em 1Password!)
cat > app/keystore.properties <<EOF
storeFile=blaxx-release.jks
storePassword=<SENHA-FORTE-AQUI>
keyAlias=blaxx-release
keyPassword=<SENHA-FORTE-AQUI>
EOF

# Adiciona ao .gitignore (se ainda não tiver)
echo "app/blaxx-release.jks" >> .gitignore
echo "app/keystore.properties" >> .gitignore

# Backup do .jks num local seguro — perder = não poder publicar updates!
```

---

## 🟡 P2 — Antes de listar nas lojas

### Apple Wallet #9 — Certificados Pass

1. https://developer.apple.com/account/resources/identifiers/list/passTypeId
   → Register a Pass Type ID
   - Description: `BlaXx Card`
   - Identifier: `pass.com.blaxx.cartao`
2. Edit → Create Certificate → upload CSR (gerado via Keychain Access)
   → Download `.cer`
3. Keychain Access → exportar como `.p12` (com senha)
4. Backend — Render → Environment:
   - `APPLE_WALLET_PASS_TYPE_ID` = `pass.com.blaxx.cartao`
   - `APPLE_WALLET_TEAM_ID` = (mesmo do APNS)
   - `APPLE_WALLET_P12_BASE64` = `base64 -i cert.p12`
   - `APPLE_WALLET_P12_PASSWORD` = senha do .p12

---

### App Store Connect #10 — Registrar app

1. https://appstoreconnect.apple.com → My Apps → +
   - Bundle ID: `Blaxx-Pontos-Inc.BlaxxPontos`
   - SKU: `blaxx-pontos-ios`
   - Primary language: PT-BR
2. App Information → preencher (metadata em `blaxx_app/fastlane/metadata/pt-BR/`)
3. Users and Access → Keys → API Key (para Fastlane)
   - Download `.p8`
   - Anote Key ID + Issuer ID
4. Localmente: `cp ~/Downloads/AuthKey_*.p8 ~/.fastlane/`
5. `fastlane match init` (se for usar match)

---

### Play Console #11

1. https://play.google.com/console → Create app
   - Default language: PT-BR
   - App or game: App
   - Free
2. Service Account para Fastlane:
   - Google Cloud Console → IAM → Service Accounts → Create
   - Role: nenhuma (vai dar permissão no Play Console)
   - Create key (JSON) → download
3. Play Console → Setup → API access → Link Google Cloud → Grant access ao service account
   - Role: "Release Manager"
4. Salvar JSON em `blaxx_android/fastlane/play-service-account.json` (gitignored)
5. Data Safety form: copiar conteúdo de `PLAY_DATA_SAFETY.md` (já preparado)

---

## 🟢 P3 — Sustainability ops

### Render #13 — Proteger /metrics

```
Render → Environment:
  METRICS_USER = blaxx-metrics
  METRICS_PASS = <gere com: openssl rand -hex 24>
```

Depois: `curl -u blaxx-metrics:SENHA https://blaxx-pontos-exe.onrender.com/metrics`
deve retornar prometheus format. Conecte ao Grafana Cloud (grátis até 10k metrics).

---

### Render #16 — Upgrade pro Starter ($7/mo)

**Por quê:** Plano Free hiberna após 15min sem tráfego — cold start de 30-60s
no primeiro request. Starter = sempre ligado + 0.5 CPU + 512MB.

Render dashboard → Settings → Instance Type → Update → Starter.

---

### KYC #17 — Provider real

Opções nacionais (compatíveis com nosso stack):

| Provider | Pricing | Forte em |
|----------|---------|----------|
| Idwall | R$ 0.50-2 / consulta | KYC + KYB + biometria + COAF lists |
| Caf | sob consulta | KYC + onboarding |
| Unico | R$ 1-3 / consulta | Biometria facial líder de mercado |

Para BlaXx Pontos (volume baixo inicial), **Idwall** é o mais flexível.

Setup:
1. https://idwall.co/contato → demo gratuito
2. Receber API key
3. Render → `IDWALL_API_KEY`
4. Substituir `app/services/kyc.py` validate_cpf por chamada Idwall (manter
   fallback para BrasilAPI quando Idwall não responder).

---

## Resumo numérico

Total de itens: 14 ações do usuário
- 🔴 P0: 2 (decisão PIX, setup FORK_PUSH_TOKEN)
- 🟠 P1: 4 (Sentry, Apple Push+APNS, Firebase, Android keystore)
- 🟡 P2: 3 (Apple Wallet, App Store, Play Console)
- 🟢 P3: 5 (METRICS auth, Render upgrade, KYC vendor, Sentry projects per stack, backup secrets)

Tempo total estimado: ~6-12 horas de configuração + $99/ano Apple + $25 Play Console único.
