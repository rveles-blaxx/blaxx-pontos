-- Migration: Google OAuth — versão SQLite · 2026-05-25
--
-- SQLite NÃO suporta:
--   - ALTER TABLE ALTER COLUMN ... DROP NOT NULL
--   - DO $$ ... END $$ (PL/pgSQL)
--   - CREATE UNIQUE INDEX ... WHERE x IS NOT NULL (sintaxe diferente)
--
-- Workaround: ADD COLUMN funciona, mas relaxar NOT NULL do password_hash
-- exige recriar a tabela (CREATE TABLE ... SELECT ... DROP ... RENAME).
--
-- Para PostgreSQL (Neon prod), use 2026-05-25_google_oauth.sql.
--
-- Rodar:
--   sqlite3 instance/blaxx.db < migrations/2026-05-25_google_oauth_sqlite.sql

BEGIN TRANSACTION;

-- 1) Adiciona coluna google_sub (idempotente: falha silenciosa se já existir
-- — por isso usamos um sub-SELECT que verifica)
-- SQLite não tem "IF NOT EXISTS" pra coluna, então usamos pragma:
-- ATENÇÃO: o script falha se rodar 2x. Use Caminho A (rm + recreate) se for o caso.

ALTER TABLE users ADD COLUMN google_sub VARCHAR(64);

-- 2) Index único parcial (SQLite 3.8+ suporta sintaxe similar)
CREATE UNIQUE INDEX IF NOT EXISTS uq_users_google_sub
    ON users(google_sub)
    WHERE google_sub IS NOT NULL;

-- 3) Recria a tabela users sem o NOT NULL no password_hash.
-- SQLite não tem ALTER COLUMN — precisa copiar tudo pra outra tabela.

CREATE TABLE users_new (
    id VARCHAR(32) PRIMARY KEY NOT NULL,
    name VARCHAR(120) NOT NULL,
    email VARCHAR(180) UNIQUE NOT NULL,
    cpf VARCHAR(14) UNIQUE NOT NULL,
    password_hash VARCHAR(255),       -- AGORA NULLABLE
    pix_key VARCHAR(180),
    google_sub VARCHAR(64) UNIQUE,    -- já criado acima
    email_verified_at DATETIME,
    terms_accepted_version VARCHAR(10),
    terms_accepted_at DATETIME,
    password_changed_at DATETIME NOT NULL,
    created_at DATETIME NOT NULL
);

INSERT INTO users_new
SELECT id, name, email, cpf, password_hash, pix_key, google_sub,
       email_verified_at, terms_accepted_version, terms_accepted_at,
       password_changed_at, created_at
FROM users;

DROP TABLE users;
ALTER TABLE users_new RENAME TO users;

-- Recria índices que existiam antes
CREATE UNIQUE INDEX IF NOT EXISTS uq_users_email ON users(email);
CREATE UNIQUE INDEX IF NOT EXISTS uq_users_cpf ON users(cpf);
CREATE UNIQUE INDEX IF NOT EXISTS uq_users_google_sub ON users(google_sub) WHERE google_sub IS NOT NULL;

COMMIT;

-- Verificação:
-- .schema users
-- Deve mostrar password_hash sem NOT NULL e google_sub presente.
