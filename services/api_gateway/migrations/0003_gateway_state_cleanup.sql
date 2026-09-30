BEGIN;

CREATE OR REPLACE FUNCTION gateway_cleanup_expired_state(p_limit integer DEFAULT 10000)
RETURNS TABLE(nonces_deleted integer, idempotency_deleted integer)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
BEGIN
    IF p_limit < 1 OR p_limit > 100000 THEN
        RAISE EXCEPTION 'cleanup limit out of range' USING ERRCODE = '22023';
    END IF;
    WITH doomed AS (
        SELECT ctid FROM gateway_request_nonces
         WHERE expires_at <= clock_timestamp() ORDER BY expires_at LIMIT p_limit
    )
    DELETE FROM gateway_request_nonces n USING doomed d WHERE n.ctid = d.ctid;
    GET DIAGNOSTICS nonces_deleted = ROW_COUNT;

    WITH doomed AS (
        SELECT ctid FROM gateway_idempotency_requests
         WHERE expires_at <= clock_timestamp() ORDER BY expires_at LIMIT p_limit
    )
    DELETE FROM gateway_idempotency_requests r USING doomed d WHERE r.ctid = d.ctid;
    GET DIAGNOSTICS idempotency_deleted = ROW_COUNT;
    RETURN NEXT;
END;
$$;

DO $$ BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'tally_gateway_worker') THEN
        CREATE ROLE tally_gateway_worker NOLOGIN;
    END IF;
END $$;
GRANT EXECUTE ON FUNCTION gateway_cleanup_expired_state(integer) TO tally_gateway_worker;

INSERT INTO gateway_schema_migrations(version) VALUES (3) ON CONFLICT DO NOTHING;
COMMIT;
