BEGIN;

CREATE OR REPLACE FUNCTION gateway_begin_idempotency(
    p_merchant_id text, p_key text, p_fingerprint bytea, p_expires_at timestamptz
) RETURNS TABLE(outcome text, response_status smallint, response_body jsonb)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE prior gateway_idempotency_requests%ROWTYPE; inserted_count integer;
BEGIN
    IF p_merchant_id IS DISTINCT FROM current_setting('app.merchant_id', true) THEN
        RAISE EXCEPTION 'merchant context mismatch' USING ERRCODE = '42501';
    END IF;
    DELETE FROM gateway_idempotency_requests r
     WHERE r.merchant_id = p_merchant_id AND r.idempotency_key = p_key
       AND r.expires_at <= clock_timestamp();
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

INSERT INTO gateway_schema_migrations(version) VALUES (2) ON CONFLICT DO NOTHING;
COMMIT;
