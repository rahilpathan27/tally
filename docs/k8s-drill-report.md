# Kubernetes canary drill

`make k8s-up && make k8s-drill` on 2026-10-01: the real Helm chart on a local minikube profile
(`tally`, Kubernetes 1.31.4, Calico enforcing the chart's NetworkPolicies, Pod Security
`restricted`), Argo Rollouts v1.8.3, in-cluster PostgreSQL, Redis and Prometheus, 4 CPUs and
4.8 GB. Signed merchant traffic: about 0.8 UPI payments/s (create + confirm), just under the
gateway's 120 requests/min per-merchant limit. This is a local cluster, not AWS.

| Release | Expected | Result | Time |
| --- | --- | --- | ---: |
| Healthy config change to `core` | promoted | Healthy; error ratio, p99 latency and ledger integrity each passed 9 measurements | 253 s |
| `core` pointed at a missing ledger | aborted, stable keeps serving | `RolloutAborted`: error ratio failed 2 measurements (limit 1); stable ReplicaSet kept serving with 2 pods | 71 s |

Traffic during the drill: 529 payments, 505 created and confirmed, 23 creates answered 503
(`LEDGER_PROVISIONING_UNAVAILABLE`) by the broken canary during its window, so about 4% of payments
were affected before the automatic abort. `min(tally_ledger_integrity_ok)` stayed 1 throughout.

## Defects the drill found (fixed)

- **Ledger verifier gauge never recovered.** A transient database error at pod start set
  `tally_ledger_integrity_ok{check="verifier_available"}` to 0 and nothing reset it, so one blip
  meant a permanent `LedgerInvariantViolation` page and every core/ledger canary aborting by the
  money gate. Fixed in `services/ledger/api.py` (`verify_once`), with a unit test; the runbook now
  covers the case.
- **Error-ratio query returned no data for healthy canaries.** With traffic but no 5xx, the
  numerator has no series and the ratio is empty (inconclusive), so a healthy canary paused
  instead of promoting. Fixed with `or vector(0)`.
- **Migration hook referenced the release ConfigMap**, which does not exist yet during a
  pre-install hook; migrations now take only their Secret.
- **Vault needed `TALLY_VAULT_TEST_PANS`** in the chart config; risk needed `TALLY_STEP_UP_SECRET`.
- The first drill run produced only 422/429 responses (an empty VPA directory and a request rate
  above the merchant limit), so its "broken release passed" result proved nothing and was
  discarded; the load generator now registers its VPAs and stays under the limit.

The drill also confirmed that NetworkPolicies are enforced: the core worker cannot call the core
API (no such path is declared), and the load generator needs its own explicit policy pair.
