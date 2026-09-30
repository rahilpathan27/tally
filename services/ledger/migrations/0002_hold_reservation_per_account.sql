BEGIN;

CREATE OR REPLACE FUNCTION ledger_place_hold(p_idempotency_key text, p_postings jsonb)
RETURNS bigint
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
DECLARE
    v_existing_id bigint;
    v_existing_hash bytea;
    v_payload_hash bytea;
    v_hold_id bigint;
    v_bad_account text;
BEGIN
    IF p_idempotency_key IS NULL OR length(p_idempotency_key) = 0 THEN
        RAISE EXCEPTION 'idempotency key is required' USING ERRCODE = '22023';
    END IF;
    IF p_postings IS NULL OR jsonb_typeof(p_postings) <> 'array'
       OR jsonb_array_length(p_postings) < 2 THEN
        RAISE EXCEPTION 'at least two hold postings are required' USING ERRCODE = '22023';
    END IF;
    IF EXISTS (
        SELECT 1 FROM jsonb_array_elements(p_postings) AS elements(value)
        WHERE jsonb_typeof(value) <> 'object'
           OR value->>'account_id' IS NULL
           OR value->>'direction' IS NULL
           OR value->>'direction' NOT IN ('debit', 'credit')
           OR value->>'amount_minor' IS NULL
           OR value->>'amount_minor' !~ '^[1-9][0-9]*$'
           OR (value->>'amount_minor')::numeric > 9007199254740991
    ) THEN
        RAISE EXCEPTION 'hold posting fields are invalid or amount exceeds safe integer limit'
            USING ERRCODE = '22023';
    END IF;
    IF (
        SELECT coalesce(sum(amount_minor) FILTER (WHERE direction = 'debit'), 0)
             <> coalesce(sum(amount_minor) FILTER (WHERE direction = 'credit'), 0)
        FROM jsonb_to_recordset(p_postings) AS p(account_id text, direction text, amount_minor bigint)
    ) THEN
        RAISE EXCEPTION 'hold is unbalanced' USING ERRCODE = '23514';
    END IF;

    v_payload_hash := digest(p_postings::text, 'sha256');
    PERFORM pg_advisory_xact_lock(hashtextextended('ledger-hold:' || p_idempotency_key, 0));
    SELECT hold_id, payload_hash INTO v_existing_id, v_existing_hash
    FROM ledger_holds WHERE idempotency_key = p_idempotency_key;
    IF FOUND THEN
        IF v_existing_hash <> v_payload_hash THEN
            RAISE EXCEPTION 'hold idempotency key reused with a different payload'
                USING ERRCODE = '23505';
        END IF;
        RETURN v_existing_id;
    END IF;
    IF (
        SELECT count(DISTINCT a.currency) > 1
        FROM jsonb_to_recordset(p_postings) AS p(account_id text)
        JOIN ledger_accounts a ON a.account_id = p.account_id
    ) THEN
        RAISE EXCEPTION 'cross-currency hold requires explicit FX legs' USING ERRCODE = '22023';
    END IF;
    IF EXISTS (
        SELECT 1
        FROM jsonb_to_recordset(p_postings) AS p(account_id text)
        LEFT JOIN ledger_accounts a ON a.account_id = p.account_id
        WHERE a.account_id IS NULL
    ) THEN
        RAISE EXCEPTION 'unknown ledger account' USING ERRCODE = '23503';
    END IF;
    PERFORM a.account_id
    FROM ledger_accounts a
    WHERE a.account_id IN (
        SELECT DISTINCT p.account_id
        FROM jsonb_to_recordset(p_postings) AS p(account_id text)
    )
    ORDER BY a.account_id
    FOR UPDATE;
    IF EXISTS (
        SELECT 1
        FROM jsonb_to_recordset(p_postings) AS p(account_id text)
        JOIN ledger_accounts a ON a.account_id = p.account_id
        WHERE a.closed
    ) THEN
        RAISE EXCEPTION 'hold on closed account' USING ERRCODE = '55000';
    END IF;

    WITH deltas AS (
        SELECT a.account_id,
               sum(CASE
                   WHEN (a.account_type IN ('asset', 'expense') AND p.direction = 'debit')
                     OR (a.account_type IN ('liability', 'equity', 'income') AND p.direction = 'credit')
                   THEN p.amount_minor ELSE -p.amount_minor END) AS delta
        FROM jsonb_to_recordset(p_postings) AS p(account_id text, direction text, amount_minor bigint)
        JOIN ledger_accounts a ON a.account_id = p.account_id
        GROUP BY a.account_id
    ), per_hold AS (
        SELECT hp.hold_id, hp.account_id,
               sum(CASE
                   WHEN (a.account_type IN ('asset', 'expense') AND hp.direction = 'debit')
                     OR (a.account_type IN ('liability', 'equity', 'income') AND hp.direction = 'credit')
                   THEN hp.amount_minor ELSE -hp.amount_minor END) AS natural_pending
        FROM ledger_hold_postings hp
        JOIN ledger_holds h ON h.hold_id = hp.hold_id AND h.status = 'pending'
        JOIN ledger_accounts a ON a.account_id = hp.account_id
        GROUP BY hp.hold_id, hp.account_id
    ), reserved AS (
        SELECT account_id, sum(least(natural_pending, 0)) AS natural_pending
        FROM per_hold
        GROUP BY account_id
    )
    SELECT a.account_id INTO v_bad_account
    FROM deltas d
    JOIN ledger_accounts a USING (account_id)
    LEFT JOIN ledger_account_balances b USING (account_id)
    LEFT JOIN reserved r USING (account_id)
    WHERE NOT a.allow_negative
      AND coalesce(b.posted_minor, 0) + coalesce(r.natural_pending, 0) + d.delta < 0
    LIMIT 1;
    IF v_bad_account IS NOT NULL THEN
        RAISE EXCEPTION 'hold would make available balance negative: %', v_bad_account
            USING ERRCODE = '23514';
    END IF;

    INSERT INTO ledger_holds (idempotency_key, payload_hash)
    VALUES (p_idempotency_key, v_payload_hash)
    RETURNING hold_id INTO v_hold_id;
    INSERT INTO ledger_hold_postings (hold_id, account_id, direction, amount_minor, currency)
    SELECT v_hold_id, p.account_id, p.direction::ledger_direction, sum(p.amount_minor), a.currency
    FROM jsonb_to_recordset(p_postings) AS p(account_id text, direction text, amount_minor bigint)
    JOIN ledger_accounts a ON a.account_id = p.account_id
    GROUP BY p.account_id, p.direction, a.currency;
    INSERT INTO ledger_account_balances (account_id, pending_debits, pending_credits)
    SELECT p.account_id,
           coalesce(sum(p.amount_minor) FILTER (WHERE p.direction = 'debit'), 0),
           coalesce(sum(p.amount_minor) FILTER (WHERE p.direction = 'credit'), 0)
    FROM jsonb_to_recordset(p_postings) AS p(account_id text, direction text, amount_minor bigint)
    GROUP BY p.account_id
    ON CONFLICT (account_id) DO UPDATE
    SET pending_debits = ledger_account_balances.pending_debits + EXCLUDED.pending_debits,
        pending_credits = ledger_account_balances.pending_credits + EXCLUDED.pending_credits,
        version = ledger_account_balances.version + 1,
        updated_at = now();
    RETURN v_hold_id;
END;
$$;

INSERT INTO ledger_schema_migrations(version) VALUES (2) ON CONFLICT DO NOTHING;

COMMIT;
