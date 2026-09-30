BEGIN;

ALTER TABLE payment_intents
    ADD COLUMN IF NOT EXISTS risk_context jsonb NOT NULL DEFAULT '{}',
    ADD COLUMN IF NOT EXISTS risk_outcome jsonb;

INSERT INTO core_schema_migrations(version) VALUES (8) ON CONFLICT DO NOTHING;
COMMIT;
