-- ============================================================================
-- Sprint 1 hardening · 2026-05-28 · Indices em campos de busca frequente
-- ============================================================================
-- Aplique apos deployar o backend novo. Idempotente via IF NOT EXISTS
-- (Postgres). Em SQLite, IF NOT EXISTS tambem funciona desde 3.8.
--
-- Justificativa: extrato, admin pending charges, notification unread-count,
-- antifraude por IP/email — todos faziam sequencial scan ate aqui.
-- ============================================================================

-- Transaction (ledger)
CREATE INDEX IF NOT EXISTS ix_transactions_wallet_id  ON transactions (wallet_id);
CREATE INDEX IF NOT EXISTS ix_transactions_type       ON transactions (type);
CREATE INDEX IF NOT EXISTS ix_transactions_created_at ON transactions (created_at);
CREATE INDEX IF NOT EXISTS ix_tx_wallet_created       ON transactions (wallet_id, created_at);

-- PixCharge
CREATE INDEX IF NOT EXISTS ix_pix_charges_user_id     ON pix_charges (user_id);
CREATE INDEX IF NOT EXISTS ix_pix_charges_status      ON pix_charges (status);
CREATE INDEX IF NOT EXISTS ix_pix_charges_created_at  ON pix_charges (created_at);
CREATE INDEX IF NOT EXISTS ix_pix_charge_user_created ON pix_charges (user_id, created_at);

-- Notification
CREATE INDEX IF NOT EXISTS ix_notifications_user_id    ON notifications (user_id);
CREATE INDEX IF NOT EXISTS ix_notifications_created_at ON notifications (created_at);
CREATE INDEX IF NOT EXISTS ix_notif_user_read_created  ON notifications (user_id, read_at, created_at);

-- LoginAttempt (antifraude)
CREATE INDEX IF NOT EXISTS ix_login_attempts_email_attempted ON login_attempts (email_attempted);
CREATE INDEX IF NOT EXISTS ix_login_attempts_ip              ON login_attempts (ip);
CREATE INDEX IF NOT EXISTS ix_login_attempts_created_at      ON login_attempts (created_at);

-- AuditLog
CREATE INDEX IF NOT EXISTS ix_audit_logs_user_id        ON audit_logs (user_id);
CREATE INDEX IF NOT EXISTS ix_audit_logs_event          ON audit_logs (event);
CREATE INDEX IF NOT EXISTS ix_audit_logs_correlation_id ON audit_logs (correlation_id);
CREATE INDEX IF NOT EXISTS ix_audit_logs_created_at     ON audit_logs (created_at);
CREATE INDEX IF NOT EXISTS ix_audit_user_created        ON audit_logs (user_id, created_at);

-- Voucher
CREATE INDEX IF NOT EXISTS ix_vouchers_user_id ON vouchers (user_id);
