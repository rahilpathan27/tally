# Security controls

Checklist of implemented controls with the code and tests that back them. This is a simulation;
none of this is a certification. See [threat-model.md](threat-model.md) for residual risks.

## Authentication

- **Merchant API:** HMAC-SHA256 over method, raw path and query, body SHA-256, timestamp and nonce
  (`libs/security/hmac_auth.py`, `services/api_gateway/auth.py`); nonce single-use; scopes;
  key rotation (24-hour overlap) and revocation from the dashboard; keys expire.
  API secrets are needed to verify HMAC, so they are stored **encrypted** (AES-GCM, key ID as
  associated data), not hashed; this deviates from the plan's "hashed API secrets".
- **Back office:** scrypt password hashes with a dummy-hash path for unknown users; lockout after
  five failures; TOTP MFA (RFC 6238, secrets encrypted, codes single-use by time step); ES256 JWT
  access tokens with `kid` rotation and algorithm pinning; rotating refresh tokens with
  family-wide revocation on reuse; `HttpOnly`/`Secure`/`SameSite=Strict` cookies; double-submit
  CSRF token for mutations (`services/backoffice_api/auth.py`, `libs/security/jwt_tokens.py`).

## Authorization

- Deny by default: every BFF route declares one permission via `require()`; the test suite
  enumerates all routes and checks every role (`services/backoffice_api/rbac.py`,
  `tests/security/test_backoffice_security.py`).
- Tenant isolation: merchant queries run as `tally_app` with row-level security keyed on
  `app.merchant_id`; column grants hide `secret_ciphertext`; `tally_ops` is read-only and
  cross-tenant for platform staff (ADR-017).
- Dual control: ledger adjustments, recon adjustments, limit changes, merchant risk policy,
  dashboard refunds above the merchant threshold, risk-rule activation and champion model
  promotion (`services/backoffice_api/approvals.py`, `services/recon/service.py`,
  `services/risk/registry.py`).

## Data protection

- Card data only in the vault (envelope encryption, allow-listed test PANs, CVV rejected,
  detokenisation limited to the network simulator identity).
- Structured JSON logs with redaction of card numbers, CVV and secret-bearing keys
  (`libs/observability/logging.py`).
- Tamper-evident audit log with anchoring (`audit_log`, `services/workers/audit_anchor.py`).

## HTTP hardening (`libs/security/http.py`, all services)

HSTS, `nosniff`, `X-Frame-Options: DENY`, restrictive CSP, `no-referrer`, `no-store`; body size
limits enforced while streaming; JSON-only mutations (415 otherwise); strict CORS on the BFF;
CSV exports neutralise formula injection.

## Outbound

SSRF-safe webhook delivery (`libs/security/ssrf.py`); webhook signatures with replay window
(`libs/security/webhook_signing.py`).

## Abuse and races

Idempotency at gateway and ledger, row locks, per-merchant money lock, globally ordered account
locks in the ledger (`tests/security/test_concurrency_abuse.py`, chaos harness).

## Monitoring

AML-lite detectors (structuring, rapid in-out, merchant volume spike, sanctions stub) raise
de-duplicated alerts that analysts group into cases (`services/workers/aml.py`). Demonstration
only; not a compliance programme.

## Not implemented here (see Phase 14 / PROGRESS)

mTLS between services, KMS-managed keys, WAF, image signing and admission control, secrets
manager integration, and a persistent JWT signing key store.
