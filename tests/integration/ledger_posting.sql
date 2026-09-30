BEGIN;

INSERT INTO ledger_accounts (account_id, account_type, currency, allow_negative)
VALUES
    ('it-cash', 'asset', 'INR', false),
    ('it-merchant', 'liability', 'INR', false),
    ('it-equity', 'equity', 'INR', true),
    ('it-closed', 'liability', 'INR', false),
    ('it-usd', 'asset', 'USD', true);
UPDATE ledger_accounts SET closed = true WHERE account_id = 'it-closed';

DO $$
DECLARE
    entry_id bigint;
    replay_id bigint;
    v_hold_id bigint;
    positive_hold_id bigint;
    current_status ledger_hold_status;
    pending bigint;
    posted bigint;
BEGIN
    entry_id := ledger_post_entry(
        'it-seed',
        '[{"account_id":"it-cash","direction":"debit","amount_minor":100000},
          {"account_id":"it-equity","direction":"credit","amount_minor":100000}]'::jsonb
    );
    replay_id := ledger_post_entry(
        'it-seed',
        '[{"account_id":"it-cash","direction":"debit","amount_minor":100000},
          {"account_id":"it-equity","direction":"credit","amount_minor":100000}]'::jsonb
    );
    IF replay_id <> entry_id THEN RAISE EXCEPTION 'same-key replay did not return original entry'; END IF;

    BEGIN
        PERFORM ledger_post_entry(
            'it-seed',
            '[{"account_id":"it-cash","direction":"debit","amount_minor":100001},
              {"account_id":"it-equity","direction":"credit","amount_minor":100001}]'::jsonb
        );
        RAISE EXCEPTION 'same-key different-payload replay was accepted';
    EXCEPTION WHEN unique_violation THEN NULL;
    END;

    BEGIN
        PERFORM ledger_post_entry(
            'it-unbalanced',
            '[{"account_id":"it-cash","direction":"debit","amount_minor":2},
              {"account_id":"it-equity","direction":"credit","amount_minor":1}]'::jsonb
        );
        RAISE EXCEPTION 'unbalanced entry was accepted';
    EXCEPTION WHEN check_violation THEN NULL;
    END;

    BEGIN
        PERFORM ledger_post_entry(
            'it-overdraw',
            '[{"account_id":"it-cash","direction":"credit","amount_minor":100001},
              {"account_id":"it-merchant","direction":"debit","amount_minor":100001}]'::jsonb
        );
        RAISE EXCEPTION 'negative asset balance was accepted';
    EXCEPTION WHEN check_violation THEN NULL;
    END;

    BEGIN
        PERFORM ledger_post_entry(
            'it-closed-account',
            '[{"account_id":"it-cash","direction":"debit","amount_minor":1},
              {"account_id":"it-closed","direction":"credit","amount_minor":1}]'::jsonb
        );
        RAISE EXCEPTION 'closed account posting was accepted';
    EXCEPTION WHEN object_not_in_prerequisite_state THEN NULL;
    END;

    BEGIN
        PERFORM ledger_post_entry(
            'it-cross-currency',
            '[{"account_id":"it-cash","direction":"debit","amount_minor":1},
              {"account_id":"it-usd","direction":"credit","amount_minor":1}]'::jsonb
        );
        RAISE EXCEPTION 'cross-currency entry was accepted';
    EXCEPTION WHEN invalid_parameter_value THEN NULL;
    END;

    v_hold_id := ledger_place_hold(
        'it-auth',
        '[{"account_id":"it-cash","direction":"credit","amount_minor":70000},
          {"account_id":"it-equity","direction":"debit","amount_minor":70000}]'::jsonb
    );
    pending := (SELECT pending_credits FROM ledger_account_balances WHERE account_id = 'it-cash');
    IF pending <> 70000 THEN RAISE EXCEPTION 'hold did not reserve pending credits'; END IF;
    positive_hold_id := ledger_place_hold(
        'it-pending-credit',
        '[{"account_id":"it-cash","direction":"debit","amount_minor":90000},
          {"account_id":"it-equity","direction":"credit","amount_minor":90000}]'::jsonb
    );

    BEGIN
        PERFORM ledger_place_hold(
            'it-auth-too-large',
            '[{"account_id":"it-cash","direction":"credit","amount_minor":40000},
              {"account_id":"it-equity","direction":"debit","amount_minor":40000}]'::jsonb
        );
        RAISE EXCEPTION 'oversized hold was accepted';
    EXCEPTION WHEN check_violation THEN NULL;
    END;

    PERFORM ledger_post_hold(v_hold_id);
    PERFORM ledger_post_hold(v_hold_id);
    posted := (SELECT posted_minor FROM ledger_account_balances WHERE account_id = 'it-cash');
    pending := (SELECT pending_credits FROM ledger_account_balances WHERE account_id = 'it-cash');
    IF posted <> 30000 OR pending <> 0 THEN
        RAISE EXCEPTION 'hold post did not atomically move pending amount to posted balance';
    END IF;
    PERFORM ledger_void_hold(positive_hold_id);
    SELECT status INTO current_status
    FROM ledger_holds WHERE ledger_holds.hold_id = v_hold_id;
    IF current_status <> 'posted' THEN RAISE EXCEPTION 'hold status did not become posted'; END IF;

    v_hold_id := ledger_place_hold(
        'it-netted-hold',
        '[{"account_id":"it-cash","direction":"credit","amount_minor":20000},
          {"account_id":"it-cash","direction":"debit","amount_minor":5000},
          {"account_id":"it-equity","direction":"debit","amount_minor":15000}]'::jsonb
    );
    pending := (SELECT pending_credits FROM ledger_account_balances WHERE account_id = 'it-cash');
    IF pending <> 20000 THEN RAISE EXCEPTION 'duplicate account sides were not aggregated'; END IF;
    BEGIN
        PERFORM ledger_place_hold(
            'it-netted-overdraw',
            '[{"account_id":"it-cash","direction":"credit","amount_minor":20000},
              {"account_id":"it-equity","direction":"debit","amount_minor":20000}]'::jsonb
        );
        RAISE EXCEPTION 'hold exceeded net available balance';
    EXCEPTION WHEN check_violation THEN NULL;
    END;
    PERFORM ledger_void_hold(v_hold_id);

    v_hold_id := ledger_place_hold(
        'it-auth-to-void',
        '[{"account_id":"it-cash","direction":"credit","amount_minor":10000},
          {"account_id":"it-equity","direction":"debit","amount_minor":10000}]'::jsonb
    );
    PERFORM ledger_void_hold(v_hold_id);
    pending := (SELECT pending_credits FROM ledger_account_balances WHERE account_id = 'it-cash');
    IF pending <> 0 THEN RAISE EXCEPTION 'void did not release pending funds'; END IF;

    IF EXISTS (SELECT 1 FROM ledger_verify_integrity() WHERE NOT ok) THEN
        RAISE EXCEPTION 'ledger integrity verifier reported a failure';
    END IF;
END;
$$;

SET LOCAL ROLE tally_ledger_app;
DO $$
BEGIN
    BEGIN
        INSERT INTO ledger_journal_entries (
            idempotency_key, payload_hash, previous_hash, entry_hash
        ) VALUES (
            'it-direct-write', decode(repeat('00', 32), 'hex'),
            decode(repeat('00', 32), 'hex'), decode(repeat('00', 32), 'hex')
        );
        RAISE EXCEPTION 'application role inserted directly into immutable history';
    EXCEPTION WHEN insufficient_privilege THEN NULL;
    END;
    IF EXISTS (SELECT 1 FROM ledger_verify_integrity() WHERE NOT ok) THEN
        RAISE EXCEPTION 'application role could not read ledger integrity results';
    END IF;
END;
$$;
RESET ROLE;

ROLLBACK;
