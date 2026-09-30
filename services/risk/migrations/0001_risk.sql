BEGIN;

CREATE TABLE IF NOT EXISTS risk_schema_migrations (
    version integer PRIMARY KEY,
    applied_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

-- Rule sets are immutable versions; exactly one is active.
CREATE TABLE IF NOT EXISTS risk_rule_versions (
    version integer PRIMARY KEY,
    definition jsonb NOT NULL,
    created_by text NOT NULL,
    approved_by text,
    active boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    activated_at timestamptz,
    CHECK (approved_by IS NULL OR approved_by <> created_by)
);
CREATE UNIQUE INDEX IF NOT EXISTS risk_rule_one_active_idx ON risk_rule_versions(active)
    WHERE active;

CREATE TABLE IF NOT EXISTS risk_model_versions (
    version text PRIMARY KEY,
    stage text NOT NULL CHECK (stage IN ('registered', 'challenger', 'champion', 'retired')),
    artifact_path text NOT NULL,
    artifact_sha256 jsonb NOT NULL,
    metrics jsonb NOT NULL,
    registered_by text NOT NULL,
    promoted_by text,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    promoted_at timestamptz
);
CREATE UNIQUE INDEX IF NOT EXISTS risk_one_champion_idx ON risk_model_versions(stage)
    WHERE stage = 'champion';
CREATE UNIQUE INDEX IF NOT EXISTS risk_one_challenger_idx ON risk_model_versions(stage)
    WHERE stage = 'challenger';

CREATE TABLE IF NOT EXISTS risk_decisions (
    decision_id uuid PRIMARY KEY,
    payment_id text NOT NULL,
    merchant_id text NOT NULL,
    decision text NOT NULL CHECK (decision IN ('allow', 'step_up', 'review', 'block')),
    model_version text,
    model_score double precision,
    model_decision text,
    rule_version integer,
    rule_hits jsonb NOT NULL DEFAULT '[]',
    reason_codes jsonb NOT NULL DEFAULT '[]',
    features jsonb NOT NULL,
    challenger_version text,
    challenger_score double precision,
    challenger_decision text,
    amount_minor bigint NOT NULL,
    method text NOT NULL,
    latency_us integer NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE UNIQUE INDEX IF NOT EXISTS risk_decisions_payment_idx ON risk_decisions(payment_id);
CREATE INDEX IF NOT EXISTS risk_decisions_created_idx ON risk_decisions(created_at DESC);

CREATE TABLE IF NOT EXISTS risk_review_cases (
    case_id uuid PRIMARY KEY,
    decision_id uuid NOT NULL REFERENCES risk_decisions(decision_id),
    payment_id text NOT NULL UNIQUE,
    merchant_id text NOT NULL,
    status text NOT NULL CHECK (status IN ('open', 'approved', 'declined')),
    assigned_to text,
    sla_due_at timestamptz NOT NULL,
    resolution_note text,
    resolved_by text,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    resolved_at timestamptz
);
CREATE INDEX IF NOT EXISTS risk_review_open_idx ON risk_review_cases(sla_due_at)
    WHERE status = 'open';

CREATE TABLE IF NOT EXISTS risk_review_actions (
    action_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    case_id uuid NOT NULL REFERENCES risk_review_cases(case_id),
    actor text NOT NULL,
    action text NOT NULL,
    note text,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE IF NOT EXISTS risk_step_up_challenges (
    challenge_id uuid PRIMARY KEY,
    decision_id uuid NOT NULL REFERENCES risk_decisions(decision_id),
    payment_id text NOT NULL UNIQUE,
    code_hash bytea NOT NULL,
    attempts integer NOT NULL DEFAULT 0 CHECK (attempts <= 5),
    status text NOT NULL CHECK (status IN ('pending', 'verified', 'failed', 'expired')),
    expires_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE IF NOT EXISTS risk_drift_reports (
    report_id uuid PRIMARY KEY,
    model_version text NOT NULL,
    sample_size integer NOT NULL,
    features jsonb NOT NULL,
    alerts integer NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE IF NOT EXISTS merchant_risk_policies (
    merchant_id text PRIMARY KEY,
    tier text NOT NULL DEFAULT 'standard' CHECK (tier IN ('standard', 'high_risk', 'enterprise')),
    fail_mode text NOT NULL DEFAULT 'open' CHECK (fail_mode IN ('open', 'closed')),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE OR REPLACE FUNCTION risk_reject_history_mutation() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'risk history is append-only' USING ERRCODE = '55000';
END;
$$;
DROP TRIGGER IF EXISTS risk_decisions_no_mutation ON risk_decisions;
CREATE TRIGGER risk_decisions_no_mutation BEFORE UPDATE OR DELETE ON risk_decisions
FOR EACH ROW EXECUTE FUNCTION risk_reject_history_mutation();
DROP TRIGGER IF EXISTS risk_review_actions_no_mutation ON risk_review_actions;
CREATE TRIGGER risk_review_actions_no_mutation BEFORE UPDATE OR DELETE ON risk_review_actions
FOR EACH ROW EXECUTE FUNCTION risk_reject_history_mutation();

INSERT INTO risk_schema_migrations(version) VALUES (1) ON CONFLICT DO NOTHING;
COMMIT;
