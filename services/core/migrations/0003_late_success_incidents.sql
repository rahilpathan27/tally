BEGIN;

ALTER TABLE payment_intents ADD COLUMN IF NOT EXISTS late_success_until timestamptz;

CREATE TABLE IF NOT EXISTS core_recovery_incidents (
    incident_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    payment_id uuid NOT NULL REFERENCES payment_intents(payment_id),
    merchant_id text NOT NULL REFERENCES merchants(merchant_id),
    incident_type text NOT NULL CHECK (incident_type = 'late_success_after_reversal'),
    bank_status text NOT NULL,
    correction_entry_id bigint,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE(payment_id, incident_type)
);

CREATE OR REPLACE FUNCTION core_reject_recovery_incident_mutation() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'recovery incidents are append-only' USING ERRCODE = '55000';
END;
$$;
DROP TRIGGER IF EXISTS core_recovery_incidents_no_mutation ON core_recovery_incidents;
CREATE TRIGGER core_recovery_incidents_no_mutation
BEFORE UPDATE OR DELETE ON core_recovery_incidents
FOR EACH ROW EXECUTE FUNCTION core_reject_recovery_incident_mutation();

CREATE INDEX IF NOT EXISTS payment_intents_late_success_idx
    ON payment_intents(next_recovery_at)
    WHERE status = 'reversed' AND late_success_until IS NOT NULL;

INSERT INTO core_schema_migrations(version) VALUES (3) ON CONFLICT DO NOTHING;
COMMIT;
