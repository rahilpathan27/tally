BEGIN;

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS merchants (
    merchant_id text PRIMARY KEY,
    display_name text NOT NULL,
    status text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'suspended', 'closed')),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE IF NOT EXISTS merchant_api_keys (
    key_id text PRIMARY KEY,
    merchant_id text NOT NULL REFERENCES merchants(merchant_id),
    secret_ciphertext bytea NOT NULL,
    scopes text[] NOT NULL CHECK (cardinality(scopes) > 0),
    mode text NOT NULL CHECK (mode IN ('test', 'live')),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    expires_at timestamptz,
    revoked_at timestamptz
);
CREATE INDEX IF NOT EXISTS merchant_api_keys_merchant_idx ON merchant_api_keys(merchant_id);

CREATE TABLE IF NOT EXISTS gateway_request_nonces (
    key_id text NOT NULL REFERENCES merchant_api_keys(key_id),
    nonce text NOT NULL CHECK (length(nonce) BETWEEN 16 AND 200),
    expires_at timestamptz NOT NULL,
    PRIMARY KEY (key_id, nonce)
);
CREATE INDEX IF NOT EXISTS gateway_request_nonces_expiry_idx ON gateway_request_nonces(expires_at);

CREATE OR REPLACE FUNCTION gateway_consume_nonce(p_key_id text, p_nonce text, p_expires_at timestamptz)
RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE inserted_count integer;
BEGIN
    INSERT INTO gateway_request_nonces(key_id, nonce, expires_at)
    SELECT k.key_id, p_nonce, p_expires_at
      FROM merchant_api_keys k JOIN merchants m USING (merchant_id)
     WHERE k.key_id = p_key_id AND k.revoked_at IS NULL
       AND (k.expires_at IS NULL OR k.expires_at > clock_timestamp())
       AND m.status = 'active'
    ON CONFLICT (key_id, nonce) DO NOTHING;
    GET DIAGNOSTICS inserted_count = ROW_COUNT;
    RETURN inserted_count = 1;
END;
$$;

CREATE TABLE IF NOT EXISTS gateway_idempotency_requests (
    merchant_id text NOT NULL REFERENCES merchants(merchant_id),
    idempotency_key text NOT NULL CHECK (length(idempotency_key) BETWEEN 1 AND 200),
    request_fingerprint bytea NOT NULL CHECK (octet_length(request_fingerprint) = 32),
    state text NOT NULL CHECK (state IN ('in_progress', 'completed')),
    response_status smallint,
    response_body jsonb,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    expires_at timestamptz NOT NULL,
    PRIMARY KEY (merchant_id, idempotency_key),
    CHECK ((state = 'in_progress' AND response_status IS NULL AND response_body IS NULL)
        OR (state = 'completed' AND response_status BETWEEN 100 AND 599 AND response_body IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS gateway_idempotency_expiry_idx
    ON gateway_idempotency_requests(expires_at);

CREATE OR REPLACE FUNCTION gateway_begin_idempotency(
    p_merchant_id text, p_key text, p_fingerprint bytea, p_expires_at timestamptz
) RETURNS TABLE(outcome text, response_status smallint, response_body jsonb)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE prior gateway_idempotency_requests%ROWTYPE; inserted_count integer;
BEGIN
    IF p_merchant_id IS DISTINCT FROM current_setting('app.merchant_id', true) THEN
        RAISE EXCEPTION 'merchant context mismatch' USING ERRCODE = '42501';
    END IF;
    INSERT INTO gateway_idempotency_requests(
        merchant_id, idempotency_key, request_fingerprint, state, expires_at
    ) VALUES (p_merchant_id, p_key, p_fingerprint, 'in_progress', p_expires_at)
    ON CONFLICT (merchant_id, idempotency_key) DO NOTHING;
    GET DIAGNOSTICS inserted_count = ROW_COUNT;

    SELECT * INTO prior FROM gateway_idempotency_requests r
     WHERE r.merchant_id = p_merchant_id AND r.idempotency_key = p_key FOR UPDATE;
    IF prior.request_fingerprint <> p_fingerprint THEN
        RAISE EXCEPTION 'idempotency key payload mismatch' USING ERRCODE = '23505';
    ELSIF prior.state = 'completed' THEN
        RETURN QUERY SELECT 'replay'::text, prior.response_status, prior.response_body;
    ELSIF inserted_count = 1 THEN
        RETURN QUERY SELECT 'started'::text, NULL::smallint, NULL::jsonb;
    ELSE
        RETURN QUERY SELECT 'in_progress'::text, NULL::smallint, NULL::jsonb;
    END IF;
END;
$$;

CREATE OR REPLACE FUNCTION gateway_complete_idempotency(
    p_merchant_id text, p_key text, p_fingerprint bytea,
    p_status smallint, p_body jsonb
) RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE changed_count integer;
BEGIN
    IF p_merchant_id IS DISTINCT FROM current_setting('app.merchant_id', true) THEN
        RAISE EXCEPTION 'merchant context mismatch' USING ERRCODE = '42501';
    END IF;
    UPDATE gateway_idempotency_requests
       SET state = 'completed', response_status = p_status, response_body = p_body
     WHERE merchant_id = p_merchant_id AND idempotency_key = p_key
       AND request_fingerprint = p_fingerprint AND state = 'in_progress';
    GET DIAGNOSTICS changed_count = ROW_COUNT;
    IF changed_count <> 1 THEN
        RAISE EXCEPTION 'idempotency reservation missing or changed' USING ERRCODE = '23514';
    END IF;
END;
$$;

CREATE TABLE IF NOT EXISTS gateway_audit_events (
    sequence_id bigserial PRIMARY KEY,
    merchant_id text REFERENCES merchants(merchant_id),
    actor_key_id text,
    action text NOT NULL,
    request_id text NOT NULL,
    event_data jsonb NOT NULL,
    previous_hash bytea,
    event_hash bytea NOT NULL UNIQUE,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE OR REPLACE FUNCTION gateway_append_audit_event(
    p_merchant_id text, p_actor_key_id text, p_action text, p_request_id text, p_data jsonb
) RETURNS bigint LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE prev bytea; digest bytea; seq bigint; canonical text;
BEGIN
    IF p_merchant_id IS NOT NULL AND p_merchant_id IS DISTINCT FROM current_setting('app.merchant_id', true) THEN
        RAISE EXCEPTION 'merchant context mismatch' USING ERRCODE = '42501';
    END IF;
    PERFORM pg_advisory_xact_lock(7301, 1);
    SELECT event_hash INTO prev FROM gateway_audit_events ORDER BY sequence_id DESC LIMIT 1;
    canonical := concat_ws('|', coalesce(encode(prev, 'hex'), ''),
        coalesce(p_merchant_id, ''), coalesce(p_actor_key_id, ''), p_action, p_request_id,
        p_data::text);
    digest := digest(canonical, 'sha256');
    INSERT INTO gateway_audit_events(
        merchant_id, actor_key_id, action, request_id, event_data, previous_hash, event_hash
    ) VALUES (p_merchant_id, p_actor_key_id, p_action, p_request_id, p_data, prev, digest)
    RETURNING sequence_id INTO seq;
    RETURN seq;
END;
$$;

CREATE OR REPLACE FUNCTION gateway_revoke_audit_mutation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'gateway audit history is append-only' USING ERRCODE = '55000';
END;
$$;
DROP TRIGGER IF EXISTS gateway_audit_no_update_delete ON gateway_audit_events;
CREATE TRIGGER gateway_audit_no_update_delete
BEFORE UPDATE OR DELETE ON gateway_audit_events
FOR EACH ROW EXECUTE FUNCTION gateway_revoke_audit_mutation();

CREATE OR REPLACE FUNCTION gateway_verify_audit_chain()
RETURNS boolean LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE item gateway_audit_events%ROWTYPE; prior bytea; expected bytea; canonical text;
BEGIN
    FOR item IN SELECT * FROM gateway_audit_events ORDER BY sequence_id LOOP
        IF item.previous_hash IS DISTINCT FROM prior THEN
            RETURN false;
        END IF;
        canonical := concat_ws('|', coalesce(encode(prior, 'hex'), ''),
            coalesce(item.merchant_id, ''), coalesce(item.actor_key_id, ''), item.action,
            item.request_id, item.event_data::text);
        expected := digest(canonical, 'sha256');
        IF item.event_hash IS DISTINCT FROM expected THEN
            RETURN false;
        END IF;
        prior := item.event_hash;
    END LOOP;
    RETURN true;
END;
$$;

ALTER TABLE gateway_idempotency_requests ENABLE ROW LEVEL SECURITY;
ALTER TABLE gateway_idempotency_requests FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS gateway_idempotency_tenant_policy ON gateway_idempotency_requests;
CREATE POLICY gateway_idempotency_tenant_policy ON gateway_idempotency_requests
    USING (merchant_id = current_setting('app.merchant_id', true))
    WITH CHECK (merchant_id = current_setting('app.merchant_id', true));
ALTER TABLE gateway_audit_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE gateway_audit_events FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS gateway_audit_tenant_policy ON gateway_audit_events;
CREATE POLICY gateway_audit_tenant_policy ON gateway_audit_events
    USING (merchant_id IS NULL OR merchant_id = current_setting('app.merchant_id', true))
    WITH CHECK (merchant_id IS NULL OR merchant_id = current_setting('app.merchant_id', true));

CREATE TABLE IF NOT EXISTS gateway_schema_migrations (
    version integer PRIMARY KEY,
    applied_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
INSERT INTO gateway_schema_migrations(version) VALUES (1) ON CONFLICT DO NOTHING;

DO $$ BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'tally_gateway_app') THEN
        CREATE ROLE tally_gateway_app NOLOGIN;
    END IF;
END $$;

GRANT SELECT ON merchants, merchant_api_keys, gateway_idempotency_requests TO tally_gateway_app;
GRANT EXECUTE ON FUNCTION gateway_consume_nonce(text, text, timestamptz) TO tally_gateway_app;
GRANT EXECUTE ON FUNCTION gateway_begin_idempotency(text, text, bytea, timestamptz) TO tally_gateway_app;
GRANT EXECUTE ON FUNCTION gateway_complete_idempotency(text, text, bytea, smallint, jsonb) TO tally_gateway_app;
GRANT EXECUTE ON FUNCTION gateway_append_audit_event(text, text, text, text, jsonb) TO tally_gateway_app;
GRANT EXECUTE ON FUNCTION gateway_verify_audit_chain() TO tally_gateway_app;

COMMIT;
