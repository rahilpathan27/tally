# ADR-013: Three-way reconciliation with deterministic breaks

## Context

The platform must prove that its ledger, its switch records and the sponsor bank agree, find
every difference, classify it, and let operations resolve it without editing history. Bank
statements arrive in different formats with different conventions and may be re-delivered.

## Options

1. Two-way ledger-vs-bank matching in SQL.
2. A pure, in-memory three-way engine (ledger nostro postings, switch log, bank statement) behind
   format adapters, with persistence and workflow around it.
3. A third-party reconciliation product (out of scope for a self-contained simulation).

## Decision

Option 2. `services/recon/engine.py` has no I/O: exact reference match, many-to-one grouping of
batched refund lines, fuzzy matching (kind + amount + payer VPA + ±10 minutes, then VPA + time
when amounts differ), then per-reference classification into the break taxonomy. Break IDs are
UUIDv5 of (date, source, type, key), so re-running a date upserts the same rows; breaks that no
longer reproduce auto-resolve, and timing differences auto-resolve when the next statement
carries the line. Format adapters parse exactly (no rounding, `Decimal` JSON numbers) and report
bad lines instead of dropping them; fixed-width fields are never truncated. Originals are stored in
object storage with SHA-256, and a re-delivered identical file is ingested once.

Resolution uses a generic `maker_checker_requests` table: a proposal moves the break to
`pending_approval`; a different actor approves; execution posts a ledger entry keyed by the request
ID (retry-safe) to suspense or bank charges, then resolves the break. The database enforces
`checker <> maker`.

## Consequences

- Accuracy is measured, not asserted: `scripts/recon_eval.py` plants labelled breaks across all
  formats (including reference-less lines) and publishes recall, precision and classification.
- Scope is the platform nostro (UPI transfers, refunds, payouts). Card acquirer settlements and
  chargebacks are not reconciled.
- The recon service reads the core's tables directly (same database in this modular deployment)
  and the ledger through its API.
