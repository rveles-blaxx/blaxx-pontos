-- Migration · PIX manual (QR estático) · 2026-05-25
-- Adiciona campos no PixCharge para fluxo "cliente paga, admin confirma".

BEGIN;

ALTER TABLE pix_charges
    ADD COLUMN IF NOT EXISTS claimed_paid_at TIMESTAMP,
    ADD COLUMN IF NOT EXISTS confirmed_by_user_id VARCHAR(32)
        REFERENCES users(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS flow VARCHAR(16) NOT NULL DEFAULT 'mp';

-- Status enum nova: PENDING_CONFIRMATION + REJECTED
-- Postgres usa o tipo nativo, então precisamos adicionar valores:
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_type t
        JOIN pg_enum e ON t.oid = e.enumtypid
        WHERE t.typname = 'pixchargestatus' AND e.enumlabel = 'pending_confirmation'
    ) THEN
        ALTER TYPE pixchargestatus ADD VALUE 'pending_confirmation';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_type t
        JOIN pg_enum e ON t.oid = e.enumtypid
        WHERE t.typname = 'pixchargestatus' AND e.enumlabel = 'rejected'
    ) THEN
        ALTER TYPE pixchargestatus ADD VALUE 'rejected';
    END IF;
END $$;

COMMIT;
