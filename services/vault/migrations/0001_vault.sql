BEGIN;

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS vault_cards (
    token text PRIMARY KEY,
    encrypted_pan bytea NOT NULL CHECK (octet_length(encrypted_pan) >= 89),
    bin_prefix char(6) NOT NULL CHECK (bin_prefix ~ '^[0-9]{6}$'),
    last4 char(4) NOT NULL CHECK (last4 ~ '^[0-9]{4}$'),
    expiry_month smallint NOT NULL CHECK (expiry_month BETWEEN 1 AND 12),
    expiry_year smallint NOT NULL CHECK (expiry_year BETWEEN 2000 AND 9999),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    revoked_at timestamptz
);

CREATE TABLE IF NOT EXISTS vault_access_events (
    sequence_id bigserial PRIMARY KEY,
    token text NOT NULL REFERENCES vault_cards(token),
    caller_id text NOT NULL CHECK (caller_id = 'network-simulator'),
    request_id text NOT NULL,
    action text NOT NULL CHECK (action = 'detokenize'),
    previous_hash bytea,
    event_hash bytea NOT NULL UNIQUE,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE OR REPLACE FUNCTION vault_append_access_event(
    p_token text, p_caller_id text, p_request_id text
) RETURNS bigint LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE prior bytea; current_hash bytea; seq bigint; canonical text;
BEGIN
    IF p_caller_id <> 'network-simulator' THEN
        RAISE EXCEPTION 'caller is not permitted to detokenize' USING ERRCODE = '42501';
    END IF;
    PERFORM pg_advisory_xact_lock(7401, 1);
    SELECT event_hash INTO prior FROM vault_access_events ORDER BY sequence_id DESC LIMIT 1;
    canonical := concat_ws('|', coalesce(encode(prior, 'hex'), ''),
        p_token, p_caller_id, p_request_id, 'detokenize');
    current_hash := digest(canonical, 'sha256');
    INSERT INTO vault_access_events(
        token, caller_id, request_id, action, previous_hash, event_hash
    ) VALUES (p_token, p_caller_id, p_request_id, 'detokenize', prior, current_hash)
    RETURNING sequence_id INTO seq;
    RETURN seq;
END;
$$;

CREATE OR REPLACE FUNCTION vault_reject_audit_mutation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'vault access history is append-only' USING ERRCODE = '55000';
END;
$$;
DROP TRIGGER IF EXISTS vault_access_no_update_delete ON vault_access_events;
CREATE TRIGGER vault_access_no_update_delete
BEFORE UPDATE OR DELETE ON vault_access_events
FOR EACH ROW EXECUTE FUNCTION vault_reject_audit_mutation();

CREATE OR REPLACE FUNCTION vault_verify_access_chain()
RETURNS boolean LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path = pg_catalog, public AS $$
DECLARE item vault_access_events%ROWTYPE; prior bytea; expected bytea; canonical text;
BEGIN
    FOR item IN SELECT * FROM vault_access_events ORDER BY sequence_id LOOP
        IF item.previous_hash IS DISTINCT FROM prior THEN RETURN false; END IF;
        canonical := concat_ws('|', coalesce(encode(prior, 'hex'), ''),
            item.token, item.caller_id, item.request_id, item.action);
        expected := digest(canonical, 'sha256');
        IF item.event_hash IS DISTINCT FROM expected THEN RETURN false; END IF;
        prior := item.event_hash;
    END LOOP;
    RETURN true;
END;
$$;

CREATE TABLE IF NOT EXISTS vault_schema_migrations (
    version integer PRIMARY KEY,
    applied_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
INSERT INTO vault_schema_migrations(version) VALUES (1) ON CONFLICT DO NOTHING;

DO $$ BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'tally_vault_app') THEN
        CREATE ROLE tally_vault_app NOLOGIN;
    END IF;
END $$;

GRANT SELECT, INSERT ON vault_cards TO tally_vault_app;
GRANT EXECUTE ON FUNCTION vault_append_access_event(text, text, text) TO tally_vault_app;
GRANT EXECUTE ON FUNCTION vault_verify_access_chain() TO tally_vault_app;

COMMIT;
