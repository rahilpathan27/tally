# Runbook: bank outage (`BankSuccessRateDrop`, `CircuitBreakerOpen`)

**Severity:** page.

1. Switch and Bank Health dashboard: one bank or all? Outcome mix (`declined`, `no_response`,
   `breaker_open`, `debit_succeeded_credit_failed`).
2. Declines only: likely the bank's own risk or account issues; inform merchants, no action on our side.
3. `no_response`/`breaker_open`: the circuit breaker is protecting the bank; payments go to
   `pending_unknown` and are resolved by status checks. Watch `PendingUnknownStuck`.
4. Confirm with the bank's status page or support; record the incident window (needed for recon).
5. Do not reroute to another bank: VPA routing is fixed by the payer's bank (ADR-010).
6. After recovery: breakers close after 5 s of health; run recon for the affected date and expect
   timing differences and late successes.
