\set ON_ERROR_STOP on
BEGIN;

INSERT INTO merchants(merchant_id, display_name)
VALUES ('integration-merchant-a', 'Integration A'), ('integration-merchant-b', 'Integration B');
INSERT INTO merchant_api_keys(key_id, merchant_id, secret_ciphertext, scopes, mode)
VALUES ('integration-key-a', 'integration-merchant-a', decode('aabbcc', 'hex'), ARRAY['payments:write'], 'test');

DO $$
DECLARE result record; fp bytea := digest('request-one', 'sha256');
BEGIN
    IF NOT gateway_consume_nonce('integration-key-a', 'nonce-000000000001', clock_timestamp() + interval '10 minutes') THEN
        RAISE EXCEPTION 'first nonce must be consumed';
    END IF;
    IF gateway_consume_nonce('integration-key-a', 'nonce-000000000001', clock_timestamp() + interval '10 minutes') THEN
        RAISE EXCEPTION 'duplicate nonce must be rejected';
    END IF;

    PERFORM set_config('app.merchant_id', 'integration-merchant-a', true);
    SELECT * INTO result FROM gateway_begin_idempotency(
        'integration-merchant-a', 'order-001', fp, clock_timestamp() + interval '1 day'
    );
    IF result.outcome <> 'started' THEN RAISE EXCEPTION 'first request must start'; END IF;
    PERFORM gateway_complete_idempotency(
        'integration-merchant-a', 'order-001', fp, 201::smallint, '{"payment_id":"pay-1"}'::jsonb
    );
    SELECT * INTO result FROM gateway_begin_idempotency(
        'integration-merchant-a', 'order-001', fp, clock_timestamp() + interval '1 day'
    );
    IF result.outcome <> 'replay' OR result.response_status <> 201
       OR result.response_body->>'payment_id' <> 'pay-1' THEN
        RAISE EXCEPTION 'completed request must replay its original response';
    END IF;

    BEGIN
        PERFORM * FROM gateway_begin_idempotency(
            'integration-merchant-a', 'order-001', digest('different-request', 'sha256'),
            clock_timestamp() + interval '1 day'
        );
        RAISE EXCEPTION 'changed payload should fail';
    EXCEPTION WHEN unique_violation THEN NULL;
    END;

    BEGIN
        PERFORM * FROM gateway_begin_idempotency(
            'integration-merchant-b', 'order-001', fp, clock_timestamp() + interval '1 day'
        );
        RAISE EXCEPTION 'cross-tenant context mismatch should fail';
    EXCEPTION WHEN insufficient_privilege THEN NULL;
    END;

    PERFORM set_config('app.merchant_id', 'integration-merchant-b', true);
    SELECT * INTO result FROM gateway_begin_idempotency(
        'integration-merchant-b', 'private-order', fp, clock_timestamp() + interval '1 day'
    );
    IF result.outcome <> 'started' THEN RAISE EXCEPTION 'tenant B fixture did not start'; END IF;
    PERFORM set_config('app.merchant_id', 'integration-merchant-a', true);

    PERFORM gateway_append_audit_event(
        'integration-merchant-a', 'integration-key-a', 'payment.create', 'req-1', '{"amount_minor":125}'::jsonb
    );
    PERFORM gateway_append_audit_event(
        'integration-merchant-a', 'integration-key-a', 'payment.confirm', 'req-2', '{"result":"ok"}'::jsonb
    );
    IF NOT gateway_verify_audit_chain() THEN RAISE EXCEPTION 'audit chain validation failed'; END IF;

    INSERT INTO gateway_request_nonces(key_id, nonce, expires_at)
    VALUES ('integration-key-a', 'expired-nonce-00001', clock_timestamp() - interval '1 second');
    INSERT INTO gateway_idempotency_requests(
        merchant_id, idempotency_key, request_fingerprint, state, expires_at
    ) VALUES ('integration-merchant-a', 'expired-order', fp, 'in_progress',
              clock_timestamp() - interval '1 second');
    SELECT * INTO result FROM gateway_cleanup_expired_state(10);
    -- Previous runs may leave other expired nonces in this shared test DB.
    -- The cleanup is bounded, so assert it removed at least this fixture.
    IF result.nonces_deleted < 1 OR result.idempotency_deleted < 1 THEN
        RAISE EXCEPTION 'expired state was not cleaned up';
    END IF;
    IF EXISTS (SELECT FROM gateway_request_nonces WHERE nonce = 'expired-nonce-00001')
       OR EXISTS (SELECT FROM gateway_idempotency_requests WHERE idempotency_key = 'expired-order') THEN
        RAISE EXCEPTION 'expired fixture state remains after cleanup';
    END IF;

    BEGIN
        UPDATE gateway_audit_events SET action = 'tampered' WHERE request_id = 'req-1';
        RAISE EXCEPTION 'audit update should fail';
    EXCEPTION WHEN object_not_in_prerequisite_state THEN NULL;
    END;
END $$;

SET LOCAL ROLE tally_gateway_app;
SELECT set_config('app.merchant_id', 'integration-merchant-a', true);
DO $$
DECLARE visible_count integer;
BEGIN
    SELECT count(*) INTO visible_count FROM gateway_idempotency_requests
     WHERE merchant_id = 'integration-merchant-b';
    IF visible_count <> 0 THEN RAISE EXCEPTION 'RLS exposed another tenant'; END IF;
    SELECT count(*) INTO visible_count
      FROM gateway_lookup_api_key('integration-key-a');
    IF visible_count <> 1 THEN RAISE EXCEPTION 'authorized key lookup did not return its row'; END IF;
    BEGIN
        PERFORM * FROM merchant_api_keys;
        RAISE EXCEPTION 'direct key-table read should be denied';
    EXCEPTION WHEN insufficient_privilege THEN NULL;
    END;
    BEGIN
        INSERT INTO gateway_idempotency_requests(
            merchant_id, idempotency_key, request_fingerprint, state, expires_at
        ) VALUES ('integration-merchant-a', 'bypass', digest('x', 'sha256'),
                  'in_progress', clock_timestamp() + interval '1 day');
        RAISE EXCEPTION 'direct table write should be denied';
    EXCEPTION WHEN insufficient_privilege THEN NULL;
    END;
    IF NOT gateway_verify_audit_chain() THEN RAISE EXCEPTION 'app role audit verification failed'; END IF;
END $$;
RESET ROLE;

ROLLBACK;
SELECT 'gateway auth integration checks passed' AS result;
