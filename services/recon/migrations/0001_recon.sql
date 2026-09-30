BEGIN;

CREATE TABLE IF NOT EXISTS recon_schema_migrations (
    version integer PRIMARY KEY,
    applied_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE IF NOT EXISTS recon_files (
    file_id uuid PRIMARY KEY,
    source text NOT NULL CHECK (source ~ '^[a-z0-9-]{1,40}$'),
    business_date date NOT NULL,
    format text NOT NULL CHECK (format IN ('csv_rupees_ist', 'fixed_paise_utc', 'json_offset')),
    object_key text NOT NULL,
    sha256 text NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    row_count integer NOT NULL CHECK (row_count >= 0),
    issue_count integer NOT NULL CHECK (issue_count >= 0),
    issues jsonb NOT NULL DEFAULT '[]',
    ingested_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (source, business_date, sha256)
);

CREATE TABLE IF NOT EXISTS recon_runs (
    run_id uuid PRIMARY KEY,
    source text NOT NULL,
    business_date date NOT NULL,
    file_ids uuid[] NOT NULL,
    bank_lines integer NOT NULL,
    internal_records integer NOT NULL,
    matched_lines integer NOT NULL,
    unmatched_lines integer NOT NULL,
    breaks_by_type jsonb NOT NULL,
    open_break_value_minor bigint NOT NULL,
    duration_ms integer NOT NULL,
    started_at timestamptz NOT NULL,
    finished_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX IF NOT EXISTS recon_runs_date_idx ON recon_runs(business_date, source);

CREATE TABLE IF NOT EXISTS recon_matches (
    run_id uuid NOT NULL REFERENCES recon_runs(run_id),
    match_type text NOT NULL,
    refs text[] NOT NULL,
    bank_lines integer[] NOT NULL
);
CREATE INDEX IF NOT EXISTS recon_matches_run_idx ON recon_matches(run_id);

-- Break IDs are deterministic (date, source, type, key): re-runs update, never duplicate.
CREATE TABLE IF NOT EXISTS recon_breaks (
    break_id uuid PRIMARY KEY,
    source text NOT NULL,
    business_date date NOT NULL,
    break_type text NOT NULL CHECK (break_type IN (
        'missing_at_bank', 'missing_internally', 'amount_mismatch', 'duplicate',
        'status_mismatch', 'timing_difference', 'fee_tax_mismatch', 'unknown'
    )),
    reference text,
    bank_reference text,
    kind text NOT NULL,
    amount_minor bigint NOT NULL CHECK (amount_minor >= 0),
    internal_amount_minor bigint,
    bank_amount_minor bigint,
    status text NOT NULL CHECK (status IN (
        'open', 'auto_resolved', 'pending_approval', 'resolved'
    )),
    suggested_action text NOT NULL,
    detail text NOT NULL,
    evidence jsonb NOT NULL DEFAULT '{}',
    first_seen_run uuid NOT NULL REFERENCES recon_runs(run_id),
    last_seen_run uuid NOT NULL REFERENCES recon_runs(run_id),
    sla_due_at timestamptz NOT NULL,
    resolution text,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    resolved_at timestamptz
);
CREATE INDEX IF NOT EXISTS recon_breaks_queue_idx ON recon_breaks(status, business_date);
CREATE INDEX IF NOT EXISTS recon_breaks_reference_idx ON recon_breaks(reference);

CREATE TABLE IF NOT EXISTS break_actions (
    action_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    break_id uuid NOT NULL REFERENCES recon_breaks(break_id),
    actor text NOT NULL,
    action text NOT NULL,
    note text,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

-- Dual control for sensitive manual actions (recon adjustments now; more types in Phase 11).
CREATE TABLE IF NOT EXISTS maker_checker_requests (
    request_id uuid PRIMARY KEY,
    action_type text NOT NULL,
    subject_id text NOT NULL,
    payload jsonb NOT NULL,
    maker text NOT NULL,
    status text NOT NULL CHECK (status IN (
        'pending', 'approved', 'rejected', 'executed', 'failed'
    )),
    checker text,
    decision_reason text,
    result jsonb,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    decided_at timestamptz,
    CHECK (checker IS NULL OR checker <> maker),
    CHECK ((status = 'pending') = (checker IS NULL))
);
CREATE UNIQUE INDEX IF NOT EXISTS maker_checker_one_pending_idx
    ON maker_checker_requests(action_type, subject_id) WHERE status = 'pending';

CREATE OR REPLACE FUNCTION recon_reject_history_mutation() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'reconciliation history is append-only' USING ERRCODE = '55000';
END;
$$;
DROP TRIGGER IF EXISTS break_actions_no_mutation ON break_actions;
CREATE TRIGGER break_actions_no_mutation BEFORE UPDATE OR DELETE ON break_actions
FOR EACH ROW EXECUTE FUNCTION recon_reject_history_mutation();
DROP TRIGGER IF EXISTS recon_files_no_mutation ON recon_files;
CREATE TRIGGER recon_files_no_mutation BEFORE UPDATE OR DELETE ON recon_files
FOR EACH ROW EXECUTE FUNCTION recon_reject_history_mutation();

INSERT INTO recon_schema_migrations(version) VALUES (1) ON CONFLICT DO NOTHING;
COMMIT;
