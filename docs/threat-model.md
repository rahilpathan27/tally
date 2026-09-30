# Threat model (STRIDE)

Scope: the local Tally simulation — merchant API (core), ledger, vault, risk, recon, back office
(BFF), simulators, PostgreSQL ×3, Redis, Redpanda, object storage. No real money or card data.
Residual risks are stated honestly; "tested" points to the test that exercises the mitigation.

## Assets

| Asset | Why it matters |
| --- | --- |
| Ledger journal and balances | Source of truth for money; corruption creates or loses money |
| Test PANs in the vault | Stand-in for cardholder data (PCI scope) |
| Merchant API secrets, webhook secrets | Allow moving money / forging events as a merchant |
| Back-office sessions, TOTP secrets | Allow ops/approver actions (adjustments, refunds, rules) |
| Maker-checker requests, audit log | Evidence and control over manual money movement |
| Risk rules and models | Changing them silently allows fraud through |
| Recon files and breaks | Hiding a break hides lost or created money |

## Trust boundaries

1. Internet → merchant API (HMAC-signed, idempotent, rate-limited).
2. Browser → BFF (cookie session + CSRF, RBAC, MFA).
3. Core/BFF → ledger, risk, recon (internal network; internal keys; loopback locally).
4. Card-network simulator → vault detokenisation (separate credential; vault on isolated network).
5. Core → webhook destinations on the internet (SSRF boundary).
6. Services → PostgreSQL roles (`tally_app` RLS-restricted, `tally_ops` read-only, owner).

## Threats and mitigations

| STRIDE | Threat | Mitigation | Tested by | Residual risk |
| --- | --- | --- | --- | --- |
| S | Forged merchant request / stolen request replayed | HMAC over method, path, body hash, timestamp, nonce; nonce consumed once; 5-minute window | `tests/unit/test_hmac_auth.py`, `tests/integration/test_gateway_auth_dependency.py` | Stolen secret allows requests until rotation/revocation |
| S | Session hijack of back-office user | HttpOnly/Secure/SameSite=Strict cookies, 10-minute ES256 JWT, refresh rotation with family revocation on reuse, TOTP MFA with single-use steps, lockout | `tests/security/test_backoffice_security.py::_auth_flows` | Key ring is in memory (restart logs everyone out); no device binding |
| S | JWT forgery (`alg=none`, HS256 key confusion, unknown `kid`) | Algorithm pinned to ES256, key selected strictly by `kid` | `tests/security/test_security_primitives.py` | — |
| T | Double spend via concurrent requests | Row locks on payment state, table-driven transitions, idempotency keys at gateway and ledger, per-merchant money lock | `tests/security/test_concurrency_abuse.py`, `test_money_movement.py` storm | — |
| T | Over-refund / refund after chargeback | Refundable = amount − live refunds − non-won disputes under row lock | `test_money_movement.py` | — |
| T | Rewriting ledger or audit history | Append-only triggers, app role without write grants, SHA-256 hash chains, audit anchors in object storage | `tests/integration/ledger_posting.sql`, `_audit_chain` | Local object store is not WORM; a DB superuser can rewrite and re-hash the chain after the last anchor |
| T | Insider manual adjustment | Maker-checker with database `checker <> maker`, one pending request per subject, audit trail | `_dual_control`, `test_reconciliation.py`, `test_risk_flow.py` | Two colluding insiders |
| T | Tampered bank statement | Originals stored with SHA-256; duplicate delivery ingested once; bad lines reported | `tests/property/test_recon_formats.py` | No signature verification of bank files (not modelled) |
| R | User denies an approval or adjustment | Hash-chained audit log of logins, approvals, key changes, exports, chaos actions | `_audit_chain` | Chain covers back-office and gateway events; core state changes are in the append-only transition logs instead |
| I | Cross-tenant data access | Every merchant query runs as `tally_app` with RLS on `app.merchant_id`; secrets hidden by column grants | `_tenant_isolation`, authorization matrix | Core merchant API and workers use the owner role; isolation there relies on explicit `merchant_id` filters |
| I | PAN in logs/responses | Only vault holds PANs; redacting JSON logger masks Luhn-valid numbers and secret keys | `tests/integration/test_vault_api.py`, `test_security_primitives.py` | Third-party library logs outside the configured handler |
| I | SSRF via webhook URL | HTTPS-only, port allow-list, public-IP check on every resolution, connect to vetted IP (rebinding), no redirects | `tests/security/test_ssrf_and_webhook_signing.py` | Trusted-host escape hatch for local development |
| D | Request floods, huge bodies | Per-merchant rate limits, body size limits enforced while reading, 415 on non-JSON | `test_security_primitives.py`, gateway tests | No WAF locally (Terraform adds AWS WAF) |
| D | Risk service outage stalls payments | 100 ms timeout, per-merchant fail-open/closed | `test_risk_flow.py` | Fail-open accepts unscreened payments during outage |
| E | Role escalation in the console | Deny-by-default permission on every route; matrix test over all roles × routes | `_authorization_matrix` | Roles are assigned by CLI; no self-service role management |
| E | Maker approving own change via another service | Forwarded approvals carry the checker identity; recon/risk enforce `maker != checker` | `_dual_control`, risk and recon suites | — |
