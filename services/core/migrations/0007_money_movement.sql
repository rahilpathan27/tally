BEGIN;

CREATE TABLE IF NOT EXISTS merchant_settlement_configs (
    merchant_id text PRIMARY KEY REFERENCES merchants(merchant_id),
    settlement_delay_days integer NOT NULL DEFAULT 2 CHECK (settlement_delay_days BETWEEN 0 AND 30),
    cutoff_time time NOT NULL DEFAULT '00:00',
    fee_rate numeric(9, 6) NOT NULL DEFAULT 0.020000 CHECK (fee_rate BETWEEN 0 AND 1),
    fixed_fee_minor bigint NOT NULL DEFAULT 0 CHECK (fixed_fee_minor >= 0),
    gst_rate numeric(6, 4) NOT NULL DEFAULT 0.1800 CHECK (gst_rate BETWEEN 0 AND 1),
    reserve_rate numeric(9, 6) NOT NULL DEFAULT 0.000000 CHECK (reserve_rate BETWEEN 0 AND 1),
    reserve_hold_days integer NOT NULL DEFAULT 30 CHECK (reserve_hold_days BETWEEN 0 AND 365),
    refund_approval_threshold_minor bigint CHECK (refund_approval_threshold_minor > 0),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

-- Refunds -----------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS refunds (
    refund_id uuid PRIMARY KEY,
    payment_id uuid NOT NULL REFERENCES payment_intents(payment_id),
    merchant_id text NOT NULL REFERENCES merchants(merchant_id),
    amount_minor bigint NOT NULL CHECK (amount_minor BETWEEN 1 AND 9007199254740991),
    currency char(3) NOT NULL CHECK (currency = 'INR'),
    status text NOT NULL CHECK (status IN (
        'pending', 'processing', 'succeeded', 'requires_action', 'cancelled', 'failed'
    )),
    reason text,
    payable_portion_minor bigint NOT NULL DEFAULT 0 CHECK (payable_portion_minor >= 0),
    receivable_portion_minor bigint NOT NULL DEFAULT 0 CHECK (receivable_portion_minor >= 0),
    bank_attempts integer NOT NULL DEFAULT 0,
    next_attempt_at timestamptz,
    lease_until timestamptz,
    reversal_payable_credit_minor bigint CHECK (reversal_payable_credit_minor >= 0),
    failure_reason text,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    succeeded_at timestamptz,
    CHECK (payable_portion_minor + receivable_portion_minor IN (0, amount_minor))
);
CREATE INDEX IF NOT EXISTS refunds_payment_idx ON refunds(payment_id);
CREATE INDEX IF NOT EXISTS refunds_due_idx ON refunds(next_attempt_at)
    WHERE status = 'processing';

CREATE TABLE IF NOT EXISTS refund_transitions (
    transition_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    refund_id uuid NOT NULL REFERENCES refunds(refund_id),
    merchant_id text NOT NULL REFERENCES merchants(merchant_id),
    from_state text,
    to_state text NOT NULL,
    actor text NOT NULL,
    reason text NOT NULL,
    occurred_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
DROP TRIGGER IF EXISTS refund_transitions_no_mutation ON refund_transitions;
CREATE TRIGGER refund_transitions_no_mutation
BEFORE UPDATE OR DELETE ON refund_transitions
FOR EACH ROW EXECUTE FUNCTION core_reject_transition_mutation();

-- Disputes ----------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS disputes (
    dispute_id uuid PRIMARY KEY,
    payment_id uuid NOT NULL REFERENCES payment_intents(payment_id),
    merchant_id text NOT NULL REFERENCES merchants(merchant_id),
    amount_minor bigint NOT NULL CHECK (amount_minor BETWEEN 1 AND 9007199254740991),
    currency char(3) NOT NULL CHECK (currency = 'INR'),
    reason_code text NOT NULL CHECK (reason_code IN (
        'fraudulent', 'product_not_received', 'duplicate', 'credit_not_processed', 'other'
    )),
    status text NOT NULL CHECK (status IN (
        'opening', 'needs_response', 'under_review', 'won', 'lost'
    )),
    network_reference text NOT NULL UNIQUE,
    payable_portion_minor bigint NOT NULL DEFAULT 0 CHECK (payable_portion_minor >= 0),
    receivable_portion_minor bigint NOT NULL DEFAULT 0 CHECK (receivable_portion_minor >= 0),
    reversal_payable_credit_minor bigint CHECK (reversal_payable_credit_minor >= 0),
    evidence_text text,
    evidence_object_key text,
    evidence_sha256 text,
    respond_by timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    resolved_at timestamptz
);
CREATE INDEX IF NOT EXISTS disputes_payment_idx ON disputes(payment_id);

CREATE TABLE IF NOT EXISTS dispute_transitions (
    transition_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    dispute_id uuid NOT NULL REFERENCES disputes(dispute_id),
    merchant_id text NOT NULL REFERENCES merchants(merchant_id),
    from_state text,
    to_state text NOT NULL,
    actor text NOT NULL,
    reason text NOT NULL,
    occurred_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
DROP TRIGGER IF EXISTS dispute_transitions_no_mutation ON dispute_transitions;
CREATE TRIGGER dispute_transitions_no_mutation
BEFORE UPDATE OR DELETE ON dispute_transitions
FOR EACH ROW EXECUTE FUNCTION core_reject_transition_mutation();

-- Chargeback and review outcomes feed risk-model labels (Phase 10).
CREATE TABLE IF NOT EXISTS risk_labels (
    label_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    payment_id uuid NOT NULL REFERENCES payment_intents(payment_id),
    merchant_id text NOT NULL REFERENCES merchants(merchant_id),
    label text NOT NULL CHECK (label IN ('fraud', 'legitimate')),
    source text NOT NULL CHECK (source IN ('chargeback', 'analyst_review')),
    source_id text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (source, source_id)
);

-- Settlements and payouts -------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS settlements (
    settlement_id uuid PRIMARY KEY,
    merchant_id text NOT NULL REFERENCES merchants(merchant_id),
    business_date date NOT NULL,
    currency char(3) NOT NULL CHECK (currency = 'INR'),
    status text NOT NULL CHECK (status IN ('computed', 'posted', 'empty')),
    gross_minor bigint NOT NULL,
    payable_debits_minor bigint NOT NULL,
    payable_credits_minor bigint NOT NULL,
    fee_minor bigint NOT NULL,
    gst_minor bigint NOT NULL,
    reserve_held_minor bigint NOT NULL,
    reserve_released_minor bigint NOT NULL,
    recovered_minor bigint NOT NULL,
    shortfall_minor bigint NOT NULL,
    net_payout_minor bigint NOT NULL,
    fee_rate numeric(9, 6) NOT NULL,
    fixed_fee_minor bigint NOT NULL,
    gst_rate numeric(6, 4) NOT NULL,
    reserve_rate numeric(9, 6) NOT NULL,
    reserve_release_on date,
    ledger_entry_id bigint,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    posted_at timestamptz,
    UNIQUE (merchant_id, business_date),
    CHECK (gross_minor >= 0 AND payable_debits_minor >= 0 AND payable_credits_minor >= 0
        AND fee_minor >= 0 AND gst_minor >= 0 AND reserve_held_minor >= 0
        AND reserve_released_minor >= 0 AND recovered_minor >= 0 AND shortfall_minor >= 0
        AND net_payout_minor >= 0),
    CHECK (fee_minor + gst_minor + reserve_held_minor + recovered_minor + net_payout_minor
        - reserve_released_minor - shortfall_minor
        = gross_minor - payable_debits_minor + payable_credits_minor)
);

-- Each payable-affecting event can be settled exactly once.
CREATE TABLE IF NOT EXISTS settlement_items (
    settlement_id uuid NOT NULL REFERENCES settlements(settlement_id),
    item_type text NOT NULL CHECK (item_type IN (
        'payment', 'refund_debit', 'refund_cancel', 'dispute_debit', 'dispute_won',
        'reserve_release', 'payout_return'
    )),
    item_id uuid NOT NULL,
    amount_minor bigint NOT NULL CHECK (amount_minor >= 0),
    fee_minor bigint NOT NULL DEFAULT 0 CHECK (fee_minor >= 0),
    PRIMARY KEY (item_type, item_id)
);
CREATE INDEX IF NOT EXISTS settlement_items_settlement_idx ON settlement_items(settlement_id);

CREATE TABLE IF NOT EXISTS payouts (
    payout_id uuid PRIMARY KEY,
    settlement_id uuid NOT NULL UNIQUE REFERENCES settlements(settlement_id),
    merchant_id text NOT NULL REFERENCES merchants(merchant_id),
    amount_minor bigint NOT NULL CHECK (amount_minor BETWEEN 1 AND 9007199254740991),
    currency char(3) NOT NULL CHECK (currency = 'INR'),
    status text NOT NULL CHECK (status IN ('pending', 'sent', 'paid', 'returned')),
    bank_reference text,
    attempts integer NOT NULL DEFAULT 0,
    next_attempt_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    lease_until timestamptz,
    instruction_object_key text,
    instruction_sha256 text,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    resolved_at timestamptz
);
CREATE INDEX IF NOT EXISTS payouts_due_idx ON payouts(next_attempt_at)
    WHERE status IN ('pending', 'sent');

-- Webhooks ----------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS webhook_endpoints (
    endpoint_id uuid PRIMARY KEY,
    merchant_id text NOT NULL REFERENCES merchants(merchant_id),
    url text NOT NULL CHECK (length(url) BETWEEN 8 AND 2048),
    secret_ciphertext bytea NOT NULL,
    enabled_events text[] NOT NULL CHECK (cardinality(enabled_events) > 0),
    status text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'disabled')),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    disabled_at timestamptz
);
CREATE INDEX IF NOT EXISTS webhook_endpoints_merchant_idx ON webhook_endpoints(merchant_id);

CREATE TABLE IF NOT EXISTS webhook_deliveries (
    delivery_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    endpoint_id uuid NOT NULL REFERENCES webhook_endpoints(endpoint_id),
    merchant_id text NOT NULL REFERENCES merchants(merchant_id),
    event_id uuid NOT NULL,
    event_type text NOT NULL,
    payload jsonb NOT NULL,
    status text NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'succeeded', 'failed', 'dead')),
    attempts integer NOT NULL DEFAULT 0,
    next_attempt_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    lease_until timestamptz,
    last_status_code integer,
    last_error text,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    delivered_at timestamptz,
    UNIQUE (endpoint_id, event_id)
);
CREATE INDEX IF NOT EXISTS webhook_deliveries_due_idx ON webhook_deliveries(next_attempt_at)
    WHERE status IN ('pending', 'failed');

CREATE TABLE IF NOT EXISTS webhook_delivery_attempts (
    attempt_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    delivery_id uuid NOT NULL REFERENCES webhook_deliveries(delivery_id),
    merchant_id text NOT NULL REFERENCES merchants(merchant_id),
    attempted_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    status_code integer,
    error text,
    duration_ms integer NOT NULL CHECK (duration_ms >= 0)
);

ALTER TABLE refunds ENABLE ROW LEVEL SECURITY;
ALTER TABLE disputes ENABLE ROW LEVEL SECURITY;
ALTER TABLE settlements ENABLE ROW LEVEL SECURITY;
ALTER TABLE payouts ENABLE ROW LEVEL SECURITY;
ALTER TABLE webhook_endpoints ENABLE ROW LEVEL SECURITY;
ALTER TABLE webhook_deliveries ENABLE ROW LEVEL SECURITY;
DO $$
DECLARE t text;
BEGIN
    FOREACH t IN ARRAY ARRAY['refunds', 'disputes', 'settlements', 'payouts',
                             'webhook_endpoints', 'webhook_deliveries'] LOOP
        EXECUTE format('DROP POLICY IF EXISTS %I ON %I', t || '_tenant', t);
        EXECUTE format(
            'CREATE POLICY %I ON %I USING (merchant_id = current_setting(''app.merchant_id'', true)) '
            'WITH CHECK (merchant_id = current_setting(''app.merchant_id'', true))',
            t || '_tenant', t);
    END LOOP;
END $$;

INSERT INTO core_schema_migrations(version) VALUES (7) ON CONFLICT DO NOTHING;
COMMIT;
