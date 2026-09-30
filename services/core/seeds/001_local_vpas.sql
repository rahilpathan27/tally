INSERT INTO core_vpas(vpa, bank_id, active) VALUES
    ('payer@bank-a', 'bank-a', true),
    ('merchant@bank-b', 'bank-b', true)
ON CONFLICT (vpa) DO UPDATE SET bank_id = EXCLUDED.bank_id, active = true;
