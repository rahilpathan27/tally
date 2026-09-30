BEGIN;

ALTER TABLE payment_intents
    ADD COLUMN IF NOT EXISTS recovery_attempts integer NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS next_recovery_at timestamptz,
    ADD COLUMN IF NOT EXISTS recovery_deadline timestamptz,
    ADD COLUMN IF NOT EXISTS bank_reference text;

CREATE INDEX IF NOT EXISTS payment_intents_recovery_idx
    ON payment_intents(next_recovery_at, updated_at)
    WHERE status IN ('pending_unknown', 'reversal_pending');

INSERT INTO core_schema_migrations(version) VALUES (2) ON CONFLICT DO NOTHING;
COMMIT;
