import { z } from "zod";

// Amounts are integers in minor units; anything else is a contract violation.
export const Minor = z.number().int().safe();
const Text = z.string();
const Maybe = <T extends z.ZodType>(s: T) => s.nullable().optional();

export const Session = z.object({
  user_id: Text,
  email: Text,
  roles: z.array(Text),
  merchant_id: Maybe(Text),
});
export type Session = z.infer<typeof Session>;

export const LoginResult = z.union([
  z.object({ mfa_required: z.literal(true), mfa_token: Text }),
  Session.extend({ csrf_token: Text, expires_in: z.number() }),
]);

export const PaymentRow = z.object({
  payment_id: Text,
  amount_minor: Minor,
  currency: Text,
  payment_method_type: Text,
  status: Text,
  payer_vpa: Maybe(Text),
  payee_vpa: Maybe(Text),
  created_at: Text,
  succeeded_at: Maybe(Text),
  risk_decision: Maybe(Text),
  merchant_id: Maybe(Text),
});
export const PaymentPage = z.object({ items: z.array(PaymentRow), next_cursor: Maybe(Text) });
export type PaymentRow = z.infer<typeof PaymentRow>;

export const Posting = z.object({ account_id: Text, direction: Text, amount_minor: Minor });
export const LedgerLink = z
  .object({
    kind: Text,
    entry_id: Maybe(Text),
    hold_id: Maybe(Text),
    status: Maybe(Text),
    idempotency_key: Text,
    postings: Maybe(z.array(Posting)),
  })
  .passthrough();
export const Transition = z.object({
  from_state: Maybe(Text),
  to_state: Text,
  accepted: z.boolean(),
  actor: Text,
  reason: Text,
  occurred_at: Text,
});
export const Refund = z
  .object({ refund_id: Text, amount_minor: Minor, status: Text, created_at: Text })
  .passthrough();
export const PaymentDetail = z
  .object({
    payment_id: Text,
    merchant_id: Text,
    amount_minor: Minor,
    currency: Text,
    status: Text,
    payment_method_type: Text,
    payer_vpa: Maybe(Text),
    payee_vpa: Maybe(Text),
    created_at: Text,
    risk_outcome: Maybe(z.record(z.string(), z.unknown())),
    timeline: z.array(Transition),
    refunds: z.array(Refund),
    ledger: z.array(LedgerLink),
  })
  .passthrough();
export type PaymentDetail = z.infer<typeof PaymentDetail>;

export const RefundResult = z
  .object({ status: Text, refund_id: Maybe(Text), request_id: Maybe(Text) })
  .passthrough();

export const Settlement = z
  .object({
    settlement_id: Text,
    business_date: Text,
    status: Text,
    gross_minor: Minor,
    payable_debits_minor: Minor,
    payable_credits_minor: Minor,
    fee_minor: Minor,
    gst_minor: Minor,
    reserve_held_minor: Minor,
    reserve_released_minor: Minor,
    recovered_minor: Minor,
    shortfall_minor: Minor,
    net_payout_minor: Minor,
    payout_status: Maybe(Text),
  })
  .passthrough();
export const SettlementItem = z.object({
  item_type: Text,
  item_id: Text,
  amount_minor: Minor,
  fee_minor: Minor,
});
export const SettlementDetail = Settlement.extend({
  items: z.array(SettlementItem.passthrough()),
  payout: Maybe(z.record(z.string(), z.unknown())),
});

export const Dispute = z
  .object({
    dispute_id: Text,
    payment_id: Text,
    amount_minor: Minor,
    reason_code: Text,
    status: Text,
    respond_by: Text,
  })
  .passthrough();

export const ApiKey = z.object({
  key_id: Text,
  scopes: z.array(Text),
  mode: Text,
  created_at: Text,
  expires_at: Maybe(Text),
  revoked_at: Maybe(Text),
});
export const CreatedKey = z
  .object({ key_id: Text, secret: Text, scopes: z.array(Text), mode: Text })
  .passthrough();

export const Delivery = z.object({
  delivery_id: Text,
  event_id: Text,
  event_type: Text,
  status: Text,
  attempts: z.number(),
  last_status_code: Maybe(z.number()),
  last_error: Maybe(Text),
  created_at: Text,
  delivered_at: Maybe(Text),
});
export const WebhookEndpoint = z.object({
  endpoint_id: Text,
  url: Text,
  enabled_events: z.array(Text),
  status: Text,
  created_at: Text,
  deliveries: z.array(Delivery),
});

export const Analytics = z.object({
  daily: z.array(
    z.object({ day: Text, attempts: z.number(), succeeded: z.number(), volume_minor: Minor }),
  ),
  by_method: z.array(z.object({ method: Text, attempts: z.number(), succeeded: z.number() })),
  by_bank: z.array(z.object({ bank_id: Text, attempts: z.number(), succeeded: z.number() })),
  confirm_latency_ms: z.object({ p50: Maybe(z.number()), p95: Maybe(z.number()) }),
  refunds: z.object({ refunds: z.number(), refunded_minor: Minor }),
});

export const ReconRun = z
  .object({
    run_id: Text,
    source: Text,
    business_date: Text,
    bank_lines: z.number(),
    matched_lines: z.number(),
    unmatched_lines: z.number(),
    breaks_by_type: z.record(z.string(), z.number()),
    open_break_value_minor: Minor,
    duration_ms: z.number(),
    finished_at: Text,
  })
  .passthrough();
export const ReconBreak = z
  .object({
    break_id: Text,
    source: Text,
    business_date: Text,
    break_type: Text,
    reference: Maybe(Text),
    bank_reference: Maybe(Text),
    kind: Text,
    amount_minor: Minor,
    status: Text,
    suggested_action: Text,
    detail: Text,
    sla_due_at: Text,
    created_at: Text,
  })
  .passthrough();
export const ReconBreakDetail = ReconBreak.extend({
  evidence: z.object({
    ledger: z.array(z.record(z.string(), z.unknown())),
    switch: Maybe(z.record(z.string(), z.unknown())),
    bank: z.array(z.record(z.string(), z.unknown())),
  }),
  actions: z.array(
    z.object({ actor: Text, action: Text, note: Maybe(Text), created_at: Text }),
  ),
  adjustments_available: z.record(z.string(), Text),
});

export const Approval = z
  .object({
    request_id: Text,
    action_type: Text,
    subject_id: Text,
    payload: z.unknown(),
    maker: Text,
    status: Text,
    created_at: Text,
    can_decide: z.boolean().optional(),
  })
  .passthrough();

export const ReviewCase = z
  .object({
    case_id: Text,
    payment_id: Text,
    merchant_id: Text,
    status: Text,
    decision: Text,
    model_score: Maybe(z.number()),
    amount_minor: Minor,
    method: Text,
    sla_due_at: Text,
    sla_breached: z.boolean(),
    reason_codes: z.array(z.object({ code: Text, description: Text, source: Text })),
    features: z.record(z.string(), z.number()),
  })
  .passthrough();

export const SwitchSnapshot = z.object({
  in_flight: z.array(
    z
      .object({
        payment_id: Text,
        merchant_id: Text,
        status: Text,
        payment_method_type: Text,
        amount_minor: Minor,
        age_seconds: z.number(),
        recovery_attempts: z.number(),
      })
      .passthrough(),
  ),
  banks: z.array(
    z.object({
      bank_id: Text,
      succeeded: z.number(),
      failed: z.number(),
      unknown: z.number(),
      success_rate_bps: z.number(),
    }),
  ),
  breakers: z.record(z.string(), z.unknown()),
});
export type SwitchSnapshot = z.infer<typeof SwitchSnapshot>;

export const TrialBalance = z.object({
  as_of: Maybe(Text),
  rows: z.array(
    z.object({
      account_id: Text,
      account_type: Text,
      currency: Text,
      debit_minor: Minor,
      credit_minor: Minor,
      balance_minor: Minor,
    }),
  ),
  total_debit_minor: Minor,
  total_credit_minor: Minor,
  balanced: z.boolean(),
});
export const Statement = z.object({
  account_id: Text,
  lines: z.array(
    z.object({
      posting_id: Text,
      entry_id: Text,
      idempotency_key: Text,
      direction: Text,
      amount_minor: Minor,
      natural_delta_minor: z.number().int(),
      created_at: Text,
      source_key: Text,
    }),
  ),
  next_cursor: Maybe(Text),
});
export const IntegrityChecks = z.array(z.object({ check_name: Text, ok: z.boolean(), detail: Text }));

export const AuditPage = z.object({
  items: z.array(
    z
      .object({ seq: z.number(), occurred_at: Text, actor: Text, action: Text, subject: Text })
      .passthrough(),
  ),
  chain: z.object({ ok: z.boolean(), entries: z.number(), broken_at: Maybe(z.number()) }),
  latest_anchor: Maybe(z.record(z.string(), z.unknown())),
});

export const Anything = z.unknown();
export const AnyRecord = z.record(z.string(), z.unknown());
