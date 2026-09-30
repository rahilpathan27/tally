BEGIN;

CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE TABLE ledger_schema_migrations (
    version integer PRIMARY KEY,
    applied_at timestamptz NOT NULL DEFAULT now()
);

CREATE TYPE ledger_account_type AS ENUM ('asset', 'liability', 'equity', 'income', 'expense');
CREATE TYPE ledger_direction AS ENUM ('debit', 'credit');
CREATE TYPE ledger_hold_status AS ENUM ('pending', 'posted', 'void');

CREATE TABLE ledger_accounts (
    account_id text PRIMARY KEY,
    account_type ledger_account_type NOT NULL,
    currency char(3) NOT NULL CHECK (currency ~ '^[A-Z]{3}$'),
    allow_negative boolean NOT NULL DEFAULT false,
    closed boolean NOT NULL DEFAULT false,
    shard_parent_id text REFERENCES ledger_accounts(account_id),
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE ledger_account_balances (
    account_id text PRIMARY KEY REFERENCES ledger_accounts(account_id),
    posted_minor bigint NOT NULL DEFAULT 0,
    pending_debits bigint NOT NULL DEFAULT 0 CHECK (pending_debits >= 0),
    pending_credits bigint NOT NULL DEFAULT 0 CHECK (pending_credits >= 0),
    version bigint NOT NULL DEFAULT 0 CHECK (version >= 0),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE ledger_journal_entries (
    entry_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    idempotency_key text NOT NULL UNIQUE,
    payload_hash bytea NOT NULL CHECK (octet_length(payload_hash) = 32),
    previous_hash bytea NOT NULL CHECK (octet_length(previous_hash) = 32),
    entry_hash bytea NOT NULL UNIQUE CHECK (octet_length(entry_hash) = 32),
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE ledger_postings (
    posting_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    entry_id bigint NOT NULL REFERENCES ledger_journal_entries(entry_id),
    account_id text NOT NULL REFERENCES ledger_accounts(account_id),
    direction ledger_direction NOT NULL,
    amount_minor bigint NOT NULL CHECK (amount_minor > 0),
    currency char(3) NOT NULL CHECK (currency ~ '^[A-Z]{3}$'),
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ledger_postings_account_time_idx ON ledger_postings (account_id, created_at, posting_id);
CREATE INDEX ledger_postings_entry_idx ON ledger_postings (entry_id);

CREATE TABLE ledger_holds (
    hold_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    idempotency_key text NOT NULL UNIQUE,
    payload_hash bytea NOT NULL CHECK (octet_length(payload_hash) = 32),
    status ledger_hold_status NOT NULL DEFAULT 'pending',
    entry_id bigint UNIQUE REFERENCES ledger_journal_entries(entry_id),
    created_at timestamptz NOT NULL DEFAULT now(),
    resolved_at timestamptz,
    CHECK ((status = 'posted') = (entry_id IS NOT NULL)),
    CHECK ((status = 'pending') = (resolved_at IS NULL))
);

CREATE TABLE ledger_hold_postings (
    hold_id bigint NOT NULL REFERENCES ledger_holds(hold_id),
    account_id text NOT NULL REFERENCES ledger_accounts(account_id),
    direction ledger_direction NOT NULL,
    amount_minor bigint NOT NULL CHECK (amount_minor > 0),
    currency char(3) NOT NULL CHECK (currency ~ '^[A-Z]{3}$'),
    PRIMARY KEY (hold_id, account_id, direction)
);

CREATE TABLE ledger_balance_snapshots (
    snapshot_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    captured_at timestamptz NOT NULL DEFAULT now(),
    last_entry_id bigint,
    integrity_hash bytea NOT NULL CHECK (octet_length(integrity_hash) = 32)
);
CREATE TABLE ledger_balance_snapshot_items (
    snapshot_id bigint NOT NULL REFERENCES ledger_balance_snapshots(snapshot_id),
    account_id text NOT NULL REFERENCES ledger_accounts(account_id),
    posted_minor bigint NOT NULL,
    version bigint NOT NULL,
    PRIMARY KEY (snapshot_id, account_id)
);

CREATE TABLE ledger_chain_head (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    last_entry_id bigint,
    last_hash bytea NOT NULL CHECK (octet_length(last_hash) = 32)
);
INSERT INTO ledger_chain_head (singleton, last_entry_id, last_hash)
VALUES (true, NULL, decode(repeat('00', 32), 'hex'));

CREATE FUNCTION ledger_reject_history_mutation() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'ledger history is append-only';
END;
$$;

CREATE TRIGGER ledger_entries_no_mutation
BEFORE UPDATE OR DELETE ON ledger_journal_entries
FOR EACH ROW EXECUTE FUNCTION ledger_reject_history_mutation();
CREATE TRIGGER ledger_postings_no_mutation
BEFORE UPDATE OR DELETE ON ledger_postings
FOR EACH ROW EXECUTE FUNCTION ledger_reject_history_mutation();
CREATE TRIGGER ledger_holds_no_delete
BEFORE DELETE ON ledger_holds
FOR EACH ROW EXECUTE FUNCTION ledger_reject_history_mutation();
CREATE TRIGGER ledger_hold_postings_no_mutation
BEFORE UPDATE OR DELETE ON ledger_hold_postings
FOR EACH ROW EXECUTE FUNCTION ledger_reject_history_mutation();

CREATE FUNCTION ledger_post_entry(p_idempotency_key text, p_postings jsonb)
RETURNS bigint
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
DECLARE
    v_existing_id bigint;
    v_existing_hash bytea;
    v_payload_hash bytea;
    v_previous_hash bytea;
    v_entry_hash bytea;
    v_entry_id bigint;
    v_bad_account text;
BEGIN
    IF p_idempotency_key IS NULL OR length(p_idempotency_key) = 0 THEN
        RAISE EXCEPTION 'idempotency key is required' USING ERRCODE = '22023';
    END IF;
    IF p_postings IS NULL OR jsonb_typeof(p_postings) <> 'array'
       OR jsonb_array_length(p_postings) < 2 THEN
        RAISE EXCEPTION 'at least two postings are required' USING ERRCODE = '22023';
    END IF;
    IF EXISTS (
        SELECT 1
        FROM jsonb_array_elements(p_postings) AS elements(value)
        WHERE jsonb_typeof(value) <> 'object'
           OR value->>'account_id' IS NULL
           OR value->>'direction' IS NULL
           OR value->>'direction' NOT IN ('debit', 'credit')
           OR value->>'amount_minor' IS NULL
           OR value->>'amount_minor' !~ '^[1-9][0-9]*$'
           OR (value->>'amount_minor')::numeric > 9007199254740991
    ) THEN
        RAISE EXCEPTION 'posting fields are invalid or amount exceeds safe integer limit'
            USING ERRCODE = '22023';
    END IF;

    v_payload_hash := digest(p_postings::text, 'sha256');
    PERFORM pg_advisory_xact_lock(hashtextextended('ledger-idempotency:' || p_idempotency_key, 0));
    SELECT entry_id, payload_hash INTO v_existing_id, v_existing_hash
    FROM ledger_journal_entries WHERE idempotency_key = p_idempotency_key;
    IF FOUND THEN
        IF v_existing_hash <> v_payload_hash THEN
            RAISE EXCEPTION 'idempotency key reused with a different payload'
                USING ERRCODE = '23505';
        END IF;
        RETURN v_existing_id;
    END IF;

    IF EXISTS (
        SELECT 1
        FROM jsonb_to_recordset(p_postings) AS p(account_id text, direction text, amount_minor bigint)
        LEFT JOIN ledger_accounts a ON a.account_id = p.account_id
        WHERE a.account_id IS NULL
    ) THEN
        RAISE EXCEPTION 'unknown ledger account' USING ERRCODE = '23503';
    END IF;

    -- Lock every participating account in global lexical order before reading balances.
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
        RAISE EXCEPTION 'posting to closed account' USING ERRCODE = '55000';
    END IF;
    IF (
        SELECT count(DISTINCT a.currency) > 1
        FROM jsonb_to_recordset(p_postings) AS p(account_id text)
        JOIN ledger_accounts a ON a.account_id = p.account_id
    ) THEN
        RAISE EXCEPTION 'cross-currency entry requires explicit FX legs' USING ERRCODE = '22023';
    END IF;

    IF (
        SELECT coalesce(sum(amount_minor) FILTER (WHERE direction = 'debit'), 0)
             <> coalesce(sum(amount_minor) FILTER (WHERE direction = 'credit'), 0)
        FROM jsonb_to_recordset(p_postings) AS p(account_id text, direction text, amount_minor bigint)
    ) THEN
        RAISE EXCEPTION 'journal entry is unbalanced' USING ERRCODE = '23514';
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
        RAISE EXCEPTION 'posting would make account negative: %', v_bad_account
            USING ERRCODE = '23514';
    END IF;

    PERFORM 1 FROM ledger_chain_head WHERE singleton FOR UPDATE;
    SELECT last_hash INTO v_previous_hash FROM ledger_chain_head WHERE singleton;
    -- Reserve the sequence value first so it is included in the tamper-evident digest.
    v_entry_id := nextval(pg_get_serial_sequence('ledger_journal_entries', 'entry_id'));
    v_entry_hash := digest(
        v_entry_id::text || ':' || encode(convert_to(p_idempotency_key, 'UTF8'), 'hex') || ':' ||
        encode(v_payload_hash, 'hex') || ':' || encode(v_previous_hash, 'hex'),
        'sha256'
    );
    INSERT INTO ledger_journal_entries (
        entry_id, idempotency_key, payload_hash, previous_hash, entry_hash
    ) OVERRIDING SYSTEM VALUE VALUES (
        v_entry_id, p_idempotency_key, v_payload_hash, v_previous_hash, v_entry_hash
    );
    INSERT INTO ledger_postings (entry_id, account_id, direction, amount_minor, currency)
    SELECT v_entry_id, p.account_id, p.direction::ledger_direction, p.amount_minor, a.currency
    FROM jsonb_to_recordset(p_postings) AS p(account_id text, direction text, amount_minor bigint)
    JOIN ledger_accounts a ON a.account_id = p.account_id;

    INSERT INTO ledger_account_balances (account_id, posted_minor, version)
    SELECT d.account_id, d.delta, 1
    FROM (
        SELECT a.account_id,
               sum(CASE
                   WHEN (a.account_type IN ('asset', 'expense') AND p.direction = 'debit')
                     OR (a.account_type IN ('liability', 'equity', 'income') AND p.direction = 'credit')
                   THEN p.amount_minor ELSE -p.amount_minor END) AS delta
        FROM jsonb_to_recordset(p_postings) AS p(account_id text, direction text, amount_minor bigint)
        JOIN ledger_accounts a ON a.account_id = p.account_id
        GROUP BY a.account_id
    ) AS d
    ON CONFLICT (account_id) DO UPDATE
    SET posted_minor = ledger_account_balances.posted_minor + EXCLUDED.posted_minor,
        version = ledger_account_balances.version + 1,
        updated_at = now();

    UPDATE ledger_chain_head
    SET last_entry_id = v_entry_id, last_hash = v_entry_hash
    WHERE singleton;
    RETURN v_entry_id;
END;
$$;

REVOKE ALL ON FUNCTION ledger_post_entry(text, jsonb) FROM PUBLIC;

CREATE FUNCTION ledger_place_hold(p_idempotency_key text, p_postings jsonb)
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

CREATE FUNCTION ledger_void_hold(p_hold_id bigint) RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
DECLARE
    v_status ledger_hold_status;
BEGIN
    SELECT status INTO v_status FROM ledger_holds WHERE hold_id = p_hold_id FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'hold not found' USING ERRCODE = 'P0002';
    END IF;
    IF v_status = 'void' THEN
        RETURN;
    END IF;
    IF v_status = 'posted' THEN
        RAISE EXCEPTION 'posted hold cannot be voided' USING ERRCODE = '55000';
    END IF;
    PERFORM a.account_id
    FROM ledger_accounts a
    JOIN ledger_hold_postings hp ON hp.account_id = a.account_id
    WHERE hp.hold_id = p_hold_id
    ORDER BY a.account_id
    FOR UPDATE OF a;
    UPDATE ledger_account_balances b
    SET pending_debits = b.pending_debits - d.debits,
        pending_credits = b.pending_credits - d.credits,
        version = b.version + 1,
        updated_at = now()
    FROM (
        SELECT account_id,
               coalesce(sum(amount_minor) FILTER (WHERE direction = 'debit'), 0) AS debits,
               coalesce(sum(amount_minor) FILTER (WHERE direction = 'credit'), 0) AS credits
        FROM ledger_hold_postings WHERE hold_id = p_hold_id GROUP BY account_id
    ) d
    WHERE b.account_id = d.account_id;
    UPDATE ledger_holds SET status = 'void', resolved_at = now() WHERE hold_id = p_hold_id;
END;
$$;

CREATE FUNCTION ledger_post_hold(p_hold_id bigint) RETURNS bigint
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
DECLARE
    v_status ledger_hold_status;
    v_entry_id bigint;
    v_key text;
    v_postings jsonb;
BEGIN
    SELECT status, entry_id, idempotency_key INTO v_status, v_entry_id, v_key
    FROM ledger_holds WHERE hold_id = p_hold_id FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'hold not found' USING ERRCODE = 'P0002';
    END IF;
    IF v_status = 'posted' THEN
        RETURN v_entry_id;
    END IF;
    IF v_status = 'void' THEN
        RAISE EXCEPTION 'void hold cannot be posted' USING ERRCODE = '55000';
    END IF;
    PERFORM a.account_id
    FROM ledger_accounts a
    JOIN ledger_hold_postings hp ON hp.account_id = a.account_id
    WHERE hp.hold_id = p_hold_id
    ORDER BY a.account_id
    FOR UPDATE OF a;

    SELECT jsonb_agg(jsonb_build_object(
        'account_id', account_id,
        'direction', direction::text,
        'amount_minor', amount_minor
    ) ORDER BY account_id, direction)
    INTO v_postings
    FROM ledger_hold_postings WHERE hold_id = p_hold_id;

    -- Temporarily exclude this reservation within this atomic transaction.
    UPDATE ledger_holds SET status = 'void', resolved_at = now() WHERE hold_id = p_hold_id;
    v_entry_id := ledger_post_entry('hold-post:' || p_hold_id::text, v_postings);
    UPDATE ledger_account_balances b
    SET pending_debits = b.pending_debits - d.debits,
        pending_credits = b.pending_credits - d.credits,
        version = b.version + 1,
        updated_at = now()
    FROM (
        SELECT account_id,
               coalesce(sum(amount_minor) FILTER (WHERE direction = 'debit'), 0) AS debits,
               coalesce(sum(amount_minor) FILTER (WHERE direction = 'credit'), 0) AS credits
        FROM ledger_hold_postings WHERE hold_id = p_hold_id GROUP BY account_id
    ) d
    WHERE b.account_id = d.account_id;
    UPDATE ledger_holds
    SET status = 'posted', entry_id = v_entry_id, resolved_at = now()
    WHERE hold_id = p_hold_id;
    RETURN v_entry_id;
END;
$$;

REVOKE ALL ON FUNCTION ledger_place_hold(text, jsonb) FROM PUBLIC;
REVOKE ALL ON FUNCTION ledger_void_hold(bigint) FROM PUBLIC;
REVOKE ALL ON FUNCTION ledger_post_hold(bigint) FROM PUBLIC;

CREATE FUNCTION ledger_verify_integrity()
RETURNS TABLE(check_name text, ok boolean, detail text)
LANGUAGE sql
STABLE
AS $$
    WITH entry_totals AS (
        SELECT e.entry_id, p.currency,
               sum(p.amount_minor) FILTER (WHERE p.direction = 'debit') AS debits,
               sum(p.amount_minor) FILTER (WHERE p.direction = 'credit') AS credits
        FROM ledger_journal_entries e
        LEFT JOIN ledger_postings p USING (entry_id)
        GROUP BY e.entry_id, p.currency
    ), history_balances AS (
        SELECT a.account_id,
               coalesce(sum(CASE
                   WHEN (a.account_type IN ('asset', 'expense') AND p.direction = 'debit')
                     OR (a.account_type IN ('liability', 'equity', 'income') AND p.direction = 'credit')
                   THEN p.amount_minor ELSE -p.amount_minor END), 0)::bigint AS posted_minor
        FROM ledger_accounts a
        LEFT JOIN ledger_postings p USING (account_id)
        GROUP BY a.account_id
    ), chain AS (
        SELECT e.*,
               lag(e.entry_hash, 1, decode(repeat('00', 32), 'hex'))
                   OVER (ORDER BY e.entry_id) AS expected_previous_hash
        FROM ledger_journal_entries e
    ), chain_result AS (
        SELECT bool_and(
            previous_hash = expected_previous_hash
            AND entry_hash = digest(
                entry_id::text || ':' || encode(convert_to(idempotency_key, 'UTF8'), 'hex') || ':' ||
                encode(payload_hash, 'hex') || ':' || encode(previous_hash, 'hex'), 'sha256'
            )
        ) AS ok
        FROM chain
    )
    SELECT 'entries_balanced', coalesce(bool_and(debits = credits), true), 'debit and credit totals by entry/currency'
    FROM entry_totals
    UNION ALL
    SELECT 'balances_match_history', coalesce(bool_and(b.posted_minor = h.posted_minor), true), 'cached posted balances match immutable postings'
    FROM history_balances h
    LEFT JOIN ledger_account_balances b USING (account_id)
    UNION ALL
    SELECT 'hash_chain', coalesce((SELECT ok FROM chain_result), true), 'entry hash and predecessor links'
$$;

REVOKE ALL ON FUNCTION ledger_verify_integrity() FROM PUBLIC;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'tally_ledger_app') THEN
        CREATE ROLE tally_ledger_app NOLOGIN;
    END IF;
END;
$$;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
REVOKE ALL ON ledger_accounts, ledger_account_balances, ledger_journal_entries,
    ledger_postings, ledger_holds, ledger_hold_postings, ledger_balance_snapshots,
    ledger_balance_snapshot_items, ledger_chain_head, ledger_schema_migrations FROM PUBLIC;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO tally_ledger_app;
GRANT SELECT ON ledger_accounts, ledger_account_balances, ledger_journal_entries,
    ledger_postings, ledger_holds, ledger_hold_postings, ledger_balance_snapshots,
    ledger_balance_snapshot_items TO tally_ledger_app;
GRANT EXECUTE ON FUNCTION ledger_post_entry(text, jsonb) TO tally_ledger_app;
GRANT EXECUTE ON FUNCTION ledger_place_hold(text, jsonb) TO tally_ledger_app;
GRANT EXECUTE ON FUNCTION ledger_void_hold(bigint) TO tally_ledger_app;
GRANT EXECUTE ON FUNCTION ledger_post_hold(bigint) TO tally_ledger_app;
GRANT EXECUTE ON FUNCTION ledger_verify_integrity() TO tally_ledger_app;

INSERT INTO ledger_schema_migrations (version) VALUES (1);
COMMIT;
