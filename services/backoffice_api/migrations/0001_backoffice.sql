BEGIN;

CREATE TABLE IF NOT EXISTS backoffice_schema_migrations (
    version integer PRIMARY KEY,
    applied_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE IF NOT EXISTS backoffice_users (
    user_id uuid PRIMARY KEY,
    email text NOT NULL UNIQUE CHECK (email = lower(email) AND email LIKE '%@%'),
    password_hash text NOT NULL,
    roles text[] NOT NULL CHECK (cardinality(roles) > 0 AND roles <@ ARRAY[
        'merchant_admin', 'merchant_developer', 'merchant_viewer', 'ops_analyst',
        'risk_analyst', 'approver', 'operator'
    ]::text[]),
    merchant_id text REFERENCES merchants(merchant_id),
    totp_secret_ciphertext bytea,
    mfa_enabled boolean NOT NULL DEFAULT false,
    last_totp_step bigint,
    status text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'locked', 'disabled')),
    failed_logins integer NOT NULL DEFAULT 0,
    locked_until timestamptz,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    last_login_at timestamptz,
    -- Merchant roles are tenant-scoped; platform roles must not be tied to one merchant.
    CHECK (
        (merchant_id IS NOT NULL AND roles <@ ARRAY['merchant_admin', 'merchant_developer',
                                                    'merchant_viewer']::text[])
        OR (merchant_id IS NULL AND NOT roles && ARRAY['merchant_admin', 'merchant_developer',
                                                       'merchant_viewer']::text[])
    )
);

-- Refresh tokens rotate on every use; presenting a used token revokes its whole family.
CREATE TABLE IF NOT EXISTS backoffice_refresh_tokens (
    token_hash bytea PRIMARY KEY CHECK (octet_length(token_hash) = 32),
    family_id uuid NOT NULL,
    user_id uuid NOT NULL REFERENCES backoffice_users(user_id),
    issued_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    expires_at timestamptz NOT NULL,
    used_at timestamptz,
    revoked_at timestamptz
);
CREATE INDEX IF NOT EXISTS backoffice_refresh_family_idx ON backoffice_refresh_tokens(family_id);

-- Tamper-evident audit log for security- and money-relevant actions.
CREATE TABLE IF NOT EXISTS audit_log (
    seq bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    occurred_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    actor text NOT NULL,
    action text NOT NULL,
    subject text NOT NULL,
    merchant_id text,
    details jsonb NOT NULL,
    previous_hash bytea NOT NULL CHECK (octet_length(previous_hash) = 32),
    entry_hash bytea NOT NULL UNIQUE CHECK (octet_length(entry_hash) = 32)
);

CREATE OR REPLACE FUNCTION audit_append(
    p_actor text, p_action text, p_subject text, p_merchant_id text, p_details jsonb
) RETURNS bigint LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE v_prev bytea; v_seq bigint; v_hash bytea; v_at timestamptz := clock_timestamp();
BEGIN
    PERFORM pg_advisory_xact_lock(hashtextextended('audit-log-chain', 0));
    SELECT entry_hash INTO v_prev FROM audit_log ORDER BY seq DESC LIMIT 1;
    v_prev := coalesce(v_prev, decode(repeat('00', 32), 'hex'));
    v_seq := nextval(pg_get_serial_sequence('audit_log', 'seq'));
    v_hash := digest(concat_ws('|', v_seq::text, encode(v_prev, 'hex'), v_at::text, p_actor,
                               p_action, p_subject, coalesce(p_merchant_id, ''),
                               p_details::text), 'sha256');
    INSERT INTO audit_log(seq, occurred_at, actor, action, subject, merchant_id, details,
                          previous_hash, entry_hash)
    OVERRIDING SYSTEM VALUE
    VALUES (v_seq, v_at, p_actor, p_action, p_subject, p_merchant_id, p_details, v_prev, v_hash);
    RETURN v_seq;
END;
$$;

CREATE OR REPLACE FUNCTION audit_verify() RETURNS TABLE(ok boolean, entries bigint, broken_at bigint)
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE r audit_log%ROWTYPE; v_prev bytea := decode(repeat('00', 32), 'hex'); v_count bigint := 0;
BEGIN
    FOR r IN SELECT * FROM audit_log ORDER BY seq LOOP
        v_count := v_count + 1;
        IF r.previous_hash <> v_prev OR r.entry_hash <> digest(concat_ws('|', r.seq::text,
               encode(v_prev, 'hex'), r.occurred_at::text, r.actor, r.action, r.subject,
               coalesce(r.merchant_id, ''), r.details::text), 'sha256') THEN
            RETURN QUERY SELECT false, v_count, r.seq;
            RETURN;
        END IF;
        v_prev := r.entry_hash;
    END LOOP;
    RETURN QUERY SELECT true, v_count, NULL::bigint;
END;
$$;

CREATE OR REPLACE FUNCTION audit_reject_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'audit log is append-only' USING ERRCODE = '55000';
END;
$$;
DROP TRIGGER IF EXISTS audit_log_no_mutation ON audit_log;
CREATE TRIGGER audit_log_no_mutation BEFORE UPDATE OR DELETE ON audit_log
FOR EACH ROW EXECUTE FUNCTION audit_reject_mutation();
DROP TRIGGER IF EXISTS audit_log_no_truncate ON audit_log;
CREATE TRIGGER audit_log_no_truncate BEFORE TRUNCATE ON audit_log
FOR EACH STATEMENT EXECUTE FUNCTION audit_reject_mutation();

-- Chain heads copied to WORM object storage; a rewritten chain no longer matches an anchor.
CREATE TABLE IF NOT EXISTS audit_anchors (
    anchor_id uuid PRIMARY KEY,
    seq bigint NOT NULL,
    entry_hash bytea NOT NULL,
    object_key text NOT NULL,
    object_sha256 text NOT NULL,
    anchored_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE IF NOT EXISTS merchant_limits (
    merchant_id text PRIMARY KEY REFERENCES merchants(merchant_id),
    per_txn_max_minor bigint CHECK (per_txn_max_minor > 0),
    daily_max_minor bigint CHECK (daily_max_minor > 0),
    updated_by text NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

-- AML-lite (demonstration only; not a compliance programme).
CREATE TABLE IF NOT EXISTS aml_alerts (
    alert_id uuid PRIMARY KEY,
    alert_type text NOT NULL CHECK (alert_type IN (
        'structuring', 'rapid_in_out', 'merchant_volume_spike', 'sanctions_match'
    )),
    subject text NOT NULL,
    merchant_id text,
    details jsonb NOT NULL,
    status text NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'escalated', 'closed')),
    case_id uuid,
    dedupe_key text NOT NULL UNIQUE,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE IF NOT EXISTS aml_cases (
    case_id uuid PRIMARY KEY,
    status text NOT NULL CHECK (status IN ('open', 'reported', 'closed')),
    summary text NOT NULL,
    assigned_to text,
    created_by text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    closed_at timestamptz
);
CREATE TABLE IF NOT EXISTS sanctions_list (
    entry_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name_normalized text NOT NULL UNIQUE,
    source text NOT NULL,
    note text NOT NULL DEFAULT 'synthetic demonstration entry'
);
INSERT INTO sanctions_list(name_normalized, source) VALUES
    ('blockedperson', 'synthetic-demo'),
    ('shellcorp', 'synthetic-demo'),
    ('mule0', 'synthetic-demo')
ON CONFLICT DO NOTHING;

-- Row-level security for the back office --------------------------------------------------
DO $$ BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'tally_app') THEN
        CREATE ROLE tally_app NOLOGIN NOBYPASSRLS;
    END IF;
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'tally_ops') THEN
        CREATE ROLE tally_ops NOLOGIN BYPASSRLS;
    END IF;
END $$;

-- Tables without a direct merchant_id get policies through their parent.
ALTER TABLE settlement_items ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS settlement_items_tenant ON settlement_items;
CREATE POLICY settlement_items_tenant ON settlement_items USING (EXISTS (
    SELECT 1 FROM settlements s WHERE s.settlement_id = settlement_items.settlement_id
      AND s.merchant_id = current_setting('app.merchant_id', true)));

DO $$
DECLARE t text;
BEGIN
    FOREACH t IN ARRAY ARRAY['refund_transitions', 'dispute_transitions', 'core_outbox',
                             'risk_decisions', 'risk_review_cases', 'merchant_api_keys',
                             'merchant_limits', 'webhook_delivery_attempts',
                             'merchant_settlement_configs', 'aml_alerts'] LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('DROP POLICY IF EXISTS %I ON %I', t || '_tenant', t);
        EXECUTE format(
            'CREATE POLICY %I ON %I USING (merchant_id = current_setting(''app.merchant_id'', true))',
            t || '_tenant', t);
    END LOOP;
END $$;

GRANT USAGE ON SCHEMA public TO tally_app, tally_ops;
GRANT SELECT ON payment_intents, payment_transitions, refunds, refund_transitions, disputes,
    dispute_transitions, settlements, settlement_items, payouts, webhook_deliveries,
    webhook_delivery_attempts, core_outbox, risk_decisions, risk_review_cases, merchant_limits,
    merchant_settlement_configs, core_vpas
    TO tally_app, tally_ops;
-- Secrets stay out of reach: only column-level grants on tables that hold ciphertext
-- (a table-level grant would cover every column and could not be narrowed by a revoke).
GRANT SELECT (key_id, merchant_id, scopes, mode, created_at, expires_at, revoked_at)
    ON merchant_api_keys TO tally_app, tally_ops;
GRANT SELECT (endpoint_id, merchant_id, url, enabled_events, status, created_at, disabled_at)
    ON webhook_endpoints TO tally_app, tally_ops;
GRANT SELECT ON recon_runs, recon_breaks, break_actions, maker_checker_requests, aml_alerts,
    aml_cases, audit_log, audit_anchors, merchants, risk_rule_versions, risk_model_versions,
    risk_drift_reports, core_recovery_incidents, core_ledger_commands TO tally_ops;
GRANT EXECUTE ON FUNCTION audit_append(text, text, text, text, jsonb), audit_verify()
    TO tally_app, tally_ops;

INSERT INTO backoffice_schema_migrations(version) VALUES (1) ON CONFLICT DO NOTHING;
COMMIT;
