# E2E Tests · Blaxx Pontos (Sprint 4 / S4-3)

Smoke tests de integracao end-to-end com Playwright.

## Setup

```bash
cd tests/e2e
npm install
npx playwright install --with-deps chromium
```

## Rodar

```bash
# Default: aponta pra prod (https://blaxxpontos.netlify.app)
npx playwright test

# Em dev local
E2E_BASE_URL=http://localhost:8080 npx playwright test

# Headed (ve o browser)
npm run test:headed

# Debug step-by-step
npm run test:debug

# Ver relatorio
npm run report
```

## Cobertura atual

- Landing: console errors, titulo
- Mobile: hamburger menu (S3-2) abre/fecha + ESC
- LGPD: cookie banner (S3-7) aparece e desaparece
- Seguranca: CSP headers (S3-5), HSTS, X-Frame
- Forms: login / cadastro renderizam
- Cadastro: 3 checkboxes LGPD

## Proximas adicoes (Sprint 5)

- Fluxo de cadastro completo (preenche + valida CPF)
- Login + redirect pra dashboard
- Compra de pontos com QR PIX
- Resgate de pontos (validar gate de CPF G:)
- DELETE /account + export (LGPD endpoints)

## CI

Playwright suporta GitHub Actions out-of-the-box. Adicionar
`.github/workflows/e2e.yml` quando o repo for separado.
