BEGIN;

-- A reservation left in progress by a crashed request can be taken over after its lease.
ALTER TABLE gateway_idempotency_requests
    ADD COLUMN IF NOT EXISTS locked_until timestamptz NOT NULL
        DEFAULT clock_timestamp() + interval '60 seconds';

DROP FUNCTION IF EXISTS gateway_begin_idempotency(text, text, bytea, timestamptz);
CREATE FUNCTION gateway_begin_idempotency(
    p_merchant_id text, p_key text, p_fingerprint bytea, p_expires_at timestamptz,
    p_lease_seconds integer DEFAULT 60
) RETURNS TABLE(outcome text, response_status smallint, response_body jsonb)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE prior gateway_idempotency_requests%ROWTYPE; inserted_count integer;
BEGIN
    IF p_merchant_id IS DISTINCT FROM current_setting('app.merchant_id', true) THEN
        RAISE EXCEPTION 'merchant context mismatch' USING ERRCODE = '42501';
    END IF;
    IF p_lease_seconds < 1 OR p_lease_seconds > 3600 THEN
        RAISE EXCEPTION 'lease out of range' USING ERRCODE = '22023';
    END IF;
    DELETE FROM gateway_idempotency_requests r
     WHERE r.merchant_id = p_merchant_id AND r.idempotency_key = p_key
       AND r.expires_at <= clock_timestamp();
    INSERT INTO gateway_idempotency_requests(
        merchant_id, idempotency_key, request_fingerprint, state, expires_at, locked_until
    ) VALUES (
        p_merchant_id, p_key, p_fingerprint, 'in_progress', p_expires_at,
        clock_timestamp() + make_interval(secs => p_lease_seconds)
    )
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
    ELSIF prior.locked_until <= clock_timestamp() THEN
        -- The previous holder crashed or stalled past its lease; the handler is re-entrant
        -- because every effect below it is guarded by state transitions and idempotent keys.
        UPDATE gateway_idempotency_requests r
           SET locked_until = clock_timestamp() + make_interval(secs => p_lease_seconds)
         WHERE r.merchant_id = p_merchant_id AND r.idempotency_key = p_key;
        RETURN QUERY SELECT 'started'::text, NULL::smallint, NULL::jsonb;
    ELSE
        RETURN QUERY SELECT 'in_progress'::text, NULL::smallint, NULL::jsonb;
    END IF;
END;
$$;

-- Release a reservation whose handler failed with a retryable (5xx) error.
CREATE OR REPLACE FUNCTION gateway_release_idempotency(
    p_merchant_id text, p_key text, p_fingerprint bytea
) RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
BEGIN
    IF p_merchant_id IS DISTINCT FROM current_setting('app.merchant_id', true) THEN
        RAISE EXCEPTION 'merchant context mismatch' USING ERRCODE = '42501';
    END IF;
    DELETE FROM gateway_idempotency_requests
     WHERE merchant_id = p_merchant_id AND idempotency_key = p_key
       AND request_fingerprint = p_fingerprint AND state = 'in_progress';
END;
$$;

REVOKE ALL ON FUNCTION gateway_begin_idempotency(text, text, bytea, timestamptz, integer)
    FROM PUBLIC;
REVOKE ALL ON FUNCTION gateway_release_idempotency(text, text, bytea) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION gateway_begin_idempotency(text, text, bytea, timestamptz, integer)
    TO tally_gateway_app;
GRANT EXECUTE ON FUNCTION gateway_release_idempotency(text, text, bytea) TO tally_gateway_app;

INSERT INTO gateway_schema_migrations(version) VALUES (5) ON CONFLICT DO NOTHING;
COMMIT;
