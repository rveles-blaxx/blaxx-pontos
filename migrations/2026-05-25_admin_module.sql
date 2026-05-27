-- Migration: Módulo Admin + VIP · 2026-05-25
-- Aplica em PostgreSQL (Neon).
--
-- Adiciona em users:
--   role        VARCHAR(16) NOT NULL DEFAULT 'user'  ('user' | 'admin')
--   is_vip      BOOLEAN     NOT NULL DEFAULT false   (sem limites diários)

BEGIN;

ALTER TABLE users
    ADD COLUMN IF NOT EXISTS role VARCHAR(16) NOT NULL DEFAULT 'user';

ALTER TABLE users
    ADD COLUMN IF NOT EXISTS is_vip BOOLEAN NOT NULL DEFAULT false;

-- Marca o primeiro usuário (Mariana) como admin pra você ter acesso ao painel.
-- Em produção real, faça isso manualmente com um UPDATE específico.
UPDATE users SET role = 'admin' WHERE email = 'mariana@blaxx.com';

COMMIT;

-- Confirmação:
-- SELECT email, role, is_vip FROM users;
