# ADR-005: HMAC signing for merchant API requests

## Context

Merchant writes need request authenticity and a bounded replay window. Signatures must bind the HTTP method, exact path (including query string), body digest, timestamp, and a unique nonce.

## Options

- Signed bearer tokens: simple validation, but less convenient for per-request payload binding.
- HMAC request signing: binds each request to a merchant secret and supports key rotation by key ID.

## Decision

Use HMAC-SHA256 over a newline-delimited canonical request. Require secrets of at least 32 bytes, compare signatures with a constant-time function, validate the timestamp against a configurable window, and include a caller-generated nonce. The gateway must atomically consume `(key_id, nonce)` in shared storage before processing; the signing primitive alone does not prevent replay.

## Consequences

The signing behavior is deterministic and unit-testable. The HTTP gateway still needs durable API-key lookup, shared nonce consumption, scopes, rate limits, and tenant isolation before this primitive is a complete authentication system. Local test credentials must not be used outside the simulation.
