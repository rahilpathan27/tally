BEGIN;

-- Generalise the durable command log beyond payments (refunds, settlements, payouts, disputes).
ALTER TABLE core_ledger_commands ALTER COLUMN payment_id DROP NOT NULL;
ALTER TABLE core_ledger_commands
    ADD COLUMN IF NOT EXISTS subject_type text NOT NULL DEFAULT 'payment'
        CHECK (subject_type IN ('payment', 'refund', 'settlement', 'payout', 'dispute')),
    ADD COLUMN IF NOT EXISTS subject_id uuid,
    ADD COLUMN IF NOT EXISTS kind text;
UPDATE core_ledger_commands SET subject_id = payment_id WHERE subject_id IS NULL;
ALTER TABLE core_ledger_commands ALTER COLUMN subject_id SET NOT NULL;
ALTER TABLE core_ledger_commands DROP CONSTRAINT IF EXISTS core_ledger_commands_state_check;
ALTER TABLE core_ledger_commands ADD CONSTRAINT core_ledger_commands_state_check
    CHECK (state IN ('pending', 'completed', 'skipped', 'rejected'));
CREATE INDEX IF NOT EXISTS core_ledger_commands_pending_idx
    ON core_ledger_commands(merchant_id, created_at) WHERE state = 'pending';

-- The outbox now carries events for several aggregate types.
ALTER TABLE core_outbox DROP CONSTRAINT IF EXISTS core_outbox_aggregate_id_fkey;
ALTER TABLE core_outbox
    ADD COLUMN IF NOT EXISTS aggregate_type text NOT NULL DEFAULT 'payment',
    ADD COLUMN IF NOT EXISTS publish_attempts integer NOT NULL DEFAULT 0;

ALTER TABLE payment_intents ADD COLUMN IF NOT EXISTS succeeded_at timestamptz;
UPDATE payment_intents p SET succeeded_at = t.occurred_at
FROM payment_transitions t
WHERE t.payment_id = p.payment_id AND t.accepted AND t.to_state = 'succeeded'
  AND p.succeeded_at IS NULL;
CREATE INDEX IF NOT EXISTS payment_intents_settleable_idx
    ON payment_intents(merchant_id, succeeded_at) WHERE status = 'succeeded';

INSERT INTO core_schema_migrations(version) VALUES (6) ON CONFLICT DO NOTHING;
COMMIT;
