# Runbook: core overload (`CoreShedding`)

**Severity:** ticket (page if `ErrorBudgetBurnFast` also fires). Core is at its in-flight cap
(`TALLY_MAX_IN_FLIGHT`, default 64 per pod) and is answering the excess with
`503 OVERLOADED` + `Retry-After: 1`. Merchants retry safely: idempotency keys are released on 5xx,
so a retried create or confirm runs exactly once. No money is at risk; latency is being protected.

## Triage
1. Payments Overview dashboard: is offered load above normal (a merchant batch, a spike) or did
   capacity drop (pods restarting, a slow dependency)?
2. Ledger Health: posting p99. The ledger's single hash-chain head serialises postings
   (measured ceiling ≈ 140 postings/s on the Phase 15 test hardware); when posting latency rises,
   core requests wait on it and in-flight counts climb.
3. Risk p99 and bank outcomes: a slow dependency also fills the in-flight budget.

## Mitigate
- Capacity-bound on core CPU: scale core (HPA does this on CPU; raise `maxReplicas` if pinned).
- Bound by the ledger: adding core pods does not help. Reduce the load (contact the merchant
  driving the spike, lower their rate limit) and track ledger chain sharding (load-test report).
- Never raise `TALLY_MAX_IN_FLIGHT` far beyond capacity: that turns fast 503s back into
  timeouts after the work is done.

## Verify
`tally_load_shed_total` stops increasing and merchant API p99 returns under 1.5 s.
