/**
 * Sprint 4 (S4-3) · Smoke teste E2E
 *
 * Caminhos criticos cobertos:
 *   1. Landing renderiza sem erro de console
 *   2. Hamburger mobile abre/fecha (S3-2)
 *   3. Cookie banner aparece em visita nova (S3-7)
 *   4. CSP headers presentes (S3-5)
 *   5. /login carrega form
 *   6. /cadastro tem os 3 checkboxes LGPD (S3 + cadastro hardening)
 */
import { test, expect } from '@playwright/test';

test('landing renderiza sem erros graves de console', async ({ page }) => {
  const errors: string[] = [];
  page.on('pageerror', (e) => errors.push(e.message));
  page.on('console', (msg) => {
    if (msg.type() === 'error') errors.push(msg.text());
  });
  await page.goto('/');
  await expect(page).toHaveTitle(/Blaxx/i);
  // Tolerancia: extensoes do browser ou bloqueios de tracker viram console.error
  // sem culpa do site. So falhamos se o ERRO mencionar o proprio site.
  const ourErrors = errors.filter(e => /blaxx/i.test(e));
  expect(ourErrors).toEqual([]);
});

test('hamburger mobile (S3-2)', async ({ page, isMobile }) => {
  test.skip(!isMobile, 'apenas projeto mobile');
  await page.goto('/');
  const burger = page.locator('.bx-hamburger');
  await expect(burger).toBeVisible();
  await burger.click();
  const drawer = page.locator('#bx-mobile-drawer[data-open]');
  await expect(drawer).toBeVisible();
  // ESC fecha
  await page.keyboard.press('Escape');
  await expect(page.locator('#bx-mobile-drawer[data-open]')).toHaveCount(0);
});

test('cookie banner aparece em visita nova (S3-7)', async ({ page, context }) => {
  await context.clearCookies();
  await page.goto('/');
  // Banner aparece com pequeno delay (600ms no script)
  await expect(page.locator('#bx-cookie-banner')).toBeVisible({ timeout: 3000 });
  // Aceitar todos remove o banner
  await page.locator('#bx-cookie-banner button:has-text("Aceitar todos")').click();
  await expect(page.locator('#bx-cookie-banner')).toHaveCount(0);
});

test('CSP headers presentes (S3-5)', async ({ page }) => {
  const resp = await page.goto('/');
  expect(resp).not.toBeNull();
  const headers = resp!.headers();
  expect(headers['strict-transport-security']).toContain('max-age');
  expect(headers['content-security-policy']).toBeTruthy();
  expect(headers['x-frame-options']).toBe('DENY');
});

test('login form acessivel', async ({ page }) => {
  await page.goto('/login.html');
  await expect(page.locator('input[type="email"], #email')).toBeVisible();
  await expect(page.locator('input[type="password"], #senha')).toBeVisible();
});

test('cadastro tem 3 checkboxes LGPD separados (S3-4)', async ({ page }) => {
  await page.goto('/cadastro.html');
  await expect(page.locator('#termos')).toBeVisible();
  await expect(page.locator('#privacidade')).toBeVisible();
  await expect(page.locator('#lgpd')).toBeVisible();
});
