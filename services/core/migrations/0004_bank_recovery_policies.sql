BEGIN;

CREATE TABLE IF NOT EXISTS core_bank_recovery_policies (
    bank_id text NOT NULL,
    min_amount_minor bigint NOT NULL CHECK (min_amount_minor >= 1),
    max_amount_minor bigint CHECK (max_amount_minor >= min_amount_minor),
    status_check_deadline_seconds integer NOT NULL
        CHECK (status_check_deadline_seconds BETWEEN 1 AND 86400),
    late_success_window_seconds integer NOT NULL
        CHECK (late_success_window_seconds BETWEEN 1 AND 604800),
    deemed_outcome text NOT NULL CHECK (deemed_outcome IN ('auto_reverse', 'deemed_success')),
    PRIMARY KEY (bank_id, min_amount_minor)
);

ALTER TABLE payment_intents
    ADD COLUMN IF NOT EXISTS recovery_policy text NOT NULL DEFAULT 'auto_reverse'
        CHECK (recovery_policy IN ('auto_reverse', 'deemed_success')),
    ADD COLUMN IF NOT EXISTS late_success_window_seconds integer NOT NULL DEFAULT 600
        CHECK (late_success_window_seconds BETWEEN 1 AND 604800),
    ADD COLUMN IF NOT EXISTS recovery_lease_until timestamptz;

INSERT INTO core_bank_recovery_policies(
    bank_id, min_amount_minor, max_amount_minor, status_check_deadline_seconds,
    late_success_window_seconds, deemed_outcome
) VALUES
    ('bank-a', 1, NULL, 30, 600, 'auto_reverse'),
    ('bank-b', 1, NULL, 30, 600, 'auto_reverse')
ON CONFLICT (bank_id, min_amount_minor) DO NOTHING;

INSERT INTO core_schema_migrations(version) VALUES (4) ON CONFLICT DO NOTHING;
COMMIT;
