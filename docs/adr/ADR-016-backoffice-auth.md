# ADR-016: Back-office authentication and authorization

## Context

The dashboard and ops console need browser sessions for staff and merchant users, strong
authentication for people who can move money, and an authorization model that cannot silently
leave a route open.

## Decision

- Cookie sessions carrying a 10-minute ES256 JWT (pinned algorithm, `kid` rotation), plus an opaque
  refresh token that rotates on each use; reuse revokes the token family. Cookies are
  `HttpOnly; Secure; SameSite=Strict`, and mutations need a double-submit CSRF header.
- Password (scrypt) then TOTP MFA; a TOTP time step is accepted once.
- One permission matrix; every route declares a permission through a single dependency, and the
  test suite enumerates routes × roles.
- The BFF calls core/recon/risk with internal keys and forwards the user's identity as
  `x-actor`, so each service records and enforces maker/checker identities itself.

## Consequences

The signing key ring is generated at start-up (in memory); a restart ends sessions. Production
would load keys from a secrets manager and publish a JWKS for other verifiers.
