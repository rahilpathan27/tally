# Progress

## Done

- Phase 1 scaffold: repository layout, Python 3.12 tooling, Makefile, CI workflow, local Compose dependencies, architecture overview, and foundation ADR.
- Phase 1 runtime: three PostgreSQL 16 databases, Redis 7, Redpanda, and a loopback-only SeaweedFS S3-compatible server start successfully. PostgreSQL, Redis, Redpanda, and SeaweedFS health checks pass.
- Phase 2 exact money: frozen integer-minor-unit type, supported currency exponent table, safe JSON integer bounds, centralized Decimal half-even rounding, deterministic largest-remainder allocation, and CI AST float-ban.
- Phase 2 ledger schema: append-only journals and postings, account and cached-balance tables, idempotency, account locks in deterministic order, hold lifecycle functions, per-currency balancing, non-negative checks considering active holds, SHA-256 hash chain, integrity verifier, and a least-privilege app role in PostgreSQL migration 0001.
- Phase 2 Python reference model: postings, normal balances, idempotency, holds, pending/available balances, snapshots/as-of calculations, and hash/invariant verifier.
- Phase 2 HTTP boundary: FastAPI endpoints for health, journal posting, hold placement/post/void, account balance, and integrity checks. A generated OpenAPI v1 contract is checked into the repository.
- Phase 2 quality gates: AST float-ban, mutation-score gate, and CI checks for lint/type checks, tests, mutation testing, and OpenAPI drift.
- Phase 3 partial: HMAC-SHA256 request authentication dependency resolves active merchant keys, validates method/raw-path/query/body-digest/timestamp/nonce, loads key status/scopes/mode, and atomically consumes nonce values. PostgreSQL stores encrypted merchant keys, idempotency reservations and final responses, row-level tenant policies, and append-only hash-chained audit events. Redis fixed-window rate limiting uses one Lua operation. A one-time create/revoke CLI manages local merchant API keys. ADR-005 and ADR-006 record the decisions.
- Added ADR-001 money, ADR-002 ledger locking, ADR-003 local object store, ledger design, and guarantees documentation.

## Verified

- `docker compose up -d --wait` succeeds. All dependency containers report healthy; published ports bind to loopback.
- `make ledger-migrate` and `make ledger-test-integration` pass on the local versioned ledger database; rerunning migration is idempotent.
- PostgreSQL integration assertions cover same-key replay, changed-payload rejection, unbalanced entry rejection, insufficient funds, closed accounts, cross-currency entries, holds, hold post/void, integrity verification, and app-role direct-write denial.
- `make lint` passes Ruff lint/format, strict mypy for libraries/services/tests, and the float-ban scan.
- `make test` passes: 57 tests (3 service integration tests are skipped unless their connection URLs are set), including HMAC/key-encryption checks, Hypothesis properties, API input models, concurrent replay and posting checks, allocation conservation, and holds.
- OpenAPI export/check passes, and mutation testing passes its configured 70% minimum (76.0% in the last full run).
- HMAC primitive tests cover valid requests, method/path/body tampering, unknown keys, stale timestamps, and minimum secret length. The dependency integration test covers encrypted key lookup, valid request authentication, scope enforcement, tamper rejection, and duplicate nonce rejection. Latest lint, type, float-ban, full test, and OpenAPI checks pass.
- `make gateway-migrate` applies gateway schema versions 1 through 4; it is repeatable.
- `make gateway-test-integration` passes PostgreSQL checks for nonce replay rejection, idempotency replay/conflict, 20 concurrent duplicate reservations with exactly one starter, merchant-context isolation, RLS visibility against a populated second tenant, direct key-table/write denial, audit-chain immutability/verification, and expired-state cleanup. Its Redis integration check also verifies atomic fixed-window limits against the local Redis service.
- Ledger API smoke checks against the Documents project root verified readiness, repeated hold-post replay (same entry ID and idempotency key), and all three integrity checks.

## Known gaps

- The local FastAPI ledger API connects using the Compose superuser, binds to loopback, and has no network authentication; the app role remains a database role template rather than the API's runtime identity.
- Phase 3 is incomplete: no merchant-facing gateway routes, full middleware integration, atomic rotation workflow, production KMS/envelope encryption, API-key quotas, or endpoint idempotency response wrapper. Local AES-GCM encryption requires a separately managed 32-byte key and is not a substitute for KMS. Expired rows can be cleaned with `make gateway-cleanup`, but no scheduled cleanup worker exists. The local ledger API still uses the Compose superuser and remains unauthenticated; the gateway auth dependency is not wired into it.
- Snapshot tables exist and the Python model can snapshot/as-of, but there is no scheduled PostgreSQL snapshot writer, DB as-of query, or independent verifier worker.
- No ledger throughput/latency benchmark has been measured. The global chain-head lock serializes database writes.
- Later phases remain unimplemented: remaining gateway/auth integration, vault, orchestrator/simulators, recovery/chaos harness, refunds/settlements/webhooks, reconciliation, risk, security/compliance workflows, frontend, observability, cloud deployment, and end-to-end demos/reports.
- SeaweedFS is an S3-compatible local substitution for the inaccessible pinned MinIO image. Its S3 endpoint is unauthenticated and loopback-only; it does not provide production Object Lock guarantees.
- GitHub Actions has not run on a hosted runner. Only the same local lint/type/test commands were run.

## Deviations

- The initial MinIO image pull returned access denied. ADR-003 records SeaweedFS 3.93 as the local S3-compatible replacement.
- This is a synthetic simulation. It does not move real money, accept real card data, or claim PCI DSS/RBI certification or production readiness.
