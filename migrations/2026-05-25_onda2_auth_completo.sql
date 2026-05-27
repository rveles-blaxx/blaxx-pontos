-- Migration Onda 2 · Auth refactor completo · 2026-05-25
-- Aplica em PostgreSQL (Neon). Idempotente.
--
-- Adiciona em users: phone, birth_date, avatar_url, auth_provider, status,
--                    failed_login_attempts, locked_until, last_login_at,
--                    mfa_enabled, privacy_accepted_at, lgpd_accepted_at, updated_at
--
-- Cria tabelas: login_attempts, audit_logs, trusted_devices, refresh_tokens,
--               user_consents, social_accounts, mfa_secrets, user_profiles
--
-- Para SQLite local: use `rm instance/blaxx.db && python3 run.py && python3 seed.py`
-- (SQLite recria do zero com os models novos).

BEGIN;

-- ===== users: novos campos =====

ALTER TABLE users
    ADD COLUMN IF NOT EXISTS phone VARCHAR(20) UNIQUE,
    ADD COLUMN IF NOT EXISTS birth_date TIMESTAMP,
    ADD COLUMN IF NOT EXISTS avatar_url VARCHAR(500),
    ADD COLUMN IF NOT EXISTS auth_provider VARCHAR(16) NOT NULL DEFAULT 'email',
    ADD COLUMN IF NOT EXISTS status VARCHAR(16) NOT NULL DEFAULT 'active',
    ADD COLUMN IF NOT EXISTS failed_login_attempts INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS locked_until TIMESTAMP,
    ADD COLUMN IF NOT EXISTS last_login_at TIMESTAMP,
    ADD COLUMN IF NOT EXISTS mfa_enabled BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS privacy_accepted_at TIMESTAMP,
    ADD COLUMN IF NOT EXISTS lgpd_accepted_at TIMESTAMP,
    ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP NOT NULL DEFAULT NOW();

-- ===== login_attempts =====

CREATE TABLE IF NOT EXISTS login_attempts (
    id VARCHAR(32) PRIMARY KEY,
    user_id VARCHAR(32) REFERENCES users(id) ON DELETE SET NULL,
    email_attempted VARCHAR(180) NOT NULL,
    ip VARCHAR(64),
    user_agent VARCHAR(500),
    success BOOLEAN NOT NULL DEFAULT FALSE,
    reason VARCHAR(120),
    created_at TIMESTAMP NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_login_attempts_email ON login_attempts(email_attempted);
CREATE INDEX IF NOT EXISTS ix_login_attempts_ip ON login_attempts(ip);
CREATE INDEX IF NOT EXISTS ix_login_attempts_created ON login_attempts(created_at);

-- ===== audit_logs =====

CREATE TABLE IF NOT EXISTS audit_logs (
    id VARCHAR(32) PRIMARY KEY,
    user_id VARCHAR(32) REFERENCES users(id) ON DELETE SET NULL,
    event VARCHAR(64) NOT NULL,
    ip VARCHAR(64),
    user_agent VARCHAR(500),
    device_id VARCHAR(64),
    status VARCHAR(16) NOT NULL DEFAULT 'ok',
    reason VARCHAR(255),
    correlation_id VARCHAR(64),
    extra_data VARCHAR(1000),
    created_at TIMESTAMP NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_audit_logs_user ON audit_logs(user_id);
CREATE INDEX IF NOT EXISTS ix_audit_logs_event ON audit_logs(event);
CREATE INDEX IF NOT EXISTS ix_audit_logs_created ON audit_logs(created_at);

-- ===== trusted_devices =====

CREATE TABLE IF NOT EXISTS trusted_devices (
    id VARCHAR(32) PRIMARY KEY,
    user_id VARCHAR(32) NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    device_id VARCHAR(64) NOT NULL,
    name VARCHAR(120),
    ip VARCHAR(64),
    last_used_at TIMESTAMP NOT NULL DEFAULT NOW(),
    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMP NOT NULL,
    CONSTRAINT uq_trusted_device UNIQUE (user_id, device_id)
);

-- ===== refresh_tokens (rotação) =====

CREATE TABLE IF NOT EXISTS refresh_tokens (
    id VARCHAR(32) PRIMARY KEY,
    user_id VARCHAR(32) NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_hash VARCHAR(64) UNIQUE NOT NULL,
    device_id VARCHAR(64),
    ip VARCHAR(64),
    user_agent VARCHAR(500),
    parent_id VARCHAR(32),
    expires_at TIMESTAMP NOT NULL,
    revoked_at TIMESTAMP,
    created_at TIMESTAMP NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_refresh_tokens_user ON refresh_tokens(user_id);
CREATE INDEX IF NOT EXISTS ix_refresh_tokens_expires ON refresh_tokens(expires_at);

-- ===== user_consents =====

CREATE TABLE IF NOT EXISTS user_consents (
    id VARCHAR(32) PRIMARY KEY,
    user_id VARCHAR(32) NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    type VARCHAR(20) NOT NULL,
    version VARCHAR(10) NOT NULL,
    accepted_at TIMESTAMP NOT NULL DEFAULT NOW(),
    ip VARCHAR(64)
);
CREATE INDEX IF NOT EXISTS ix_user_consents_user_type ON user_consents(user_id, type);

-- ===== social_accounts =====

CREATE TABLE IF NOT EXISTS social_accounts (
    id VARCHAR(32) PRIMARY KEY,
    user_id VARCHAR(32) NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    provider VARCHAR(20) NOT NULL,
    provider_user_id VARCHAR(255) NOT NULL,
    provider_email VARCHAR(180),
    avatar_url VARCHAR(500),
    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_social_provider_user UNIQUE (provider, provider_user_id)
);

-- ===== mfa_secrets =====

CREATE TABLE IF NOT EXISTS mfa_secrets (
    id VARCHAR(32) PRIMARY KEY,
    user_id VARCHAR(32) UNIQUE NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    secret VARCHAR(128) NOT NULL,
    enabled BOOLEAN NOT NULL DEFAULT FALSE,
    last_used_at TIMESTAMP,
    created_at TIMESTAMP NOT NULL DEFAULT NOW()
);

-- ===== user_profiles =====

CREATE TABLE IF NOT EXISTS user_profiles (
    id VARCHAR(32) PRIMARY KEY,
    user_id VARCHAR(32) UNIQUE NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    referral_code VARCHAR(20) UNIQUE,
    referred_by_code VARCHAR(20),
    bio VARCHAR(500),
    address_line VARCHAR(200),
    city VARCHAR(120),
    state VARCHAR(2),
    zipcode VARCHAR(10),
    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP NOT NULL DEFAULT NOW()
);

COMMIT;

-- Verificação pós-migration:
-- \d users
-- SELECT table_name FROM information_schema.tables WHERE table_schema='public'
--   AND table_name IN ('login_attempts','audit_logs','trusted_devices',
--                      'refresh_tokens','user_consents','social_accounts',
--                      'mfa_secrets','user_profiles');
