# Load-test report

**Run:** 2026-10-01, `scripts/loadtest_k8s.py` (k6 `loadtest/k6/payments.js`) against the real
Helm chart on a local Kubernetes cluster. Every number below is from this laptop, not AWS.

| | |
| --- | --- |
| Machine | Apple M2, 8 cores, 16 GB, macOS 26.6 |
| Cluster | minikube (Docker driver), 6 CPUs / 7 GB; Docker Desktop VM 8 vCPU / 12.5 GB |
| Topology | core API × 3, core worker × 1, ledger × 2, risk × 2, vault, recon, back office, three simulators; one in-cluster PostgreSQL 16 pod (general, ledger, vault databases) and Redis; each Python pod is one uvicorn process |
| Traffic | signed UPI payments, create + confirm, 20 merchants, 20,000 payers with their own device and Indian IP context; amounts ₹10–₹4,010 |
| Client | k6 1.3 inside the cluster (arrival-rate executors: offered load does not slow down when the system does) |
| Measured | end-to-end time per payment (create → confirm response, which carries the final status), server-side latency and CPU from Prometheus/metrics-server, and afterwards payment states and the ledger verifier |

## Results

| Offered (payments/s) | Achieved | Dropped by k6 | Failed requests | e2e p50 | e2e p95 | e2e p99 | core p99 (server) | ledger post p99 | risk p99 | CPU used (all pods) |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 60 (3 min) | 60.0 | 0 | 0 | 18 ms | 31 ms | 63 ms | 41 ms | 5 ms | 9 ms | 1.0 cores |
| 90 (3 min) | 90.0 | 0 | 0 | 21 ms | 50 ms | 189 ms | 96 ms | 20 ms | 28 ms | 1.6 cores |
| **120 (3 min)** | **120.0** | 0 | 0 | 25 ms | 102 ms | **424 ms** | 97 ms | 9 ms | 24 ms | 2.4 cores |
| 150 (3 min) | 147.8 | 224 | 0 | 58 ms | 1,960 ms | 2,935 ms | 2,167 ms | 966 ms | 230 ms | 3.6 cores |
| ramp 20 → 200 (5 min) | 111 average | 2,555 | 0.36% (client timeouts) | 82 ms | 10.3 s | 12.7 s | 9.2 s | 900 ms | 357 ms | — |

Correctness held in every run: every created payment reached a terminal state (or was held in
risk review, which is by design); no payment stayed in `pending_unknown`; the ledger verifier
(balanced entries, balances vs postings, hash chain) passed after each run, including the
overloaded ones. 34,043 payments in the ramp all succeeded even though p99 reached 12.7 s.

**Sustainable throughput on this hardware: about 120 payments/s** within the latency SLO
(p50 < 300 ms, p99 < 1.5 s). The plan's target of 300+ payments/s end to end is **not met here**.

## Where the limit is

At 150 payments/s total CPU use is 3.6 of 6 cores and the ledger pods use 0.13 cores, yet
ledger posting p99 jumps from 9 ms to 966 ms. Sampling `pg_stat_activity` during a 150/s run
showed about 18 of the ledger's 20 database connections waiting on `Lock:tuple`: every posting
updates the single hash-chain head row (`ledger_chain_head`) and holds that row lock until its
commit, including the WAL fsync. Ledger throughput is therefore capped at roughly
1 ÷ commit latency, about 140 postings/s on this Docker disk; more pods or CPU do not move it.
This is the cost of one totally ordered, tamper-evident chain over every entry.

What would raise it (not implemented, recorded for a later ADR):

1. **Partitioned chains:** one hash chain per ledger shard (e.g. by merchant), each with its own
   head, plus a periodic anchor that hashes all shard heads together. Postings for different
   merchants stop contending; tamper evidence is preserved per shard and globally at anchors.
2. **Faster commits:** a database volume with low fsync latency (RDS io2/gp3 with provisioned
   IOPS) raises the single-chain ceiling directly; it cannot be measured on a laptop.
3. Batching several postings per chain-head update (group commit inside the ledger).

## Overload behaviour and admission control

The first spike test (60 → 180 → 60 payments/s) found that, beyond the ceiling, core queued
every request: p99 reached 16.2 s, clients timed out after the work had been done, and k6 had
to drop 1,914 iterations. Core now has admission control (`libs/common/admission.py`): at most
`TALLY_MAX_IN_FLIGHT` (64) merchant requests per pod; the rest get `503 OVERLOADED` with
`Retry-After: 1` immediately. Retrying is safe because mutations carry idempotency keys and the
gateway releases a key on 5xx. A `CoreShedding` alert and an [overload runbook](runbooks/overload.md)
cover it.

| Spike 60 → 180 → 60 payments/s | Without admission control | With (cap 64 per pod) |
| --- | ---: | ---: |
| Payments completed | 22,085 | 23,999 |
| Dropped iterations | 1,914 | 0 |
| Failed requests | 3.6% (timeouts) | 6.0% (immediate 503, retryable) |
| e2e p50 / p99 / max | 1.1 s / 16.2 s / 19.6 s | 31 ms / 2.1 s / 3.1 s |
| Ledger verifier afterwards | pass | pass |

With admission control, more requests are refused, but each refusal is instant and retryable,
and the admitted payments keep bounded latency. A lower cap would trade more refusals for a
lower p99. k6 does not retry, so the 323 payments created but not confirmed in the second run
are counted as abandoned by the client; none of them has any ledger effect.

## Reproduce

```bash
make k8s-up
uv run python -m scripts.loadtest_k8s --prepare --scenario steady --rate 120 --duration 3m
uv run python -m scripts.loadtest_k8s --skip-seed --scenario ramp --rate 200
uv run python -m scripts.loadtest_k8s --skip-seed --scenario spike --rate 180
```

Results are written to `.data/loadtest/<scenario>-<rate>.json`.
