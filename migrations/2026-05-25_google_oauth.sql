-- Migration: Google OAuth · 2026-05-25
-- Aplica em PostgreSQL (Neon). Idempotente.
--
-- Mudanças:
--   1. users.password_hash → NULLABLE (usuário Google-only não tem senha)
--   2. users.google_sub    → nova coluna, UNIQUE, nullable (sub do ID token)
--
-- Rodar:
--   psql "$DATABASE_URL" -f migrations/2026-05-25_google_oauth.sql
--
-- Ou pelo painel do Neon: console.neon.tech → seu projeto → SQL Editor.

BEGIN;

-- 1) password_hash aceita NULL agora
ALTER TABLE users
    ALTER COLUMN password_hash DROP NOT NULL;

-- 2) Adiciona coluna google_sub se não existir
ALTER TABLE users
    ADD COLUMN IF NOT EXISTS google_sub VARCHAR(64);

-- 3) Garante unicidade do google_sub (idempotente: só cria se não existe)
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_indexes
        WHERE tablename = 'users'
          AND indexname = 'uq_users_google_sub'
    ) THEN
        EXECUTE 'CREATE UNIQUE INDEX uq_users_google_sub ON users(google_sub) WHERE google_sub IS NOT NULL';
    END IF;
END $$;

COMMIT;

-- Verificação pós-migration (rode separado pra conferir):
-- \d users
-- Deve mostrar:
--   password_hash | varchar(255) |           |          |   (sem NOT NULL)
--   google_sub    | varchar(64)  |           |          |
-- E o índice "uq_users_google_sub" UNIQUE, btree (google_sub) WHERE (google_sub IS NOT NULL).
