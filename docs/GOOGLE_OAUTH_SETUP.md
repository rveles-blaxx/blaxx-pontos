# Setup Google Login · Runbook (10 min)

> 🟢 **Nota de infra (27/06/2026):** onde o runbook diz para setar secrets no
> **Fly.io** (`fly secrets set …`), faça no **Render** (env vars do serviço
> `blaxx-pontos-exe`, que deploya do **fork** `RVELES/blaxx-pontos`, não do
> canônico `rveles-blaxx/blaxx-pontos`). O domínio autorizado no
> Google deixa de ser `*.fly.dev` e passa a `blaxx-pontos-exe.onrender.com` /
> `blaxxpontos.com.br`.

> ## ✅ Já existe — não recrie (confirmado em 02/08/2026)
>
> O BlaXx usa **dois projetos** no Google Cloud: o client **Web** vive no
> `602998235238` e o **iOS/Mac** no `105341431878`. Os Client IDs são
> **públicos por design** (viajam no bundle JS e no `Info.plist` do app),
> então ficam registrados aqui:
>
> | Client | ID | Onde é consumido |
> |---|---|---|
> | **Web** | `602998235238-ab43odgkvqjph1l0tgu8n49iafgkrcke` | `blaxx/assets/blaxx-config.js` → `window.BLAXX_GOOGLE_CLIENT_ID` |
> | **iOS/Mac** | `105341431878-3msc2p3tjk3p5ro6i34d0b0qks3nf9dj` | `blaxx_app/.../Info.plist` + `GoogleAuthService.swift` |
>
> ⚠️ **A armadilha que isto custou:** o default WEB do backend apontava para
> `1086156839608-779t8vpo…`, de **outro projeto**, onde nenhum client do BlaXx
> existe. Como `/auth/google` só aceita ID token cujo `aud` esteja na lista, o
> login web devolvia `401 Token Google inválido` para todos — e nenhum teste
> pegava, porque a suíte usa client IDs fictícios. Corrigido em `app/config.py`
> e fixado por `tests/test_google_oauth.py::test_17`, que compara o valor real
> do `blaxx-config.js` com os defaults do backend.
>
> **Não é preciso setar `GOOGLE_WEB_CLIENT_ID`/`GOOGLE_IOS_CLIENT_ID` no
> Render** — os defaults do código já são os corretos. Setar continua possível
> e sobrepõe (útil para um segundo ambiente).

O passo a passo abaixo serve para **criar do zero** (novo ambiente, ou se os
clients forem revogados). Você precisa de **2 OAuth Client IDs** (1 web, 1
iOS/Mac). É grátis.

---

## 1 · Criar projeto Google Cloud (1 min)

1. Acesse https://console.cloud.google.com
2. Topo da tela, clique no seletor de projetos → **"Novo projeto"**
3. Nome do projeto: **`Blaxx Pontos`**
4. Organização: deixe vazio
5. **Criar**

Aguarde 30s e selecione o projeto recém-criado no topo.

---

## 2 · Configurar OAuth Consent Screen (3 min)

1. Menu lateral → **APIs e serviços** → **Tela de permissão OAuth**
2. Tipo de usuário: **Externo** → **Criar**

**Informações do app:**
- Nome do app: **`Blaxx Pontos`**
- E-mail de suporte: seu Gmail
- Logo do app: opcional — pode subir `preview_appicon.png` depois (mas é obrigatório se quiser sair do modo Test)

**Domínio do app:**
- Página inicial: `https://blaxxpontos.netlify.app`
- Política de privacidade: `https://blaxxpontos.netlify.app/seguranca`
- Termos de serviço: `https://blaxxpontos.netlify.app/termos`

**Domínios autorizados:**
- `netlify.app`
- `fly.dev`

**E-mail de contato do desenvolvedor:** seu Gmail

→ **Salvar e continuar**

**Escopos:** clique em **Adicionar ou remover escopos**, marque:
- `.../auth/userinfo.email`
- `.../auth/userinfo.profile`
- `openid`

→ **Atualizar** → **Salvar e continuar**

**Usuários de teste:** adicione SEU PRÓPRIO Gmail. (Enquanto o app está em modo Test, só esses e-mails conseguem entrar. Funciona para nós no MVP.)

→ **Salvar e continuar** → **Voltar para o painel**

---

## 3 · Criar OAuth Client ID — WEB (2 min)

1. Menu lateral → **APIs e serviços** → **Credenciais**
2. **Criar credenciais** → **ID do cliente OAuth**
3. Tipo de aplicativo: **Aplicativo da Web**
4. Nome: **`Blaxx Web (Netlify)`**

**Origens JavaScript autorizadas** (adicione AS 3):
```
https://blaxxpontos.netlify.app
http://localhost:8080
http://127.0.0.1:5050
```

**URIs de redirecionamento autorizados:**
```
https://blaxxpontos.netlify.app/login
https://blaxxpontos.netlify.app/cadastro
http://localhost:8080/login
```

→ **Criar**

→ Aparece um modal com **ID do cliente** e **Segredo do cliente**.
→ **COPIE O ID DO CLIENTE** (formato: `123456789-abc...apps.googleusercontent.com`). Esse é PÚBLICO.
→ **COPIE O SEGREDO**. Esse é PRIVADO — só vai no backend.

---

## 4 · Criar OAuth Client ID — iOS/Mac (2 min)

1. **Criar credenciais** novamente → **ID do cliente OAuth**
2. Tipo de aplicativo: **iOS**
3. Nome: **`Blaxx iOS/Mac`**
4. ID do pacote (Bundle ID): **`com.blaxx.BlaxxPontos`**
   - Confira no Xcode: `BlaxxPontos.xcodeproj` → target → General → Bundle Identifier.
   - Se for diferente, use o que estiver lá.

→ **Criar**

→ Modal aparece com **ID do cliente** (formato: `123456789-xyz...apps.googleusercontent.com`)
→ **COPIE O ID DO CLIENTE iOS**. Não há segredo nesse tipo — iOS não precisa.

---

## 5 · Me passa os 3 valores

Cola na conversa, exatamente nesse formato:

```
GOOGLE_WEB_CLIENT_ID=123456789-abc...apps.googleusercontent.com
GOOGLE_WEB_CLIENT_SECRET=GOCSPX-xxxxxxxxxxxxxx
GOOGLE_IOS_CLIENT_ID=123456789-xyz...apps.googleusercontent.com
```

Eu então:
1. Adiciono o `WEB_CLIENT_ID` no `blaxx-app.js` do site (público, sem problema)
2. Adiciono o `WEB_CLIENT_ID` no backend para validar tokens vindos do site
3. Adiciono o `IOS_CLIENT_ID` no backend para validar tokens vindos do app
4. Adiciono o `GOOGLE_WEB_CLIENT_SECRET` como secret no Fly.io
   (`fly secrets set GOOGLE_WEB_CLIENT_SECRET=...`)
5. Adiciono o `IOS_CLIENT_ID` no `BlaxxPontos.entitlements` (pra OAuth callback)

---

## 6 · Quando quiser sair do modo Test (futuro)

Enquanto o app estiver em **Test**, só os e-mails que você cadastrou na Step 2 conseguem
entrar. Funciona pra MVP. Pra abrir pra todo mundo:

1. Tela de permissão OAuth → **Publicar app**
2. Google pode pedir verificação (logo, política de privacidade real, vídeo demo)
3. Aprovação demora ~2 semanas — não faça antes do GA.

---

## Custos

Tudo aqui é **grátis** até 100.000.000 de logins/mês. Sem cartão de crédito necessário.
