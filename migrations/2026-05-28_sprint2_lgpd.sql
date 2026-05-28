-- ============================================================================
-- Sprint 2 hardening · 2026-05-28 · LGPD + MFA encryption
-- ============================================================================
-- Aplique apos deployar o backend novo. Idempotente via IF NOT EXISTS
-- (Postgres). Em SQLite usa CASE-INSENSITIVE.
--
-- Mudancas:
--   1. users.is_deleted + users.deleted_at (soft-delete LGPD art. 18)
--   2. mfa_secrets.secret VARCHAR(128) -> VARCHAR(512) (Fernet ciphertext)
--
-- IMPORTANTE: secrets MFA existentes (legacy plaintext) sao tratados como
-- texto claro pelo decrypt_secret e re-cifrados na proxima chamada de
-- /mfa/setup. Pra forcar re-cifra em massa, rode o script abaixo (Python).
-- ============================================================================

-- 1. User: campos soft-delete (LGPD art. 18)
ALTER TABLE users ADD COLUMN IF NOT EXISTS is_deleted BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE users ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMP;

-- 2. MfaSecret: aumentar coluna pra Fernet ciphertext
--    Postgres: ALTER TYPE
ALTER TABLE mfa_secrets ALTER COLUMN secret TYPE VARCHAR(512);
--    SQLite: ALTER TYPE nao existe — schema reflete via SQLAlchemy.

-- 3. TxType.EXPIRE — em Postgres adicionar valor ao enum
-- (Sprint 2 cron de expiracao). Em SQLite o ENUM e' VARCHAR, ja aceita.
DO $$
BEGIN
    ALTER TYPE txtype ADD VALUE IF NOT EXISTS 'expire';
EXCEPTION
    WHEN undefined_object THEN NULL;  -- enum nao existe (DB recem-criado)
    WHEN duplicate_object THEN NULL;  -- ja adicionado
END $$;
