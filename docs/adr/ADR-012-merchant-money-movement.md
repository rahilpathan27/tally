# ADR-012: Refunds, disputes, settlement and payouts on the ledger

## Context

Phase 8 adds money that flows back out of merchant balances. A refund or chargeback can exceed
what the merchant currently holds (for example after a settlement paid them out); a settlement
must never pay out money that a concurrent refund also spends; and every step must survive a crash
between the core database and the ledger.

## Options

1. Let merchant payable go negative and net it at settlement.
2. Keep payable non-negative; record shortfalls in a merchant receivable (asset) and recover them
   from later settlements; serialize balance-dependent operations per merchant.
3. Optimistic retries on ledger `INSUFFICIENT_FUNDS` with new idempotency keys, no lock.

## Decision

Option 2. Per merchant the ledger holds `payable`, `reserve`, `payout_in_transit` (liabilities) and
`receivable` (asset). Refund and dispute debits split between payable (what it covers) and
receivable. Balance-dependent operations (refund debit, dispute debit/win, refund cancellation,
settlement) take a per-merchant PostgreSQL session advisory lock, replay any command a crashed
request left pending, then compute and persist the next command before calling the ledger. A 4xx
from the ledger is a definitive rejection; a transport error or 5xx leaves the command pending for
a replay with the same key. A sweeper replays commands older than 30 seconds.

Settlement for `(merchant, IST business date)` includes each payable-affecting event exactly once
(`settlement_items` primary key). `compute_settlement` removes exactly
`sales − debits + credits` from payable, split into fee, GST on fees, reserve, receivable recovery,
shortfall (a new receivable when the window is negative) and net payout; a database CHECK repeats
the identity. Fees are Decimal rates rounded half-even once per payment through `round_to_minor`;
GST is computed on the settlement's total fee. Fee income is written to one of eight shards chosen
by hashing the settlement key, so retries hit the same shard. Payouts move net payout to
`payout_in_transit`; the bank result either clears it to the nostro or returns it to payable, where
the next settlement picks it up as a `payout_return` item.

## Consequences

- After a settlement posts, merchant payable equals the merchant's not-yet-eligible sales; the
  integration test asserts this identity after each run.
- One slow ledger call blocks other balance-dependent operations for the same merchant (up to the
  10-second lock wait, then `503 MERCHANT_OPERATION_IN_PROGRESS`); payments themselves are not
  blocked because credits cannot make an account negative.
- Refunds and payouts use the bank simulator's rails for both card and UPI; card refunds through
  the network simulator are not modelled.
- GST on fees at a single rate is a simplification; invoices, TDS and state-wise GST are out of scope.
