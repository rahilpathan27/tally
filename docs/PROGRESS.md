# Progress

## Done

- Phase 1 scaffold: repository layout, Python 3.12 tooling, Makefile, CI workflow, local Compose dependencies, architecture overview, and foundation ADR.
- Phase 1 runtime: three PostgreSQL 16 databases, Redis 7, Redpanda, and a loopback-only SeaweedFS S3-compatible server start successfully. PostgreSQL, Redis, Redpanda, and SeaweedFS health checks pass.
- Phase 2 exact money: frozen integer-minor-unit type, supported currency exponent table, safe JSON integer bounds, centralized Decimal half-even rounding, deterministic largest-remainder allocation, and CI AST float-ban.
- Phase 2 ledger schema: append-only journals and postings, account and cached-balance tables, idempotency, account locks in deterministic order, hold lifecycle functions, per-currency balancing, non-negative checks considering active holds, SHA-256 hash chain, integrity verifier, and a least-privilege app role in PostgreSQL migration 0001.
- Phase 2 Python reference model: postings, normal balances, idempotency, holds, pending/available balances, snapshots/as-of calculations, and hash/invariant verifier.
- Phase 2 HTTP boundary: FastAPI endpoints for health, journal posting, hold placement/post/void, account balance, and integrity checks. A generated OpenAPI v1 contract is checked into the repository.
- Phase 2 quality gates: AST float-ban, mutation-score gate, and CI checks for lint/type checks, tests, mutation testing, and OpenAPI drift.
- Phase 3 gateway controls: HMAC-SHA256 route authentication validates method/raw-path/query/body-digest/timestamp/nonce, resolves active encrypted merchant keys, enforces scopes, and consumes nonces once. `GatewayRoute` composes auth, per-merchant rate-limit policy, required mutation idempotency keys, request fingerprinting, cached JSON response replay, and safe in-progress responses. PostgreSQL stores merchant keys/idempotency results with tenant RLS, a restricted key lookup function, bounded cleanup, and append-only hash-chained audit events. A CLI creates, atomically rotates, and revokes keys.
- Phase 4 vault: a dedicated internal-network PostgreSQL database stores envelope-encrypted, explicitly allowlisted published test PANs and only non-sensitive display metadata. The local API returns opaque tokens, refuses CVV/unknown fields with scrubbed validation errors, hashes caller request IDs before audit storage, and only permits detokenization to the network-simulator identity using a separate local credential. Access is hash-chain audited and append-only.
- Added ADR-001 money, ADR-002 ledger locking, ADR-003 local object store, ledger design, and guarantees documentation.

## Verified

- `docker compose up -d --wait` succeeds. All dependency containers report healthy; published ports bind to loopback.
- `make ledger-migrate` and `make ledger-test-integration` pass on the local versioned ledger database; rerunning migration is idempotent.
- PostgreSQL integration assertions cover same-key replay, changed-payload rejection, unbalanced entry rejection, insufficient funds, closed accounts, cross-currency entries, holds, hold post/void, integrity verification, and app-role direct-write denial.
- `make lint` passes Ruff lint/format, strict mypy for libraries/services/tests, and the float-ban scan.
- `make test` passes: 66 tests (6 database/Redis integration tests skip in the default no-environment run). Dedicated gateway and vault integration Make targets run those checks against the local services.
- OpenAPI export/check passes, and mutation testing passes its configured 70% minimum (76.0% in the last full run).
- HMAC primitive tests cover valid requests, method/path/body tampering, unknown keys, stale timestamps, and minimum secret length. The dependency integration test covers encrypted key lookup, valid request authentication, scope enforcement, tamper rejection, and duplicate nonce rejection. Latest lint, type, float-ban, full test, and OpenAPI checks pass.
- `make gateway-migrate` applies gateway schema versions 1 through 4; it is repeatable.
- `make gateway-test-integration` passes PostgreSQL and Redis checks for nonce replay rejection, encrypted-key authentication and scope enforcement, idempotency replay/conflict, route-level cached response replay without handler re-execution, 20 concurrent duplicate reservations with exactly one starter, merchant-context isolation, RLS visibility against a populated second tenant, direct key-table/write denial, audit-chain immutability/verification, fixed-window limits, and expired-state cleanup.
- Ledger API smoke checks against the Documents project root verified readiness, repeated hold-post replay (same entry ID and idempotency key), and all three integrity checks.
- `make vault-migrate` and `make vault-test-integration` pass. Integration coverage checks database role restrictions, audit-chain verification and mutation rejection, tokenization response/log scrubbing, ciphertext storage, CVV rejection, detokenization authorization, and Compose network isolation.
- Vault unit tests cover envelope encryption round-trip, fresh-key ciphertext variance, context binding, tamper rejection, test-PAN allowlisting/Luhn/expiry, and supported published card test numbers.

## Known gaps

- The local FastAPI ledger API connects using the Compose superuser, binds to loopback, and has no network authentication; the app role remains a database role template rather than the API's runtime identity.
- Merchant resource endpoints are not implemented yet; Phase 5 will add payment routes to the Phase 3 `GatewayRoute` controls. Gateway keys use local AES-GCM with a separately managed 32-byte key, not production KMS. Expired rows can be cleaned with `make gateway-cleanup`, but no scheduled cleanup worker exists. The internal ledger API remains separate, loopback-only, and connected using the Compose superuser.
- Vault encryption currently uses a locally supplied AES-GCM KEK, and simulator identity uses a local shared credential rather than mTLS. No production KMS, certificate identity, key lifecycle, external audit anchoring, or real card-data support is implemented.
- Snapshot tables exist and the Python model can snapshot/as-of, but there is no scheduled PostgreSQL snapshot writer, DB as-of query, or independent verifier worker.
- No ledger throughput/latency benchmark has been measured. The global chain-head lock serializes database writes.
- Later phases remain unimplemented: orchestrator/simulators beyond the vault's detokenization boundary, recovery/chaos harness, refunds/settlements/webhooks, reconciliation, risk, security/compliance workflows, frontend, observability, cloud deployment, and end-to-end demos/reports.
- SeaweedFS is an S3-compatible local substitution for the inaccessible pinned MinIO image. Its S3 endpoint is unauthenticated and loopback-only; it does not provide production Object Lock guarantees.
- GitHub Actions has not run on a hosted runner. Only the same local lint/type/test commands were run.

## Deviations

- The initial MinIO image pull returned access denied. ADR-003 records SeaweedFS 3.93 as the local S3-compatible replacement.
- This is a synthetic simulation. It does not move real money, accept real card data, or claim PCI DSS/RBI certification or production readiness.
