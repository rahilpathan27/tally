INSERT INTO ledger_accounts (account_id, account_type, currency, allow_negative)
VALUES
    ('bank:simulated:INR', 'asset', 'INR', true),
    ('merchant:demo:payable:INR', 'liability', 'INR', false),
    ('merchant:demo:reserve:INR', 'liability', 'INR', false),
    ('merchant:demo:pending_settlement:INR', 'liability', 'INR', false),
    ('platform:fee_income:INR', 'income', 'INR', true),
    ('platform:tax_payable:INR', 'liability', 'INR', false),
    ('platform:suspense:INR', 'liability', 'INR', true),
    ('platform:writeoff:INR', 'expense', 'INR', true),
    ('platform:opening_equity:INR', 'equity', 'INR', true)
ON CONFLICT (account_id) DO NOTHING;
