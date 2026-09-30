BEGIN;

CREATE TABLE IF NOT EXISTS core_schema_migrations (
    version integer PRIMARY KEY,
    applied_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE IF NOT EXISTS core_vpas (
    vpa text PRIMARY KEY CHECK (vpa ~ '^[a-zA-Z0-9._-]{2,64}@[a-zA-Z0-9.-]{2,64}$'),
    bank_id text NOT NULL,
    active boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE IF NOT EXISTS payment_intents (
    payment_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    merchant_id text NOT NULL REFERENCES merchants(merchant_id),
    amount_minor bigint NOT NULL CHECK (amount_minor BETWEEN 1 AND 9007199254740991),
    currency char(3) NOT NULL CHECK (currency = 'INR'),
    payment_method_type text NOT NULL CHECK (payment_method_type IN ('card', 'upi')),
    payment_method_token text,
    payer_vpa text REFERENCES core_vpas(vpa),
    payee_vpa text REFERENCES core_vpas(vpa),
    status text NOT NULL CHECK (status IN (
        'created', 'risk_review', 'authorizing', 'authorized', 'capturing', 'succeeded',
        'failed', 'pending_unknown', 'reversal_pending', 'reversed', 'cancelled', 'expired'
    )),
    ledger_hold_id bigint,
    mode text NOT NULL CHECK (mode IN ('test', 'live')),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CHECK (
        (payment_method_type = 'card' AND payment_method_token IS NOT NULL
            AND payer_vpa IS NULL AND payee_vpa IS NULL)
        OR (payment_method_type = 'upi' AND payment_method_token IS NULL
            AND payer_vpa IS NOT NULL AND payee_vpa IS NOT NULL)
    )
);
CREATE INDEX IF NOT EXISTS payment_intents_merchant_created_idx
    ON payment_intents(merchant_id, created_at DESC, payment_id DESC);

CREATE TABLE IF NOT EXISTS payment_transitions (
    transition_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    payment_id uuid NOT NULL REFERENCES payment_intents(payment_id),
    merchant_id text NOT NULL REFERENCES merchants(merchant_id),
    from_state text,
    to_state text NOT NULL,
    accepted boolean NOT NULL,
    actor text NOT NULL,
    reason text NOT NULL,
    correlation_id text NOT NULL,
    occurred_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX IF NOT EXISTS payment_transitions_payment_idx
    ON payment_transitions(payment_id, transition_id);

CREATE OR REPLACE FUNCTION core_reject_transition_mutation() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'payment transition history is append-only' USING ERRCODE = '55000';
END;
$$;
DROP TRIGGER IF EXISTS payment_transitions_no_mutation ON payment_transitions;
CREATE TRIGGER payment_transitions_no_mutation
BEFORE UPDATE OR DELETE ON payment_transitions
FOR EACH ROW EXECUTE FUNCTION core_reject_transition_mutation();

CREATE TABLE IF NOT EXISTS core_outbox (
    event_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    aggregate_id uuid NOT NULL REFERENCES payment_intents(payment_id),
    merchant_id text NOT NULL REFERENCES merchants(merchant_id),
    event_type text NOT NULL,
    payload jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    published_at timestamptz
);
CREATE INDEX IF NOT EXISTS core_outbox_unpublished_idx ON core_outbox(created_at)
    WHERE published_at IS NULL;

CREATE TABLE IF NOT EXISTS core_ledger_commands (
    command_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    payment_id uuid NOT NULL REFERENCES payment_intents(payment_id),
    merchant_id text NOT NULL REFERENCES merchants(merchant_id),
    operation text NOT NULL CHECK (operation IN ('place_hold', 'post_hold', 'void_hold', 'post_entry')),
    idempotency_key text NOT NULL UNIQUE,
    request jsonb NOT NULL,
    state text NOT NULL CHECK (state IN ('pending', 'completed', 'skipped')) DEFAULT 'pending',
    result jsonb,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    completed_at timestamptz
);

INSERT INTO core_schema_migrations(version) VALUES (1) ON CONFLICT DO NOTHING;

ALTER TABLE payment_intents ENABLE ROW LEVEL SECURITY;
ALTER TABLE payment_transitions ENABLE ROW LEVEL SECURITY;
CREATE POLICY payment_intents_tenant ON payment_intents
    USING (merchant_id = current_setting('app.merchant_id', true))
    WITH CHECK (merchant_id = current_setting('app.merchant_id', true));
CREATE POLICY payment_transitions_tenant ON payment_transitions
    USING (merchant_id = current_setting('app.merchant_id', true))
    WITH CHECK (merchant_id = current_setting('app.merchant_id', true));

COMMIT;
