# Runbook: ledger discrepancy (`LedgerInvariantViolation`, `LedgerVerifierStalled`)

**Severity:** page. Money may have been created or lost. Stop and investigate before anything else.

## Triage (first 10 minutes)
1. Ledger Health dashboard: which `check` label is 0 — `entries_balanced`, `balances_match_history`,
   `hash_chain` or `trial_balance`?
2. Freeze manual money movement: reject pending ledger adjustments and recon adjustments in the
   approvals inbox; pause settlement runs (`POST /internal/v1/settlements/run` is idempotent, so
   pausing is safe).
3. Run the report: `POST /internal/v1/integrity-report` (ledger internal key). It snapshots balances
   and recomputes them from postings.

## Diagnose
- Only `check="verifier_available"` is 0 and every other check is 1: the verifier could not reach
  the ledger database; this is an availability problem, not a money one. Since Phase 14 a later
  successful run clears it automatically. If it stays 0, check the pod's database connectivity
  (NetworkPolicy egress to the data subnets, RDS status). The same series gates Argo Rollouts
  canaries (`tally-ledger-integrity`), so while it is 0 every core and ledger release aborts.
- `balances_match_history` false: compare `ledger_account_balances` with the recomputation in
  `ledger_verify_snapshot(<snapshot>)` to find the account and entry range.
- `hash_chain` false: someone changed history. Find the first entry whose hash does not verify
  (`ledger_verify_integrity` detail), then check `pg_stat_activity`, database audit logs and who
  holds owner or superuser credentials. Treat as a security incident (`security-event.md`).
- `entries_balanced` false: an entry bypassed `ledger_post_entry`; same as above.

## Recover
- Never edit history back. Post correcting entries through a maker-checker ledger adjustment,
  referencing the incident. If history was altered, restore from PITR into a separate instance to
  recover the original rows as evidence.
- Resume operations only after `/v1/integrity` is all green and the trial balance balances.

## Verify
`LedgerInvariantViolation` resolves within one verifier interval (60 s in services, 2 s in the dev
stack) plus one scrape.
