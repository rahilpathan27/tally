BEGIN;

-- Platform chart of accounts required by settlement, refunds and disputes.
INSERT INTO ledger_accounts (account_id, account_type, currency, allow_negative) VALUES
    ('bank:simulated:INR', 'asset', 'INR', true),
    ('platform:fee_income:INR', 'income', 'INR', true),
    ('platform:tax_payable:INR', 'liability', 'INR', false),
    ('platform:refunds_clearing:INR', 'liability', 'INR', false),
    ('platform:suspense:INR', 'liability', 'INR', true),
    ('platform:writeoff:INR', 'expense', 'INR', true),
    ('platform:opening_equity:INR', 'equity', 'INR', true)
ON CONFLICT (account_id) DO NOTHING;

-- Hot income account: writes are spread over shards; reads sum them via shard_parent_id.
INSERT INTO ledger_accounts (account_id, account_type, currency, allow_negative, shard_parent_id)
SELECT 'platform:fee_income:INR:shard:' || n, 'income', 'INR', true, 'platform:fee_income:INR'
FROM generate_series(0, 7) AS n
ON CONFLICT (account_id) DO NOTHING;

INSERT INTO ledger_account_balances (account_id)
SELECT account_id FROM ledger_accounts ON CONFLICT DO NOTHING;

-- Natural-sign delta of one posting for its account type.
CREATE OR REPLACE FUNCTION ledger_natural_delta(
    p_type ledger_account_type, p_direction ledger_direction, p_amount bigint
) RETURNS bigint LANGUAGE sql IMMUTABLE AS $$
    SELECT CASE
        WHEN (p_type IN ('asset', 'expense') AND p_direction = 'debit')
          OR (p_type IN ('liability', 'equity', 'income') AND p_direction = 'credit')
        THEN p_amount ELSE -p_amount END
$$;

-- Posted balance of an account (and its shards) as of an instant, from immutable postings.
CREATE OR REPLACE FUNCTION ledger_balance_as_of(p_account_id text, p_as_of timestamptz)
RETURNS bigint LANGUAGE sql STABLE AS $$
    SELECT coalesce(sum(ledger_natural_delta(a.account_type, p.direction, p.amount_minor)), 0)
    FROM ledger_postings p
    JOIN ledger_accounts a ON a.account_id = p.account_id
    WHERE (a.account_id = p_account_id OR a.shard_parent_id = p_account_id)
      AND p.created_at <= p_as_of
$$;

-- Trial balance: debit and credit totals per account; shard rows roll up to their parent.
CREATE OR REPLACE FUNCTION ledger_trial_balance(p_as_of timestamptz DEFAULT 'infinity')
RETURNS TABLE(account_id text, account_type text, currency char(3),
              debit_minor bigint, credit_minor bigint, balance_minor bigint)
LANGUAGE sql STABLE AS $$
    SELECT coalesce(a.shard_parent_id, a.account_id) AS account_id,
           min(a.account_type::text) AS account_type,
           min(a.currency) AS currency,
           coalesce(sum(p.amount_minor) FILTER (WHERE p.direction = 'debit'), 0)::bigint,
           coalesce(sum(p.amount_minor) FILTER (WHERE p.direction = 'credit'), 0)::bigint,
           coalesce(sum(ledger_natural_delta(a.account_type, p.direction, p.amount_minor)), 0)
               ::bigint
    FROM ledger_accounts a
    LEFT JOIN ledger_postings p ON p.account_id = a.account_id AND p.created_at <= p_as_of
    GROUP BY coalesce(a.shard_parent_id, a.account_id)
    ORDER BY 1
$$;

-- Snapshot every cached balance with the chain head it corresponds to.
CREATE OR REPLACE FUNCTION ledger_take_snapshot() RETURNS bigint
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE
    v_snapshot bigint;
    v_last bigint;
    v_hash bytea;
BEGIN
    -- The chain-head lock excludes concurrent postings so the snapshot is consistent.
    SELECT last_entry_id, last_hash INTO v_last, v_hash
    FROM ledger_chain_head WHERE singleton FOR UPDATE;
    INSERT INTO ledger_balance_snapshots(last_entry_id, integrity_hash)
    VALUES (v_last, v_hash) RETURNING snapshot_id INTO v_snapshot;
    INSERT INTO ledger_balance_snapshot_items(snapshot_id, account_id, posted_minor, version)
    SELECT v_snapshot, account_id, posted_minor, version FROM ledger_account_balances;
    RETURN v_snapshot;
END;
$$;

-- Verify a snapshot against a recomputation from postings up to its last entry.
CREATE OR REPLACE FUNCTION ledger_verify_snapshot(p_snapshot_id bigint)
RETURNS TABLE(account_id text, snapshot_minor bigint, recomputed_minor bigint)
LANGUAGE sql STABLE AS $$
    SELECT i.account_id, i.posted_minor,
           coalesce((
               SELECT sum(ledger_natural_delta(a.account_type, p.direction, p.amount_minor))
               FROM ledger_postings p JOIN ledger_accounts a ON a.account_id = p.account_id
               WHERE p.account_id = i.account_id
                 AND p.entry_id <= coalesce(s.last_entry_id, 0)
           ), 0)::bigint
    FROM ledger_balance_snapshot_items i
    JOIN ledger_balance_snapshots s USING (snapshot_id)
    WHERE i.snapshot_id = p_snapshot_id
$$;

REVOKE ALL ON FUNCTION ledger_take_snapshot() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION ledger_take_snapshot() TO tally_ledger_app;
GRANT EXECUTE ON FUNCTION ledger_balance_as_of(text, timestamptz) TO tally_ledger_app;
GRANT EXECUTE ON FUNCTION ledger_trial_balance(timestamptz) TO tally_ledger_app;
GRANT EXECUTE ON FUNCTION ledger_verify_snapshot(bigint) TO tally_ledger_app;

INSERT INTO ledger_schema_migrations (version) VALUES (3);
COMMIT;
