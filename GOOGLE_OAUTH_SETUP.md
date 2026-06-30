# Setup Google Login · Runbook (10 min)

> 🟢 **Nota de infra (27/06/2026):** onde o runbook diz para setar secrets no
> **Fly.io** (`fly secrets set …`), faça no **Render** (env vars do serviço
> `blaxx-pontos-exe`, repo `rveles-blaxx/blaxx-pontos`). O domínio autorizado no
> Google deixa de ser `*.fly.dev` e passa a `blaxx-pontos-exe.onrender.com` /
> `blaxxpontos.com.br`.

Você precisa criar **2 OAuth Client IDs** no Google Cloud (1 para web, 1 para iOS/Mac).
Sem isso o botão "Entrar com Google" não funciona. É grátis.

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
