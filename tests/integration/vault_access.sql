BEGIN;

INSERT INTO vault_cards(token, encrypted_pan, bin_prefix, last4, expiry_month, expiry_year)
VALUES ('vault-access-check', decode(repeat('ab', 101), 'hex'), '424242', '4242', 12, 2099);

SELECT vault_append_access_event('vault-access-check', 'network-simulator', 'sql-check');
DO $$ BEGIN
    IF NOT vault_verify_access_chain() THEN RAISE EXCEPTION 'audit hash chain did not verify'; END IF;
END $$;

DO $$ BEGIN
    BEGIN
        PERFORM vault_append_access_event('vault-access-check', 'merchant-api', 'forbidden');
        RAISE EXCEPTION 'unauthorized caller was accepted';
    EXCEPTION WHEN insufficient_privilege THEN NULL;
    END;
END $$;

DO $$ BEGIN
    BEGIN
        UPDATE vault_access_events SET request_id = 'changed' WHERE request_id = 'sql-check';
        RAISE EXCEPTION 'audit update was accepted';
    EXCEPTION WHEN SQLSTATE '55000' THEN NULL;
    END;
    BEGIN
        DELETE FROM vault_access_events WHERE request_id = 'sql-check';
        RAISE EXCEPTION 'audit delete was accepted';
    EXCEPTION WHEN SQLSTATE '55000' THEN NULL;
    END;
END $$;

SET LOCAL ROLE tally_vault_app;
DO $$ BEGIN
    BEGIN
        PERFORM 1 FROM vault_access_events;
        RAISE EXCEPTION 'application role can read audit table';
    EXCEPTION WHEN insufficient_privilege THEN NULL;
    END;
    BEGIN
        INSERT INTO vault_access_events(token, caller_id, request_id, action, event_hash)
        VALUES ('vault-access-check', 'network-simulator', 'direct-write', 'detokenize', decode(repeat('00', 32), 'hex'));
        RAISE EXCEPTION 'application role can directly write audit table';
    EXCEPTION WHEN insufficient_privilege THEN NULL;
    END;
END $$;

ROLLBACK;
