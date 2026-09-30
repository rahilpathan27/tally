# Runbook: stuck unknown outcomes (`PendingUnknownStuck`, `RecoveryWorkerLag`)

**Severity:** page. Customers may have been debited without a result.

1. Switch monitor (console `/ops`): which payments, which bank, how many status checks.
2. Is the recovery worker alive? `RecoveryWorkerLag` > 0 and `tally_payments_in_state` flat means
   the core's recovery loop is not running: check core pods/logs for `payment recovery cycle failed`.
   Recovery leases expire after 30 s, so a restarted worker resumes automatically.
3. If the worker runs but status stays `unknown`, the bank's status API is degraded: see
   `bank-outage.md`. The per-bank deemed-outcome policy (`core_bank_recovery_policies`) decides at
   the deadline; do not force outcomes by hand.
4. Manual trigger: `POST /internal/v1/recovery/run` (recovery key) runs one pass.
5. After resolution, check reconciliation for the business date: late successes appear as
   `status_mismatch` breaks with money parked in `platform:suspense:INR`.
