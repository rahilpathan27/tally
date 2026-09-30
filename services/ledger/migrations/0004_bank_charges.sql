BEGIN;

-- Bank charges (and GST on them) found by reconciliation are booked as platform expense.
INSERT INTO ledger_accounts (account_id, account_type, currency, allow_negative)
VALUES ('platform:bank_charges:INR', 'expense', 'INR', true)
ON CONFLICT (account_id) DO NOTHING;
INSERT INTO ledger_account_balances (account_id) VALUES ('platform:bank_charges:INR')
ON CONFLICT DO NOTHING;

CREATE INDEX IF NOT EXISTS ledger_postings_account_posting_idx
    ON ledger_postings (account_id, posting_id);

INSERT INTO ledger_schema_migrations (version) VALUES (4);
COMMIT;
